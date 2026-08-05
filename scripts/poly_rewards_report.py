#!/usr/bin/env python3
"""
scripts/poly_rewards_report.py — persistence summary for the LP-reward
monitor log (scripts/monitor_poly_rewards.py).

A single scan (scripts/scan_poly_rewards.py) can't tell you whether a 970%/
day estimate is real or a one-cycle fluke about to be filled by another
maker. This groups the monitor's snapshots by market slug and reports how
many cycles each one stayed in the top-N and how its yield moved — the
evidence to look at before resting any real capital.

Run:  python scripts/poly_rewards_report.py [path-to-log]
      (default: poly_rewards_log.jsonl in the cwd)
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "poly_rewards_log.jsonl")
    if not path.exists():
        print(f"no log yet at {path} — start scripts/monitor_poly_rewards.py first")
        return

    cycles = 0
    by_slug = defaultdict(list)  # slug -> list of (ts, yield, est_daily_usd, question)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        cycles += 1
        for row in snap.get("top", []):
            by_slug[row["slug"]].append((
                snap["ts"], row["yield_per_dollar_per_day"],
                row["est_daily_usd"], row["question"],
            ))

    if cycles == 0:
        print(f"log at {path} is empty")
        return

    print(f"═══ Poly rewards persistence — {cycles} cycles, {len(by_slug)} distinct markets ═══\n")
    print(f"  {'cycles':>7s} {'%cycles':>8s} {'avg yield':>10s} {'min':>8s} {'max':>8s} {'question'}")

    rows = []
    for slug, samples in by_slug.items():
        yields = [s[1] for s in samples]
        rows.append({
            "slug": slug,
            "question": samples[-1][3],
            "n": len(samples),
            "pct_cycles": len(samples) / cycles * 100.0,
            "avg_yield": statistics.mean(yields),
            "min_yield": min(yields),
            "max_yield": max(yields),
        })

    # Persistent AND high-yield first — a market that only shows up once is
    # noise regardless of how good that one snapshot looked.
    rows.sort(key=lambda r: (r["pct_cycles"], r["avg_yield"]), reverse=True)

    for r in rows[:30]:
        print(f"  {r['n']:7d} {r['pct_cycles']:7.0f}% {r['avg_yield']*100:9.0f}% "
              f"{r['min_yield']*100:7.0f}% {r['max_yield']*100:7.0f}%  {r['question'][:55]}")

    persistent = [r for r in rows if r["pct_cycles"] >= 80.0]
    print(f"\n{len(persistent)}/{len(rows)} markets held a top-N spot in >=80% of cycles — "
          "those are the candidates worth a closer look. Everything else showed up once or "
          "twice and vanished, which is exactly what you'd expect if other makers fill the gap "
          "fast once a market shows a fat, uncontested spread.")


if __name__ == "__main__":
    sys.exit(main())
