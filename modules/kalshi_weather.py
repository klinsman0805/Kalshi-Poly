"""
modules/kalshi_weather.py — Kalshi NEAR-LOCK engine (paper).

A thin subclass of the Polymarket WeatherEngine. It reuses that engine's model
(`bucket_prob`), gating (`_gate`), grouping, and dashboard row shape verbatim —
the STRATEGY is identical. Only the two venue-specific seams are overridden:

  • refresh()      → discover Kalshi markets instead of Polymarket ones
  • _book_confirm()→ Kalshi's public quote IS the real book top (no Gamma-vs-CLOB
                     gap to correct), so re-price on that quote with the Kalshi
                     quadratic fee rather than walking a CLOB ladder.

weather.py is imported, never modified. Intended to run in its OWN process with
WEATHER_LIVE=false and its own WEATHER_EXEC_LOG, so the stock WeatherExecutor
runs paper-only against Kalshi with zero coupling to the live Polymarket bot.
"""

import logging
from datetime import datetime, timezone

from modules.weather import (WeatherEngine, SIGNAL_GROUP, SIGNAL_RANK,
                             PRICE_MIN_C, PRICE_MAX_C, MAX_SPREAD_C, MIN_EDGE_C,
                             MAX_EDGE_C, STAKE_USD, credible_quote)
from feeds import kalshi_weather as kfeed
from feeds.kalshi_stations import KALSHI_STATIONS

log = logging.getLogger("modules.kalshi_weather")


class KalshiWeatherEngine(WeatherEngine):
    def __init__(self, metar, executor=None, on_log=None, cities=None):
        # cities=None → every mapped city that has climatology (set after load)
        self._requested_cities = cities
        super().__init__(metar, executor=executor, on_log=on_log)

    def _tradeable_cities(self):
        """Mapped cities whose station has °F climatology loaded (others would
        only ever render MONITOR)."""
        want = self._requested_cities or list(KALSHI_STATIONS.keys())
        ok = []
        for c in want:
            m = KALSHI_STATIONS.get(c)
            if m and m["icao"] in self.climo and "pmf" in self.climo[m["icao"]]:
                ok.append(c)
        return ok

    def refresh(self):
        # mirror of WeatherEngine.refresh, swapping the discovery source only
        self._load_climo()
        try:
            events = kfeed.fetch_temperature_events(cities=self._tradeable_cities())
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.on_log("✗", f"[kalshi-wx] discovery failed: {e}")
            return self.rows
        self.metar.set_stations({
            e["station"]: self.climo[e["station"]]["tz"]
            for e in events
            if e["source"] == "metar" and e["station"] in self.climo
        })
        self.metar.poll()
        self.gfs.poll(self.metar.snapshot())   # shadow only — see gfs_ensemble

        today = datetime.now(timezone.utc).date()
        rows = []
        for e in events:
            if e["date"] and (today - e["date"]).days > 1:
                continue
            row = self._compute_event(e)
            if row:
                rows.append(row)
        rows.sort(key=lambda r: (SIGNAL_GROUP.get(r["signal"], (9, "other"))[0],
                                 SIGNAL_RANK.get(r["signal"], 99),
                                 -(r["best_p"] or 0), r["city"]))
        self.rows = rows
        self.last_refresh = __import__("time").time()
        if self.executor:
            try:
                self.executor.on_refresh(rows)
            except Exception as ex:  # noqa: BLE001
                self.on_log("✗", f"[kalshi-wx] executor error: {ex}")
        return rows

    def _book_confirm(self, best, kind=None):
        """Re-price the candidate on Kalshi's real top-of-book quote.

        Unlike Polymarket (whose Gamma screening quote can be fiction vs the CLOB),
        the Kalshi /markets quote already IS the live best bid/ask, so there is no
        ladder to walk — we re-apply the money gates on that quote, using the
        Kalshi quadratic taker fee, and size at the configured stake. A paper
        fill will be booked at this ask by the executor.
        """
        ask_c, bid_c = best.get("ask_c"), best.get("bid_c")
        if ask_c is None or bid_c is None:
            return "NO-BOOK", "missing quote"
        if not credible_quote(ask_c, bid_c):
            best["edge_c"] = None
            return "NO-BOOK", "dust / one-sided book"
        fee_c = kfeed.taker_fee_c(ask_c)
        best["fee_c"] = round(fee_c, 2)
        best["edge_c"] = round(best["p"] * 100 - ask_c - fee_c, 1)
        best["shares_planned"] = max(int(best.get("min_size") or 1),
                                     round(STAKE_USD / (ask_c / 100.0)))
        best["limit_c"] = ask_c
        best["book_depth"] = best.get("shares_planned")
        spread = ask_c - bid_c
        if spread > MAX_SPREAD_C:
            return "WIDE", f"spread {spread:.0f}c > {MAX_SPREAD_C:.0f}c"
        if ask_c < PRICE_MIN_C:
            return "MKT-LOCKED", f"ask {ask_c:.0f}c < {PRICE_MIN_C:.0f}c — resolved against us"
        if ask_c > PRICE_MAX_C:
            return "PRICED", f"ask {ask_c:.0f}c > {PRICE_MAX_C:.0f}c"
        if best["edge_c"] < MIN_EDGE_C:
            return "THIN-EDGE", f"edge {best['edge_c']}c < {MIN_EDGE_C}c"
        if best["edge_c"] > MAX_EDGE_C:
            return "TOO-GOOD", f"edge {best['edge_c']}c > {MAX_EDGE_C:.0f}c — market disagrees"
        return "ENTER", f"p {best['p']:.2f} @ {ask_c:.0f}c ×{best['shares_planned']}"
