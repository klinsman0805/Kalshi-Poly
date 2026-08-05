from dotenv import load_dotenv; load_dotenv()
from engine import kalshi_get
import json
r = kalshi_get("/portfolio/positions", {"limit": 50})
mp = (r or {}).get("market_positions") or []
print("REAL Kalshi positions still held:")
for p in mp:
    q = p.get("position") or p.get("position_fp")
    if q in (None, 0, "0", "0.00"): continue
    print("   %-34s qty=%-8s exposure=%s" % (p.get("ticker"), q, p.get("market_exposure")))
