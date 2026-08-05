"""
modules/poly_rewards_papersim.py — full-pipeline PAPER simulation of the LP
auto-executor: real book, real bands (feeds.poly_rewards.get_band), real
METAR (the same MetarFeed instance PolyRewardsExec already polls), real
dead-bucket AND trend-exit logic — but NO order is ever sent to Polymarket.
Every entry, reprice, inferred fill, and exit is computed from live market
data and recorded to a JSONL ledger, so a report run against that ledger
reflects what the automated pipeline (built this session: get_band(),
poly_rewards_live.place_at_band(), PolyRewardsAutoExec's sequential state
machine, the METAR trend-exit heuristic) would actually have earned or lost
without risking a cent of real capital.

Fill inference: since nothing rests on the real book, a "fill" is
approximated as the real best_bid_c reaching or crossing our simulated
resting BUY price. This is optimistic — a real order at that price competes
with whatever else is already resting there and might queue rather than
fill instantly — but it is directionally right: if the market's own bid
trades through our price, a real resting order at that price would very
likely have traded too. Good enough for a feasibility read, not a
substitute for watching real fills once this goes live.

Sequential, single simulated position at a time — same "fully exit before
considering the next" design as the real autoexec. Safe to run
unconditionally (no flag needed): it holds no keys, places nothing, and
only ever appends to its own ledger.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds.poly_rewards import get_band
from feeds.poly_weather import fetch_temperature_events
from modules.poly_rewards_exec import (PolyRewardsExec, _trend_continuing_adverse,
                                       bucket_dead, observed_extreme)

log = logging.getLogger("modules.poly_rewards_papersim")

PAPER_LOG = Path("poly_rewards_papersim.jsonl")
REPRICE_COOLDOWN_SEC = 60
# Safety net, independent of any specific staleness cause: no simulated
# position should be able to block the pipeline forever. Found live: a
# filled Seattle position sat unresolved for 36+ hours because
# fetch_temperature_events() only returns today's/tomorrow's markets — once
# real-world date rolled past the position's own market date, _check_filled
# could no longer find its bucket and silently no-opped every cycle
# forever (the single-position-at-a-time design has no other way back to
# idle). This timeout force-closes ANY position — resting or filled — once
# it's been open this long, regardless of why it's stuck.
MAX_POSITION_HOURS = float(os.getenv("POLY_REWARDS_PAPERSIM_MAX_HOURS", "20"))


class PolyRewardsPaperSim:
    """One simulated position at a time, replayed from PAPER_LOG on start so
    a service restart doesn't lose track of an in-progress simulated trade."""

    def __init__(self, exec_=None, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.exec = exec_ or PolyRewardsExec(on_log=self.on_log)
        self.pos = self._load_open_position()
        self._last_reprice_ts = 0.0
        self._miss_streak = 0   # consecutive cycles where the position's market couldn't be found at all

    def _load_open_position(self):
        if not PAPER_LOG.exists():
            return None
        pos = None
        for line in open(PAPER_LOG):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec["type"] == "paper_entered":
                pos = dict(rec)
            elif pos and rec.get("id") == pos.get("id"):
                if rec["type"] == "paper_repriced":
                    pos["entry_price_c"] = rec["price_c"]
                elif rec["type"] == "paper_filled":
                    pos["filled"] = True
                    pos["filled_ts"] = rec["ts"]
                    pos["fill_price_c"] = rec["price_c"]
                elif rec["type"] == "paper_exited":
                    pos = None
        return pos

    def _persist(self, rec):
        try:
            with open(PAPER_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # noqa: BLE001
            log.warning("papersim log write failed: %s", e)

    def cycle(self, scan_results):
        if not self.pos:
            self._maybe_enter(scan_results)
            return
        if self._age_hours() >= MAX_POSITION_HOURS:
            self._force_close("timeout")
            return
        if not self.pos.get("filled"):
            self._check_resting()
        else:
            self._check_filled()

    def _age_hours(self):
        entered = datetime.fromisoformat(self.pos["ts"])
        return (datetime.now(timezone.utc) - entered).total_seconds() / 3600.0

    def _force_close(self, reason):
        """Force-resolves whatever position is open, regardless of state,
        using the best real price data still reachable. Never leaves a
        position open indefinitely — this is the fallback of last resort
        when the normal fill/dead-bucket checks can't run to completion
        (market rolled off the feed, band lookup failing, or just too much
        time elapsed)."""
        band = None
        try:
            band = get_band(self.pos["condition_id"])
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[paper-sim] band fetch failed during force-close: {e}")

        if not self.pos.get("filled"):
            # Never held real exposure — just stop the reward-accrual clock,
            # no cost/proceeds to compute.
            self._persist({"type": "paper_exited", "id": self.pos["id"],
                          "ts": datetime.now(timezone.utc).isoformat(), "reason": reason,
                          "exit_price_c": None, "cost_usd": 0.0, "proceeds_usd": 0.0, "pnl_usd": 0.0})
            self.on_log("!", f"[paper-sim] ABANDONED (unfilled, {reason}) {self.pos['city']} "
                             f"{self.pos['kind']} — never filled, no exposure to close")
            self.pos = None
            return

        exit_price_c = band["best_bid_c"] if band and band.get("best_bid_c") is not None else 0.0
        cost = self.pos["size"] * self.pos["fill_price_c"] / 100.0
        proceeds = self.pos["size"] * exit_price_c / 100.0
        pnl = proceeds - cost
        self._persist({"type": "paper_exited", "id": self.pos["id"],
                      "ts": datetime.now(timezone.utc).isoformat(), "reason": reason,
                      "exit_price_c": exit_price_c, "cost_usd": cost,
                      "proceeds_usd": proceeds, "pnl_usd": pnl})
        self.on_log("✗" if pnl < 0 else "✓",
                   f"[paper-sim] FORCE-CLOSED ({reason}) {self.pos['city']} {self.pos['kind']} — "
                   f"cost ${cost:.2f}, proceeds ${proceeds:.2f} (best available price, "
                   f"band {'unavailable' if not band else 'ok'}), pnl ${pnl:+.2f}")
        self.pos = None

    def _maybe_enter(self, scan_results):
        candidates = self.exec.check(scan_results)
        if not candidates:
            return
        top = max(candidates, key=lambda c: c["yield_per_dollar_per_day"])
        band = get_band(top["condition_id"])
        if not band or not band.get("token_id") or band.get("mid_c") is None:
            return
        pos = {
            "type": "paper_entered", "id": f"{top['condition_id']}-{int(time.time())}",
            "ts": datetime.now(timezone.utc).isoformat(),
            "condition_id": top["condition_id"], "token_id": band["token_id"],
            "city": top["city"], "kind": top["kind"], "date": top["date"], "station": top["station"],
            "entry_price_c": band["mid_c"], "size": band["min_size"],
            "est_daily_usd": top["est_daily_usd"], "filled": False, "filled_ts": None,
        }
        self.pos = pos
        self._persist(pos)
        self.on_log("→", f"[paper-sim] ENTERED {pos['city']} {pos['kind']} @ {pos['entry_price_c']:.1f}c "
                         f"({pos['size']:.0f} sh) — est ${pos['est_daily_usd']:.2f}/day reward")

    def _check_resting(self):
        band = get_band(self.pos["condition_id"])
        if not band:
            # 3 consecutive misses (~15 min at a 5-min cycle) means the
            # market has likely dropped off the bulk rewards feed entirely
            # (closed/resolved) rather than a transient fetch hiccup —
            # abandon rather than wait for MAX_POSITION_HOURS.
            self._miss_streak += 1
            if self._miss_streak >= 3:
                self._miss_streak = 0
                self._force_close("band_unavailable")
            return
        self._miss_streak = 0

        if band.get("best_bid_c") is not None and band["best_bid_c"] >= self.pos["entry_price_c"]:
            ts = datetime.now(timezone.utc).isoformat()
            self.pos["filled"] = True
            self.pos["filled_ts"] = ts
            self.pos["fill_price_c"] = self.pos["entry_price_c"]
            self._persist({"type": "paper_filled", "id": self.pos["id"], "ts": ts,
                          "price_c": self.pos["entry_price_c"], "size": self.pos["size"]})
            self.on_log("→", f"[paper-sim] FILLED (inferred) {self.pos['city']} {self.pos['kind']} "
                             f"@ {self.pos['entry_price_c']:.1f}c — real bid reached {band['best_bid_c']:.1f}c")
            return

        if time.time() - self._last_reprice_ts < REPRICE_COOLDOWN_SEC:
            return
        if not (band["band_lo_c"] <= self.pos["entry_price_c"] <= band["band_hi_c"]):
            self._last_reprice_ts = time.time()
            new_price = band["mid_c"]
            self.on_log("→", f"[paper-sim] REPRICE {self.pos['city']} {self.pos['kind']} "
                             f"{self.pos['entry_price_c']:.1f}c -> {new_price:.1f}c (band moved "
                             f"{band['band_lo_c']:.1f}-{band['band_hi_c']:.1f}c)")
            self.pos["entry_price_c"] = new_price
            self._persist({"type": "paper_repriced", "id": self.pos["id"],
                          "ts": datetime.now(timezone.utc).isoformat(), "price_c": new_price})

    def _check_filled(self):
        try:
            events = fetch_temperature_events()
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[paper-sim] event fetch failed: {e}")
            return
        bucket = None
        for e in events:
            for b in e.get("buckets", []):
                if b.get("condition_id") == self.pos["condition_id"]:
                    bucket = b
        if not bucket:
            # The bug this was built to fix: fetch_temperature_events() only
            # returns today's/tomorrow's markets, so once real-world date
            # rolls past this position's own market date, its bucket
            # disappears from the feed and this would silently no-op
            # forever without a way back to idle. Force-close instead —
            # this is expected to happen almost every time a filled
            # position survives into the next calendar day.
            self._miss_streak += 1
            if self._miss_streak >= 3:
                self._miss_streak = 0
                self._force_close("rolled_off_feed")
            return
        self._miss_streak = 0

        self.exec.metar.set_stations({self.pos["station"]: self.exec._climo_tz.get(self.pos["station"], "")})
        self.exec.metar.poll()
        st = self.exec.metar.snapshot().get(self.pos["station"])
        if not st:
            return
        lo, hi = bucket.get("lo"), bucket.get("hi")
        # Extreme must be read in the BUCKET'S unit — was hardcoded to Celsius
        # and compared against °F bounds on US markets (fixed 2026-08-04).
        ext = observed_extreme(st, self.pos["kind"], bucket.get("unit"))
        if ext is None:
            return
        ext_s = round(ext)
        dead = bucket_dead(ext, self.pos["kind"], lo, hi)
        trend_dead = False
        if not dead:
            trend_dead = _trend_continuing_adverse(st.get("today_obs") or [], self.pos["kind"], lo, hi)
        if not dead and not trend_dead:
            return

        band = get_band(self.pos["condition_id"])
        exit_price_c = band["best_bid_c"] if band and band.get("best_bid_c") is not None else 0.0
        cost = self.pos["size"] * self.pos["fill_price_c"] / 100.0
        proceeds = self.pos["size"] * exit_price_c / 100.0
        pnl = proceeds - cost
        reason = "trend_exit" if trend_dead else "dead_bucket"
        self._persist({"type": "paper_exited", "id": self.pos["id"],
                      "ts": datetime.now(timezone.utc).isoformat(), "reason": reason,
                      "exit_price_c": exit_price_c, "cost_usd": cost,
                      "proceeds_usd": proceeds, "pnl_usd": pnl})
        self.on_log("✗" if pnl < 0 else "✓",
                   f"[paper-sim] EXITED ({reason}) {self.pos['city']} {self.pos['kind']} — "
                   f"cost ${cost:.2f}, proceeds ${proceeds:.2f}, pnl ${pnl:+.2f}")
        self.pos = None
