"""
modules/soccer_exec.py — order execution for the Soccer module.

Turns a fired VALUE / DRAW-VALUE signal into an actual buy on the cheaper venue.
Soccer value bets are entries you HOLD to the match result (they settle days
later), so this places the entry and tracks it as an open position — there is no
intra-session close-out.

SAFETY — real money requires BOTH gates, so a stray UI click can never go live:
  1. env  SOCCER_LIVE=true        (operator arms it at launch)
  2. runtime mode == "live"       (toggle in the dashboard, default "paper")
Anything less → PAPER: identical sizing/dedup/logging, simulated fill, no order.

Risk caps: per-signal stake, max concurrent positions, one entry per match.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("modules.soccer_exec")

POS_LOG = Path(os.getenv("SOCCER_POS_LOG", "soccer_positions.jsonl"))
ENV_ARMED = os.getenv("SOCCER_LIVE", "false").strip().lower() == "true"
STAKE_USD = float(os.getenv("SOCCER_STAKE_USD", "5"))
MAX_OPEN = int(os.getenv("SOCCER_MAX_OPEN", "3"))
MAX_PER_MATCH = int(os.getenv("SOCCER_MAX_PER_MATCH", "1"))
KALSHI_SLIPPAGE_C = int(os.getenv("SOCCER_KALSHI_SLIPPAGE_C", "1"))


class SoccerExecutor:
    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.mode = "paper"          # "paper" | "live" (live also needs ENV_ARMED)
        self.stake_usd = STAKE_USD
        self.open = []               # list of open position dicts
        self.placed = set()          # dedup keys (match, slot, venue)
        self.session = {"placed": 0, "filled": 0, "staked_usd": 0.0, "rejected": 0}
        self._poly = None            # lazy PolyClient for live Poly orders

    # ── mode control ─────────────────────────────────────────────────────────
    def set_mode(self, mode):
        mode = (mode or "paper").lower()
        if mode == "live" and not ENV_ARMED:
            self.on_log("!", "[exec] LIVE requested but SOCCER_LIVE!=true — staying PAPER")
            self.mode = "paper"
            return self.mode
        self.mode = "live" if mode == "live" else "paper"
        self.on_log("⚙", f"[exec] mode = {self.mode.upper()}"
                         + (" (REAL ORDERS)" if self.mode == "live" else ""))
        return self.mode

    @property
    def is_live(self):
        return self.mode == "live" and ENV_ARMED

    # ── main entry point, called per match row each refresh ──────────────────
    def consider(self, row):
        if row.get("chip") not in ("VALUE", "DRAW-VALUE", "NEWS-LAG"):
            return
        buy = row.get("chip_buy") or {}
        venue, slot, ask = buy.get("venue"), buy.get("slot"), buy.get("ask")
        if not venue or ask is None:
            return
        match = f"{row['home']} vs {row['away']}"
        key = (match, slot, venue)
        if key in self.placed:
            return
        # risk caps
        if len(self.open) >= MAX_OPEN:
            return
        if sum(1 for p in self.open if p["match"] == match) >= MAX_PER_MATCH:
            return

        target = buy.get("ticker") if venue == "kalshi" else buy.get("token")
        if not target:
            self.on_log("!", f"[exec] no order target for {match} {slot}@{venue} — skip")
            self.placed.add(key)  # don't retry a structurally unplaceable signal
            self.session["rejected"] += 1
            return

        min_size = 1 if venue == "kalshi" else int(buy.get("min_size") or 5)
        price_usd = ask / 100.0
        shares = max(int(round(self.stake_usd / price_usd)) if price_usd > 0 else 0, min_size)
        cost = round(shares * price_usd, 2)

        self.placed.add(key)  # mark attempted up-front (dedup even if it fails)
        filled, detail = self._place(venue, target, ask, shares)
        if filled <= 0:
            self.on_log("✗", f"[exec] {self.mode.upper()} {match} buy {venue} {slot} "
                             f"@{ask}¢ ×{shares} — no fill ({detail})")
            self.session["rejected"] += 1
            return

        pos = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode, "match": match, "start": row.get("start"),
            "venue": venue, "slot": slot, "side": "yes",
            "price": ask, "filled": filled, "cost_usd": round(filled * price_usd, 2),
            "chip": row["chip"], "basis": row.get("chip_basis"), "edge_c": row.get("chip_edge"),
            "detail": detail, "phase": "open",
        }
        self.open.append(pos)
        self.session["placed"] += 1
        self.session["filled"] += filled
        self.session["staked_usd"] = round(self.session["staked_usd"] + pos["cost_usd"], 2)
        self._log(pos)
        self.on_log("✅", f"[exec] {self.mode.upper()} BOUGHT {venue} {slot} @{ask}¢ ×{filled} "
                         f"(${pos['cost_usd']}) — {match} [{row['chip']} +{row.get('chip_edge')}¢]")

    # ── venue order placement ────────────────────────────────────────────────
    def _place(self, venue, target, ask, shares):
        """Returns (filled_count, detail_str). PAPER → simulated full fill."""
        if not self.is_live:
            return shares, "paper-fill"
        try:
            if venue == "kalshi":
                return self._kalshi_buy(target, ask, shares)
            return self._poly_buy(target, ask, shares)
        except Exception as e:  # noqa: BLE001
            log.exception("order placement failed")
            return 0, f"error: {e}"

    def _kalshi_buy(self, ticker, ask, shares):
        import trader
        price_c = min(99, int(ask) + KALSHI_SLIPPAGE_C)  # cross to ensure IOC fill
        cid = trader._make_client_id("wc", "val")
        body = {
            "ticker": ticker, "side": "yes", "action": "buy", "count": shares,
            "time_in_force": "immediate_or_cancel", "client_order_id": cid,
            "yes_price": price_c,
        }
        resp = trader._post_order(body)
        order = resp.get("order", {}) if isinstance(resp, dict) else {}
        filled = int(float(order.get("fill_count_fp", "0") or "0"))
        return filled, f"kalshi ioc @{price_c}c oid={order.get('order_id', cid)}"

    def _poly_buy(self, token, ask, shares):
        import polymarket
        if self._poly is None:
            polymarket.DRY_RUN = False
            self._poly = polymarket.PolyClient()
        filled = self._poly.place_fok(token, int(ask), int(shares), fee_bps=0)
        return int(filled), f"poly fok @{ask}c"

    # ── persistence ──────────────────────────────────────────────────────────
    def _log(self, pos):
        try:
            with open(POS_LOG, "a") as f:
                f.write(json.dumps({"type": "soccer_position", **pos}) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("pos log write: %s", e)

    def state(self):
        return {
            "mode": self.mode, "live": self.is_live, "env_armed": ENV_ARMED,
            "stake_usd": self.stake_usd,
            "open": self.open, "session": self.session,
            "config": {"max_open": MAX_OPEN, "max_per_match": MAX_PER_MATCH,
                       "kalshi_slippage_c": KALSHI_SLIPPAGE_C},
        }
