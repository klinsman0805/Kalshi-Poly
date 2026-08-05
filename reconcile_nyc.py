"""Reconcile the NYC|2026-07-27|low duplicate-key state left by the pos_id bug.

Does two things:
 1. Stamps both existing "open" records (the dead 69-70F and the live 67-68F)
    with a real pos_id, so the new identity-based code can tell them apart.
 2. Writes a settle record for the dead 69-70F position — it never got a bid
    and never will (bucket lost), so it's a straight write-off. The live
    67-68F position is untouched and left open to settle normally once
    Kalshi finalizes it.

Idempotent: skips if pos_id is already present.
"""
import json, uuid
from datetime import datetime, timezone

PATH = "kalshi_weather_paper.jsonl"

with open(PATH) as f:
    lines = f.read().splitlines()

recs = [json.loads(l) for l in lines]

dead_idx = live_idx = None
for i, r in enumerate(recs):
    if r.get("key") == "NYC|2026-07-27|low" and r.get("type") == "open":
        if r["label"].startswith("69"):
            dead_idx = i
        elif r["label"].startswith("67"):
            live_idx = i

if dead_idx is None or live_idx is None:
    raise SystemExit(f"expected both records, got dead={dead_idx} live={live_idx}")

if "pos_id" in recs[dead_idx] and "pos_id" in recs[live_idx]:
    print("already reconciled — no-op")
    raise SystemExit(0)

dead_pid = uuid.uuid4().hex
live_pid = uuid.uuid4().hex
recs[dead_idx]["pos_id"] = dead_pid
recs[live_idx]["pos_id"] = live_pid

dead = recs[dead_idx]
settle = {
    "type": "settle", "key": dead["key"], "pos_id": dead_pid, "won": False,
    "mode": dead.get("mode"), "closed_early": True, "exit": "DEAD-EXIT",
    "reason": "bucket lost (min hit 68.0F, dawn-set bucket 69-70F dead by "
              "20:13 local) — no bid ever appeared; write-off reconciled "
              "during pos_id migration",
    "salvage_usd": 0.0, "sold_shares": 0.0, "sold_at_c": None,
    "gross_pnl": -dead["cost_usd"], "fee_usd": 0.0, "pnl_usd": -dead["cost_usd"],
    "settled": datetime.now(timezone.utc).isoformat(),
}

lines[dead_idx] = json.dumps(recs[dead_idx])
lines[live_idx] = json.dumps(recs[live_idx])
lines.append(json.dumps(settle))

with open(PATH, "w") as f:
    f.write("\n".join(lines) + "\n")

print(f"dead  (69-70F) pos_id={dead_pid}  settled pnl={-dead['cost_usd']:+.2f}")
print(f"live  (67-68F) pos_id={live_pid}  left open")
