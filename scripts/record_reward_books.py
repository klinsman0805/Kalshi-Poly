#!/usr/bin/env python3
"""
scripts/record_reward_books.py — record Polymarket reward-market books.

Measurement only. Places no orders, and runs as its own short-lived process
rather than inside anything that trades.

The question it exists to answer: what does a dollar of resting capital
actually earn in a reward market, given who is already quoting it? The existing
scanner estimates that, and its own docstring records the estimate being 20.5x
too high against a real payment. The causes that remain are sampling problems —
rewards are scored across a 10,080-sample epoch and a snapshot cannot see how
long a competitor's order has rested — so the fix is a time series, not a
better formula.

Usage:
    ./venv/bin/python scripts/record_reward_books.py
    ./venv/bin/python scripts/record_reward_books.py --min-rate 100 --verbose
    ./venv/bin/python scripts/record_reward_books.py --survey     # no writes

Intended for cron every 15 minutes. Per-market throttle and the round-robin
cursor persist in candidate_data/reward_book_state.json, so successive runs
walk the universe instead of re-reading its head.
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")

from modules import reward_book_log as RBL                        # noqa: E402
from modules.reward_book_log import LOG_PATH, RewardBookLogger    # noqa: E402


def survey(min_rate):
    """What is on offer, without writing anything."""
    from feeds.poly_rewards import fetch_reward_markets
    allm = fetch_reward_markets(tag_slug=None, min_rate=0.0, use_cache=False)
    tgt = [m for m in allm if (m.get("rate_per_day") or 0) >= min_rate]
    total = sum(m.get("rate_per_day") or 0 for m in allm)
    tsum = sum(m.get("rate_per_day") or 0 for m in tgt)
    print(f"reward universe : {len(allm):,} markets, ${total:,.0f}/day")
    print(f"at >= ${min_rate:g}/day : {len(tgt):,} markets, ${tsum:,.0f}/day "
          f"({100*tsum/total if total else 0:.0f}% of the pot)")
    print(f"\n{'pool $/day':>10}  {'band':>5}  {'minsz':>6}  question")
    for m in sorted(tgt, key=lambda x: -(x.get("rate_per_day") or 0))[:20]:
        print(f"{m.get('rate_per_day') or 0:10.0f}  {m.get('max_spread_c'):5}  "
              f"{m.get('min_size'):6}  {(m.get('question') or '')[:58]}")
    return tgt


def summarise(path):
    """What the slice just written actually says about competition."""
    import json
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    rows = rows[-200:]
    if not rows:
        return
    priced = [r for r in rows if r.get("yield_per_dollar_per_day") is not None]
    print(f"        {len(rows)} rows, {len(priced)} with a yield estimate")
    if priced:
        ys = sorted(r["yield_per_dollar_per_day"] for r in priced)
        print(f"        est yield/$/day: p50 {ys[len(ys)//2]:.5f}  "
              f"p90 {ys[int(.9*len(ys))]:.5f}  max {ys[-1]:.5f}")
        for flag in ("no_competition_detected", "below_min_payout"):
            n = sum(1 for r in priced if r.get(flag))
            print(f"        {flag}: {n} of {len(priced)}")
    q = sorted(r.get("qual_total") or 0 for r in rows)
    print(f"        qualifying shares already resting: p50 {q[len(q)//2]:,.0f}  "
          f"p90 {q[int(.9*len(q))]:,.0f}  max {q[-1]:,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rate", type=float, default=None,
                    help=f"ignore pools below this $/day (default {RBL.MIN_RATE:g})")
    ap.add_argument("--max-markets", type=int, default=None,
                    help=f"markets per cycle (default {RBL.MAX_MARKETS})")
    ap.add_argument("--survey", action="store_true", help="show the universe, write nothing")
    ap.add_argument("--verbose", action="store_true", help="summarise the slice captured")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR if args.quiet else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.min_rate is not None:
        RBL.MIN_RATE = args.min_rate
    if args.max_markets is not None:
        RBL.MAX_MARKETS = args.max_markets

    if args.survey:
        survey(RBL.MIN_RATE)
        return 0

    lg = RewardBookLogger(state_path=LOG_PATH.with_name("reward_book_state.json"))
    n = lg.snapshot()
    lg.close()
    st = lg.state()
    print(f"reward books  min_rate=${st['min_rate']:g}  rows={n:4d}  -> {st['path']}")
    if st["last_error"]:
        print(f"        last_error: {st['last_error']}")
    if args.verbose and n:
        summarise(st["path"])
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
