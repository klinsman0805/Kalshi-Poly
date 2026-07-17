"""
feeds/metar.py — settlement-station observation feed (aviationweather.gov).

Polls METAR for the stations that Polymarket temperature markets settle to
(via Wunderground / NOAA obs pages, both of which mirror these observations)
and tracks the running daily-max temperature per station in *station-local*
time — that running max IS the number the market will settle on, up to the
remaining hours of the day.

Notes that matter for settlement fidelity:
  • International stations report whole °C; Wunderground's daily max is the
    max over these same obs, so tracking METAR == tracking the source.
  • US stations carry a T-group remark (temp in tenths, e.g. T02890178) and
    6-hourly max groups; the API's decoded `temp` and `maxT` fields surface
    them. We fold `maxT` into the running max when it maps to the same local
    day so a max that happened between polls isn't missed.
  • The API serves a lookback window (hours=N), so a restart never loses the
    day's max — we recompute it from history on every poll.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("feeds.metar")

API = "https://aviationweather.gov/api/data/metar"
LOOKBACK_HOURS = 20          # enough to rebuild the local day's max after restart
TIMEOUT = 20
# aviationweather caps a single response at ~400 obs total, so a 49-station
# batch returns only ~8 obs/station (the last few hours) — which silently
# misses the afternoon peak and corrupts each station's daily max. Request in
# small chunks so every station gets its full LOOKBACK_HOURS of history.
CHUNK = 10


class MetarFeed:
    """Tracks running local-day max temperature for a set of ICAO stations."""

    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self._tz = {}          # icao -> ZoneInfo
        self._state = {}       # icao -> view dict (see poll)
        self._lock = threading.Lock()
        self.last_poll_ts = None
        self.last_error = None

    def set_stations(self, station_tz: dict):
        """station_tz: {icao: tz_name}. Unknown-tz stations are ignored."""
        with self._lock:
            for icao, tz in station_tz.items():
                if icao in self._tz:
                    continue
                try:
                    self._tz[icao] = ZoneInfo(tz)
                except Exception:  # noqa: BLE001
                    log.warning("bad tz %s for %s — station skipped", tz, icao)

    # ── polling ──────────────────────────────────────────────────────────────
    def poll(self):
        with self._lock:
            icaos = sorted(self._tz)
        if not icaos:
            return
        obs, errors = [], 0
        for i in range(0, len(icaos), CHUNK):
            chunk = icaos[i:i + CHUNK]
            try:
                r = requests.get(API, params={"ids": ",".join(chunk), "format": "json",
                                              "hours": LOOKBACK_HOURS}, timeout=TIMEOUT)
                r.raise_for_status()
                obs.extend(r.json())
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.last_error = str(e)
        if errors:
            self.on_log("!", f"[metar] {errors}/{(len(icaos)+CHUNK-1)//CHUNK} chunks failed")
        if not obs:
            return
        if not errors:
            self.last_error = None
        by_station = {}
        for o in obs:
            by_station.setdefault(o.get("icaoId"), []).append(o)
        now = datetime.now(timezone.utc)
        with self._lock:
            for icao, rows in by_station.items():
                tz = self._tz.get(icao)
                if tz is None:
                    continue
                self._state[icao] = self._reduce(icao, rows, tz, now)
            self.last_poll_ts = time.time()

    @staticmethod
    def _obs_time(o):
        t = o.get("reportTime") or o.get("obsTime")
        if isinstance(t, (int, float)):                    # epoch seconds
            return datetime.fromtimestamp(t, tz=timezone.utc)
        if isinstance(t, str):
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _f(c):
        return None if c is None else c * 9.0 / 5.0 + 32.0

    def _reduce(self, icao, rows, tz, now_utc):
        """Fold a station's obs into running max AND min for *today's* local date.

        Tracked in both °C and °F. aviationweather.gov decodes the METAR T-group
        into `temp` as a float with tenths precision when present (US ASOS
        stations), so the °F extreme is boundary-accurate — °F markets settle on
        whole °F derived from that same tenths data.
        """
        local_today = now_utc.astimezone(tz).date()
        latest = latest_ts = None
        obs = sorted((o for o in rows
                      if self._obs_time(o) is not None and o.get("temp") is not None),
                     key=self._obs_time)
        today = []          # [(ts, temp_c, maxT, minT)] for the local day, ascending
        for o in obs:
            ts = self._obs_time(o)
            if latest_ts is None or ts > latest_ts:
                latest, latest_ts = float(o["temp"]), ts
            if ts.astimezone(tz).date() == local_today:
                today.append((ts, float(o["temp"]), o.get("maxT"), o.get("minT")))
        n_today = len(today)
        if not today:
            return {"icao": icao, "local_date": local_today.isoformat(),
                    "local_hour": now_utc.astimezone(tz).hour + now_utc.astimezone(tz).minute / 60.0,
                    "tz": str(tz), "temp_c": latest, "temp_f": self._f(latest),
                    "max_c": None, "min_c": None, "max_f": None, "min_f": None,
                    "max_age_min": None, "min_age_min": None, "obs_today": 0,
                    "latest_obs_utc": latest_ts.isoformat() if latest_ts else None}

        max_c = max(t for _, t, _, _ in today)
        min_c = min(t for _, t, _, _ in today)
        # 6-hourly groups (US stations) can prove an extreme the hourly obs missed.
        # We cannot know WHEN inside that 6h window it occurred, so stamp it at the
        # report: a correct LOWER bound on the age (conservative — never overstates
        # how long the extreme has held).
        six_max_ts = six_min_ts = None
        for ts, _t, mx, mn in today:
            if ts.astimezone(tz).hour >= 6:
                if mx is not None and float(mx) > max_c:
                    max_c, six_max_ts = float(mx), ts
                if mn is not None and float(mn) < min_c:
                    min_c, six_min_ts = float(mn), ts

        # ── age of each extreme, measured over the CURRENT diurnal swing ──────
        # Naively "first touch today" breaks when the day STARTS near its extreme:
        # Chengdu opened at 27C (yesterday's leftover heat), cooled to 23, then
        # climbed back to 27 by midday. First-touch said the max was 867min old and
        # waved the trade through — while the temperature was at its peak and still
        # rising. The honest clock starts at the overnight TROUGH: only the ascent
        # since the daily min tells us whether today's peak is in.
        def _first_touch(value, after_ts, want_max):
            for ts, t, _mx, _mn in today:
                if after_ts is not None and ts < after_ts:
                    continue
                if (t >= value - 1e-9) if want_max else (t <= value + 1e-9):
                    return ts
            return None

        min_ts_raw = _first_touch(min_c, None, want_max=False) or today[0][0]
        max_ts_raw = _first_touch(max_c, None, want_max=True) or today[0][0]
        # max: clock runs from the first touch AT/AFTER the day's trough. If the max
        # was only ever hit before the trough, the day has since cooled — genuinely
        # locked — so keep the original (old) stamp.
        max_ts = _first_touch(max_c, min_ts_raw, want_max=True) or max_ts_raw
        # min: mirror image — measure the descent since the day's peak.
        min_ts = _first_touch(min_c, max_ts_raw, want_max=False) or min_ts_raw
        if six_max_ts is not None:
            max_ts = six_max_ts
        if six_min_ts is not None:
            min_ts = six_min_ts
        max_f, min_f = self._f(max_c), self._f(min_c)

        def _age(t):
            return None if t is None else (now_utc - t).total_seconds() / 60.0
        return {
            "icao": icao,
            "local_date": local_today.isoformat(),
            "local_hour": now_utc.astimezone(tz).hour + now_utc.astimezone(tz).minute / 60.0,
            "tz": str(tz),
            "temp_c": latest,
            "temp_f": self._f(latest),
            "max_c": max_c, "min_c": min_c,
            "max_f": max_f, "min_f": min_f,
            # minutes since the current max/min was FIRST reached = how long the
            # extreme has held. A fresh extreme means the temperature is still
            # moving and the day is NOT locked.
            "max_age_min": _age(max_ts),
            "min_age_min": _age(min_ts),
            "obs_today": n_today,
            "latest_obs_utc": latest_ts.isoformat() if latest_ts else None,
        }

    # ── access ───────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def station(self, icao):
        with self._lock:
            v = self._state.get(icao)
            return dict(v) if v else None
