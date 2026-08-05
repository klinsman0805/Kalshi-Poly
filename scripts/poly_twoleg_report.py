#!/usr/bin/env python3
"""
scripts/poly_twoleg_report.py — reports the two numbers the two-leg strategy
actually depends on, from poly_twoleg.jsonl.

The design argument is: both legs filling is risk-free profit, and a
one-sided fill is rescued to roughly break-even by auto-complete. Neither
claim is worth anything until measured, so this reports exactly:

  1. BOTH-FILL RATE — how often the pair completes naturally (the profitable
     case) versus needing a rescue.
  2. REAL AUTO-COMPLETE COST — what completing actually cost each time,
     versus the ~0 the structure predicts. If this drifts negative, the book
     is moving between fill and completion, and that is a latency problem a
     websocket fill feed would fix.

Also reports the band_fraction each position used, so the reward-vs-fill
trade-off can be tuned on evidence rather than taste.

Run: python scripts/poly_twoleg_report.py
"""

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

LOG = Path("poly_twoleg.jsonl")


def main():
    if not LOG.exists():
        print("no two-leg log yet — nothing to report")
        return

    placed, completed, closed = {}, {}, {}
    fills = defaultdict(list)
    for line in open(LOG):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        t, pid = r.get("type"), r.get("id")
        if t == "twoleg_placed":
            placed[pid] = r
        elif t == "twoleg_fill":
            fills[pid].append(r)
        elif t == "twoleg_completed":
            completed[pid] = r
        elif t == "twoleg_closed":
            closed[pid] = r

    if not placed:
        print("no positions quoted yet")
        return

    how = Counter(c.get("how") for c in completed.values())
    both = how.get("both_legs_filled", 0)
    rescued = sum(v for k, v in how.items() if k and k.startswith("auto_complete"))
    unfilled = sum(1 for pid in placed if pid not in completed)

    print(f"positions quoted        : {len(placed)}")
    print(f"  both legs filled      : {both}   (risk-free complete set)")
    print(f"  auto-completed        : {rescued}   (one leg filled, rescued)")
    print(f"  expired unfilled      : {unfilled}   (reward-only, no exposure)")
    print()

    if completed:
        pnls = [c.get("pnl_usd") or 0.0 for c in completed.values()]
        print(f"realized set P&L        : ${sum(pnls):+.4f} over {len(pnls)} completions")
        print(f"  mean per completion   : ${statistics.mean(pnls):+.4f}")

    rescue_pnls = [c.get("pnl_usd") or 0.0 for c in completed.values()
                   if (c.get("how") or "").startswith("auto_complete")]
    if rescue_pnls:
        print()
        print("=== auto-complete cost (the load-bearing assumption) ===")
        print(f"  n={len(rescue_pnls)}  mean=${statistics.mean(rescue_pnls):+.4f}  "
              f"worst=${min(rescue_pnls):+.4f}  best=${max(rescue_pnls):+.4f}")
        if statistics.mean(rescue_pnls) < -0.25:
            print("  READ: completions are costing real money — the book is moving")
            print("        between fill and rescue. That is detection latency;")
            print("        a websocket fill feed is the fix.")
        else:
            print("  READ: completions are landing near break-even, as the")
            print("        YES+NO=$1.00 structure predicts.")

    print()
    print("=== by band_fraction (reward weight vs fill rate) ===")
    by_bf = defaultdict(lambda: {"n": 0, "filled": 0, "weight": 0.0})
    for pid, p in placed.items():
        bf = p.get("band_fraction")
        b = by_bf[bf]
        b["n"] += 1
        b["weight"] = p.get("score_weight", 0.0)
        if pid in completed:
            b["filled"] += 1
    for bf in sorted(by_bf):
        b = by_bf[bf]
        rate = 100.0 * b["filled"] / b["n"] if b["n"] else 0.0
        print(f"  band_fraction={bf}  score_weight={b['weight']:.2f}  "
              f"n={b['n']}  any-fill={rate:.0f}%")


if __name__ == "__main__":
    main()
