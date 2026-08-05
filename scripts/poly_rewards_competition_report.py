#!/usr/bin/env python3
"""
scripts/poly_rewards_competition_report.py — tests the practitioner claim that
Beijing 05:00-08:00 (UTC 21:00-00:00) is the best window to farm Polymarket LP
rewards, using the snapshot history we already collected (no capital, no API).

The claim (x.com/094551YY pinned post, 2026-06-09): "每天北京时间5-8点爽吃LP奖励"
— roughly "Beijing 5-8am each day is the sweet spot for eating LP rewards."
The mechanism would have to be COMPETITION: reward is split pro-rata across
makers, so the same resting order earns more when fewer others are quoting.

We never persisted the competing-depth scores directly, but we persisted both
est_daily_usd and rate_per_day, and est_daily_usd = rate_per_day * our_share
by construction (feeds/poly_rewards.score_market). So:

    our_share = est_daily_usd / rate_per_day

is recoverable exactly, and our_share is precisely the quantity competition
drives down. Higher median our_share in a given hour == thinner competing
liquidity in that hour == the claim is real.

Caveats stated up front:
  - Snapshots keep only the top N markets per cycle (POLY_REWARDS_TOP_N), so
    this measures competition among the BEST-yielding markets, not all of them.
  - our_share inherits every approximation in score_market (single-book proxy,
    point-in-time rather than time-weighted, no cross-market normalization).
    It is being used here only as a RELATIVE measure across hours, where those
    biases should largely cancel, not as an absolute yield.
  - Weather markets are themselves diurnal, so hour-of-day effects could be
    confounded by which cities happen to be mid-day at a given UTC hour.

Run: python scripts/poly_rewards_competition_report.py
"""

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "poly_rewards_log.jsonl")

# The claimed window, in UTC. Beijing (UTC+8) 05:00-08:00 -> UTC 21:00-00:00.
CLAIM_UTC_HOURS = {21, 22, 23}


def main():
    if not LOG_PATH.exists():
        print(f"no log at {LOG_PATH}")
        return

    by_hour = defaultdict(list)      # utc hour -> [our_share, ...]
    pool_by_hour = defaultdict(list)  # utc hour -> [total_pool_usd_day, ...]
    n_by_hour = defaultdict(list)     # utc hour -> [n_scored, ...]
    shares_all = []
    snapshots = 0

    for line in open(LOG_PATH):
        line = line.strip()
        if not line:
            continue
        snap = json.loads(line)
        hour = datetime.fromisoformat(snap["ts"]).hour
        snapshots += 1
        pool_by_hour[hour].append(snap.get("total_pool_usd_day") or 0.0)
        n_by_hour[hour].append(snap.get("n_scored") or 0)
        for row in snap.get("top") or []:
            rate = row.get("rate_per_day") or 0.0
            est = row.get("est_daily_usd") or 0.0
            if rate <= 0:
                continue
            share = est / rate
            by_hour[hour].append(share)
            shares_all.append(share)

    if not shares_all:
        print("no usable rows")
        return

    print(f"snapshots: {snapshots}   scored rows: {len(shares_all)}")
    print(f"log spans: {LOG_PATH}")
    print()

    # ── sanity check on the estimator itself ────────────────────────────────
    shares_all.sort()
    print("=== our_share distribution (est_daily_usd / rate_per_day) ===")
    print("This is the fraction of a market's ENTIRE daily reward pool our model")
    print("claims a single min-size resting order would capture.")
    for q, name in ((0.50, "median"), (0.75, "p75"), (0.90, "p90"), (1.0, "max")):
        idx = min(len(shares_all) - 1, int(q * len(shares_all)))
        print(f"  {name:>6}: {shares_all[idx]:.3f}")
    over_half = sum(1 for s in shares_all if s > 0.5)
    print(f"  rows claiming >50% of the whole pool: {over_half} "
          f"({100.0*over_half/len(shares_all):.1f}%)")
    print()

    # ── the actual hour-of-day test ─────────────────────────────────────────
    print("=== median our_share by UTC hour (higher = less competition) ===")
    print(f"{'UTC':>4} {'Beijing':>8} {'n':>6} {'median':>8} {'mean':>8} "
          f"{'pool$/day':>11} {'mkts':>6}   claim")
    for hour in range(24):
        vals = by_hour.get(hour)
        if not vals:
            continue
        bj = (hour + 8) % 24
        mark = "  <-- claimed" if hour in CLAIM_UTC_HOURS else ""
        print(f"{hour:>4} {bj:>7}h {len(vals):>6} {statistics.median(vals):>8.3f} "
              f"{statistics.mean(vals):>8.3f} "
              f"{statistics.mean(pool_by_hour[hour]):>11.0f} "
              f"{statistics.mean(n_by_hour[hour]):>6.0f}{mark}")
    print()

    inside = [s for h, v in by_hour.items() if h in CLAIM_UTC_HOURS for s in v]
    outside = [s for h, v in by_hour.items() if h not in CLAIM_UTC_HOURS for s in v]
    if inside and outside:
        mi, mo = statistics.median(inside), statistics.median(outside)
        print("=== verdict on the Beijing 05:00-08:00 claim ===")
        print(f"  inside  window (UTC 21-23): n={len(inside):>5}  median our_share={mi:.3f}")
        print(f"  outside window            : n={len(outside):>5}  median our_share={mo:.3f}")
        if mo > 0:
            lift = (mi - mo) / mo * 100.0
            print(f"  relative difference: {lift:+.1f}%")
        # Mann-Whitney U would be better; without scipy on the droplet, report
        # the overlap plainly and let the size of the gap speak.
        print()
        if abs(mi - mo) < 0.02 * max(mo, 1e-9):
            print("  READ: no meaningful difference — the claimed window is not")
            print("        distinguishable from any other hour in our data.")
        elif mi > mo:
            print("  READ: directionally CONSISTENT with the claim, but see the")
            print("        caveats in this file's docstring before acting on it.")
        else:
            print("  READ: OPPOSITE of the claim — competition is HIGHER in the")
            print("        claimed window in our data.")


if __name__ == "__main__":
    main()
