#!/usr/bin/env python3
"""
scripts/stop_shadow_report.py — judge the market-price stop-loss shadow.

Joins every "stop_shadow" record (modules/weather_exec.py._watch_stop_shadow —
what a "sell once the credible mark drops below STOP_SHADOW_MARK_C" rule would
have salvaged) against the position's EVENTUAL real settle record, by pos_id.
Answers the only question that matters before this ever goes live: does
selling early net out ahead of what actually happened, across real positions,
not a hypothesis?

  - would_salvage > real pnl_usd  -> the stop would have HELPED (real loss was
    worse than the shadow salvage, or a dead-exit never found a bid at all)
  - would_salvage < real pnl_usd  -> the stop would have HURT (the position
    recovered/won after the shadow trigger — a false alarm)

Run: python scripts/stop_shadow_report.py [--log weather_live.jsonl]
"""

import argparse
import json
from pathlib import Path


def load(path):
    shadows, settles = {}, {}
    if not Path(path).exists():
        return shadows, settles
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        pid = r.get("pos_id") or r.get("key")
        if r.get("type") == "stop_shadow":
            shadows[pid] = r
        elif r.get("type") == "settle":
            settles[pid] = r
    return shadows, settles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="weather_live.jsonl")
    args = ap.parse_args()

    shadows, settles = load(args.log)
    if not shadows:
        print(f"no stop_shadow records yet in {args.log} — nothing to judge. "
              f"This accumulates forward as positions trigger it; check back later.")
        return

    resolved = [(s, settles[pid]) for pid, s in shadows.items() if pid in settles]
    pending = len(shadows) - len(resolved)

    helped = hurt = 0
    helped_usd = hurt_usd = 0.0
    print(f"{'city':<14}{'shadow':>8}{'real pnl':>10}{'verdict':>10}")
    for s, sv in resolved:
        would = s["would_salvage_usd"] - (s.get("cost_usd") or 0)  # as a pnl, not raw salvage
        real = sv.get("pnl_usd") or 0.0
        delta = would - real
        verdict = "HELPED" if delta > 0.01 else ("HURT" if delta < -0.01 else "wash")
        if verdict == "HELPED":
            helped += 1; helped_usd += delta
        elif verdict == "HURT":
            hurt += 1; hurt_usd += delta
        print(f"{s['city']:<14}{would:>+8.2f}{real:>+10.2f}{verdict:>10}")

    print(f"\n{len(resolved)} resolved, {pending} still open/pending settlement")
    if resolved:
        print(f"stop would have HELPED {helped}x (net {helped_usd:+.2f}), "
              f"HURT {hurt}x (net {hurt_usd:+.2f}), "
              f"net {helped_usd + hurt_usd:+.2f}")
        if len(resolved) < 15:
            print("\nn is still small — this is a running tally, not a verdict. "
                  "Keep accumulating before promoting this off shadow.")


if __name__ == "__main__":
    main()
