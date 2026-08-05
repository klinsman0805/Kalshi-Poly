"""Correct the Shenzhen 28C (07-29 re-entry) settle record to match the real
on-chain trade: SELL 7 @ 0.09 (confirmed via asset-filtered trade history),
not the buggy sold_shares=6.63/salvage=$2.61 place_sell_fok reported."""
import json

PATH = "weather_live.jsonl"
POS_ID = "26faf747730049eea1bdbd45879e5579"

with open(PATH) as f:
    lines = f.read().splitlines()

changed = False
for i, l in enumerate(lines):
    if not l.strip():
        continue
    r = json.loads(l)
    if r.get("type") == "settle" and r.get("pos_id") == POS_ID:
        true_proceeds = round(7 * 0.09, 2)
        true_cost = 3.50
        true_pnl = round(true_proceeds - true_cost, 2)
        r["sold_shares"] = 7.0
        r["salvage_usd"] = true_proceeds
        r["gross_pnl"] = true_pnl
        r["pnl_usd"] = true_pnl
        r["reason"] += " [corrected 2026-07-29: place_sell_fok's balance-fallback " \
                        "path returned sold=7.0 (matches real on-chain balance) " \
                        "without correspondingly fixing accumulated proceeds, " \
                        "which stayed at a stale $2.61 from mid-loop retries " \
                        "-- real trade was a single clean SELL 7 @ 0.09c, " \
                        "verified via asset-filtered trade history]"
        lines[i] = json.dumps(r)
        changed = True
        print("corrected:", json.dumps(r, indent=1))

if not changed:
    print("no matching record found")
else:
    with open(PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
