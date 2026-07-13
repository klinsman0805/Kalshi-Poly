"""
modules/weather.py — NEAR-LOCK weather signal engine (monitor + paper).

Strategy: in the last hours of a city's local day, the daily max temperature
is largely locked in — the settlement station has already printed it. Compare
the market's bucket prices against P(final max | observed running max), where
that probability comes from a station-specific remaining-rise climatology
(data/weather_climo.json, built by scripts/build_weather_climo.py) — an
empirical table, not a forecast.

Signal: ENTER a bucket when
    model p ≥ WEATHER_P_MIN   (default 0.92)
    ask   ≤ WEATHER_PRICE_MAX_C cents (default 82)
    net edge = p·100 − ask ≥ WEATHER_MIN_EDGE_C (default 8)
    local hour ≥ WEATHER_MIN_LOCAL_HOUR (default 13 — diurnal peak forming)
plus data-sanity gates (enough obs today, live book, sane spread).

Only °C buckets on metar-sourced stations with climatology are tradeable;
everything else renders as monitor-only. The observed max and the settlement
value are the same METAR feed for these markets (see feeds/poly_weather.py).
"""

import logging
import json
import os
import time
from pathlib import Path

from feeds.poly_weather import fetch_temperature_events

log = logging.getLogger("modules.weather")

CLIMO_PATH = Path(os.getenv("WEATHER_CLIMO", "data/weather_climo.json"))

P_MIN = float(os.getenv("WEATHER_P_MIN", "0.92"))
PRICE_MAX_C = float(os.getenv("WEATHER_PRICE_MAX_C", "82"))
MIN_EDGE_C = float(os.getenv("WEATHER_MIN_EDGE_C", "8"))
MIN_LOCAL_HOUR = float(os.getenv("WEATHER_MIN_LOCAL_HOUR", "13"))
MIN_OBS_TODAY = int(os.getenv("WEATHER_MIN_OBS_TODAY", "3"))
MAX_SPREAD_C = float(os.getenv("WEATHER_MAX_SPREAD_C", "10"))


class WeatherEngine:
    def __init__(self, metar, executor=None, on_log=None):
        self.metar = metar
        self.executor = executor
        self.on_log = on_log or (lambda i, m: None)
        self.climo = {}
        self.rows = []              # per-city dashboard rows (today's markets)
        self.last_refresh = None
        self.last_error = None
        self._climo_mtime = None
        self._load_climo()

    def _load_climo(self):
        """Load (or hot-reload) the climatology table when the file changes."""
        try:
            mtime = CLIMO_PATH.stat().st_mtime
        except FileNotFoundError:
            if self._climo_mtime is None:
                self.on_log("!", f"[weather] no climatology at {CLIMO_PATH} — "
                                 "monitor-only until scripts/build_weather_climo.py runs")
                self._climo_mtime = 0
            return
        if mtime == self._climo_mtime:
            return
        try:
            self.climo = json.loads(CLIMO_PATH.read_text())
            self._climo_mtime = mtime
            self.on_log("◆", f"[weather] climatology loaded: {len(self.climo)} stations")
        except Exception as e:  # noqa: BLE001
            self.on_log("✗", f"[weather] climatology load failed: {e}")

    # ── model ────────────────────────────────────────────────────────────────
    def bucket_prob(self, icao, month, hour, run_max_c, lo, hi):
        """P(final whole-°C max lands in [lo, hi]) given running max."""
        st = self.climo.get(icao)
        if not st:
            return None
        pmf = (st.get("pmf", {}).get(str(month)) or {}).get(str(int(hour)))
        if not pmf:
            return None
        r = round(run_max_c)
        p = 0.0
        for k_str, pk in pmf.items():
            final = r + int(k_str)
            if (lo is None or final >= lo) and (hi is None or final <= hi):
                p += pk
        return p

    # ── refresh ──────────────────────────────────────────────────────────────
    def refresh(self):
        self._load_climo()
        try:
            events = fetch_temperature_events()
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.on_log("✗", f"[weather] discovery failed: {e}")
            return self.rows
        # register stations we know the timezone for (from climatology)
        self.metar.set_stations({
            e["station"]: self.climo[e["station"]]["tz"]
            for e in events
            if e["source"] == "metar" and e["station"] in self.climo
        })
        self.metar.poll()

        rows = []
        for e in events:
            row = self._compute_event(e)
            if row:
                rows.append(row)
        rows.sort(key=lambda r: (r["signal"] != "ENTER",
                                 not (r["is_today"] and r["tradeable"]),
                                 r["date"] or "9999", -(r["best_p"] or 0), r["city"]))
        self.rows = rows
        self.last_refresh = time.time()
        if self.executor:
            try:
                self.executor.on_refresh(rows)
            except Exception as ex:  # noqa: BLE001
                self.on_log("✗", f"[weather] executor error: {ex}")
        return rows

    def _compute_event(self, e):
        icao = e["station"]
        st = self.metar.station(icao) if icao else None
        tradeable = (e["source"] == "metar" and icao in self.climo
                     and all(b["unit"] == "C" for b in e["buckets"]))
        # only today's local date is a NEAR-LOCK candidate
        is_today = bool(st and e["date"] and st["local_date"] == e["date"].isoformat())
        if not st and not e["date"]:
            return None

        buckets, best = [], None
        for b in e["buckets"]:
            p = None
            if tradeable and is_today and st["max_c"] is not None:
                p = self.bucket_prob(icao, int(st["local_date"][5:7]),
                                     st["local_hour"], st["max_c"], b["lo"], b["hi"])
            ask_c = b["ask"] * 100 if b["ask"] is not None else None
            bid_c = b["bid"] * 100 if b["bid"] is not None else None
            edge_c = (p * 100 - ask_c) if (p is not None and ask_c is not None) else None
            bv = {
                "label": self._label(b),
                "lo": b["lo"], "hi": b["hi"], "unit": b["unit"],
                "bid_c": bid_c, "ask_c": ask_c,
                "p": round(p, 4) if p is not None else None,
                "edge_c": round(edge_c, 1) if edge_c is not None else None,
                "condition_id": b["condition_id"],
                "token_yes": b["token_yes"],
                "min_size": b["min_size"],
            }
            buckets.append(bv)
            if p is not None and (best is None or p > best["p"]):
                best = {**bv, "p": p}

        signal, why = self._gate(e, st, is_today, tradeable, best)
        return {
            "city": e["city"], "date": e["date"].isoformat() if e["date"] else None,
            "station": icao, "source": e["source"], "slug": e["slug"],
            "tradeable": tradeable, "is_today": is_today,
            "local_hour": round(st["local_hour"], 2) if st else None,
            "temp_c": st["temp_c"] if st else None,
            "max_c": st["max_c"] if st else None,
            "obs_today": st["obs_today"] if st else 0,
            "buckets": buckets,
            "best_p": round(best["p"], 4) if best else None,
            "best_label": best["label"] if best else None,
            "signal": signal, "why": why,
            "entry": ({\
                "condition_id": best["condition_id"], "token_yes": best["token_yes"],
                "label": best["label"], "ask_c": best["ask_c"], "bid_c": best["bid_c"],
                "p": round(best["p"], 4), "edge_c": best["edge_c"],
                "min_size": best["min_size"], "city": e["city"],
                "date": e["date"].isoformat(), "station": icao, "slug": e["slug"],
            } if signal == "ENTER" and best else None),
        }

    def _gate(self, e, st, is_today, tradeable, best):
        if not tradeable:
            return "MONITOR", ("HKO source" if e["source"] == "hko" else
                               "no climatology" if e["station"] not in self.climo
                               else "non-°C buckets" if e["source"] == "metar"
                               else "unknown source")
        if not is_today:
            return "WAIT", "not station-local today"
        if st is None or st["max_c"] is None:
            return "NO-DATA", "no observations"
        if st["obs_today"] < MIN_OBS_TODAY:
            return "NO-DATA", f"only {st['obs_today']} obs today"
        if st["local_hour"] < MIN_LOCAL_HOUR:
            return "EARLY", f"local {st['local_hour']:.1f}h < {MIN_LOCAL_HOUR}h"
        if best is None:
            return "NO-DATA", "no model probability"
        if best["p"] < P_MIN:
            return "NO-LOCK", f"best p {best['p']:.2f} < {P_MIN}"
        if best["ask_c"] is None or best["bid_c"] is None:
            return "NO-BOOK", "missing quote"
        if best["ask_c"] - best["bid_c"] > MAX_SPREAD_C:
            return "WIDE", f"spread {best['ask_c'] - best['bid_c']:.0f}c"
        if best["ask_c"] > PRICE_MAX_C:
            return "PRICED", f"ask {best['ask_c']:.0f}c > {PRICE_MAX_C:.0f}c"
        if best["edge_c"] is None or best["edge_c"] < MIN_EDGE_C:
            return "THIN-EDGE", f"edge {best['edge_c']}c < {MIN_EDGE_C}c"
        return "ENTER", f"p {best['p']:.2f} @ {best['ask_c']:.0f}c"

    @staticmethod
    def _label(b):
        u = "°" + b["unit"]
        if b["lo"] is None:
            return f"≤{b['hi']}{u}"
        if b["hi"] is None:
            return f"≥{b['lo']}{u}"
        if b["lo"] == b["hi"]:
            return f"{b['lo']}{u}"
        return f"{b['lo']}–{b['hi']}{u}"

    def state(self):
        return {
            "rows": self.rows,
            "last_refresh": self.last_refresh,
            "last_error": self.last_error,
            "climo_stations": len(self.climo),
            "config": {"p_min": P_MIN, "price_max_c": PRICE_MAX_C,
                       "min_edge_c": MIN_EDGE_C, "min_local_hour": MIN_LOCAL_HOUR},
        }
