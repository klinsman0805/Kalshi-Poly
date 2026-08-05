"""Emergency sell for the Shenzhen 28C position — ready to fire on demand.
Sells whatever is actually held (reads real on-chain balance first), via the
same FAK emergency-unwind path used for naked-leg unwinds. Prints proceeds
and updates nothing else — the position's own settle bookkeeping is handled
separately, right after, from the real fill this script reports.
"""
from dotenv import load_dotenv; load_dotenv()
import polymarket

TOKEN = "114146541615504751857344845354063852791907153540194110529623777732711112197027"
NEG_RISK = True

client = polymarket.PolyClient()
held = client._held_shares(TOKEN)
print(f"real held balance: {held}")
if held is None or held < 0.5:
    print("nothing meaningful to sell — aborting")
    raise SystemExit(0)

fee = polymarket.fetch_live_fee_bps(TOKEN) or 0
sold = client.place_sell_fok(TOKEN, held, fee, neg_risk=NEG_RISK)
proceeds = client._last_sell_proceeds_usd
fill_c = client._last_fill_price_cents
print(f"sold: {sold} shares | proceeds: ${proceeds} | avg fill: {fill_c}c")
