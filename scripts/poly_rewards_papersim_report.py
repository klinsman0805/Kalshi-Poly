#!/usr/bin/env python3
"""
scripts/poly_rewards_papersim_report.py — summarizes poly_rewards_papersim.jsonl
(modules.poly_rewards_papersim), the fully-paper simulation of the LP auto-
executor pipeline: real book/band/METAR data, real dead-bucket + trend-exit
logic, no real order ever sent.

For each simulated position: entry, any reprices, inferred fill, and exit
(if any). Reward accrual is estimated as est_daily_usd prorated by however
long the position was actually resting AND scoring — i.e. only the time
between entry/last-reprice and fill (or now, if never filled) counts,
since a filled order stops resting and stops earning reward.

Run: python scripts/poly_rewards_papersim_report.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOG_PATH = Path("poly_rewards_papersim.jsonl")


def _parse_ts(s):
    return datetime.fromisoformat(s)


def main():
    if not LOG_PATH.exists():
        print("no papersim log yet — nothing to report")
        return

    positions = {}   # id -> accumulated record
    order = []
    for line in open(LOG_PATH):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        pid = rec["id"]
        if rec["type"] == "paper_entered":
            positions[pid] = {"id": pid, "city": rec["city"], "kind": rec["kind"],
                              "date": rec["date"], "entered_ts": rec["ts"],
                              "est_daily_usd": rec["est_daily_usd"], "size": rec["size"],
                              "price_events": [(rec["ts"], rec["entry_price_c"])],
                              "filled_ts": None, "exited": None}
            order.append(pid)
        elif pid in positions:
            p = positions[pid]
            if rec["type"] == "paper_repriced":
                p["price_events"].append((rec["ts"], rec["price_c"]))
            elif rec["type"] == "paper_filled":
                p["filled_ts"] = rec["ts"]
            elif rec["type"] == "paper_exited":
                p["exited"] = rec

    now = datetime.now(timezone.utc)
    total_reward = 0.0
    total_pnl = 0.0
    n_entered, n_filled, n_exited = 0, 0, 0

    print(f"{'city':<16}{'kind':<6}{'date':<12}{'resting_from':<21}{'resting_to':<21}"
          f"{'hrs_resting':>12}{'est_reward_$':>13}{'filled':>8}{'exit_pnl_$':>12}")
    for pid in order:
        p = positions[pid]
        n_entered += 1
        resting_end = p["filled_ts"] or now.isoformat()
        hrs = (_parse_ts(resting_end) - _parse_ts(p["entered_ts"])).total_seconds() / 3600.0 \
            if isinstance(resting_end, str) else (now - _parse_ts(p["entered_ts"])).total_seconds() / 3600.0
        reward = p["est_daily_usd"] * (hrs / 24.0)
        total_reward += reward
        filled = "yes" if p["filled_ts"] else "no"
        if p["filled_ts"]:
            n_filled += 1
        pnl_str = "-"
        if p["exited"]:
            n_exited += 1
            pnl = p["exited"]["pnl_usd"]
            total_pnl += pnl
            pnl_str = f"{pnl:+.2f}"
        print(f"{p['city']:<16}{p['kind']:<6}{p['date']:<12}{p['entered_ts'][:19]:<21}"
              f"{str(resting_end)[:19]:<21}{hrs:>12.2f}{reward:>13.2f}{filled:>8}{pnl_str:>12}")

    print()
    print(f"positions entered: {n_entered}  filled: {n_filled}  exited: {n_exited}")
    print(f"total estimated LP-reward accrual: ${total_reward:.2f}")
    print(f"total realized exit pnl (fills that later exited): ${total_pnl:.2f}")
    print(f"combined simulated result: ${total_reward + total_pnl:.2f}")


if __name__ == "__main__":
    main()
