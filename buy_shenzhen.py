"""Manual re-entry into Shenzhen 28C — same token, same normal stake as the
original entry ($3.70ish). FOK at the live ask, all-or-nothing (this is a
fresh entry decision, not an emergency unwind, so FOK is correct)."""
from dotenv import load_dotenv; load_dotenv()
import polymarket

TOKEN = "114146541615504751857344845354063852791907153540194110529623777732711112197027"
NEG_RISK = True
STAKE_USD = 3.70

client = polymarket.PolyClient()
ask_c = int(round(70))  # will be overridden by live check below

import requests
r = requests.get("http://localhost:5001/api/weather", timeout=15).json()
row = next((x for x in r.get("rows") or []
            if x.get("city") == "Shenzhen" and x.get("kind") == "high" and x.get("is_today")), None)
bucket = next((b for b in (row or {}).get("buckets", []) if b.get("label") == "28°C"), {})
live_ask = bucket.get("ask_c")
model_p = bucket.get("p")
print(f"live ask: {live_ask}c | model p: {model_p}")
if live_ask is None or live_ask <= 0 or live_ask > 60:
    print("ask missing or too high (>60c) -- aborting, re-check before buying")
    raise SystemExit(0)

shares = max(1, round(STAKE_USD / (live_ask / 100.0)))
fee = polymarket.fetch_live_fee_bps(TOKEN) or 0
filled = client.place_fok(TOKEN, live_ask, float(shares), fee, neg_risk=NEG_RISK)
fill_c = client._last_fill_price_cents
print(f"filled: {filled} shares @ {fill_c}c | cost ~${(filled or 0) * (fill_c or 0) / 100.0:.2f}")
