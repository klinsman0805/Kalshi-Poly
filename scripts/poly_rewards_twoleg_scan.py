#!/usr/bin/env python3
"""
scripts/poly_rewards_twoleg_scan.py — finds TWO-LEGGED LP quoting plans and
prices their real downside.

Everything this project traded so far was a single BUY leg on a HIGH-priced
bucket (Buenos Aires 85c, NYC 81c, Phoenix 70c). That structure has the worst
possible payoff shape: a fill costs you the full entry price if the bucket
loses, and the LP reward is a fraction of a cent against it.

Two facts change the shape completely.

1. YES + NO always redeem to exactly $1.00. So resting a BID on YES and a BID
   on NO is not two directional bets — if BOTH fill you hold a complete set,
   which is worth $1.00 no matter how the weather turns out. Buying both for
   less than $1.00 total is a risk-free profit, and it needs no inventory and
   no short: two buy orders.

2. The scoring formula counts `bids on m` and `bids on m'` on OPPOSITE sides
   (Q_one vs Q_two), so those same two buy orders make us two-sided for
   rewards — worth up to 3x the single-sided rate, and the ONLY way to score
   at all when the midpoint sits outside [0.10, 0.90], which is exactly where
   every near-decided weather bucket lives.

The asymmetry that matters, and the reason to prefer LOW-priced buckets:
on a bucket trading near 4c, the leg that can lose (the YES bid) risks ~4c a
share, while the leg that wins (the NO bid near 94c) makes ~6c. On a bucket
trading near 85c those roles invert and the losing leg costs 85c a share —
which is precisely the loss we kept taking.

Outputs, per market, the concrete plan: what to bid on each side, what both
legs filling nets, and what the worst single-sided fill costs.

Run: python scripts/poly_rewards_twoleg_scan.py [max_yes_price_c]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feeds.poly_rewards import fetch_reward_markets, _fetch_book  # noqa: E402

MAX_YES_C = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
# Quote this far inside the reward band's edge rather than at the midpoint.
# Scoring is (v-s)/v squared, so sitting at the midpoint maximises reward and
# also maximises the chance of being lifted; sitting at the band edge scores
# ~0 but is nearly untouchable. This is the tunable that trades reward rate
# against fill risk, and it is the single most important knob in the strategy.
BAND_FRACTION = 0.6


def plan_for(market):
    tokens = market.get("tokens") or []
    yes = next((t for t in tokens if (t.get("outcome") or "").lower() == "yes"), None)
    no = next((t for t in tokens if (t.get("outcome") or "").lower() == "no"), None)
    if not yes or not no:
        return None
    try:
        y_bids, y_asks = _fetch_book(yes["token_id"])
        n_bids, n_asks = _fetch_book(no["token_id"])
    except Exception:  # noqa: BLE001
        return None
    if not y_bids or not y_asks or not n_bids or not n_asks:
        return None

    v = market["max_spread_c"]
    size = market["min_size"]
    y_mid = (y_bids[0][0] + y_asks[0][0]) / 2.0
    n_mid = (n_bids[0][0] + n_asks[0][0]) / 2.0
    if y_mid > MAX_YES_C:
        return None

    # Bid below each midpoint, inside the reward band. Round DOWN to the tick
    # so we never accidentally sit above where we intended.
    y_bid = int(y_mid - v * BAND_FRACTION)
    n_bid = int(n_mid - v * BAND_FRACTION)
    if y_bid < 1 or n_bid < 1:
        return None

    total = y_bid + n_bid
    both_fill = (100.0 - total) / 100.0 * size      # complete set redeems at $1
    # Worst realistic single-sided outcome: only the YES leg fills and the
    # bucket settles NO, so the shares are worthless.
    only_yes = -(y_bid / 100.0) * size
    only_no = (100.0 - n_bid) / 100.0 * size        # NO leg alone, bucket settles NO
    capital = total / 100.0 * size

    return {
        "question": market.get("question"), "rate": market["rate_per_day"],
        "y_mid": y_mid, "n_mid": n_mid, "y_bid": y_bid, "n_bid": n_bid,
        "size": size, "total_c": total, "capital": capital,
        "both_fill": both_fill, "only_yes": only_yes, "only_no": only_no,
        "y_best_bid": y_bids[0][0], "n_best_bid": n_bids[0][0],
    }


def main():
    markets = fetch_reward_markets(tag_slug="weather", min_rate=5.0)
    markets = [m for m in markets if "temperature in" in (m.get("question") or "").lower()]
    print(f"scanning {len(markets)} rewarded temperature markets for YES mid <= {MAX_YES_C}c "
          f"(quoting at {BAND_FRACTION:.0%} of band)\n")

    plans = []
    for m in markets:
        p = plan_for(m)
        if p:
            plans.append(p)
    plans.sort(key=lambda p: -p["both_fill"])

    print(f"{'market':<40}{'pool':>6}{'Ybid':>6}{'Nbid':>6}{'sum':>6}"
          f"{'capital':>9}{'BOTH':>8}{'onlyY':>8}{'onlyN':>8}")
    for p in plans[:20]:
        print(f"{(p['question'] or '')[:40]:<40}{p['rate']:>6.0f}{p['y_bid']:>6.0f}"
              f"{p['n_bid']:>6.0f}{p['total_c']:>6.0f}{p['capital']:>9.2f}"
              f"{p['both_fill']:>+8.2f}{p['only_yes']:>+8.2f}{p['only_no']:>+8.2f}")

    if plans:
        viable = [p for p in plans if p["both_fill"] > 0]
        print(f"\n{len(viable)}/{len(plans)} have a positive complete-set edge "
              f"(both legs filling is profitable regardless of the weather)")
        worst = min(p["only_yes"] for p in plans)
        print(f"worst single-sided downside across all plans: ${worst:.2f} per position")


if __name__ == "__main__":
    main()
