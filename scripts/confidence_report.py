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
    all_records, all_outcomes = records, outcomes
    model_p = [r["model_p"] for r in records]
    market_p = [C.market_prob(r) for r in records]

    print(f"labelled rows scored: {len(rows)}"
          + (f"   venue={args.venue}" if args.venue else ""))
    mix = C.signal_mix(records)
    print(f"signal mix: {mix}")
    # Scope matters and is easy to misread. These are ALL scored candidates,
    # including every row a gate refused — that is the point of recording them,
    # but it makes the numbers non-comparable with any figure computed over
    # trades actually taken. The ENTER subset is the one the decision rides on.
    n_enter = mix.get("ENTER", 0)
    print(f"scope: every scored candidate, entered or not. "
          f"ENTER rows in this set: {n_enter}")
    if not n_enter:
        print("       -> no tradeable row has settled yet, so nothing here "
              "describes a trade the bot would have placed.")
    cont_r, cont_o, n_decided = C.split_contested(records, outcomes)
    print(f"price split: {n_decided} already decided at the quote "
          f"(<{C.CONTESTED_LO_C:.0f}c or >{C.CONTESTED_HI_C:.0f}c), "
          f"{len(cont_r)} still contested")
    print("       -> the acceptance test scores the CONTESTED set. A market at",
          "2c or 99c is a settled outcome, not a forecast, and scoring")
    print("          against it flatters the price to a near-zero Brier.")
    print()
    # From here on the acceptance test runs on the contested set only.
    if not cont_r:
        print("── no contested markets labelled yet ──")
        print("   Every settled market so far was already decided at its quote.")
        print("   Nothing here can measure forecasting skill.")
        return
    # Narrow again. model_p is a "the extreme has plateaued, will it hold"
    # model; rows that failed a timing gate never reached it, and scoring it
    # there measures a regime the strategy deliberately avoids.
    tim_r, tim_o, n_timing = C.split_timing(cont_r, cont_o)
    print(f"timing split: {n_timing} of the {len(cont_r)} contested markets were",
          "refused before the model was consulted")
    if not tim_r:
        print("       -> nothing left. The acceptance test needs a market that",
              "cleared data and timing AND is still contested.")
        _gates(cont_r, cont_o)
        return
    records, outcomes = tim_r, tim_o
    model_p = [r["model_p"] for r in records]
    market_p = [C.market_prob(r) for r in records]
    print(f"── acceptance test: contested AND past timing (n={len(records)}) ──")


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
    if len(records) >= args.min_n:
        half = len(records) // 2
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
        print(f"── calibration skipped: {len(records)} contested markets, "
              f"need {args.min_n} ──")
        print("   Fitting below that is how this project produced two filters "
              "that inverted on re-check.\n")

    print("── break-even by price band (all scored candidates) ──")
    for b in C.break_even_by_price(records, outcomes):
        flag = "LOSES" if b["gap_pp"] < 0 else "clears"
        print(f"   {b['band']:<9} n={b['n']:<4} win {b['win_rate']:.0%}  "
              f"needs {b['break_even']:.0%}  gap {b['gap_pp']:+.1f}pp  {flag}")



    enter = [(r, o) for r, o in zip(records, outcomes)
             if r.get("signal") == "ENTER"]
    if enter:
        er, eo = [r for r, _ in enter], [o for _, o in enter]
        print()
        print("── the ENTER subset: rows the engine actually wanted ──")
        ev = C.evaluate([r["model_p"] for r in er],
                        [C.market_prob(r) for r in er], eo, label="ENTER only")
        print(f"   n {ev['n']}   base rate {_fmt(ev['base_rate'])}   "
              f"overconfidence {ev['overconfidence']:+.4f}")
        print(f"   Brier  model {_fmt(ev['brier_model'])}   "
              f"base-rate {_fmt(ev['brier_base_rate'])}   "
              f"market {_fmt(ev['brier_market'])}")
        print(f"   {ev['verdict']}")
        for b in C.break_even_by_price(er, eo):
            flag = "LOSES" if b["gap_pp"] < 0 else "clears"
            print(f"   {b['band']:<9} n={b['n']:<4} win {b['win_rate']:.0%}  "
                  f"needs {b['break_even']:.0%}  gap {b['gap_pp']:+.1f}pp  {flag}")
    else:
        print()
        print("── the ENTER subset is still empty ──")
        print("   Until a row the engine wanted to trade settles, the acceptance")
        print("   test describes the candidate population, not the strategy.")

    _gates(all_records, all_outcomes)




def _gates(records, outcomes):
    """What each gate saved or cost, on labelled markets."""
    rows = C.gate_scorecard(records, outcomes)
    if not rows:
        return
    print()
    print("── gate scorecard: what each refusal was worth ──")
    print("   (cents per share if we had bought at the quoted ask;",
          "negative means the gate saved money)")
    print("   %-12s %-4s %-8s %-9s %-10s %-9s %s" % (
        "gate", "n", "won", "avg ask", "total EV", "per trade", "verdict"))
    for g in rows:
        print("   %-12s %-4d %-8s %-9.0f %-10.1f %-9.2f %s" % (
            g["signal"], g["n"], "%d (%.0f%%)" % (g["wins"], 100 * g["win_rate"]),
            g["avg_ask_c"], g["total_ev_c"], g["ev_per_trade_c"], g["verdict"]))
    total = sum(g["total_ev_c"] for g in rows)
    print("   %-12s %-4d %-8s %-9s %-10.1f" % (
        "ALL", sum(g["n"] for g in rows), "", "", total))
    print("   -> refusing every one of these was worth %+.1fc per share overall"
          % -total)

if __name__ == "__main__":
    main()
