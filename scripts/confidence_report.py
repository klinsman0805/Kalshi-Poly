#!/usr/bin/env python3
"""
scripts/confidence_report.py — run the acceptance test on whatever is labelled.

Joins recorded candidates to resolved outcomes and scores three forecasters
against each other:

  model        the engine's own model_p, as it stands today
  market       the ask price, which is a competing forecast and the real
               benchmark — an 80c contract is the market saying 80%
  calibrated   model_p after Platt scaling, fitted on one half and scored on
               the other, so the number is out of sample

A model ships only if it beats BOTH the base rate and the market. Anything less
means either the score carries no information or the price already knows.

Run:  python scripts/confidence_report.py [--venue kalshi|poly] [--min-n 30]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(override=False)

from modules import confidence as C                              # noqa: E402
from scripts.label_candidates import (                           # noqa: E402
    LABEL_PATH, load_candidates, _market_id)


def _fmt(v, nd=4):
    return "—" if v is None else f"{v:.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=["kalshi", "poly"], default=None)
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()

    labels = {}
    if LABEL_PATH.exists():
        with LABEL_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("won") is not None:
                    labels[r["market_id"]] = r["won"]
    print(f"resolved markets: {len(labels)}")
    if not labels:
        print("\nNothing labelled yet. Run scripts/label_candidates.py first; "
              "markets settle the morning after their day closes.")
        return

    # One row per market — the last snapshot before the decision. Using every
    # snapshot would weight busy markets more heavily and leak the same outcome
    # into the training set many times over.
    latest = {}
    for rec in load_candidates():
        if args.venue and rec.get("venue") != args.venue:
            continue
        mid = _market_id(rec)
        if mid in labels:
            prev = latest.get(mid)
            if prev is None or rec.get("ts", "") > prev.get("ts", ""):
                latest[mid] = rec

    rows = [(r, labels[m]) for m, r in latest.items()
            if r.get("model_p") is not None and r.get("ask_c") is not None]
    if not rows:
        print("no labelled rows carry both a model probability and a price")
        return

    records = [r for r, _ in rows]
    outcomes = [o for _, o in rows]
    model_p = [r["model_p"] for r in records]
    market_p = [C.market_prob(r) for r in records]

    print(f"labelled rows scored: {len(rows)}"
          + (f"   venue={args.venue}" if args.venue else ""))
    print(f"signal mix: {C.signal_mix(records)}\n")

    for probs, name, is_bench in ((model_p, "model_p (as shipped)", False),
                                  (market_p, "market price", True)):
        ev = C.evaluate(probs, market_p, outcomes, label=name, benchmark=is_bench)
        print(f"── {name} ──")
        print(f"   n {ev['n']}   base rate {_fmt(ev['base_rate'])}   "
              f"mean predicted {_fmt(ev['mean_predicted'])}   "
              f"overconfidence {ev['overconfidence']:+.4f}")
        print(f"   Brier  model {_fmt(ev['brier_model'])}   "
              f"base-rate {_fmt(ev['brier_base_rate'])}   "
              f"market {_fmt(ev['brier_market'])}")
        print(f"   {ev['verdict']}")
        if ev["reliability"]:
            print("   reliability:")
            for b in ev["reliability"]:
                print(f"     {b['bin']}  n={b['n']:<4} predicted {b['predicted']:.3f}"
                      f"  actual {b['actual']:.3f}  gap {b['gap']:+.3f}")
        print()

    # Out-of-sample Platt calibration. Split by market so the same day cannot
    # appear on both sides.
    if len(rows) >= args.min_n:
        half = len(rows) // 2
        fit_p, fit_y = model_p[:half], outcomes[:half]
        test_p, test_y = model_p[half:], outcomes[half:]
        params = C.fit_platt(fit_p, fit_y)
        cal = [C.apply_platt(p, params) for p in test_p]
        ev = C.evaluate(cal, market_p[half:], test_y, label="calibrated")
        print(f"── model_p after Platt calibration (fit n={half}, "
              f"tested out of sample n={len(test_y)}) ──")
        print(f"   A={params[0]:.3f} B={params[1]:.3f}")
        print(f"   Brier  calibrated {_fmt(ev['brier_model'])}   "
              f"base-rate {_fmt(ev['brier_base_rate'])}   "
              f"market {_fmt(ev['brier_market'])}")
        print(f"   {ev['verdict']}\n")
    else:
        print(f"── calibration skipped: {len(rows)} labelled rows, "
              f"need {args.min_n} ──")
        print("   Fitting below that is how this project produced two filters "
              "that inverted on re-check.\n")

    print("── break-even by price band ──")
    for b in C.break_even_by_price(records, outcomes):
        flag = "LOSES" if b["gap_pp"] < 0 else "clears"
        print(f"   {b['band']:<9} n={b['n']:<4} win {b['win_rate']:.0%}  "
              f"needs {b['break_even']:.0%}  gap {b['gap_pp']:+.1f}pp  {flag}")


if __name__ == "__main__":
    main()
