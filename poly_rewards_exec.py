"""
modules/poly_rewards_exec.py — NEAR-LOCK-gated LP-reward candidate finder.

The LP-reward scanner (feeds/poly_rewards.py) answers "which market pays the
most per dollar of resting capital" — it says nothing about the risk of that
resting order actually getting FILLED. A filled LP order isn't a reward, it's
a real directional position, with none of the weather bot's own entry vetting
behind it.

This reuses the SAME plateau/lock check the trading bot already relies on
(MetarFeed's age-since-trough tracking, modules.weather's MIN_MAX_AGE_MIN /
MIN_LOCAL_HOUR thresholds) to filter LP candidates down to ones whose bucket
outcome is already effectively decided — high-temp buckets at night (the
day's peak already happened), low-temp buckets during the day (the overnight
low already happened). If a NEAR-LOCK-gated order gets filled, it's a bet
that's already mostly settled, not a live gamble picked up as a side effect
of chasing yield.

PAPER/LOGGING ONLY — this never calls polymarket.place_gtc(). It answers "if
we were farming rewards, which candidates would actually be safe to rest an
order on right now" and logs that, so the candidate list and lock timing can
be judged against real outcomes before anything here places a live order.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds.metar import MetarFeed
from feeds.poly_weather import fetch_temperature_events
from modules.weather import MIN_LOCAL_HOUR, MIN_LOCAL_HOUR_LOW, MIN_MAX_AGE_MIN

log = logging.getLogger("modules.poly_rewards_exec")

CANDIDATE_LOG = Path("poly_rewards_candidates.jsonl")
CLIMO_PATH = Path("data/weather_climo.json")


class PolyRewardsExec:
    """Cross-references LP-reward scan results against the real-time METAR
    plateau check, logs (never places) NEAR-LOCK-gated candidates."""

    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.metar = MetarFeed()
        self._climo_tz = {}
        self._logged_today = set()   # (condition_id, date) fired once each
        self._load_climo_tz()

    def _load_climo_tz(self):
        try:
            climo = json.loads(CLIMO_PATH.read_text())
            self._climo_tz = {icao: v["tz"] for icao, v in climo.items() if v.get("tz")}
        except Exception as e:  # noqa: BLE001
            log.warning("no climatology tz table available: %s", e)

    def check(self, scan_results):
        """scan_results: the full (unsliced) list from feeds.poly_rewards.scan().
        Joins each by condition_id against live Polymarket temperature events
        to recover station/kind/city, then applies the same lock check the
        weather bot uses. Logs qualifying candidates; places nothing."""
        if not self._climo_tz:
            self._load_climo_tz()
            if not self._climo_tz:
                return []

        try:
            events = fetch_temperature_events()
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[lp-exec] event fetch failed: {e}")
            return []

        # condition_id -> (station, city, kind, date) — one lookup per bucket.
        # date is normalized to an ISO string here: poly_weather's _parse_date
        # returns a real date object, but MetarFeed's local_date is a string
        # (isoformat()) and this same value gets JSON-persisted below — a bare
        # date object silently fails a "!=" comparison against a string
        # (always unequal, no exception) and would crash json.dumps outright.
        by_cid = {}
        for e in events:
            if e.get("source") != "metar" or not e.get("station") or not e.get("date"):
                continue
            date_s = e["date"].isoformat() if hasattr(e["date"], "isoformat") else str(e["date"])
            for b in e.get("buckets", []):
                if b.get("condition_id"):
                    by_cid[b["condition_id"]] = (e["station"], e["city"], e["kind"], date_s,
                                                 bool(b.get("closed")), bool(b.get("resolved")))

        stations_needed = {}
        matched = []
        for r in scan_results:
            hit = by_cid.get(r.get("condition_id"))
            if not hit:
                continue
            station, city, kind, date, closed, resolved = hit
            if closed or resolved:
                continue  # not a real, currently-open market regardless of what the reward feed says
            tz = self._climo_tz.get(station)
            if not tz:
                continue
            stations_needed[station] = tz
            matched.append((r, station, city, kind, date))

        if not matched:
            return []

        # prune stale dedup keys so a long-running process doesn't grow this
        # set forever — a date only ever needs to be kept while it's still
        # possibly "today" for some station
        live_dates = {date for _, _, _, _, date in matched}
        self._logged_today = {k for k in self._logged_today if k[1] in live_dates}

        self.metar.set_stations(stations_needed)
        self.metar.poll()
        snap = self.metar.snapshot()

        candidates = []
        for r, station, city, kind, date in matched:
            st = snap.get(station)
            if not st or st.get("local_date") != date:
                continue  # station's local day has already rolled past this market's date
            local_hour = st.get("local_hour")
            age = st.get("max_age_min") if kind == "high" else st.get("min_age_min")
            min_hour = MIN_LOCAL_HOUR if kind == "high" else MIN_LOCAL_HOUR_LOW
            if local_hour is None or age is None:
                continue
            locked = local_hour >= min_hour and age >= MIN_MAX_AGE_MIN
            if not locked:
                continue
            # Real-book verification — don't trust our own derived mid_c/yield
            # alone. best_bid_c/best_ask_c are the actual live quotes score_market
            # just fetched; require BOTH to exist and a genuine positive gap
            # between them. A book with only one side, or bid==ask, isn't a real
            # resting opening — it's either empty or already crossed/matched.
            bid, ask = r.get("best_bid_c"), r.get("best_ask_c")
            if bid is None or ask is None or ask <= bid:
                self.on_log("→", f"[lp-exec] {city} {kind} locked but book has no real "
                                 f"two-sided gap (bid={bid}, ask={ask}) — skipping, not a real opening")
                continue
            key = (r["condition_id"], date)
            if key in self._logged_today:
                continue
            self._logged_today.add(key)
            rec = {
                "type": "lp_candidate", "ts": datetime.now(timezone.utc).isoformat(),
                "condition_id": r["condition_id"], "question": r["question"],
                "city": city, "kind": kind, "date": date, "station": station,
                "local_hour": round(local_hour, 1), "age_min": round(age, 1),
                "yield_per_dollar_per_day": round(r["yield_per_dollar_per_day"], 4),
                "est_daily_usd": round(r["est_daily_usd"], 2),
                "mid_c": round(r["mid_c"], 2), "two_sided_required": r["two_sided_required"],
                # the real evidence, not just our derived numbers
                "real_best_bid_c": bid, "real_best_ask_c": ask, "real_gap_c": round(ask - bid, 2),
                "min_size": r.get("min_size"), "capital_usd": round(r["capital_usd"], 2),
            }
            self._persist(rec)
            candidates.append(rec)
            self.on_log("→", f"[lp-exec] NEAR-LOCK candidate: {city} {kind} — "
                             f"locked {age:.0f}min, real book bid={bid}c/ask={ask}c (gap {ask-bid:.1f}c), "
                             f"yield {r['yield_per_dollar_per_day']*100:.0f}%/day, "
                             f"{'two-sided' if r['two_sided_required'] else 'single-sided ok'} "
                             f"(NOT placed — paper/logging only)")
        return candidates

    def _persist(self, rec):
        try:
            with open(CANDIDATE_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # noqa: BLE001
            log.warning("candidate log write failed: %s", e)
