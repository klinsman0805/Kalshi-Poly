"""
feeds/poly_weather.py — discovery of Polymarket daily temperature markets.

Pulls the weather-tagged events from the Gamma API, keeps the daily
"Highest temperature in <city> on <date>?" families, and parses each into a
structured event: city, market date, settlement station + source type, and
the full bucket ladder with quotes and resolution state.

Source types (parsed from the market description):
  metar — Wunderground station history or NOAA obs page; both mirror the
          airport METAR feed, so feeds/metar.py tracks the settlement value.
  hko   — Hong Kong Observatory (downtown station, 0.1 °C precision). NOT
          the airport METAR; monitor-only until we ingest HKO open data.
  ?     — unrecognised source; monitor-only.

Quotes come from Gamma's bestBid/bestAsk. Good enough for signals and paper
fills; a live executor should confirm against the CLOB book before ordering.
"""

import json
import logging
import re
from datetime import datetime

import requests

log = logging.getLogger("feeds.poly_weather")

GAMMA = "https://gamma-api.polymarket.com/events"
TIMEOUT = 15
PAGE = 100
MAX_PAGES = 4

_TITLE = re.compile(r"^Highest temperature in (.+?) on (.+?)\?$")
_SLUG_DATE = re.compile(r"-(\w+)-(\d{1,2})-(\d{4})$")
_WUND = re.compile(r"wunderground\.com/history/daily/(\S+)")
_NOAA = re.compile(r"weather\.gov/wrh/timeseries\?site=([A-Za-z0-9]{3,5})")
_HKO = re.compile(r"weather\.gov\.hk")
_B_BELOW = re.compile(r"be (-?\d+)°([CF]) or below")
_B_ABOVE = re.compile(r"be (-?\d+)°([CF]) or above")
_B_RANGE = re.compile(r"be (-?\d+)-(-?\d+)°([CF])")
_B_EXACT = re.compile(r"be (-?\d+)°([CF])")


def _jload(s, default=None):
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def _parse_bucket(question):
    """Return (lo, hi, unit) where lo/hi are inclusive whole degrees, None = open tail."""
    m = _B_BELOW.search(question)
    if m:
        return None, int(m.group(1)), m.group(2)
    m = _B_ABOVE.search(question)
    if m:
        return int(m.group(1)), None, m.group(2)
    m = _B_RANGE.search(question)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3)
    m = _B_EXACT.search(question)
    if m:
        v = int(m.group(1))
        return v, v, m.group(2)
    return None, None, None


def _parse_source(desc):
    m = _WUND.search(desc or "")
    if m:
        # station id is the last URL path segment (US urls have an extra
        # state/city segment: us/ny/new-york-city/KLGA)
        station = m.group(1).rstrip(".,)").split("/")[-1]
        if re.fullmatch(r"[A-Za-z0-9]{3,4}", station):
            return "metar", station.upper()
        return "?", None
    m = _NOAA.search(desc or "")
    if m:
        return "metar", m.group(1).upper()
    if _HKO.search(desc or ""):
        return "hko", "HKO"
    return "?", None


def _parse_date(slug):
    m = _SLUG_DATE.search(slug or "")
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
    except ValueError:
        return None


def fetch_temperature_events():
    """Return a list of parsed highest-temperature events (all cities, all dates)."""
    events, offset = [], 0
    for _ in range(MAX_PAGES):
        r = requests.get(GAMMA, params={"limit": PAGE, "offset": offset, "active": "true",
                                        "closed": "false", "tag_slug": "weather"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json()
        events.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    out = []
    for e in events:
        t = _TITLE.match(e.get("title", "") or "")
        if not t:
            continue
        markets = e.get("markets") or []
        if not markets:
            continue
        source, station = _parse_source(markets[0].get("description"))
        buckets = []
        for m in markets:
            lo, hi, unit = _parse_bucket(m.get("question", "") or "")
            if unit is None:
                continue
            tokens = _jload(m.get("clobTokenIds"), []) or []
            prices = _jload(m.get("outcomePrices"), None)
            buckets.append({
                "lo": lo, "hi": hi, "unit": unit,
                "question": m.get("question"),
                "condition_id": m.get("conditionId"),
                "token_yes": tokens[0] if tokens else None,
                "bid": float(m["bestBid"]) if m.get("bestBid") is not None else None,
                "ask": float(m["bestAsk"]) if m.get("bestAsk") is not None else None,
                "spread": float(m["spread"]) if m.get("spread") is not None else None,
                "min_size": float(m.get("orderMinSize") or 5),
                "closed": bool(m.get("closed")),
                "resolved": (m.get("umaResolutionStatus") == "resolved"),
                # outcomePrices is only meaningful once resolved — it shows a
                # mid-like value on open markets, so never settle from it alone
                "outcome_yes": (float(prices[0]) if prices else None),
                "volume24h": float(m.get("volume24hr") or 0),
            })
        if not buckets:
            continue
        # sort ladder: below-tail, exact buckets ascending, above-tail
        buckets.sort(key=lambda b: (b["lo"] if b["lo"] is not None else -999))
        out.append({
            "city": t.group(1),
            "date": _parse_date(e.get("slug")),
            "title": e.get("title"),
            "slug": e.get("slug"),
            "source": source,
            "station": station,
            "neg_risk": bool(e.get("negRisk")),
            "buckets": buckets,
        })
    return out
