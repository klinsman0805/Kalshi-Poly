#!/usr/bin/env python3
"""
scripts/scan_poly_rewards.py — rank Polymarket weather markets by LP-reward
yield per dollar of resting capital.

Read-only: pulls active reward configs + order books, estimates what a
minimum-qualifying-size two-sided quote would earn per day, ranks by yield.
Places no orders. See feeds/poly_rewards.py for the scoring approximation
and its caveats.

Run:  python scripts/scan_poly_rewards.py [--min-rate 5] [--top 20] [--all-weather]
      --all-weather includes precipitation/snowfall markets, not just
      temperature buckets (default: temperature only, matching the rest of
      this repo's weather scope).
"""

import argparse
import sys

from feeds.poly_rewards import scan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rate", type=float, default=5.0,
                     help="skip markets paying less than this $/day (default 5)")
    ap.add_argument("--top", type=int, default=25, help="rows to show (default 25)")
    ap.add_argument("--all-weather", action="store_true",
                     help="include non-temperature weather markets (precip, snowfall, etc)")
    args = ap.parse_args()

    qf = None if args.all_weather else (lambda q: "temperature in" in q.lower())
    results = scan(tag_slug="weather", min_rate=args.min_rate, question_filter=qf)

    if not results:
        print("no reward markets found above the min-rate threshold")
        return

    print(f"═══ Polymarket weather LP rewards — {len(results)} markets scored ═══\n")
    print(f"  {'yield/day':>10s} {'est $/day':>10s} {'pool $/day':>11s} {'mid':>6s} "
          f"{'2-sided':>8s} {'question':s}")
    for r in results[:args.top]:
        print(f"  {r['yield_per_dollar_per_day']*100:9.1f}% "
              f"{r['est_daily_usd']:10.2f} {r['rate_per_day']:11.2f} "
              f"{r['mid_c']:5.1f}c {str(r['two_sided_required']):>8s}  {r['question'][:60]}")

    total_daily = sum(r["est_daily_usd"] for r in results[:args.top])
    total_capital = sum(r["capital_usd"] for r in results[:args.top])
    print(f"\nTop {min(args.top, len(results))}: ${total_daily:.2f}/day estimated on "
          f"${total_capital:.2f} of resting capital "
          f"({total_daily/total_capital*100:.1f}%/day blended, if capital isn't shared or reused).")
    print("\nCaveats: point-in-time book snapshot, not time-averaged over the reward\n"
          "epoch; assumes YES book mirrors NO book; ignores acquisition cost of the\n"
          "ask-side token; does not model fill/adverse-selection risk if the market\n"
          "moves. Read feeds/poly_rewards.py:score_market before sizing real capital.")


if __name__ == "__main__":
    sys.exit(main())
