"""
modules/poly_rewards_autoexec.py — sequential, single-market LP-reward
executor: pick one NEAR-LOCK-gated candidate, rest an order on it, watch it
until it's fully resolved (filled and exited, or cleanly cancelled), THEN
consider the next one. Never runs two candidates at once — confirmed as
the intended design ("yes, fully exit before considering the next").

Gated behind POLY_REWARDS_AUTOEXEC (default OFF). Off: cycle() only logs
what it would do, same shadow-mode discipline as everything else in this
project. Composes three pieces, each independently flag-gated:
  - PolyRewardsExec.check()                   — candidate discovery
    (NEAR-LOCK gate: only rest on buckets whose outcome is already decided)
  - feeds.poly_rewards.get_band()              — fresh reference price,
    fetched on demand right before every place/reprice — never reused
    stale (this session watched one market's reference price move 5 times
    in ~35 minutes)
  - modules.poly_rewards_live.place_at_band()  — parameterized order firing
    (POLY_REWARDS_NEW_SDK; the OLD SDK cannot sign GTC orders for this
    proxy-funded account — see that module's docstring)

Fills and stop-losses (both the hard dead-bucket check and the
POLY_REWARDS_TREND_EXIT early-exit) are handled by
PolyRewardsExec.check_fills(), already wired into the scan loop — this
module's job stops once an order is resting. It only re-engages to notice
an order has fallen out of the reward-eligible band (reprice) or has fully
resolved (only then does it consider the next candidate).
"""

import logging
import os
import time

from feeds.poly_rewards import get_band
from modules.poly_rewards_exec import PolyRewardsExec
from modules.poly_rewards_live import place_at_band

log = logging.getLogger("modules.poly_rewards_autoexec")

POLY_REWARDS_AUTOEXEC = os.getenv("POLY_REWARDS_AUTOEXEC", "false").strip().lower() == "true"
# Throttle: is_order_scoring / cancel_order are real API calls per tracked
# order per cycle — this stops a fast scan loop from hammering them, and
# stops us from cancelling an order seconds after placing it on one bad read.
REPRICE_COOLDOWN_SEC = int(os.getenv("POLY_REWARDS_REPRICE_COOLDOWN_SEC", "60"))


class PolyRewardsAutoExec:
    """Single active candidate at a time. State lives entirely off
    ORDERS_LOG (via PolyRewardsExec._open_tracked_orders): nothing tracked
    means idle and free to pick a new candidate; something tracked means
    busy on it — reprice if it's fallen out of scoring, otherwise leave it
    alone (a fill is check_fills's job, not this module's)."""

    def __init__(self, exec_=None, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.exec = exec_ or PolyRewardsExec(on_log=self.on_log)
        self._last_reprice_ts = {}   # order_id -> ts, throttles reprice attempts

    def cycle(self, client, scan_results):
        """Call once per scan loop, after exec.check_fills(client) has
        already run for this cycle. scan_results is the same list passed to
        exec.check()."""
        tracked = self.exec._open_tracked_orders()
        if tracked:
            if client is not None:
                self._maybe_reprice(client, tracked)
            return

        candidates = self.exec.check(scan_results)
        if not candidates:
            return
        top = max(candidates, key=lambda c: c["yield_per_dollar_per_day"])
        self._enter(top)

    def _enter(self, candidate):
        band = get_band(candidate["condition_id"])
        if not band or not band.get("token_id"):
            self.on_log("!", f"[lp-auto] {candidate['city']} {candidate['kind']} — band fetch "
                             f"failed right before entry, skipping this cycle")
            return
        if not POLY_REWARDS_AUTOEXEC:
            self.on_log("→", f"[lp-auto] PAPER — would enter {candidate['city']} {candidate['kind']} "
                             f"now (band {band['band_lo_c']:.1f}-{band['band_hi_c']:.1f}c), "
                             f"POLY_REWARDS_AUTOEXEC is off")
            return
        place_at_band(band, side="BUY", condition_id=candidate["condition_id"],
                      city=candidate["city"], kind=candidate["kind"],
                      date=candidate["date"], station=candidate["station"])

    def _maybe_reprice(self, client, tracked):
        for order_id, o in tracked.items():
            last = self._last_reprice_ts.get(order_id, 0)
            if time.time() - last < REPRICE_COOLDOWN_SEC:
                continue
            try:
                from py_clob_client_v2.clob_types import OrderScoringParams
                scoring = client._clob.is_order_scoring(OrderScoringParams(orderId=order_id))
                is_scoring = bool(scoring.get("scoring")) if isinstance(scoring, dict) else bool(scoring)
            except Exception as e:  # noqa: BLE001
                self.on_log("!", f"[lp-auto] scoring check failed {order_id}: {e}")
                continue
            self._last_reprice_ts[order_id] = time.time()
            if is_scoring:
                continue

            self.on_log("→", f"[lp-auto] {o['city']} {o['kind']} — order {order_id} fell out of "
                             f"the reward band" + (", repricing" if POLY_REWARDS_AUTOEXEC else
                                                    " (paper — would reprice, POLY_REWARDS_AUTOEXEC is off)"))
            if not POLY_REWARDS_AUTOEXEC:
                continue

            band = get_band(o["condition_id"])
            if not band:
                continue
            try:
                from py_clob_client_v2.clob_types import OrderPayload
                client._clob.cancel_order(OrderPayload(orderID=order_id))
            except Exception as e:  # noqa: BLE001
                self.on_log("!", f"[lp-auto] cancel failed {order_id}: {e}")
                continue
            self.exec._resolve_order(order_id, "repriced_out_of_band")
            place_at_band(band, side=o["side"], size=o["size"], condition_id=o["condition_id"],
                          city=o["city"], kind=o["kind"], date=o["date"], station=o["station"])
