#!/usr/bin/env python3
"""
scripts/kalshi_paper_report.py — human-readable summary of the Kalshi paper ledger.

The paper forward-test writes one JSON line per event to kalshi_weather_paper.jsonl
(durable, restart-safe, NOT the noisy per-cycle journal). This prints it as a
clean morning-read table: settled trades with entry/exit/P&L, still-open
positions, and running totals.

Run:  python scripts/kalshi_paper_report.py [path-to-ledger]
      (default: kalshi_weather_paper.jsonl in the cwd)
"""

import json
import sys
from pathlib import Path


def exit_price(rec):
    """Normalize the two settle paths to a single exit price in cents."""
    if rec.get("sold_at_c") is not None:      # early exit: real sale price
        return rec["sold_at_c"]
    return 100.0 if rec.get("won") else 0.0   # natural settlement


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "kalshi_weather_paper.jsonl")
    if not path.exists():
        print(f"no ledger yet at {path} — nothing has traded")
        return

    # The settle record is minimal (key + exit/pnl); city/entry/label live in the
    # matching open record, so join settles back onto their open by key. Keep every
    # open (don't pop) so the join survives; track which keys are still open.
    seen_open, settles, still_open = {}, [], set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        key = r.get("key")
        if t == "open":
            seen_open[key] = r
            still_open.add(key)
        elif t == "settle":
            settles.append({**seen_open.get(key, {}), **r})   # open fields + settle fields
            still_open.discard(key)
    opens = {k: seen_open[k] for k in still_open}

    print(f"═══ Kalshi paper ledger — {path} ═══\n")

    # ── settled trades ──
    if settles:
        print(f"SETTLED ({len(settles)}):")
        print(f"  {'city':14s} {'k':3s} {'bucket':9s} {'entry':>6s} {'exit':>6s} "
              f"{'P&L':>8s}  {'how':11s} {'when':16s}")
        wins = 0
        pnl_sum = 0.0
        for r in sorted(settles, key=lambda x: x.get("settled", "")):
            ex = exit_price(r)
            pnl = r.get("pnl_usd", 0.0)
            pnl_sum += pnl
            if r.get("won"):
                wins += 1
            how = r.get("exit") or ("WIN" if r.get("won") else "LOSS")
            when = (r.get("settled", "") or "")[5:16].replace("T", " ")
            k = "▼lo" if r.get("kind") == "low" else "▲hi"
            print(f"  {r.get('city',''):14s} {k:3s} {str(r.get('label','')):9s} "
                  f"{r.get('entry_c',0):5.0f}c {ex:5.0f}c {pnl:+7.2f}  "
                  f"{how:11s} {when:16s}")
        wr = f"{100*wins/len(settles):.0f}%" if settles else "—"
        print(f"\n  → {len(settles)} settled · {wins} wins ({wr}) · "
              f"paper P&L ${pnl_sum:+.2f}")
    else:
        print("SETTLED: none yet")

    # ── still open ──
    print()
    if opens:
        print(f"OPEN ({len(opens)}):")
        print(f"  {'city':14s} {'k':3s} {'bucket':9s} {'entry':>6s} {'cost':>7s} {'opened':16s}")
        cost_sum = 0.0
        for r in sorted(opens.values(), key=lambda x: x.get("opened", "")):
            cost_sum += r.get("cost_usd", 0.0)
            when = (r.get("opened", "") or "")[5:16].replace("T", " ")
            k = "▼lo" if r.get("kind") == "low" else "▲hi"
            print(f"  {r.get('city',''):14s} {k:3s} {str(r.get('label','')):9s} "
                  f"{r.get('entry_c',0):5.0f}c ${r.get('cost_usd',0):5.2f}  {when:16s}")
        print(f"\n  → {len(opens)} open · ${cost_sum:.2f} staked")
    else:
        print("OPEN: none")


if __name__ == "__main__":
    main()
