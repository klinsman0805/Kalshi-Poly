"""
modules/weather.py — NEAR-LOCK weather signal engine (monitor + paper).

Covers both daily-HIGH and daily-LOW temperature families (event kind
"high"/"low"; lows use the remaining-fall PMF and unlock earlier in the day
since the min usually prints around sunrise), in either °C (international
cities) or °F (US cities) — each station's climatology is built in the unit
its market settles in, and observed extremes are tracked in both units.

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
import math
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds.poly_weather import (fetch_temperature_events, taker_fee_c,
                                fetch_book_asks, vwap_for_size)

log = logging.getLogger("modules.weather")

CLIMO_PATH = Path(os.getenv("WEATHER_CLIMO", "data/weather_climo.json"))

P_MIN = float(os.getenv("WEATHER_P_MIN", "0.92"))
PRICE_MAX_C = float(os.getenv("WEATHER_PRICE_MAX_C", "82"))
# Floor: a genuine lagging NEAR-LOCK bucket trades ~60–82¢. If our target bucket
# asks near zero while the market has locked another bucket near 100¢, the market
# has resolved AGAINST us — our observed extreme is off by ~1° near a boundary,
# not free money. Never buy into that.
PRICE_MIN_C = float(os.getenv("WEATHER_PRICE_MIN_C", "40"))
MIN_EDGE_C = float(os.getenv("WEATHER_MIN_EDGE_C", "8"))
MIN_LOCAL_HOUR = float(os.getenv("WEATHER_MIN_LOCAL_HOUR", "13"))
# daily min usually prints around sunrise, so lows unlock earlier than highs
MIN_LOCAL_HOUR_LOW = float(os.getenv("WEATHER_MIN_LOCAL_HOUR_LOW", "10"))
MIN_OBS_TODAY = int(os.getenv("WEATHER_MIN_OBS_TODAY", "3"))
MAX_SPREAD_C = float(os.getenv("WEATHER_MAX_SPREAD_C", "10"))
# Gamma's bestAsk only SCREENS. Before a candidate becomes a signal, re-price it
# on the real CLOB ladder for the size we'd actually buy. Off => Gamma-priced
# (fast, but signals/paper fills can be fiction).
BOOK_CONFIRM = os.getenv("WEATHER_BOOK_CONFIRM", "true").strip().lower() == "true"
STAKE_USD = float(os.getenv("WEATHER_STAKE_USD", "5"))


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
    def bucket_prob(self, icao, kind, month, hour, run_ext, lo, hi):
        """P(final whole-degree daily extreme lands in [lo, hi]) given the
        running extreme, in the station's climatology unit. kind="high":
        final = run_max + k; kind="low": final = run_min − k."""
        st = self.climo.get(icao)
        if not st:
            return None
        table = st.get("pmf") if kind == "high" else st.get("pmf_low")
        pmf = ((table or {}).get(str(month)) or {}).get(str(int(hour)))
        if not pmf:
            return None
        r = round(run_ext)
        p = 0.0
        for k_str, pk in pmf.items():
            final = r + int(k_str) if kind == "high" else r - int(k_str)
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

        today = datetime.now(timezone.utc).date()
        rows = []
        for e in events:
            # Gamma keeps some long-settled dailies flagged active — drop them
            if e["date"] and (today - e["date"]).days > 1:
                continue
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
        kind = e["kind"]
        st = self.metar.station(icao) if icao else None
        climo = self.climo.get(icao) or {}
        pmf_key = "pmf" if kind == "high" else "pmf_low"
        unit = e["buckets"][0]["unit"] if e["buckets"] else None
        # tradeable only when the climatology exists in the market's own unit
        tradeable = (e["source"] == "metar"
                     and pmf_key in climo
                     and climo.get("unit", "C") == unit
                     and all(b["unit"] == unit for b in e["buckets"]))
        # only today's local date is a NEAR-LOCK candidate
        is_today = bool(st and e["date"] and st["local_date"] == e["date"].isoformat())
        if not st and not e["date"]:
            return None
        # observed running extreme in the market's unit
        ext_field = ("max_f" if kind == "high" else "min_f") if unit == "F" else \
                    ("max_c" if kind == "high" else "min_c")
        ext = (st or {}).get(ext_field)

        buckets, best = [], None
        for b in e["buckets"]:
            p = None
            if tradeable and is_today and ext is not None:
                p = self.bucket_prob(icao, kind, int(st["local_date"][5:7]),
                                     st["local_hour"], ext, b["lo"], b["hi"])
            ask_c = b["ask"] * 100 if b["ask"] is not None else None
            bid_c = b["bid"] * 100 if b["bid"] is not None else None
            # edge is NET of the taker fee we'd pay to enter at the ask
            fee_c = taker_fee_c(ask_c)
            edge_c = (p * 100 - ask_c - fee_c) if (p is not None and ask_c is not None) else None
            bv = {
                "label": self._label(b),
                "lo": b["lo"], "hi": b["hi"], "unit": b["unit"],
                "bid_c": bid_c, "ask_c": ask_c, "fee_c": round(fee_c, 2),
                "p": round(p, 4) if p is not None else None,
                "edge_c": round(edge_c, 1) if edge_c is not None else None,
                "condition_id": b["condition_id"],
                "token_yes": b["token_yes"],
                "min_size": b["min_size"],
            }
            buckets.append(bv)
            # keep a REFERENCE (not a copy) so a book re-price updates the row
            # the dashboard renders, not just the order we send
            if p is not None and (best is None or p > best["p"]):
                best = bv

        signal, why = self._gate(e, st, is_today, tradeable, best, ext)
        # Gamma got it this far; only the real ladder decides money.
        if signal == "ENTER" and best is not None and BOOK_CONFIRM:
            signal, why = self._book_confirm(best)
        temp_now = (st or {}).get("temp_f" if unit == "F" else "temp_c") if st else None
        return {
            "city": e["city"], "kind": kind, "unit": unit,
            "date": e["date"].isoformat() if e["date"] else None,
            "station": icao, "source": e["source"], "slug": e["slug"],
            "tradeable": tradeable, "is_today": is_today,
            "local_hour": round(st["local_hour"], 2) if st else None,
            "temp_c": temp_now,          # value shown "now" in the market's unit
            "ext_c": ext,                # observed extreme in the market's unit
            "obs_today": st["obs_today"] if st else 0,
            "buckets": buckets,
            "best_p": round(best["p"], 4) if best else None,
            "best_label": best["label"] if best else None,
            "signal": signal, "why": why,
            "entry": ({\
                "condition_id": best["condition_id"], "token_yes": best["token_yes"],
                "label": best["label"], "ask_c": best["ask_c"], "bid_c": best["bid_c"],
                "p": round(best["p"], 4), "edge_c": best["edge_c"],
                "min_size": best["min_size"], "city": e["city"], "kind": kind,
                "unit": unit,
                # book-confirmed: the size we verified depth for, and what the
                # ladder really costs vs what Gamma advertised
                "shares_planned": best.get("shares_planned"),
                "book_depth": best.get("book_depth"),
                "gamma_ask_c": best.get("gamma_ask_c"),
                "limit_c": best.get("limit_c"),   # FOK limit (worst level touched)
                "date": e["date"].isoformat(), "station": icao, "slug": e["slug"],
                "neg_risk": e["neg_risk"],
            } if signal == "ENTER" and best else None),
        }

    def _book_confirm(self, best):
        """Re-price a would-be signal on the REAL CLOB ask ladder.

        Gamma's bestAsk is a screening field and can be pure fiction: one live
        FOK died because Gamma advertised 72c while the real book started at
        82c (nothing at all at 72c). We buy with a FOK that walks the ladder,
        so the honest entry price is the VWAP for the size we'd actually take,
        and the honest gate is whether that size is even there.

        Mutates `best` in place with real ask/fee/edge/depth. Returns (signal, why).
        """
        asks = fetch_book_asks(best["token_yes"])
        if asks is None:
            return "NO-BOOK", "book unavailable — not pricing on Gamma"
        if not asks:
            return "NO-BOOK", "empty book"
        gamma_ask = best["ask_c"]
        shares = max(best.get("min_size") or 5, round(STAKE_USD / (asks[0][0] / 100.0)))
        vwap, got, marginal = vwap_for_size(asks, shares)
        if vwap is None or got + 1e-9 < shares:
            return "NO-DEPTH", f"only {got:.0f}/{shares} shares on the book"
        fee = taker_fee_c(vwap)
        best["ask_c"] = round(vwap, 2)          # what we'd PAY (cost/edge basis)
        best["limit_c"] = math.ceil(marginal)   # FOK limit must clear the WORST level
        best["fee_c"] = round(fee, 2)
        best["edge_c"] = round(best["p"] * 100 - vwap - fee, 1)
        best["book_depth"] = round(got, 2)
        best["shares_planned"] = shares
        best["gamma_ask_c"] = gamma_ask          # keep for drift visibility
        # re-apply the money gates at the price we'd REALLY pay
        if best["ask_c"] < PRICE_MIN_C:
            return "MKT-LOCKED", f"real ask {best['ask_c']:.0f}c < {PRICE_MIN_C:.0f}c"
        if best["ask_c"] > PRICE_MAX_C:
            return "PRICED", f"real ask {best['ask_c']:.0f}c > {PRICE_MAX_C:.0f}c (gamma said {gamma_ask:.0f}c)"
        if best["edge_c"] < MIN_EDGE_C:
            return "THIN-EDGE", f"real edge {best['edge_c']}c < {MIN_EDGE_C}c (gamma implied more)"
        drift = abs(vwap - gamma_ask)
        return "ENTER", (f"p {best['p']:.2f} @ real {best['ask_c']:.0f}c ×{shares}"
                         + (f" (gamma {gamma_ask:.0f}c, drift {drift:.0f}c)" if drift >= 1 else ""))

    def _gate(self, e, st, is_today, tradeable, best, ext):
        if not tradeable:
            climo = self.climo.get(e["station"]) or {}
            unit = e["buckets"][0]["unit"] if e["buckets"] else "?"
            return "MONITOR", ("HKO source" if e["source"] == "hko" else
                               "no climatology" if not climo
                               else f"no {unit}° climatology" if climo.get("unit", "C") != unit
                               else "no low climatology" if e["kind"] == "low"
                               and "pmf_low" not in climo
                               else "unknown source")
        if not is_today:
            return "WAIT", "not station-local today"
        if st is None or ext is None:
            return "NO-DATA", "no observations"
        if st["obs_today"] < MIN_OBS_TODAY:
            return "NO-DATA", f"only {st['obs_today']} obs today"
        min_hour = MIN_LOCAL_HOUR if e["kind"] == "high" else MIN_LOCAL_HOUR_LOW
        if st["local_hour"] < min_hour:
            return "EARLY", f"local {st['local_hour']:.1f}h < {min_hour}h"
        if best is None:
            return "NO-DATA", "no model probability"
        if best["p"] < P_MIN:
            return "NO-LOCK", f"best p {best['p']:.2f} < {P_MIN}"
        if best["ask_c"] is None or best["bid_c"] is None:
            return "NO-BOOK", "missing quote"
        if best["ask_c"] < PRICE_MIN_C:
            return "MKT-LOCKED", f"ask {best['ask_c']:.0f}c < {PRICE_MIN_C:.0f}c — market resolved against us"
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
            "climo_f": sum(1 for v in self.climo.values() if v.get("unit") == "F"),
            "config": {"p_min": P_MIN, "price_max_c": PRICE_MAX_C,
                       "price_min_c": PRICE_MIN_C,
                       "min_edge_c": MIN_EDGE_C, "min_local_hour": MIN_LOCAL_HOUR,
                       "min_local_hour_low": MIN_LOCAL_HOUR_LOW},
        }
