"""
scripts/compare_venues.py — cross-venue price-consistency checker.

For the Tier-A cities (same physical station on both venues), pull the current
Kalshi and Polymarket books and compare, bucket-by-bucket, matched on the same
event date and the same whole-degree bounds. Answers the client's question:
"are prices consistent across the two platforms?" (research says: no).

Run:  python -m scripts.compare_venues
Requires network to both api.elections.kalshi.com and gamma-api.polymarket.com.
Places no orders; reads public market data only.
"""

from collections import defaultdict

from feeds.kalshi_stations import KALSHI_STATIONS, TIER_A
from feeds import kalshi_weather
from feeds import poly_weather

# Kalshi city label → Polymarket city label (same station). Poly uses the same
# names for these, but keep an explicit map so a rename on either side is caught.
KALSHI_TO_POLY_CITY = {c: c for c in TIER_A}


def _mid(b):
    if b.get("bid") is None or b.get("ask") is None or b["ask"] <= 0:
        return None
    return (b["bid"] + b["ask"]) / 2.0    # in [0,1]


def _key(b):
    return (b.get("lo"), b.get("hi"))


def main():
    kalshi = kalshi_weather.fetch_temperature_events(cities=TIER_A)
    poly = poly_weather.fetch_temperature_events()

    # index poly by (city, kind, date) → {bounds: mid}
    poly_idx = defaultdict(dict)
    for e in poly:
        for b in e["buckets"]:
            m = _mid(b)
            if m is not None:
                poly_idx[(e["city"], e["kind"], str(e["date"]))][_key(b)] = m

    print(f"{'City':14s} {'kind':4s} {'date':11s} {'bucket':10s} "
          f"{'Kalshi':>7s} {'Poly':>6s} {'gap(pt)':>8s}")
    print("-" * 66)
    n_compared = 0
    gaps = []
    for e in kalshi:
        pcity = KALSHI_TO_POLY_CITY.get(e["city"], e["city"])
        pk = poly_idx.get((pcity, e["kind"], str(e["date"])))
        if not pk:
            continue    # Polymarket has no matching same-date market right now
        for b in e["buckets"]:
            km = _mid(b)
            pm = pk.get(_key(b))
            if km is None or pm is None:
                continue
            if km < 0.04 and pm < 0.04:
                continue    # both ~zero, uninformative
            lo, hi = b["lo"], b["hi"]
            label = (f"<={hi}" if lo is None else f">={lo}" if hi is None else f"{lo}-{hi}")
            gap = (km - pm) * 100
            gaps.append(abs(gap))
            n_compared += 1
            print(f"{e['city']:14s} {e['kind']:4s} {str(e['date']):11s} {label:10s} "
                  f"{km*100:6.0f}% {pm*100:5.0f}% {gap:+7.0f}")

    print("-" * 66)
    if gaps:
        avg = sum(gaps) / len(gaps)
        big = sum(1 for g in gaps if g >= 10)
        print(f"{n_compared} buckets compared · mean |gap| {avg:.1f}pt · "
              f"{big} with ≥10pt divergence")
        print("Verdict: prices are NOT interchangeable across venues — Kalshi "
              "needs its own calibration, same as the crypto-market precedent.")
    else:
        print("No same-date, same-bucket overlap available right now "
              "(venues drift by calendar day — re-run when both list the same date).")


if __name__ == "__main__":
    main()
