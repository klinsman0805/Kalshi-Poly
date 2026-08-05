#!/usr/bin/env python3
"""
scripts/place_lp_test_order.py — ONE-OFF manual LP-reward test order.

Places a single small GTC (resting) buy order on the Miami 82-83F low-temp
market (the bucket that actually matches today's real, locked overnight low —
80-81F was the higher-yield candidate but the real low already printed at 82F,
outside that range, so it was quietly a loser). This is a deliberate,
one-time, manually-run script — NOT wired into the always-on scanner/
executor, and it places REAL money on a REAL order if run with DRY_RUN
unset/false.

Market parameters below are a POINT-IN-TIME snapshot (checked 2026-08-01,
~17:xx UTC) — real book at that moment: bid=4c (10 shares deep) / ask=58c
(8 shares deep), a 54c gap. This market resets daily, the token_id/
condition_id/price WILL be stale by the time you run this unless you
re-verify first:
    python -c "from feeds.poly_rewards import fetch_reward_markets as f; \
              m=[x for x in f(tag_slug='weather',min_rate=1.0) if 'Miami' in x['question'] and '82-83' in x['question'] and 'August 1' in x['question']][0]; \
              print(m['condition_id'], [t for t in m['tokens'] if t['outcome']=='Yes'][0]['token_id'], m['min_size'], m['max_spread_c'])"

Fill-risk note — WORSE here than the Buenos Aires attempt: the reward-eligible
band (mid ~31c +/- 4.5c max_spread = ~26.5-35.5c) sits far ABOVE the real best
bid of 4c. Any qualifying price is a large jump above the current market, not
a small one like Buenos Aires's 72->83c — meaning whatever we rest here will
be dramatically the most attractive price for any seller, likely HIGHER fill
risk than last time, not lower. Priced at 27c (the low end of the eligible
band) specifically to minimize that gap as much as the rules allow — it does
not eliminate it.

Placed through place_and_track (modules.poly_rewards_exec), NOT the raw
place_gtc — this records the order so the fill-monitor + stop-loss already
wired into poly-rewards.service (check_fills) can actually watch it. A raw
place_gtc call would place a real order with nothing protecting it.

Run (paper, safe, no real order): DRY_RUN=true python scripts/place_lp_test_order.py
Run for real:                     DRY_RUN=false python scripts/place_lp_test_order.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(override=False)

CONDITION_ID = "0x3439e611a3c7f5acf32088203bcfd9d8a77263af2179a37489f11978fedbe1a2"
TOKEN_ID = "115250899204879099079336398031591860326076622031852511697862076846954380889856"  # Miami 82-83F low YES
PRICE_CENTS = 27.0    # low end of the eligible band (~26.5-35.5c) — REVERIFY, see docstring
SIZE = 20             # the market's stated min_size — anything smaller won't qualify — REVERIFY
CITY, KIND, DATE, STATION = "Miami", "low", "2026-08-01", "KMIA"
NEG_RISK = False       # single-market weather event, not a multi-outcome family


def main():
    import polymarket
    from modules.poly_rewards_exec import place_and_track
    client = polymarket.PolyClient()
    order_id = place_and_track(client, TOKEN_ID, PRICE_CENTS, SIZE, "BUY",
                               CONDITION_ID, CITY, KIND, DATE, STATION, neg_risk=NEG_RISK)
    if order_id:
        print(f"placed + tracked GTC buy: {SIZE} sh @ {PRICE_CENTS}c, order_id={order_id}")
        print("poly-rewards.service will now watch this order for fills every "
              "cycle and stop-loss it if the bucket goes dead (paper-logs the "
              "decision unless POLY_REWARDS_LIVE=true).")
    else:
        print("order was NOT placed — see the error above")


if __name__ == "__main__":
    main()
