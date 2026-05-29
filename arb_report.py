"""
arb_report.py - read trades.jsonl and summarize cross-venue arb activity.

Usage:
    python arb_report.py                       # all-time summary
    python arb_report.py --since 2026-05-28    # only since date
    python arb_report.py --asset BTC           # only one asset
    python arb_report.py --hours 24            # last N hours
    python arb_report.py --file other.jsonl    # alternate log file

Outputs (per asset, then totals):
  - Attempts that crossed the qualified threshold and their decision breakdown
  - Fire -> entry / poly_miss / EV-abort / kalshi_miss conversion rates
  - Latency stats (poly leg, kalshi leg, naked duration)
  - Entry economics (combined cost, expected vs exec profit, fee source)
  - Unwind activity (count, estimated loss, full vs partial)
"""

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


def _parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_rows(path: Path) -> Iterable[dict]:
    if not path.exists():
        sys.stderr.write(f"trade log not found: {path}\n")
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _arb_rows(rows: Iterable[dict]) -> Iterable[dict]:
    arb_types = {"arb_attempt", "arb_entry", "arb_poly_miss",
                 "arb_ev_abort", "arb_unwind"}
    for r in rows:
        if r.get("type") in arb_types:
            yield r


def _fmt_stats(values, fmt="{:.1f}"):
    if not values:
        return "-"
    return (f"n={len(values)} "
            f"p50={fmt.format(statistics.median(values))} "
            f"p90={fmt.format(_quantile(values, 0.9))} "
            f"max={fmt.format(max(values))}")


def _quantile(xs, q):
    s = sorted(xs)
    i = int(round((len(s) - 1) * q))
    return s[i]


def _report(rows: list, label: str):
    by_asset = defaultdict(list)
    for r in rows:
        by_asset[r.get("asset", "?")].append(r)
    by_asset["ALL"] = rows

    print(f"\n=== Arb report - {label} ===")

    for asset in sorted(by_asset.keys()):
        items = by_asset[asset]
        if not items:
            continue

        attempts   = [r for r in items if r["type"] == "arb_attempt"]
        entries    = [r for r in items if r["type"] == "arb_entry"]
        poly_miss  = [r for r in items if r["type"] == "arb_poly_miss"]
        ev_aborts  = [r for r in items if r["type"] == "arb_ev_abort"]
        unwinds    = [r for r in items if r["type"] == "arb_unwind"]

        fires      = [r for r in attempts if r["decision"] == "fire"]
        skips      = [r for r in attempts if r["decision"] == "skip"]
        skip_breakdown = Counter(r.get("reason") for r in skips)

        print(f"\n[{asset}]  attempts={len(attempts)}  fires={len(fires)}  "
              f"entries={len(entries)}  poly_miss={len(poly_miss)}  "
              f"ev_abort={len(ev_aborts)}  unwind={len(unwinds)}")

        if skip_breakdown:
            print("  skip reasons:")
            for reason, n in sorted(skip_breakdown.items(),
                                    key=lambda kv: -kv[1]):
                print(f"    {reason:24s} {n}")

        if fires:
            fire_to_entry  = len(entries)  / len(fires) if fires else 0
            fire_to_poly_miss = len(poly_miss) / len(fires) if fires else 0
            fire_to_unwind = len(unwinds) / len(fires) if fires else 0
            print(f"  fire -> entry      : {fire_to_entry:.1%}")
            print(f"  fire -> poly_miss  : {fire_to_poly_miss:.1%}")
            print(f"  fire -> naked unwind: {fire_to_unwind:.1%}")

        if entries:
            poly_lat   = [r.get("poly_leg_latency_ms")   for r in entries if r.get("poly_leg_latency_ms")   is not None]
            kalshi_lat = [r.get("kalshi_leg_latency_ms") for r in entries if r.get("kalshi_leg_latency_ms") is not None]
            naked_lat  = [r.get("naked_duration_ms")     for r in entries if r.get("naked_duration_ms")     is not None]
            print(f"  poly leg ms   : {_fmt_stats(poly_lat)}")
            print(f"  kalshi leg ms : {_fmt_stats(kalshi_lat)}")
            print(f"  naked ms      : {_fmt_stats(naked_lat)}")

            combined  = [r.get("combined_cost") for r in entries if r.get("combined_cost") is not None]
            det_prof  = [r.get("net_profit_per_contract") for r in entries if r.get("net_profit_per_contract") is not None]
            exec_prof = [r.get("exec_profit_per_contract") for r in entries if r.get("exec_profit_per_contract") is not None]
            if combined:
                print(f"  combined cost c: median={statistics.median(combined):.1f}  "
                      f"min={min(combined)}  max={max(combined)}")
            if det_prof:
                print(f"  detection net c/contract: median={statistics.median(det_prof):.2f}")
            if exec_prof:
                print(f"  execution net c/contract: median={statistics.median(exec_prof):.2f}")

            fee_src = Counter(r.get("fee_source", "unknown") for r in entries)
            print(f"  fee source : {dict(fee_src)}")

            # Expected gross PnL = sum of (100 - combined) * count
            est_pnl_cents = sum(
                (100 - r["combined_cost"]) * r.get("count", 0)
                for r in entries if r.get("combined_cost") is not None
            )
            print(f"  expected gross PnL: ${est_pnl_cents / 100:.2f} "
                  f"(at resolution; does not account for naked unwinds or settlement risk)")

        if unwinds:
            losses = [r.get("est_loss_cents") for r in unwinds
                      if r.get("est_loss_cents") is not None]
            naked  = [r.get("naked_ms") for r in unwinds
                      if r.get("naked_ms") is not None]
            full   = sum(1 for r in unwinds if r.get("fully_unwound"))
            partial = len(unwinds) - full
            print(f"  unwinds: full={full} partial_or_failed={partial}")
            if losses:
                print(f"  estimated unwind loss c : "
                      f"sum={sum(losses):.0f}  worst={max(losses):.0f}")
            if naked:
                print(f"  naked dur ms before unwind : {_fmt_stats(naked)}")

        if ev_aborts:
            print(f"  EV recheck aborts: {len(ev_aborts)} "
                  f"(detection EV passed but execution prices made it negative)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=os.getenv("TRADES_FILE", "trades.jsonl"),
                   help="Path to trades.jsonl")
    p.add_argument("--asset", help="Filter to a single asset (BTC/ETH/SOL)")
    p.add_argument("--since", help="ISO date - include rows on or after this date")
    p.add_argument("--hours", type=float,
                   help="Include only the last N hours of activity")
    args = p.parse_args()

    rows = list(_arb_rows(_load_rows(Path(args.file))))

    if args.asset:
        rows = [r for r in rows if r.get("asset") == args.asset.upper()]

    if args.since:
        since = _parse_ts(args.since)
        if since:
            rows = [r for r in rows
                    if (t := _parse_ts(r.get("ts", ""))) and t >= since]

    if args.hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        rows = [r for r in rows
                if (t := _parse_ts(r.get("ts", ""))) and t >= cutoff]

    label_parts = []
    if args.asset:  label_parts.append(args.asset.upper())
    if args.since:  label_parts.append(f"since {args.since}")
    if args.hours:  label_parts.append(f"last {args.hours}h")
    if not label_parts: label_parts.append("all time")
    _report(rows, "  ".join(label_parts))


if __name__ == "__main__":
    main()
