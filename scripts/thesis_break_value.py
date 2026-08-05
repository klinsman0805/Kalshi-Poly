#!/usr/bin/env python3
"""
scripts/thesis_break_value.py — what would selling at the THESIS BREAK have
been worth?

weather_exec._recheck_open already re-runs the full entry thesis on every open
position every cycle, and logs a THESIS BREAK the moment it fails — with the
live bid printed right there in the message, and the words "exit window is
NOW, dead-exit will be too late". It then does nothing. The only code that
actually sells is _close_dead, which requires ARITHMETIC death (observed
extreme has passed the bucket). That test:

  - only catches OVERSHOOT (extreme moved through the bucket). A position that
    loses by UNDERSHOOTING — the extreme never climbs to the bucket at all —
    is never "provably dead" while the day is still young, so it is never sold.
  - fires, by construction, at the moment the position is worthless, which is
    exactly when the bids have gone (its own docstring says so).

This script joins the ledger's real settled losses against the THESIS BREAK
lines in the journal to measure what the ignored signal was actually worth.

Usage:
  journalctl -u kalshi-paper.service --since '2026-07-24' --no-pager \\
      > /tmp/wx.log
  python scripts/thesis_break_value.py kalshi_weather_paper.jsonl /tmp/wx.log
"""

import json
import re
import sys
from pathlib import Path

LEDGER = Path(sys.argv[1] if len(sys.argv) > 1 else "kalshi_weather_paper.jsonl")
JOURNAL = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/wx.log")

# "⚠ THESIS BREAK <city> <label>: <reasons> | bid <N>c vs entry <M>c"
BREAK_RE = re.compile(r"THESIS BREAK (.+?): (.+?) \| bid ([\d.]+)c vs entry ([\d.]+)c")


def main():
    recs = [json.loads(l) for l in open(LEDGER) if l.strip()]
    opens = {}
    for r in recs:
        if r.get("type") == "open":
            opens[r["key"]] = r
    settles = [r for r in recs if r.get("type") == "settle"]

    # first observed break per (city, label) — the earliest exit chance
    first_break = {}
    if JOURNAL.exists():
        for line in open(JOURNAL, errors="ignore"):
            m = BREAK_RE.search(line)
            if not m:
                continue
            who, _reasons, bid_c, entry_c = m.groups()
            key = (who.strip(), float(entry_c))
            if key not in first_break:
                first_break[key] = (float(bid_c), line.split("]:")[0][-8:].strip())

    print(f"{'position':<30}{'entry':>7}{'shares':>8}{'break_bid':>11}"
          f"{'actual_pnl':>12}{'if_sold':>10}{'delta':>10}")
    tot_actual = tot_ifsold = 0.0
    rows = 0
    for s in settles:
        pnl = s.get("pnl_usd") or 0.0
        if pnl > 0:
            continue
        o = opens.get(s["key"])
        if not o:
            continue
        city = s["key"].split("|")[0]
        label = o.get("label", "")
        entry_c = o.get("entry_c")
        shares = o.get("shares") or 0.0
        cost = o.get("cost_usd") or 0.0

        hit = None
        for (who, e_c), val in first_break.items():
            if who.startswith(city) and label and label in who and abs(e_c - round(entry_c)) < 1.5:
                hit = val
                break
        if not hit:
            print(f"{(city + ' ' + label)[:30]:<30}{entry_c:>7.1f}{shares:>8.1f}"
                  f"{'no break':>11}{pnl:>12.2f}{'—':>10}{'—':>10}")
            tot_actual += pnl
            tot_ifsold += pnl
            continue
        bid_c, _ts = hit
        # what a sell into that bid would have returned, net of the same exit
        # fee model the executor already applies
        proceeds = shares * bid_c / 100.0
        if_sold = proceeds - cost
        tot_actual += pnl
        tot_ifsold += if_sold
        rows += 1
        print(f"{(city + ' ' + label)[:30]:<30}{entry_c:>7.1f}{shares:>8.1f}{bid_c:>10.1f}c"
              f"{pnl:>12.2f}{if_sold:>10.2f}{if_sold - pnl:>+10.2f}")

    print()
    print(f"matched losses with a recorded break: {rows}")
    print(f"actual total on losing trades      : ${tot_actual:>8.2f}")
    print(f"if sold at first THESIS BREAK      : ${tot_ifsold:>8.2f}")
    print(f"difference                          : ${tot_ifsold - tot_actual:>+8.2f}")


if __name__ == "__main__":
    main()
