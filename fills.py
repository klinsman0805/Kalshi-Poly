from dotenv import load_dotenv; load_dotenv()
from engine import kalshi_get
r = kalshi_get("/portfolio/fills", {"limit": 100})
fills = (r or {}).get("fills") or []
tot = {}
print("%-34s %-6s %-6s %8s %8s  %s" % ("ticker","side","action","count","price_c","time"))
for f in sorted(fills, key=lambda x: str(x.get("created_time"))):
    t = f.get("ticker") or ""
    if "NYC-26JUL27" not in t: continue
    cnt = f.get("count"); px = f.get("yes_price") or f.get("price")
    print("%-34s %-6s %-6s %8s %8s  %s" % (t, f.get("side"), f.get("action"), cnt, px, str(f.get("created_time"))[:19]))
    k = (t, f.get("action"))
    tot[k] = tot.get(k, 0) + (cnt or 0)
print()
for k, v in sorted(tot.items()):
    print("   total %-34s %-5s = %s" % (k[0], k[1], v))
