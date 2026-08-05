"""
feeds/metar_conditions.py — weather-regime signals from the METAR we already poll.

The NEAR-LOCK model reads one number off each observation (the temperature) and
is blind to WHY that number is doing what it does. A flat reading can mean the
diurnal peak has genuinely passed (a real lock) or that something is temporarily
holding the temperature down — and those look identical in a pure temperature
series. Every high-side loss so far was the second kind:

  • Amsterdam 2026-07-26  5h of -DZRA under a 1600ft ceiling, dewpoint spread
                          2C (saturated, evaporative cooling). Rain stopped and
                          the ob flagged BECMG; 20->22C over the next 2h.
  • Chicago   2026-07-25  BKN022/BKN026 decks all afternoon shading the sensor;
                          one gap in the cloud and 77->80F.
  • Mexico City 2026-07-25 DSNT CB S SE, winds swinging 360/260/020, gusts 22kt,
                          24C dewpoint spread — an outflow-modulated plateau,
                          not a settled one.

All three were already described, in full, inside the raw observations we
download every cycle and throw away. This module extracts them.

SHADOW ONLY: nothing here gates a trade. `score` is recorded alongside each
entry so the effect can be measured against real outcomes — winners included —
before any of it is allowed to influence a decision. That discipline exists
because the solar-elevation idea looked equally obvious and turned out to block
10 winners (+$19.02) to dodge a single -$7.68 loser.

aviationweather.gov decodes most of this for us (`wxString`, `clouds` with base
in feet, `wgst`, `dewp`, `fltCat`); only cloud TYPE (CB/TCU) and the trend group
need a look at `rawOb`. Every function here is defensive — a parse failure must
degrade to None, never raise into the trading loop.
"""

import logging
import re

log = logging.getLogger("feeds.metar_conditions")

# precipitation reaching the ground — the codes that actually suppress heating
_PRECIP = ("DZ", "RA", "SN", "SG", "PL", "GR", "GS", "UP", "IC")
# convective cloud types / phenomena: outflow-driven temperature swings
_CB_TCU = re.compile(r"\b(?:FEW|SCT|BKN|OVC)\d{3}(CB|TCU)\b|\b(CB|TCU)\b")
_TREND = re.compile(r"\b(NOSIG|BECMG|TEMPO)\b")
# "recent precip" window — long enough that a shower that just ended still
# counts (the air and ground are still cool), short enough to clear within a
# normal afternoon.
RECENT_PRECIP_MIN = 90.0


def _has_precip(wx):
    if not wx:
        return False
    return any(code in wx for code in _PRECIP)


def _ceiling_ft(clouds):
    """Lowest BKN/OVC layer in feet — the deck that actually shades the sensor.
    None means no ceiling (clear, or only FEW/SCT above)."""
    if not clouds:
        return None
    bases = []
    for c in clouds:
        try:
            if c.get("cover") in ("BKN", "OVC", "VV") and c.get("base") is not None:
                bases.append(float(c["base"]))
        except (TypeError, ValueError, AttributeError):
            continue
    return min(bases) if bases else None


def _cover_rank(clouds):
    """Densest layer reported: CLR < FEW < SCT < BKN < OVC."""
    order = {"CLR": 0, "SKC": 0, "CAVOK": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}
    best = 0
    for c in (clouds or []):
        try:
            best = max(best, order.get(c.get("cover"), 0))
        except AttributeError:
            continue
    return best


def _obs_ts(o, obs_time_fn):
    try:
        return obs_time_fn(o)
    except Exception:  # noqa: BLE001
        return None


def parse_conditions(rows, obs_time_fn, now_utc):
    """Condition signals for ONE station.

    rows: that station's raw API observations (any order)
    obs_time_fn: callable(ob) -> tz-aware datetime or None (MetarFeed._obs_time)
    now_utc: current time, tz-aware

    Returns a flat dict of signals, or None if there's nothing usable. Never
    raises.
    """
    try:
        obs = [(_obs_ts(o, obs_time_fn), o) for o in (rows or [])]
        obs = sorted(((t, o) for t, o in obs if t is not None), key=lambda x: x[0])
        if not obs:
            return None
        latest_ts, latest = obs[-1]
        raw = latest.get("rawOb") or ""

        wx = latest.get("wxString")
        precip_now = _has_precip(wx)

        # how long since precipitation was last observed at this station
        precip_recent_min = None
        for ts, o in reversed(obs):
            if _has_precip(o.get("wxString")):
                precip_recent_min = round((now_utc - ts).total_seconds() / 60.0, 1)
                break

        clouds = latest.get("clouds")
        ceiling = _ceiling_ft(clouds)

        cb = _CB_TCU.search(raw)
        convective = bool(cb) or (wx is not None and "TS" in wx)

        trend_m = _TREND.search(raw)
        trend = trend_m.group(1) if trend_m else None

        temp_c, dewp_c = latest.get("temp"), latest.get("dewp")
        try:
            spread = round(float(temp_c) - float(dewp_c), 1) if (
                temp_c is not None and dewp_c is not None) else None
        except (TypeError, ValueError):
            spread = None

        # wind direction change since the previous ob — a large swing is the
        # signature of an outflow boundary or frontal passage, not a settled day
        wdir, wspd, wgst = latest.get("wdir"), latest.get("wspd"), latest.get("wgst")
        shift = None
        if len(obs) >= 2:
            prev = obs[-2][1]
            try:
                a, b = float(prev.get("wdir")), float(wdir)
                d = abs(a - b) % 360
                shift = round(min(d, 360 - d), 0)
            except (TypeError, ValueError):
                shift = None
        # "230V290" — the station itself reporting the wind as variable
        wind_variable = bool(re.search(r"\b\d{3}V\d{3}\b", raw))

        c = {
            "wx": wx,
            "precip_now": precip_now,
            "precip_recent_min": precip_recent_min,
            "ceiling_ft": ceiling,
            "cover": _cover_rank(clouds),
            "convective": convective,
            "trend": trend,
            "dewpoint_spread_c": spread,
            "wind_kt": wspd, "gust_kt": wgst,
            "wind_shift_deg": shift, "wind_variable": wind_variable,
            "flt_cat": latest.get("fltCat"),
            "obs_age_min": round((now_utc - latest_ts).total_seconds() / 60.0, 1),
        }
        c["score"] = _score(c)
        return c
    except Exception as e:  # noqa: BLE001
        log.debug("conditions parse failed: %s", e)
        return None


def _score(c):
    """Composite 0..1 confidence multiplier — 1.0 = clean, settled conditions.

    Deliberately a simple product of independent penalties: the individual
    flags above are the real record, and any weighting can be re-derived from
    them later. This exists so a human (and a log line) can read one number.
    """
    s = 1.0
    if c["precip_now"]:
        s *= 0.50                       # actively raining: temperature is capped
    elif c["precip_recent_min"] is not None and c["precip_recent_min"] <= RECENT_PRECIP_MIN:
        s *= 0.70                       # just stopped: the lid may be coming off
    ceil = c["ceiling_ft"]
    if ceil is not None:
        if ceil < 3000:
            s *= 0.60                   # low deck — the sensor is shaded
        elif ceil < 5000:
            s *= 0.80
    if c["convective"]:
        s *= 0.60                       # CB/TCU/TS: outflow-driven swings
    if c["trend"] in ("BECMG", "TEMPO"):
        s *= 0.80                       # the station is flagging a change
    if c["gust_kt"] or c["wind_variable"] or (c["wind_shift_deg"] or 0) >= 90:
        s *= 0.85                       # unsettled flow
    sp = c["dewpoint_spread_c"]
    if sp is not None and sp >= 15:
        s *= 0.85                       # very dry air swings fast and far
    return round(s, 3)
