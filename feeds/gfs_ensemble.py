"""
feeds/gfs_ensemble.py — observation-anchored GFS ensemble (SHADOW ONLY).

The NEAR-LOCK model answers "given the day's running extreme and the local
hour, what does climatology say the final extreme will be?" It is conditioned
on the clock and on one number, and it has no opinion about the atmosphere. A
31-member ensemble forecast has exactly the opposite blind spot: it knows the
synoptic setup but has never seen what this station actually reported today.

Neither alone is useful for a 2°F bucket. Measured on Chicago 2026-07-27, the
RAW ensemble spread of the remaining-day max does NOT narrow as the day runs
out — it stays ~9°F from 10:00 through 21:00 local, because that spread is
model-initialisation disagreement, not time-remaining uncertainty:

    from 10:00 local  spread 9.2°F      from 19:00 local  spread 8.9°F
    from 15:00 local  spread 9.2°F      from 21:00 local  spread 8.4°F

So a naive "fraction of members above the bucket" probability (the approach in
the public GFS bots) is mostly noise at our bucket width. What IS measurable is
the LEVEL error. Same station, same morning, anchoring each member against the
hours already observed:

    all 31/31 members running COLD vs KMDW  —  bias min −5.0, med −2.3, max −1.5

That is a same-day, station-specific model bias larger than a bucket. It is the
one thing the ensemble can tell us that the running extreme cannot: not "how
hot will it get" but "is every member currently under-reading this station,
i.e. does the afternoon still have room the climatology PMF isn't pricing".

That is a hypothesis about our actual failure mode — Chicago, Mexico City and
Atlanta all lost by continuing to climb after the extreme looked locked — and
it is NOT a decision. Everything here is recorded onto the entry record and
gates nothing, the same discipline applied to solar elevation (which would have
blocked 10 winners, +$19.02, to dodge one −$7.68 loser) and to the METAR
conditions score (whose composite failed to separate winners from losers). Read
it out with scripts/conditions_report.py once entries accumulate.

Data source: Open-Meteo's ensemble API — free, no key, batched across all
stations in a single request. Forecasts are requested in UTC and bucketed into
each station's local day here, because a batched request cannot carry a
per-location timezone.
"""

import logging
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("feeds.gfs_ensemble")

API = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODEL = "gfs025"
TIMEOUT = 30
# GFS ensemble runs 4×/day and Open-Meteo publishes a few hours behind; refetching
# faster than this returns the identical run.
REFRESH_SEC = 3 * 3600
# a failed fetch must not re-hammer the API from every trading cycle
RETRY_SEC = 600
# Open-Meteo caps a batched request; stay well inside it (each location returns
# 31 members × 72 hours).
CHUNK = 12
# How many of the most recent observed hours to measure each member's bias over.
# Short enough to reflect today's regime rather than the overnight, long enough
# that one bad ob doesn't set the correction.
ANCHOR_HOURS = 4
# Minimum matched member/observation hours before a bias is worth recording.
MIN_MATCHED = 2


def _f(c):
    return None if c is None else round(c * 9.0 / 5.0 + 32.0, 2)


class GfsEnsembleFeed:
    """Per-station anchored ensemble view. Never raises into the trading loop."""

    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self._raw = {}         # icao -> {"fetched": ts, "series": [ {utc_iso: temp_c}, ... ] }
        self._state = {}       # icao -> shadow block (see _anchor)
        self._lock = threading.Lock()
        self._next_fetch = 0.0
        self.last_poll_ts = None
        self.last_error = None

    # ── polling ──────────────────────────────────────────────────────────────
    def poll(self, metar_snapshot: dict):
        """Refresh forecasts if stale, then re-anchor every station.

        metar_snapshot: MetarFeed.snapshot() — supplies each station's lat/lon,
        timezone and the observations already recorded for its local day, so
        this feed needs no station table of its own and can never disagree with
        the settlement station the rest of the system is tracking.
        """
        try:
            self._fetch_if_stale(metar_snapshot)
            now = datetime.now(timezone.utc)
            state = {}
            for icao, st in (metar_snapshot or {}).items():
                blk = self._anchor(icao, st, now)
                if blk:
                    state[icao] = blk
            with self._lock:
                self._state = state
                self.last_poll_ts = time.time()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.debug("ensemble poll failed: %s", e)

    def _fetch_if_stale(self, snap):
        if time.time() < self._next_fetch:
            return
        want = [(icao, st) for icao, st in (snap or {}).items()
                if st.get("lat") is not None and st.get("lon") is not None]
        if not want:
            return
        fetched, errors = {}, 0
        for i in range(0, len(want), CHUNK):
            chunk = want[i:i + CHUNK]
            try:
                r = requests.get(API, timeout=TIMEOUT, params={
                    "latitude": ",".join(f"{st['lat']:.4f}" for _, st in chunk),
                    "longitude": ",".join(f"{st['lon']:.4f}" for _, st in chunk),
                    "hourly": "temperature_2m",
                    "models": MODEL,
                    "past_days": 1,
                    "forecast_days": 2,
                })
                r.raise_for_status()
                payload = r.json()
                if isinstance(payload, dict):        # single-location response
                    payload = [payload]
                # Open-Meteo returns locations in request order.
                for (icao, _st), loc in zip(chunk, payload):
                    series = self._members(loc)
                    if series:
                        fetched[icao] = series
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.last_error = str(e)
        if not fetched:
            self._next_fetch = time.time() + RETRY_SEC
            self.on_log("!", f"[gfs] ensemble fetch failed ({self.last_error})")
            return
        if errors:
            self.on_log("!", f"[gfs] {errors} ensemble chunk(s) failed; "
                             f"{len(fetched)} stations refreshed")
        else:
            self.last_error = None
        now = time.time()
        with self._lock:
            for icao, series in fetched.items():
                self._raw[icao] = {"fetched": now, "series": series}
        self._next_fetch = now + REFRESH_SEC

    @staticmethod
    def _members(loc):
        """[{utc_datetime: temp_c}, ...] — one dict per ensemble member."""
        h = (loc or {}).get("hourly") or {}
        times = h.get("time") or []
        if not times:
            return []
        stamps = []
        for t in times:
            try:
                stamps.append(datetime.fromisoformat(t).replace(tzinfo=timezone.utc))
            except ValueError:
                stamps.append(None)
        out = []
        for key in sorted(k for k in h if k.startswith("temperature_2m")):
            vals = h.get(key) or []
            ser = {ts: float(v) for ts, v in zip(stamps, vals)
                   if ts is not None and v is not None}
            if ser:
                out.append(ser)
        return out

    # ── anchoring ────────────────────────────────────────────────────────────
    def _anchor(self, icao, st, now_utc):
        """Bias-correct each member against today's observations, then describe
        the distribution of the remaining-day extreme. Returns None when there
        isn't enough to say anything honest."""
        with self._lock:
            raw = self._raw.get(icao)
        if not raw or not raw.get("series"):
            return None
        obs = st.get("today_obs") or []
        if len(obs) < MIN_MATCHED:
            return None
        # observations, newest last, snapped to the nearest hour so they line up
        # with the forecast grid
        by_hour = {}
        for iso, temp_c in obs:
            try:
                ts = datetime.fromisoformat(iso)
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            slot = ts.replace(minute=0, second=0, microsecond=0)
            if ts.minute >= 30:
                slot += timedelta(hours=1)
            by_hour[slot] = float(temp_c)
        if len(by_hour) < MIN_MATCHED:
            return None
        last_ob = max(by_hour)
        anchor_from = last_ob - timedelta(hours=ANCHOR_HOURS - 1)
        # the local day still ahead of us: everything after the last observation,
        # up to the end of the station's local date
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(st["tz"])
            local_date = datetime.fromisoformat(st["local_date"]).date()
        except Exception:  # noqa: BLE001
            return None

        biases, raw_ext, anch_ext = [], [], []
        matched_n = 0
        for ser in raw["series"]:
            errs = [ser[t] - by_hour[t] for t in sorted(by_hour)
                    if t >= anchor_from and t in ser]
            if len(errs) < MIN_MATCHED:
                continue
            matched_n = max(matched_n, len(errs))
            bias = statistics.fmean(errs)
            rem = [v for t, v in ser.items()
                   if t > last_ob and t.astimezone(tz).date() == local_date]
            if not rem:
                continue
            biases.append(bias)
            # a high and a low read opposite tails; record both extremes and let
            # the consumer pick, so this module stays free of trade semantics
            raw_ext.append((max(rem), min(rem)))
            anch_ext.append((max(rem) - bias, min(rem) - bias))
        if len(biases) < 3:
            return None

        hi_a = sorted(x for x, _ in anch_ext)
        lo_a = sorted(y for _, y in anch_ext)
        hi_r = sorted(x for x, _ in raw_ext)
        lo_r = sorted(y for _, y in raw_ext)

        def q(v, p):
            return v[min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))]

        blk = {
            "model": MODEL,
            "n_members": len(biases),
            "matched_hours": matched_n,
            "anchor_hours": ANCHOR_HOURS,
            "fetch_age_min": round((time.time() - raw["fetched"]) / 60.0, 1),
            "last_ob_utc": last_ob.isoformat(),
            "rem_hours": round((datetime.combine(local_date, datetime.max.time())
                                .replace(tzinfo=tz) - now_utc).total_seconds() / 3600.0, 1),
            # ── the headline signal: is every member under- or over-reading the
            # station right now? 31/31 in one direction is the interesting case.
            "members_cold": sum(1 for b in biases if b < 0),
            "members_warm": sum(1 for b in biases if b > 0),
            "bias_med_c": round(statistics.median(biases), 2),
            "bias_min_c": round(min(biases), 2),
            "bias_max_c": round(max(biases), 2),
            # ── remaining-day extreme, raw vs anchored ──
            "raw_max_med_c": round(statistics.median(hi_r), 2),
            "raw_min_med_c": round(statistics.median(lo_r), 2),
            "anch_max_med_c": round(statistics.median(hi_a), 2),
            "anch_max_p90_c": round(q(hi_a, 0.90), 2),
            "anch_max_c": round(hi_a[-1], 2),
            "anch_min_med_c": round(statistics.median(lo_a), 2),
            "anch_min_p10_c": round(q(lo_a, 0.10), 2),
            "anch_min_c": round(lo_a[0], 2),
            "spread_max_c": round(hi_a[-1] - hi_a[0], 2),
            "spread_min_c": round(lo_a[-1] - lo_a[0], 2),
        }
        blk["bias_med_f_delta"] = round(blk["bias_med_c"] * 9.0 / 5.0, 2)
        for k in ("raw_max_med", "raw_min_med", "anch_max_med", "anch_max_p90",
                  "anch_max", "anch_min_med", "anch_min_p10", "anch_min"):
            blk[k + "_f"] = _f(blk[k + "_c"])
        blk["spread_max_f"] = round(blk["spread_max_c"] * 9.0 / 5.0, 2)
        blk["spread_min_f"] = round(blk["spread_min_c"] * 9.0 / 5.0, 2)
        return blk

    # ── access ───────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def station(self, icao):
        with self._lock:
            v = self._state.get(icao)
            return dict(v) if v else None
