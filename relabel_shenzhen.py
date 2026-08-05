"""Backfill the relabeling fix onto the one historical record it affects.
Idempotent: no-op if already relabeled."""
import json

PATH = "weather_live.jsonl"
with open(PATH) as f:
    lines = f.read().splitlines()

changed = False
for i, l in enumerate(lines):
    if not l.strip():
        continue
    r = json.loads(l)
    if r.get("type") == "settle" and r.get("key") == "Shenzhen|2026-07-28|high" \
            and r.get("exit") == "MANUAL-CLOSE":
        r["exit"] = "TAKE-PROFIT"
        r["reason"] = ("reconciled from real Polymarket trade history — sold at "
                        "96c, at/above our 90c take-profit threshold; almost "
                        "certainly our own order whose settle record failed to "
                        "write, not a manual close (relabeled 2026-07-28 after "
                        "investigation)")
        lines[i] = json.dumps(r)
        changed = True
        print("relabeled:", r["key"], "->", r["exit"])

if not changed:
    print("no matching record found — already relabeled or record missing")
else:
    with open(PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
