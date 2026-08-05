"""
feeds/kalshi_weather.py — Kalshi daily-temperature market discovery.

Read-only. Fetches open high/low temperature markets for the mapped cities and
normalizes each strike ladder into the SAME event/bucket shape that
feeds/poly_weather.fetch_temperature_events() returns, so the existing
WeatherEngine can price it with no model changes. Places no orders.

Two deliberate differences from the Polymarket feed, both surfaced by research:
  • bucket boundaries are parsed from each Kalshi market's own subtitle, never
    assumed to match Polymarket's (Austin's ladder is offset one degree);
  • events carry venue="kalshi" and the ticker as the identifier (Kalshi has no
    condition_id/token); the executor branches on venue for order placement.

Prices: Kalshi yes_bid/yes_ask are dollars in [0,1], same convention as
Polymarket's bestBid/bestAsk, so downstream cents math is unchanged.
"""

import re
import logging
import requests

from feeds.kalshi_stations import KALSHI_STATIONS, UNIT

log = logging.getLogger("feeds.kalshi_weather")

PROD_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TIMEOUT = 15

# Kalshi weather taker fee (README): 0.07 * P * (1-P) per contract, in cents,
# rounded UP to the nearest cent on the full order by the caller. Max 1.75c at
# P=0.50. Distinct from Polymarket's flat-rate fee — Kalshi weather sizing must
# use THIS, not feeds/poly_weather.taker_fee_c.
KALSHI_TAKER_RATE = 0.07


def taker_fee_c(price_c):
    """Per-contract Kalshi taker fee in cents for a fill at price_c (0-100)."""
    if price_c is None:
        return 0.0
    p = max(0.0, min(1.0, price_c / 100.0))
    return KALSHI_TAKER_RATE * p * (1.0 - p) * 100.0


# subtitle forms: "89° or above" | "80° or below" | "87° to 88°"
_ABOVE = re.compile(r"(-?\d+)\s*°?\s*or\s*above", re.I)
_BELOW = re.compile(r"(-?\d+)\s*°?\s*or\s*below", re.I)
_RANGE = re.compile(r"(-?\d+)\s*°?\s*(?:to|-|–)\s*(-?\d+)\s*°?", re.I)


def _parse_bucket_bounds(subtitle: str):
    """(lo, hi) inclusive whole-degree bounds, or (None, None) if unparseable.
    lo=None => open below (≤hi); hi=None => open above (≥lo)."""
    s = subtitle or ""
    m = _ABOVE.search(s)
    if m:
        return int(m.group(1)), None
    m = _BELOW.search(s)
    if m:
        return None, int(m.group(1))
    m = _RANGE.search(s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo, hi) if lo <= hi else (hi, lo)
    return None, None


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _event_date(event_ticker: str):
    """KXHIGHMIA-26JUL24 → date(2026,7,24). None if it doesn't parse."""
    from datetime import datetime
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker or "")
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                 "%y %b %d").date()
    except ValueError:
        return None


def _fetch_series_markets(series_ticker: str):
    r = requests.get(f"{PROD_API_BASE}/markets",
                     params={"series_ticker": series_ticker, "status": "open", "limit": 400},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("markets", [])


def fetch_temperature_events(cities=None):
    """Return Kalshi daily-temperature events in poly_weather's event shape.

    One event per (city, kind, event_date). `cities` optionally restricts to a
    subset of KALSHI_STATIONS keys (e.g. Tier-A only for the first paper test).
    """
    out = []
    targets = cities or list(KALSHI_STATIONS.keys())
    for city in targets:
        m = KALSHI_STATIONS.get(city)
        if not m:
            continue
        for kind, series in (("high", m["series_high"]), ("low", m["series_low"])):
            try:
                markets = _fetch_series_markets(series)
            except Exception as e:  # noqa: BLE001
                log.debug("Kalshi %s fetch failed: %s", series, e)
                continue
            # one series can have several open event dates; group by event_ticker
            by_event = {}
            for mk in markets:
                by_event.setdefault(mk.get("event_ticker"), []).append(mk)
            for ev, mks in by_event.items():
                buckets = []
                for mk in mks:
                    lo, hi = _parse_bucket_bounds(mk.get("subtitle") or mk.get("yes_sub_title", ""))
                    if lo is None and hi is None:
                        continue
                    yb = _fnum(mk.get("yes_bid_dollars"))
                    ya = _fnum(mk.get("yes_ask_dollars"))
                    buckets.append({
                        "lo": lo, "hi": hi, "unit": UNIT,
                        "question": mk.get("title"),
                        # Kalshi identifier: the market ticker (no condition_id/token)
                        "condition_id": mk.get("ticker"),
                        "token_yes": mk.get("ticker"),
                        "ticker": mk.get("ticker"),
                        "bid": yb, "ask": ya,
                        "spread": (ya - yb) if (yb is not None and ya is not None) else None,
                        "min_size": 1.0,   # Kalshi trades in whole contracts, min 1
                        "closed": mk.get("status") not in ("active", "open"),
                        "resolved": bool(mk.get("result")),
                        "outcome_yes": None,
                        "volume24h": _fnum(mk.get("volume_24h_fp")) or 0.0,
                    })
                if not buckets:
                    continue
                buckets.sort(key=lambda b: (b["lo"] if b["lo"] is not None else -999))
                out.append({
                    "venue": "kalshi",
                    "kind": kind,
                    "city": city,
                    "date": _event_date(ev),
                    "title": f"{city} {kind} temperature",
                    "slug": ev,               # event ticker doubles as the slug
                    "source": "metar",        # observable → tradeable by the engine
                    "station": m["icao"],
                    "neg_risk": False,        # N/A on Kalshi
                    "buckets": buckets,
                })
    return out


if __name__ == "__main__":
    # smoke test: print today's Tier-A ladders
    from feeds.kalshi_stations import TIER_A
    for e in fetch_temperature_events(cities=TIER_A):
        print(f"\n{e['city']:14s} {e['kind']:4s} {e['date']}  ({e['station']})")
        for b in e["buckets"]:
            label = (f"≤{b['hi']}" if b["lo"] is None else
                     f"≥{b['lo']}" if b["hi"] is None else f"{b['lo']}-{b['hi']}")
            bid = f"{b['bid']*100:.0f}" if b["bid"] is not None else "-"
            ask = f"{b['ask']*100:.0f}" if b["ask"] is not None else "-"
            print(f"   {label:8s}°F  bid={bid:>4s} ask={ask:>4s}  {b['ticker']}")
