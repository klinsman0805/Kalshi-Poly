"""
modules/kalshi_weather_exec.py — Kalshi live/paper executor.

A subclass of the Polymarket WeatherExecutor, same relationship as
KalshiWeatherEngine is to WeatherEngine: reuse everything venue-agnostic
(mark-to-market, thesis recheck, dead-exit and take-profit TRIGGERS, session
bookkeeping, persistence, rehydration) and override only the methods that
actually place or read Polymarket orders.

Why _exit_position is fully overridden rather than parameterized: it's the one
method where Kalshi's mechanics are genuinely simpler, not just swapped. Kalshi
has no true FOK (only "immediate_or_cancel", which is FAK-shaped — partial
fills allowed, sweeps at-or-better than the limit), so none of Poly's
FOK-vs-FAK / miss-counter escalation logic (TAKE_PROFIT_FOK, _tp_fok_misses)
applies here — there's only one sell mechanism. Keep this in sync BY HAND with
weather_exec.py's _exit_position if that method's shared bookkeeping (the
settle-record shape, session totals) ever changes; the surrounding scaffolding
here is a deliberate line-for-line mirror of it.

Gate: KALSHI_WEATHER_LIVE (+ KALSHI_WEATHER_START_LIVE), independent of
WEATHER_LIVE — flipping Polymarket live has no effect on this, and vice versa.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

from modules.weather_exec import WeatherExecutor, _uninterruptible
from feeds import kalshi_order
from feeds.kalshi_stations import KALSHI_STATIONS
from feeds.kalshi_weather import taker_fee_c as kalshi_taker_fee_c
from engine import kalshi_get_public

log = logging.getLogger("modules.kalshi_weather_exec")

KALSHI_ENV_ARMED = os.getenv("KALSHI_WEATHER_LIVE", "false").strip().lower() == "true"
KALSHI_START_LIVE = os.getenv("KALSHI_WEATHER_START_LIVE", "false").strip().lower() == "true"
KALSHI_MISS_LOG = os.getenv("KALSHI_WEATHER_MISS_LOG", "kalshi_weather_misses.jsonl")


class KalshiWeatherExecutor(WeatherExecutor):
    LOG_TAG = "kalshi-wx"

    def __init__(self, on_log=None):
        super().__init__(on_log=on_log)
        # base __init__ set self.mode from Polymarket's WEATHER_LIVE/START_LIVE
        # globals — recompute from Kalshi's OWN flags so the dashboard label and
        # is_live are never accidentally driven by the Polymarket bot's settings.
        self.mode = "live" if (KALSHI_ENV_ARMED and KALSHI_START_LIVE) else "paper"

    @property
    def is_live(self):
        return self.mode == "live" and KALSHI_ENV_ARMED

    def fetch_exchange_positions(self):
        # The inherited implementation queries Polymarket's data API against a
        # Polymarket wallet — wrong exchange, wrong account. "Could not check" is
        # the only honest answer until a Kalshi /portfolio/positions equivalent
        # is wired up; returning [] would make every real position look like a ghost.
        return None

    def _place_live(self, token_id, ask_c, shares, neg_risk=None):
        """Live buy via Kalshi IOC. token_id is the market ticker (that's what
        feeds/kalshi_weather.py puts in the entry's token_yes/condition_id
        fields, for exactly this compatibility)."""
        try:
            filled, fill_c = kalshi_order.place_ioc(token_id, "buy", ask_c, int(round(shares)))
            return float(filled), fill_c
        except Exception as e:  # noqa: BLE001
            self.on_log("✗", f"[kalshi-wx] live order failed: {e}")
            return 0.0, None

    # No separate FAK path needed: Kalshi has no true FOK (see module
    # docstring) — IOC is already FAK-shaped, partial fills allowed, same as
    # every other Kalshi order. _maximize_confirmed_win calls
    # _place_live_fak(); this just points it back at the one mechanism that
    # already does the right thing.
    _place_live_fak = _place_live

    def _entry_fee_usd(self, shares, filled_c):
        """Real Kalshi taker fee on the buy fill — verified against actual
        /portfolio/fills data 2026-07-26 (formula matched real charged fees to
        within a hundredth of a cent). Omitting this from cost_usd was
        overstating every live Kalshi trade's reported P&L by the entry fee."""
        return round(shares * kalshi_taker_fee_c(filled_c) / 100.0, 4)

    # The gate itself lives in WeatherExecutor._decline_gate_ok — shared, so the
    # two venues cannot drift apart (the duplicated _exit_position carried the
    # same partial-fill bug in both files until 2026-07-26). Only the threshold
    # and the log prefix differ.
    DECLINE_GATE_DEG = float(os.getenv("KALSHI_WEATHER_DECLINE_GATE_DEG", "1.0"))
    # Kalshi's own slot limit. Falls back to Polymarket's WEATHER_MAX_OPEN when
    # unset, so removing this line restores the old shared-global behaviour.
    # Set independently to run one venue live while the other stays shut.
    MAX_OPEN = int(os.getenv("KALSHI_WEATHER_MAX_OPEN",
                             os.getenv("WEATHER_MAX_OPEN", "10")))

    # Re-entry (see WeatherExecutor.MAX_REENTRIES): Poly defaults OFF (0) and
    # stays the control arm; Kalshi runs it live from 2026-07-28 as the
    # experimental arm, at normal stake — NOT sized to try to erase a loss in
    # one trade (see the re-entry memo: that needs ~5.8x and the tail risk is
    # severe). One replacement per dead market per day.
    MAX_REENTRIES = int(os.getenv("KALSHI_WEATHER_MAX_REENTRIES", "1"))

    def _record_miss(self, entry, limit_c, shares):
        """Kalshi equivalent of the Poly miss-diagnostic: re-read this one
        market's current quote (Kalshi's public /markets/{ticker} already
        gives top-of-book directly, no ladder to walk) and log the gap."""
        ticker = entry["token_yes"]
        now_ask = now_bid = None
        try:
            m = kalshi_get_public(f"/markets/{ticker}").get("market", {})
            now_ask = float(m["yes_ask_dollars"]) * 100 if m.get("yes_ask_dollars") else None
            now_bid = float(m["yes_bid_dollars"]) * 100 if m.get("yes_bid_dollars") else None
        except Exception as e:  # noqa: BLE001
            log.debug("kalshi miss re-read %s failed: %s", ticker, e)
        gap_c = round(now_ask - limit_c, 2) if now_ask is not None else None
        rec = {
            "type": "miss", "ts": datetime.now(timezone.utc).isoformat(),
            "key": f"{entry['city']}|{entry['date']}|{entry.get('kind', 'high')}",
            "city": entry.get("city"), "label": entry.get("label"),
            "kind": entry.get("kind", "high"), "p": entry.get("p"),
            "edge_c": entry.get("edge_c"), "limit_c": round(limit_c, 2),
            "shares_wanted": shares,
            "now_ask_c": round(now_ask, 2) if now_ask is not None else None,
            "now_bid_c": round(now_bid, 2) if now_bid is not None else None,
            "gap_c": gap_c,
            "decline_at_entry": self._decline_deg(entry),  # shadow measurement
            "conditions": entry.get("conditions"),         # shadow measurement
            "ensemble": entry.get("ensemble"),             # shadow measurement
        }
        try:
            with open(KALSHI_MISS_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self._misses += 1
        except Exception as e:  # noqa: BLE001
            log.warning("kalshi miss-log write failed: %s", e)
        g = f"gap {gap_c:+.1f}c" if gap_c is not None else "book gone"
        self.on_log("!", f"[kalshi-wx] LIVE order missed {entry['city']} "
                         f"{entry['label']} — limit {limit_c:.0f}c, now ask "
                         f"{'—' if now_ask is None else f'{now_ask:.0f}c'} ({g})")

    def _refresh_account(self):
        """The base class reads Polymarket's REAL USDC balance here — meaningless
        (and actively harmful) from this Kalshi-only process: it lazily builds a
        polymarket.PolyClient(), which opens a live Poly websocket purely as a
        side effect. Caught live 2026-07-25 — a "Poly WS error" firing from the
        kalshi-paper process, and a Polymarket USDC balance sitting mislabeled
        as a Kalshi baseline in kalshi_weather_paper.jsonl (2026-07-24 03:32).
        No native Kalshi balance-tracking exists yet, so this is a deliberate
        no-op rather than pointing account tracking at the wrong venue."""
        return

    # Maximize-confirmed-wins itself now lives in the shared base class (see
    # WeatherExecutor._maximize_confirmed_win) — verified 2026-07-28 that
    # Polymarket keeps the same kind of post-midnight trading buffer Kalshi
    # does (~42min past local midnight vs Kalshi's fixed 1h, checked against
    # real settled Istanbul/London markets), so this generalizes across
    # venues and duplicating it here would only risk the two copies drifting
    # apart, the same mistake _exit_position made before 2026-07-26. Only the
    # threshold/cap constants differ per venue, same pattern as
    # DECLINE_GATE_DEG above.
    MAXIMIZE_WINS = os.getenv("KALSHI_WEATHER_MAXIMIZE_WINS", "true").strip().lower() == "true"
    MAX_WIN_ADDS = int(os.getenv("KALSHI_WEATHER_MAX_WIN_ADDS", "3"))
    WIN_ADD_MAX_ASK_C = float(os.getenv("KALSHI_WEATHER_WIN_ADD_MAX_ASK_C", "97"))

    def poll(self):
        """Kalshi-native settlement check — the base class's poll() queries
        Polymarket's Gamma API by condition_id, which is a no-op here (Kalshi
        positions carry a Kalshi ticker string, never a Poly condition hash) and
        also calls _refresh_account(), which is why this is fully overridden
        rather than calling super(). In practice nearly every position exits
        early via TAKE-PROFIT/DEAD-EXIT before this ever matters, but a position
        that ages to real settlement needs an actual Kalshi-side check or it
        would sit open in our ledger forever despite being resolved on Kalshi."""
        self._cli_crosscheck()
        with self._lock:
            open_pos = list(self.open)
        for pos in open_pos:
            ticker = pos.get("token_yes")
            if not ticker:
                continue
            try:
                m = kalshi_get_public(f"/markets/{ticker}").get("market", {})
            except Exception as e:  # noqa: BLE001
                self.on_log("!", f"[kalshi-wx] settle poll failed {pos['city']}: {e}")
                continue
            if m.get("status") != "finalized":
                continue
            won = m.get("result") == "yes"
            gross = round(pos["shares"] * ((100 - pos["entry_c"]) if won else -pos["entry_c"]) / 100.0, 2)
            fee = round(pos["shares"] * kalshi_taker_fee_c(pos["entry_c"]) / 100.0, 2)
            net = round(gross - fee, 2)
            rec = {"type": "settle", "key": pos["key"], "pos_id": self._pid(pos), "won": won,
                   "mode": pos.get("mode"),
                   "gross_pnl": gross, "fee_usd": fee, "pnl_usd": net,
                   "settled": datetime.now(timezone.utc).isoformat()}
            with self._lock:
                self.open = [p for p in self.open if self._pid(p) != self._pid(pos)]
                self.closed.append({**pos, **rec})
                self.closed = self.closed[-200:]
                self.session["settled"] += 1
                self.session["wins"] += 1 if won else 0
                self.session["wins_held"] = self.session.get("wins_held", 0) + (1 if won else 0)
                self.session["realized_pnl"] = round(self.session["realized_pnl"] + net, 2)
                self.session["realized_gross"] = round(self.session["realized_gross"] + gross, 2)
                self.session["fees_paid"] = round(self.session.get("fees_paid", 0.0) + fee, 2)
                self.session["staked_usd"] = round(
                    max(0.0, self.session["staked_usd"] - pos.get("cost_usd", 0.0)), 2)
            self._persist(rec)
            self.on_log("✅" if won else "✗",
                        f"[kalshi-wx] SETTLE {pos['city']} {pos['date']} {pos['label']} "
                        f"{'WIN' if won else 'LOSS'} net {net:+.2f} USD (gross {gross:+.2f} − fee {fee:.2f}, "
                        f"model p was {pos['model_p']})")

    def _cli_crosscheck(self):
        """Cross-check early-exited positions against NWS's actual CLI daily
        report — Kalshi's REAL settlement source, distinct from the live
        METAR feed we trade on. CLI publishes once, the following morning,
        summarizing the full prior day — it doesn't exist yet during the
        trading day, so this can only confirm after the fact, never drive an
        entry. Purpose: catch a genuine METAR-vs-CLI settlement mismatch
        (basis risk between what we watched live and what Kalshi actually
        settles to) instead of finding out the hard way at resolution.

        Only early exits (TAKE-PROFIT/DEAD-EXIT/MANUAL-CLOSE) need this — a
        position that reached natural settlement already got Kalshi's own
        real result in poll() above. `_cli_checked` is in-memory only (not
        persisted); a restart just re-checks once, which is cheap.
        """
        with self._lock:
            candidates = [c for c in self.closed
                          if c.get("closed_early") and c.get("mode") == "live"
                          and not c.get("_cli_checked")]
        for pos in candidates:
            icao = KALSHI_STATIONS.get(pos["city"], {}).get("icao")
            if not icao:
                pos["_cli_checked"] = True   # no mapping — never going to resolve
                continue
            station = icao[1:]               # KMSY -> MSY
            try:
                r = requests.get(
                    f"https://api.weather.gov/products/types/CLI/locations/{station}",
                    timeout=15)
                r.raise_for_status()
                products = r.json().get("@graph", [])
            except Exception as e:  # noqa: BLE001
                self.on_log("!", f"[kalshi-wx] CLI lookup failed {pos['city']}: {e}")
                continue
            found_text = None
            for p in products[:6]:   # CLI issues ~1-2x/day; 6 covers a few days back
                try:
                    rr = requests.get(f"https://api.weather.gov/products/{p['id']}", timeout=15)
                    txt = rr.json().get("productText", "")
                except Exception:  # noqa: BLE001
                    continue
                m = re.search(r"CLIMATE SUMMARY FOR (\w+ \d+ \d+)", txt)
                if not m:
                    continue
                try:
                    d = datetime.strptime(m.group(1), "%B %d %Y").date().isoformat()
                except ValueError:
                    continue
                if d == pos["date"]:
                    found_text = txt
                    break
            if not found_text:
                continue   # not published yet — leave unchecked, retry next poll
            m_hi = re.search(r"MAXIMUM\s+(\d+)", found_text)
            m_lo = re.search(r"MINIMUM\s+(\d+)", found_text)
            if not (m_hi and m_lo):
                pos["_cli_checked"] = True
                continue
            actual_hi, actual_lo = int(m_hi.group(1)), int(m_lo.group(1))
            kind = pos.get("kind", "high")
            actual_extreme = actual_hi if kind == "high" else actual_lo
            lo, hi = pos.get("lo"), pos.get("hi")
            # A catch-all bucket ("≥60°F" / "≤98°F") only carries ONE bound by
            # design — requiring both to be present made this always evaluate
            # False for every catch-all, silently mislabeling San Francisco's
            # real "≥60°F" win as a MISMATCH and making Dallas's "≤98°F" loss
            # look confirmed when it was actually an unrelated coincidence.
            cli_would_win = (lo is None or actual_extreme >= lo) and (hi is None or actual_extreme <= hi)
            our_won = pos.get("won")
            pos["_cli_checked"] = True
            if cli_would_win != our_won:
                self.on_log("!", f"[kalshi-wx] CLI MISMATCH {pos['city']} {pos['date']} "
                                 f"{pos['label']} — we exited assuming "
                                 f"{'WIN' if our_won else 'LOSS'}, but NWS CLI says actual "
                                 f"{kind} was {actual_extreme}F "
                                 f"({'inside' if cli_would_win else 'outside'} bucket "
                                 f"{lo}-{hi}F) — METAR vs CLI settlement basis-risk confirmed")
            else:
                self.on_log("→", f"[kalshi-wx] CLI check OK {pos['city']} {pos['date']} "
                                 f"{pos['label']} — NWS actual {kind}={actual_extreme}F "
                                 f"agrees with our read")

    @_uninterruptible
    def _exit_position(self, pos, row, won, tag, reason):
        """Mirror of WeatherExecutor._exit_position, Kalshi sell path (single
        IOC sweep — no FOK/FAK distinction to make, see module docstring)."""
        bucket = next((b for b in row.get("buckets", [])
                       if b.get("label") == pos.get("label")), {})
        bid_c = bucket.get("bid_c") or 0.0
        sold, proceeds, fill_c = 0.0, 0.0, None

        if pos.get("mode") == "live" and not self.is_live:
            if not pos.get("_exit_blocked"):
                pos["_exit_blocked"] = True
                self.on_log("!", f"[kalshi-wx] {tag} SKIPPED {pos['city']} {pos['label']} — "
                                 f"live position, executor is PAPER. Not booking a "
                                 f"simulated exit against real contracts. Arm live to "
                                 f"sell, or close it manually. ({reason})")
            return

        if pos.get("mode") == "live" and self.is_live:
            if bid_c <= 0:
                self.on_log("✗", f"[kalshi-wx] {tag} {pos['city']} — no bid to sell into, retry next cycle")
                return
            try:
                sold, fill_c = kalshi_order.place_ioc(pos["token_yes"], "sell", bid_c, int(pos["shares"]))
            except Exception as e:  # noqa: BLE001
                self.on_log("✗", f"[kalshi-wx] {tag} sell failed {pos['city']}: {e}")
                return                       # keep the position; retry next refresh
            if sold <= 0:
                self.on_log("→", f"[kalshi-wx] {tag} {pos['city']} {pos['label']} — "
                                 f"sell found nothing at {bid_c:.0f}c, holding for next check")
                return
            fee = kalshi_taker_fee_c(fill_c) * sold / 100.0 if fill_c else 0.0
            proceeds = round(sold * (fill_c or bid_c) / 100.0 - fee, 2)
        else:
            sold = float(pos["shares"])          # paper: mark out at the bid
            fee = kalshi_taker_fee_c(bid_c) * sold / 100.0
            proceeds = round(sold * bid_c / 100.0 - fee, 2)

        # IOC is FAK-shaped (partial fills allowed) — a sweep can come back short
        # of the full size (observed live: New Orleans 92-93F sold 10 of 12 on
        # 2026-07-24). Closing the position here regardless would abandon the
        # unsold remainder — untracked, unretried, real contracts left on Kalshi.
        # Mirror weather_exec.py's fix: accumulate across partial fills and only
        # book the position closed once the sellable remainder is gone.
        remaining = round(pos["shares"] - sold, 6)
        if sold > 0 and remaining >= 1.0:
            pos["_exit_sold_shares"] = pos.get("_exit_sold_shares", 0.0) + sold
            pos["_exit_proceeds_usd"] = round(pos.get("_exit_proceeds_usd", 0.0) + proceeds, 2)
            pos["shares"] = remaining
            self.on_log("→", f"[kalshi-wx] {tag} {pos['city']} {pos['label']} — partial fill "
                             f"{sold:.2f} sh (${proceeds:.2f}); {remaining:.2f} sh still held, "
                             f"retrying next cycle")
            return                            # keep the position open for the remainder
        sold = pos.get("_exit_sold_shares", 0.0) + sold
        proceeds = round(pos.get("_exit_proceeds_usd", 0.0) + proceeds, 2)
        pnl = round(proceeds - pos.get("cost_usd", 0.0), 2)
        rec = {"type": "settle", "key": pos["key"], "pos_id": self._pid(pos), "won": won,
               "mode": pos.get("mode"),
               "closed_early": True, "exit": tag, "reason": reason,
               "salvage_usd": proceeds, "sold_shares": round(sold, 6),
               "sold_at_c": round(fill_c, 2) if fill_c is not None else None,
               "gross_pnl": pnl, "fee_usd": 0.0, "pnl_usd": pnl,
               "settled": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.open = [p for p in self.open if self._pid(p) != self._pid(pos)]
            self.closed.append({**pos, **rec})
            self.closed = self.closed[-200:]
            self.session["settled"] += 1
            self.session["wins"] += 1 if won else 0
            self.session["early_exits"] = self.session.get("early_exits", 0) + 1
            self.session["realized_pnl"] = round(self.session["realized_pnl"] + pnl, 2)
            self.session["realized_gross"] = round(self.session["realized_gross"] + pnl, 2)
            self.session["staked_usd"] = round(
                max(0.0, self.session["staked_usd"] - pos.get("cost_usd", 0.0)), 2)
        self._persist(rec)
        self.on_log("✅" if pnl >= 0 else "✗",
                    f"[kalshi-wx] {tag} {pos['city']} {pos['label']} — {reason}; "
                    f"got ${proceeds:.2f} of ${pos.get('cost_usd',0):.2f} ({pnl:+.2f})")
