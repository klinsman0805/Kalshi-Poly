from dotenv import load_dotenv; load_dotenv()
from engine import kalshi_get
import json
r = kalshi_get("/portfolio/fills", {"limit": 100})
fills = (r or {}).get("fills") or []
sample = [f for f in fills if "NYC-26JUL27" in (f.get("ticker") or "")]
if sample:
    print("available fields:", sorted(sample[0].keys()))
    print()
for f in sorted(sample, key=lambda x: str(x.get("created_time"))):
    print(json.dumps({k: v for k, v in f.items() if v not in (None, "")}, default=str))
