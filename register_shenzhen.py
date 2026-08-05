"""Register the manually-bought Shenzhen re-entry into the bot's own ledger,
so take-profit / dead-exit / maximize-confirmed-win protect it going forward.
Idempotent-ish: only appends if no open record for this pos already exists."""
import json, uuid
from datetime import datetime, timezone

PATH = "weather_live.jsonl"
KEY = "Shenzhen|2026-07-29|high"

with open(PATH) as f:
    lines = f.read().splitlines()

# guard: is there already an unsettled open for this key from THIS re-entry
# (i.e. opened after the manual sell)?
opens, settles = {}, set()
for l in lines:
    if not l.strip():
        continue
    r = json.loads(l)
    if r.get("type") == "open":
        opens[r.get("pos_id") or r["key"]] = r
    elif r.get("type") == "settle":
        settles.add(r.get("pos_id") or r["key"])
still_open = [pid for pid, o in opens.items() if o["key"] == KEY and pid not in settles]
if still_open:
    print("already an unsettled open record for this key:", still_open, "-- not adding a duplicate")
    raise SystemExit(0)

pos_id = uuid.uuid4().hex
rec = {
    "type": "open", "pos_id": pos_id, "key": KEY, "mode": "live", "kind": "high",
    "city": "Shenzhen", "date": "2026-07-29", "station": "ZGSZ",
    "label": "28°C",
    "condition_id": "0xb01f80e14087a29fc56a988d919105726f9da9660efeec35c375890d323eed31",
    "token_yes": "114146541615504751857344845354063852791907153540194110529623777732711112197027",
    "slug": "highest-temperature-in-shenzhen-on-july-29-2026",
    "lo": 28, "hi": 28, "neg_risk": True,
    "entry_c": 50.0, "shares": 7.0, "cost_usd": 3.50,
    "model_p": 0.9765, "edge_c": 47.7,
    "opened": datetime.now(timezone.utc).isoformat(),
    "decline_at_entry": 0.0, "run_ext": 28.0, "ob_at_entry": 28.0,
    "_manual_reentry": True,
}
lines.append(json.dumps(rec))
with open(PATH, "w") as f:
    f.write("\n".join(lines) + "\n")
print("registered:", pos_id)
