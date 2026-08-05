#!/usr/bin/env python3
"""
scripts/conditions_report.py — read out the weather-conditions shadow experiment.

Joins the `conditions` block recorded on each entry (see feeds/metar_conditions.py)
against how that position actually resolved, and reports whether any signal
separates winners from losers. Reads the ledgers only — runs anywhere, changes
nothing.

The question this has to answer honestly is NOT "do losers look bad" — it is
"do losers look bad in a way winners DON'T". A first pass over 28 backfilled
entries said no for the composite score (winners mean 0.79 vs losers 0.68, and
the biggest loss scored a perfect 1.00) while the low-ceiling flag alone was
0 winners / 2 losers. Both numbers are too small to act on; this script exists
to re-run that as live data accumulates.

Usage:  python3 scripts/conditions_report.py [--days N]
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

LEDGERS = [
    ("poly", os.getenv("POLY_LEDGER", "weather_live.jsonl")),
    ("kalshi", os.getenv("KALSHI_LEDGER", "kalshi_weather_paper.jsonl")),
]

FLAGS = [
    ("precip_now", lambda c: bool(c.get("precip_now"))),
    ("precip<=90m", lambda c: (c.get("precip_recent_min") is not None
                               and c["precip_recent_min"] <= 90)),
    ("ceiling<5000ft", lambda c: (c.get("ceiling_ft") is not None
                                  and c["ceiling_ft"] < 5000)),
    ("ceiling<3000ft", lambda c: (c.get("ceiling_ft") is not None
                                  and c["ceiling_ft"] < 3000)),
    ("convective", lambda c: bool(c.get("convective"))),
    ("BECMG/TEMPO", lambda c: c.get("trend") in ("BECMG", "TEMPO")),
    ("gust/variable", lambda c: bool(c.get("gust_kt") or c.get("wind_variable"))),
    ("dry>=15C", lambda c: (c.get("dewpoint_spread_c") is not None
                            and c["dewpoint_spread_c"] >= 15)),
]


def load(path, venue, cutoff):
    opens, settles = {}, {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "open":
                    r["venue"] = venue
                    opens[r["key"]] = r
                elif r.get("type") == "settle":
                    settles[r["key"]] = r
    except FileNotFoundError:
        return []
    out = []
    for key, o in opens.items():
        if not o.get("conditions"):
            continue                      # pre-experiment entry
        try:
            if datetime.fromisoformat(o["opened"]) < cutoff:
                continue
        except (KeyError, ValueError):
            pass
        s = settles.get(key)
        out.append((o, s.get("pnl_usd") if s else None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    trades = []
    for venue, path in LEDGERS:
        trades.extend(load(path, venue, cutoff))
    trades.sort(key=lambda t: t[0]["opened"])

    settled = [(o, p) for o, p in trades if p is not None]
    pending = [(o, p) for o, p in trades if p is None]

    print(f"conditions experiment — {len(trades)} entries carry conditions "
          f"({len(settled)} settled, {len(pending)} still open)\n")
    if not trades:
        print("No entries recorded with conditions yet. The shadow logging went")
        print("live 2026-07-26; entries fire a few times a day, so give it time.")
        return

    print(f"{'when':<12}{'venue':<8}{'city':<15}{'kind':<6}{'score':>6}{'pnl':>9}  flags")
    for o, pnl in trades:
        c = o["conditions"]
        fl = [name for name, fn in FLAGS if fn(c)]
        when = o["opened"][5:16].replace("T", " ")
        pnl_txt = f"{pnl:+.2f}" if pnl is not None else "open"
        print(f"{when:<12}{o['venue']:<8}{o['city']:<15}{o.get('kind','high'):<6}"
              f"{c.get('score', float('nan')):>6.2f}{pnl_txt:>9}  {', '.join(fl) or 'clean'}")

    if not settled:
        print("\nNothing settled yet — no win/loss split to report.")
        return

    wins = [(o, p) for o, p in settled if p >= 0]
    losses = [(o, p) for o, p in settled if p < 0]
    ws = [o["conditions"].get("score") for o, _ in wins if o["conditions"].get("score") is not None]
    ls = [o["conditions"].get("score") for o, _ in losses if o["conditions"].get("score") is not None]

    print("\n── composite score ──")
    if ws:
        print(f"  winners n={len(ws):<3} mean={sum(ws)/len(ws):.3f}  min={min(ws):.2f}")
    if ls:
        print(f"  losers  n={len(ls):<3} mean={sum(ls)/len(ls):.3f}  max={max(ls):.2f}")
    if ws and ls:
        sep = (sum(ws)/len(ws)) - (sum(ls)/len(ls))
        print(f"  separation (want clearly >0): {sep:+.3f}")

    print("\n── per-flag: does it appear more in losers than winners? ──")
    print(f"  {'flag':<16}{'winners':>10}{'losers':>10}   read")
    for name, fn in FLAGS:
        w = sum(1 for o, _ in wins if fn(o["conditions"]))
        l = sum(1 for o, _ in losses if fn(o["conditions"]))
        wr = f"{w}/{len(wins)}"
        lr = f"{l}/{len(losses)}"
        note = ""
        if len(wins) and len(losses):
            wpct, lpct = w / len(wins), l / len(losses)
            if l and w == 0:
                note = "← losers only"
            elif lpct > wpct * 2 and l >= 2:
                note = "← skews loser"
            elif w and l == 0:
                note = "(winners only)"
        print(f"  {name:<16}{wr:>10}{lr:>10}   {note}")

    print("\n── what a score gate would have cost ──")
    for thr in (0.4, 0.5, 0.6, 0.7):
        bw = [(o, p) for o, p in wins if (o["conditions"].get("score") or 1) < thr]
        bl = [(o, p) for o, p in losses if (o["conditions"].get("score") or 1) < thr]
        print(f"  score>={thr}: blocks {len(bw)}/{len(wins)} winners "
              f"(${sum(p for _, p in bw):+.2f}) and {len(bl)}/{len(losses)} losers "
              f"(${sum(p for _, p in bl):+.2f})  net ${-sum(p for _, p in bw) - sum(p for _, p in bl):+.2f}")
    print("\n  (net > 0 means the gate would have helped. Sample size matters more")
    print("   than sign here — the solar-elevation idea looked fine at n=1 too.)")


if __name__ == "__main__":
    main()
