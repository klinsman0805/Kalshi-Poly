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
TIMEOUT = 15


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
        try:
            r = requests.get(API, params={"ids": ",".join(icaos), "format": "json",
                                          "hours": LOOKBACK_HOURS}, timeout=TIMEOUT)
            r.raise_for_status()
            obs = r.json()
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.on_log("!", f"[metar] poll failed: {e}")
            return
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
        max_c = min_c = latest = latest_ts = None
        max_f = min_f = None
        n_today = 0
        for o in rows:
            ts = self._obs_time(o)
            temp = o.get("temp")
            if ts is None or temp is None:
                continue
            if latest_ts is None or ts > latest_ts:
                latest, latest_ts = float(temp), ts
            if ts.astimezone(tz).date() != local_today:
                continue
            n_today += 1
            tc = float(temp)
            tf = self._f(tc)
            max_c = tc if max_c is None else max(max_c, tc)
            min_c = tc if min_c is None else min(min_c, tc)
            max_f = tf if max_f is None else max(max_f, tf)
            min_f = tf if min_f is None else min(min_f, tf)
            # 6-hourly max/min groups (US stations): cover the preceding 6 h;
            # only trust them for today when the report is ≥6 h into the day.
            if ts.astimezone(tz).hour >= 6:
                mx, mn = o.get("maxT"), o.get("minT")
                if mx is not None:
                    max_c = max(max_c, float(mx)); max_f = max(max_f, self._f(float(mx)))
                if mn is not None:
                    min_c = min(min_c, float(mn)); min_f = min(min_f, self._f(float(mn)))
        return {
            "icao": icao,
            "local_date": local_today.isoformat(),
            "local_hour": now_utc.astimezone(tz).hour + now_utc.astimezone(tz).minute / 60.0,
            "tz": str(tz),
            "temp_c": latest,
            "temp_f": self._f(latest),
            "max_c": max_c, "min_c": min_c,
            "max_f": max_f, "min_f": min_f,
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
