"""
modules/weather_exec.py — forward-test executor for the NEAR-LOCK weather engine.

PAPER by default: fills ENTER signals at the quoted ask, persists every event
to weather_positions.jsonl (rehydrated on restart), and settles positions from
the market's own resolution (Gamma umaResolutionStatus == "resolved") — never
from our METAR read. That way the forward test also validates that our
observation feed matches the actual settlement source.

One position per event (city+date): the near-lock trade buys the single
highest-probability bucket, not a ladder.

LIVE mode exists but is double-gated (WEATHER_LIVE=true env AND set_mode) and
routes through polymarket.PolyClient.place_fok — same pattern as
copytrade_exec. Do not arm until the paper forward-test is calibrated
(target: n ≥ 100 settlements, win rate within a few points of model p).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from feeds.poly_weather import taker_fee_c

log = logging.getLogger("modules.weather_exec")

POS_LOG = Path(os.getenv("WEATHER_EXEC_LOG", "weather_positions.jsonl"))
ENV_ARMED = os.getenv("WEATHER_LIVE", "false").strip().lower() == "true"
STAKE_USD = float(os.getenv("WEATHER_STAKE_USD", "5"))
MAX_OPEN = int(os.getenv("WEATHER_MAX_OPEN", "10"))
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"


class WeatherExecutor:
    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.mode = "paper"
        self.stake_usd = STAKE_USD
        self.open = []            # position dicts
        self.closed = []
        # realized_pnl is NET of taker fees; realized_gross and fees_paid are
        # tracked alongside so the forward test can report both.
        self.session = {"opened": 0, "settled": 0, "wins": 0,
                        "staked_usd": 0.0, "realized_pnl": 0.0,
                        "realized_gross": 0.0, "fees_paid": 0.0}
        self._lock = threading.Lock()
        self._rehydrate()

    # ── persistence ──────────────────────────────────────────────────────────
    def _rehydrate(self):
        if not POS_LOG.exists():
            return
        by_key = {}
        try:
            for line in POS_LOG.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("type") == "open":
                    by_key[rec["key"]] = rec
                elif rec.get("type") == "settle" and rec.get("key") in by_key:
                    pos = by_key.pop(rec["key"])
                    # backfill net/gross/fee for pre-fee-accounting settle records
                    gross = rec.get("gross_pnl")
                    if gross is None:
                        gross = rec.get("pnl_usd", 0.0)   # old records stored gross here
                    fee = rec.get("fee_usd")
                    if fee is None:
                        fee = round(pos.get("shares", 0) * taker_fee_c(pos.get("entry_c")) / 100.0, 2)
                    net = round(gross - fee, 2)
                    self.closed.append({**pos, **rec, "gross_pnl": gross,
                                        "fee_usd": fee, "pnl_usd": net})
                    self.session["settled"] += 1
                    self.session["wins"] += 1 if rec.get("won") else 0
                    self.session["realized_pnl"] += net
                    self.session["realized_gross"] += gross
                    self.session["fees_paid"] += fee
        except Exception as e:  # noqa: BLE001
            self.on_log("✗", f"[weatherexec] rehydrate failed: {e}")
            return
        self.open = list(by_key.values())
        self.session["opened"] = self.session["settled"] + len(self.open)
        self.session["staked_usd"] = round(
            sum(p.get("cost_usd", 0.0) for p in self.open), 2)
        self.closed = self.closed[-200:]
        if self.open or self.closed:
            self.on_log("→", f"[weatherexec] recovered {len(self.open)} open / "
                             f"{self.session['settled']} settled from {POS_LOG}")

    def _persist(self, rec):
        try:
            with open(POS_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # noqa: BLE001
            log.warning("persist failed: %s", e)

    # ── mode control ─────────────────────────────────────────────────────────
    def set_mode(self, mode):
        mode = (mode or "paper").lower()
        if mode == "live" and not ENV_ARMED:
            self.on_log("!", "[weatherexec] LIVE requested but WEATHER_LIVE!=true — staying PAPER")
            mode = "paper"
        if mode != self.mode:
            self.mode = mode
            self.on_log("◆", f"[weatherexec] mode → {mode.upper()}")

    @property
    def is_live(self):
        return self.mode == "live" and ENV_ARMED

    # ── entries (called by WeatherEngine.refresh) ────────────────────────────
    def on_refresh(self, rows):
        for row in rows:
            entry = row.get("entry")
            if not entry:
                continue
            self._consider(entry)

    def _consider(self, entry):
        key = f"{entry['city']}|{entry['date']}|{entry.get('kind', 'high')}"
        with self._lock:
            if len(self.open) >= MAX_OPEN:
                return
            if any(p["key"] == key for p in self.open):
                return
            if any(c.get("key") == key for c in self.closed):
                return
        ask_c = entry["ask_c"]
        if ask_c is None or ask_c <= 0:
            return
        shares = max(entry.get("min_size") or 5, round(self.stake_usd / (ask_c / 100.0)))
        filled_c = ask_c
        mode = "paper"
        if self.is_live:
            filled = self._place_live(entry["token_yes"], ask_c, shares,
                                      entry.get("neg_risk"))
            if filled <= 0:
                self.on_log("!", f"[weatherexec] LIVE FOK missed {entry['city']} {entry['label']}")
                return
            shares, mode = filled, "live"
        cost = round(shares * filled_c / 100.0, 2)
        pos = {
            "type": "open", "key": key, "mode": mode, "kind": entry.get("kind", "high"),
            "city": entry["city"], "date": entry["date"], "station": entry["station"],
            "label": entry["label"], "condition_id": entry["condition_id"],
            "token_yes": entry["token_yes"], "slug": entry["slug"],
            "entry_c": filled_c, "shares": shares, "cost_usd": cost,
            "model_p": entry["p"], "edge_c": entry["edge_c"],
            "opened": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.open.append(pos)
            self.session["opened"] += 1
            self.session["staked_usd"] = round(self.session["staked_usd"] + cost, 2)
        self._persist(pos)
        self.on_log("◆", f"[weatherexec] {mode.upper()} ENTER {entry['city']} {entry['label']} "
                         f"@ {filled_c:.0f}c ×{shares} (p={entry['p']}, edge +{entry['edge_c']}c)")

    def _place_live(self, token_id, ask_c, shares, neg_risk=None):
        try:
            import polymarket
            client = polymarket.PolyClient()
            fee = polymarket.fetch_live_fee_bps(token_id) or 0
            return client.place_fok(token_id, int(round(ask_c)), float(shares), fee,
                                    neg_risk=neg_risk)
        except Exception as e:  # noqa: BLE001
            self.on_log("✗", f"[weatherexec] live order failed: {e}")
            return 0.0

    # ── settlement (poll Gamma for resolutions) ──────────────────────────────
    def poll(self):
        with self._lock:
            open_pos = list(self.open)
        if not open_pos:
            return
        # Gamma needs repeated condition_ids params (comma-joining returns [])
        # AND closed=true — the endpoint silently filters out closed markets by
        # default, which is precisely the state a settling position is in.
        ids = [p["condition_id"] for p in open_pos if p.get("condition_id")]
        try:
            r = requests.get(GAMMA_MARKETS,
                             params={"condition_ids": ids, "closed": "true"},
                             timeout=15)
            r.raise_for_status()
            markets = {m.get("conditionId"): m for m in r.json()}
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[weatherexec] settle poll failed: {e}")
            return
        for pos in open_pos:
            m = markets.get(pos.get("condition_id"))
            if not m or m.get("umaResolutionStatus") != "resolved":
                continue
            try:
                prices = m.get("outcomePrices")
                prices = json.loads(prices) if isinstance(prices, str) else prices
                yes = float(prices[0])
            except (TypeError, ValueError, IndexError):
                continue
            won = yes >= 0.5
            gross = round(pos["shares"] * ((100 - pos["entry_c"]) if won else -pos["entry_c"]) / 100.0, 2)
            fee = round(pos["shares"] * taker_fee_c(pos["entry_c"]) / 100.0, 2)
            net = round(gross - fee, 2)
            rec = {"type": "settle", "key": pos["key"], "won": won,
                   "gross_pnl": gross, "fee_usd": fee, "pnl_usd": net,
                   "settled": datetime.now(timezone.utc).isoformat()}
            with self._lock:
                self.open = [p for p in self.open if p["key"] != pos["key"]]
                self.closed.append({**pos, **rec})
                self.closed = self.closed[-200:]
                self.session["settled"] += 1
                self.session["wins"] += 1 if won else 0
                self.session["realized_pnl"] = round(self.session["realized_pnl"] + net, 2)
                self.session["realized_gross"] = round(self.session["realized_gross"] + gross, 2)
                self.session["fees_paid"] = round(self.session["fees_paid"] + fee, 2)
                self.session["staked_usd"] = round(
                    max(0.0, self.session["staked_usd"] - pos.get("cost_usd", 0.0)), 2)
            self._persist(rec)
            self.on_log("✅" if won else "✗",
                        f"[weatherexec] SETTLE {pos['city']} {pos['date']} {pos['label']} "
                        f"{'WIN' if won else 'LOSS'} net {net:+.2f} USD (gross {gross:+.2f} − fee {fee:.2f}, "
                        f"model p was {pos['model_p']})")

    # ── state ────────────────────────────────────────────────────────────────
    def state(self):
        with self._lock:
            s = dict(self.session)
            s["win_rate"] = (s["wins"] / s["settled"]) if s["settled"] else None
            avg_p = ([p for p in (c.get("model_p") for c in self.closed) if p is not None])
            return {
                "mode": self.mode, "live": self.is_live, "env_armed": ENV_ARMED,
                "stake_usd": self.stake_usd, "session": s,
                "avg_model_p": round(sum(avg_p) / len(avg_p), 3) if avg_p else None,
                "open": [{k: p.get(k) for k in
                          ("mode", "city", "kind", "date", "label", "entry_c", "shares",
                           "cost_usd", "model_p", "edge_c", "opened")}
                         for p in self.open],
                "recent": [{k: c.get(k) for k in
                            ("city", "kind", "date", "label", "entry_c", "model_p",
                             "won", "pnl_usd", "gross_pnl", "fee_usd")}
                           for c in self.closed[-15:]][::-1],
            }
