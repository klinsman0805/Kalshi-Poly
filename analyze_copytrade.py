"""
analyze_copytrade.py — aggregate the copy-trade forward-test log.

Reads copytrade_positions.jsonl (copy_open / copy_settle records written by the
executor) and reports the STRATEGY's own forward performance: how many copies,
how many resolved, realized win rate, paper P&L, and the latency slippage tax.

This is the honest edge test — a followed trader's past win rate says nothing;
what matters is whether copying them from now on nets positive after slippage.

Run:  python analyze_copytrade.py [path]
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "copytrade_positions.jsonl")


def main():
    if not path.exists():
        print(f"No log yet at {path} — forward test hasn't recorded any copies.")
        return
    opens, settles = [], []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "copy_open":
            opens.append(rec)
        elif rec.get("type") == "copy_settle":
            settles.append(rec)

    n_copy = len(opens)
    n_set = len(settles)
    wins = sum(1 for s in settles if s.get("won"))
    pnl = sum(float(s.get("pnl_usd") or 0) for s in settles)
    staked_set = sum(float(s.get("cost_usd") or 0) for s in settles)
    slip = [float(o.get("slippage_c") or 0) for o in opens]
    avg_slip = sum(slip) / len(slip) if slip else 0.0

    print(f"COPY-TRADE FORWARD TEST — {path}")
    print(f"  copies opened : {n_copy}")
    print(f"  settled       : {n_set}  ({n_copy - n_set} still open)")
    if n_set:
        wr = wins / n_set
        roi = pnl / staked_set if staked_set else 0
        print(f"  realized win% : {wr*100:.1f}%  ({wins}/{n_set})")
        print(f"  paper P&L     : ${pnl:+.2f}   (ROI {roi*100:+.1f}% on ${staked_set:.0f} settled)")
    else:
        print("  realized win% : — (nothing has resolved yet)")
    print(f"  avg entry slip: {avg_slip:+.2f}¢ vs the trader's fill  (latency tax)")

    # per-wallet breakdown of settled copies
    byw = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for s in settles:
        w = byw[s.get("wallet", "?")]
        w["n"] += 1
        w["w"] += 1 if s.get("won") else 0
        w["pnl"] += float(s.get("pnl_usd") or 0)
    if byw:
        print("\n  per-wallet (settled):")
        for wal, d in sorted(byw.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
            print(f"    {wal[:10]}…  {d['w']}/{d['n']} win  ${d['pnl']:+.2f}")

    # verdict guard — echo the sample-size lesson
    if n_set < 30:
        print(f"\n  ⚠ sample too small (n={n_set}) — not decisive. Keep the loop running.")
    elif pnl > 0:
        print(f"\n  → net positive at n={n_set}; keep accumulating to confirm it's not variance.")
    else:
        print(f"\n  → net negative/flat at n={n_set}; copying these wallets shows no edge so far.")


if __name__ == "__main__":
    main()
