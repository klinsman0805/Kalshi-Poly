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

The candidate finder is PAPER/LOGGING ONLY — it never calls place_gtc(), it
just answers "which candidates would be safe to rest an order on right now."

This module ALSO tracks any GTC order actually placed (via place_and_track,
which any manual or future automated caller should go through instead of
calling polymarket.place_gtc directly) and watches it for fills. A filled LP
order is real exposure with none of the weather bot's own entry vetting
behind it — check_fills() re-checks a filled position against the exact same
monotonic dead-bucket test weather_exec._close_dead already relies on (the
daily extreme only ever moves one way, so once it's passed our bucket the
position cannot recover) and, once armed via POLY_REWARDS_LIVE, cancels the
remaining resting order and sells the filled shares immediately. Defaults to
POLY_REWARDS_LIVE=false — logs what it WOULD do, same as everything else
that started this session in shadow mode.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds.metar import MetarFeed
from feeds.poly_weather import fetch_temperature_events
from modules.weather import MIN_LOCAL_HOUR, MIN_LOCAL_HOUR_LOW, MIN_MAX_AGE_MIN

log = logging.getLogger("modules.poly_rewards_exec")

ORDERS_LOG = Path("poly_rewards_orders.jsonl")
POLY_REWARDS_LIVE = os.getenv("POLY_REWARDS_LIVE", "false").strip().lower() == "true"
# Early-exit on a still-forming (not yet mathematically dead) adverse trend —
# see _trend_continuing_adverse. Separate flag from POLY_REWARDS_LIVE: this
# changes WHEN a sell fires (earlier), not WHETHER real orders go out (that's
# still POLY_REWARDS_LIVE's job). Default off — paper-log only until proven.
POLY_REWARDS_TREND_EXIT = os.getenv("POLY_REWARDS_TREND_EXIT", "false").strip().lower() == "true"


def _trend_continuing_adverse(today_obs, kind, lo, hi, lookback=3):
    """Is the running extreme still moving toward/through this bucket's dead
    edge, RIGHT NOW — before the strict already-dead test would catch it?

    Reads the last `lookback` local-day observations from MetarFeed's
    today_obs (ascending [(iso_ts, temp_c)], whatever precision the station
    actually reports — tenths for US ASOS stations via the METAR T-group,
    whole degrees for international ones — no separate code path needed,
    the underlying data just carries whatever resolution it carries).

    Requires a CONSISTENT run: every step in the window must move the same
    direction, with at least one real move, not a flat line. A single tick
    is not a trend — this session watched a market's own reference price
    swing five times in ~35 minutes on far less signal than one new
    reading, so one-tick reactions would whipsaw constantly. Only counts as
    "continuing adverse" when also already close to the edge (within 1 unit
    of lo/hi) — a rising trend far from the edge isn't a reason to exit,
    it's just the normal diurnal climb.
    """
    if len(today_obs) < lookback + 1:
        return False
    recent = [t for _, t in today_obs[-(lookback + 1):]]
    diffs = [b - a for a, b in zip(recent, recent[1:])]
    if kind == "high":
        if hi is None:
            return False
        rising = all(d >= 0 for d in diffs) and any(d > 0 for d in diffs)
        return rising and recent[-1] >= hi - 1.0
    else:
        if lo is None:
            return False
        falling = all(d <= 0 for d in diffs) and any(d < 0 for d in diffs)
        return falling and recent[-1] <= lo + 1.0


def record_order_placed(order_id, token_id, price_cents, size, side, condition_id,
                        city, kind, date, station):
    """Shared ledger writer — the ONE place an order_placed record gets
    written, regardless of which SDK/path actually placed the order (old-SDK
    place_gtc via place_and_track below, or the new-SDK path in
    modules.poly_rewards_live.place_at_band). Order IDs live on Polymarket's
    CLOB itself, not inside either SDK, so check_fills()'s get_order() calls
    work the same regardless of which SDK placed the order."""
    if not order_id:
        # Nothing to track — a phantom "order_id": null record would make
        # check_fills() call get_order(None) forever (real bug, caught live:
        # a failed attempt still logged a tracking row, producing a 400
        # "Invalid orderID" every single cycle from then on).
        return None
    rec = {
        "type": "order_placed", "ts": datetime.now(timezone.utc).isoformat(),
        "order_id": order_id, "token_id": token_id, "condition_id": condition_id,
        "city": city, "kind": kind, "date": date, "station": station,
        "side": side, "price_c": price_cents, "size": size, "resolved": False,
    }
    try:
        with open(ORDERS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("order-tracking log write failed: %s", e)
    return order_id


def place_and_track(client, token_id, price_cents, size, side, condition_id,
                    city, kind, date, station, neg_risk=None):
    """Wraps polymarket.PolyClient.place_gtc (OLD SDK) — records everything
    check_fills() needs to recognize a fill and know which bucket/station it
    belongs to. NOTE: this SDK path cannot sign GTC orders for a proxy-funded
    account (confirmed this session — 'invalid POLY_PROXY signature', root-
    caused to a missing order-type branch in create_order's proxy signing).
    Use modules.poly_rewards_live.place_at_band for real GTC placement."""
    order_id = client.place_gtc(token_id, price_cents, size, side=side, neg_risk=neg_risk)
    return record_order_placed(order_id, token_id, price_cents, size, side,
                                condition_id, city, kind, date, station)

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
            # Never surface a candidate whose yield estimate we know we can't
            # trust. confidence="low" means either no qualifying competition
            # was visible (which pins our_share to ~1.0 and is exactly the
            # reading that measured 20.5x too high against a real payment) or
            # the complement book was unavailable. below_min_payout means the
            # program would not pay this out at all.
            if r.get("confidence") != "ok":
                self.on_log("→", f"[lp-exec] {city} {kind} locked but yield estimate is "
                                 f"low-confidence (no_competition="
                                 f"{r.get('no_competition_detected')}, complement_seen="
                                 f"{r.get('complement_seen')}) — skipping, not a real number")
                continue
            if r.get("below_min_payout"):
                self.on_log("→", f"[lp-exec] {city} {kind} locked but est ${r['est_daily_usd']:.2f}/day "
                                 f"is under the ${1.0:.0f} minimum payout — would never actually be paid")
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

    def _open_tracked_orders(self):
        """Replay ORDERS_LOG: an order_placed record starts tracking it, a
        later order_resolved record (same order_id) ends it. Returns only
        orders still being watched."""
        if not ORDERS_LOG.exists():
            return {}
        placed, resolved = {}, set()
        for line in open(ORDERS_LOG):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") == "order_placed":
                if not rec.get("order_id"):
                    continue  # defensive: a null-order_id row is never watchable
                placed[rec["order_id"]] = rec
            elif rec.get("type") == "order_resolved":
                resolved.add(rec.get("order_id"))
        return {oid: r for oid, r in placed.items() if oid not in resolved}

    def _resolve_order(self, order_id, reason, sold_shares=0.0, proceeds_usd=0.0):
        rec = {"type": "order_resolved", "ts": datetime.now(timezone.utc).isoformat(),
              "order_id": order_id, "reason": reason,
              "sold_shares": sold_shares, "proceeds_usd": proceeds_usd}
        with open(ORDERS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def check_fills(self, client):
        """For every tracked GTC order, check its real fill status. A filled
        order is real exposure — re-run the SAME monotonic dead-bucket test
        weather_exec._close_dead relies on (the daily extreme only ever moves
        one way, so once it's passed our bucket the position cannot recover).

        Paper by default (POLY_REWARDS_LIVE=false): logs what it would do.
        Once armed: cancels the remaining resting order and sells the filled
        shares immediately via a real FAK sweep — reusing the same order
        mechanics already tested for the weather bot's own dead-exits.
        """
        tracked = self._open_tracked_orders()
        if not tracked:
            return

        try:
            events = fetch_temperature_events()
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[lp-exec] event fetch failed (fill check): {e}")
            return
        by_cid_full = {}
        for e in events:
            for b in e.get("buckets", []):
                if b.get("condition_id"):
                    by_cid_full[b["condition_id"]] = b

        for order_id, o in tracked.items():
            try:
                status = client._clob.get_order(order_id)
            except Exception as e:  # noqa: BLE001
                self.on_log("!", f"[lp-exec] order status check failed {order_id}: {e}")
                continue
            if not status:
                continue
            matched = float(status.get("sizeMatched") or 0)
            state = status.get("status")
            if matched <= 0:
                if state in ("CANCELED", "CANCELLED"):
                    self._resolve_order(order_id, "cancelled_unfilled")
                continue  # still resting, untouched — nothing to protect yet

            # real exposure exists — check whether the bucket is already dead
            bucket = by_cid_full.get(o["condition_id"])
            if not bucket:
                continue
            self.metar.set_stations({o["station"]: self._climo_tz.get(o["station"], "")})
            self.metar.poll()
            st = self.metar.snapshot().get(o["station"])
            if not st:
                continue
            ext = st.get("max_c") if o["kind"] == "high" else st.get("min_c")
            if ext is None:
                continue
            ext_s = round(ext)
            lo, hi = bucket.get("lo"), bucket.get("hi")
            dead = ((o["kind"] == "high" and hi is not None and ext_s > hi) or
                    (o["kind"] == "low" and lo is not None and ext_s < lo))
            trend_dead = False
            if not dead and POLY_REWARDS_TREND_EXIT:
                trend_dead = _trend_continuing_adverse(st.get("today_obs") or [], o["kind"], lo, hi)
                if trend_dead:
                    self.on_log("!", f"[lp-exec] TREND EXIT {o['city']} {o['kind']} — "
                                     f"not yet dead (obs {ext_s}, bucket {lo}-{hi}) but recent "
                                     f"readings show a consistent move toward the edge — "
                                     f"exiting early instead of waiting for the hard dead test")
            if not dead and not trend_dead:
                continue  # filled, bucket still alive, no adverse trend either — hold

            if not POLY_REWARDS_LIVE:
                reason = "trend" if trend_dead else "dead-bucket"
                self.on_log("!", f"[lp-exec] STOP-LOSS (paper, {reason}) {o['city']} {o['kind']} — "
                                 f"order {order_id} filled {matched:.1f} sh (obs {ext_s}, "
                                 f"bucket {lo}-{hi}) — would cancel + sell now, "
                                 f"POLY_REWARDS_LIVE is off")
                continue

            # armed: cancel whatever's left resting, then sell the filled shares now.
            # Only the BUY side ever needs this — a filled SELL just means we
            # successfully unloaded shares we already held, nothing to protect.
            if o["side"] != "BUY":
                self._resolve_order(order_id, "filled_sell_no_action_needed")
                continue
            try:
                client._clob.cancel_order(order_id)
            except Exception as e:  # noqa: BLE001
                self.on_log("!", f"[lp-exec] cancel failed {order_id}: {e}")
            # place_sell_fok is the same emergency-unwind sweep the weather bot
            # uses for a naked leg — take whatever bid exists right now, no FOK
            # all-or-nothing that could leave this fully unsold.
            sold = client.place_sell_fok(o["token_id"], matched, 0)
            proceeds = getattr(client, "_last_sell_proceeds_usd", None) or 0.0
            self._resolve_order(order_id, "stop_loss_live", sold_shares=sold, proceeds_usd=proceeds)
            self.on_log("✗", f"[lp-exec] STOP-LOSS EXECUTED {o['city']} {o['kind']} — "
                             f"sold {sold:.1f}/{matched:.1f} sh, ${proceeds:.2f} salvaged")
