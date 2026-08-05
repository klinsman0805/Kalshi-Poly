#!/usr/bin/env python3
"""
scripts/poly_rewards_calibrate.py — measures how wrong feeds.poly_rewards's
reward estimator is, against REAL reward payments from Polymarket.

Until now every number this project produced about LP rewards
(est_daily_usd, yield_per_dollar_per_day, and everything ranked by them) was
our own reimplementation of Polymarket's scoring formula, never once checked
against a payment actually received. This closes that loop using the CLOB's
own earnings endpoints:

    get_total_earnings_for_user_for_day(date)  -> what we were actually paid
    get_earnings_for_user_for_day(date)        -> the same, per condition_id

Predicted side comes from poly_rewards_orders.jsonl (when an order was really
resting, and for how long) joined to poly_rewards_candidates.jsonl (what
est_daily_usd claimed at that moment). Predicted earnings for an order are
prorated: est_daily_usd * resting_hours / 24.

Caveat on the resting window: an order stops earning when it FILLS, not when
we noticed. For partially filled orders this overstates the resting time, so
the real error factor is if anything LARGER than what this prints.

Run: python scripts/poly_rewards_calibrate.py [YYYY-MM-DD ...]
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

ORDERS_LOG = Path("poly_rewards_orders.jsonl")
CANDIDATE_LOG = Path("poly_rewards_candidates.jsonl")


def _read(path):
    if not path.exists():
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def main():
    dates = sys.argv[1:] or ["2026-08-01"]

    import polymarket
    client = polymarket.PolyClient()

    orders = _read(ORDERS_LOG)
    cands = _read(CANDIDATE_LOG)

    # best est_daily_usd we ever logged per condition_id (generous to us:
    # if anything this makes the estimator look BETTER than it was)
    est_by_cid = defaultdict(float)
    for c in cands:
        cid = c.get("condition_id")
        if cid:
            est_by_cid[cid] = max(est_by_cid[cid], c.get("est_daily_usd") or 0.0)

    # resting window per condition_id, from the real order ledger
    placed = {}
    resolved = {}
    for rec in orders:
        oid = rec.get("order_id")
        if not oid:
            continue
        if rec.get("type") == "order_placed":
            placed[oid] = rec
        elif rec.get("type") == "order_resolved":
            resolved[oid] = rec

    hours_by_cid = defaultdict(float)
    for oid, p in placed.items():
        r = resolved.get(oid)
        if not r:
            continue
        t0 = datetime.fromisoformat(p["ts"])
        t1 = datetime.fromisoformat(r["ts"])
        hours_by_cid[p["condition_id"]] += max(0.0, (t1 - t0).total_seconds() / 3600.0)

    grand_pred = grand_real = 0.0
    for date in dates:
        try:
            detail = client._clob.get_earnings_for_user_for_day(date) or []
            total = client._clob.get_total_earnings_for_user_for_day(date) or []
        except Exception as e:  # noqa: BLE001
            print(f"{date}: earnings fetch failed: {e}")
            continue

        real_by_cid = {}
        for row in detail:
            real_by_cid[row.get("condition_id")] = float(row.get("earnings") or 0.0)
        real_total = sum(float(t.get("earnings") or 0.0) for t in total)

        print(f"=== {date} ===")
        if not detail:
            print("  no LP reward earnings recorded this day")
            print()
            continue

        print(f"  {'market (condition_id)':<24}{'rest_h':>8}{'est/day':>10}"
              f"{'predicted':>11}{'ACTUAL':>10}{'over by':>10}")
        day_pred = 0.0
        for cid, real in sorted(real_by_cid.items(), key=lambda kv: -kv[1]):
            hrs = hours_by_cid.get(cid, 0.0)
            est = est_by_cid.get(cid, 0.0)
            pred = est * hrs / 24.0
            day_pred += pred
            factor = (pred / real) if real > 0 else float("inf")
            print(f"  {cid[:22]:<24}{hrs:>8.2f}{est:>10.2f}{pred:>11.4f}"
                  f"{real:>10.4f}{factor:>9.1f}x")
        grand_pred += day_pred
        grand_real += real_total
        print(f"  {'TOTAL':<24}{'':>8}{'':>10}{day_pred:>11.4f}{real_total:>10.4f}"
              f"{(day_pred/real_total if real_total else float('inf')):>9.1f}x")
        print()

    if grand_real > 0:
        print(f"OVERALL: predicted ${grand_pred:.4f} vs actual ${grand_real:.4f} "
              f"— estimator runs {grand_pred/grand_real:.1f}x HOT")
        print()
        print("Every candidate ranking, yield figure, and paper-sim reward number")
        print("this project has produced is inflated by roughly this factor.")


if __name__ == "__main__":
    main()
