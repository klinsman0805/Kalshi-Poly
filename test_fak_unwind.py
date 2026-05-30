"""
test_fak_unwind.py — Real test of the FAK unwind path (the leg that lost $2.60).

The 2026-05-29 loss happened because the emergency unwind used FOK (all-or-
nothing) and sold ZERO, leaving a naked Poly position that rode to a losing
resolution. We changed it to FAK (fill-and-kill, takes whatever's there). This
script proves the new path actually sells a real position back.

Flow (only with --fire):
  1. BUY a small Poly position (FOK) to create something to unwind.
  2. Immediately call place_sell_fok() — the FAK unwind — to sell it back.
  3. Query positions before/after so you can see it flatten.

Without --fire it only previews sizes/prices (no orders). This BUYS then SELLS
the same token, so net market exposure is brief (seconds) and small.

Usage:
    python test_fak_unwind.py            # preview only
    python test_fak_unwind.py --fire     # real buy + real FAK unwind
"""
import sys
import time
from unittest.mock import MagicMock
sys.modules.setdefault("websocket", MagicMock())

from dotenv import load_dotenv
load_dotenv("/Users/klinsman.lau/Kalshi-Poly/.env", override=True)
sys.path.insert(0, "/Users/klinsman.lau/Kalshi-Poly")

import requests
import arb_trader
import polymarket

FIRE = "--fire" in sys.argv
SIZE = arb_trader.ARB_TRADE_SIZE
TOLERANCE = arb_trader.ARB_TOLERANCE
FUNDER = polymarket.os.getenv("POLY_FUNDER", "")


def poly_position(token_id):
    """Return current held shares of token_id on the funder wallet."""
    r = requests.get("https://data-api.polymarket.com/positions",
                     params={"user": FUNDER}, timeout=8)
    for p in r.json():
        if str(p.get("asset", "")) == str(token_id):
            return float(p.get("size", 0) or 0)
    return 0.0


def main():
    c = polymarket.PolyClient()
    now = int(time.time()); ws = now - (now % 900)
    info = None
    for w in [ws, ws + 900, ws - 900, ws + 1800]:
        info = polymarket.get_market_for_window("BTC", w)
        if info and info.accepting_orders:
            break
    if not info:
        print("no accepting Poly market found"); return

    # Pick whichever side (YES/NO) clears Polymarket's $1 min at SIZE shares,
    # i.e. ask ≥ $1/SIZE. With SIZE=5 that's ask ≥ 20¢. The opposite outcome of
    # a cheap token is the expensive one, so one side virtually always qualifies.
    def book_for(tok):
        bk = requests.get(f"{polymarket.POLY_REST}/book",
                          params={"token_id": tok}, timeout=5).json()
        asks = bk.get("asks") or []
        bids = bk.get("bids") or []
        a = round(float(asks[-1]["price"]) * 100) if asks else None
        b = round(float(bids[-1]["price"]) * 100) if bids else None
        return a, b

    min_ask = int(arb_trader.ARB_POLY_MIN_ORDER_USD / SIZE * 100 + 0.999)  # ceil cents
    token = None
    for cand, label in [(info.yes_token_id, "YES"), (info.no_token_id, "NO")]:
        a, b = book_for(cand)
        if a is not None and b is not None and a >= min_ask:
            token, best_ask, best_bid, side_label = cand, a, b, label
            break
    if token is None:
        print(f"neither side priced ≥ {min_ask}c to clear ${arb_trader.ARB_POLY_MIN_ORDER_USD} "
              f"min at size {SIZE} — try a more balanced window"); return
    print(f"using {side_label} token=...{token[-8:]}  best_bid={best_bid}c  best_ask={best_ask}c")

    buy_price = min(best_ask + TOLERANCE, 99)
    buy_usd = SIZE * buy_price / 100.0
    print(f"\nSTEP 1 — BUY {SIZE} @ {buy_price}c  (~${buy_usd:.2f})")
    print(f"STEP 2 — FAK unwind: SELL @ 1c limit (takes any bid, ~{best_bid}c)")
    print(f"Round-trip cost ≈ spread+fees on {SIZE} shares "
          f"(~{(buy_price - best_bid)} c/share + fees)")

    if not FIRE:
        print("\n[preview only — pass --fire to actually buy then unwind]")
        return

    polymarket.DRY_RUN = False

    print("\n--- position BEFORE:", poly_position(token), "---")
    bought = c.place_fok(token, buy_price, SIZE, info.fee_bps)
    print(f"STEP 1 RESULT: bought={bought}")
    if bought == 0:
        print("buy didn't fill — nothing to unwind; rerun in a more liquid window")
        return

    time.sleep(1.0)  # let the fill settle on the position endpoint
    held = poly_position(token)
    print(f"--- position AFTER BUY: {held} ---")

    print("\nSTEP 2 — calling place_sell_fok (the FAK unwind)...")
    sold = c.place_sell_fok(token, bought, info.fee_bps)
    print(f"STEP 2 RESULT: sold={sold} of {bought}")

    time.sleep(1.0)
    final = poly_position(token)
    print(f"\n--- position AFTER UNWIND: {final} ---")
    remaining = bought - sold
    if remaining <= arb_trader.ARB_DUST_SHARES:
        print(f"✅ FAK UNWIND WORKS — sold {sold}, {remaining:.2f} dust remaining (within tolerance)")
    else:
        print(f"❌ UNWIND INCOMPLETE — {remaining:.2f} shares still held (would trip kill-switch)")


if __name__ == "__main__":
    print(f"FIRE={'YES (real orders!)' if FIRE else 'no (preview)'}  "
          f"size={SIZE}  tolerance={TOLERANCE}c  dust_tol={arb_trader.ARB_DUST_SHARES}")
    main()
