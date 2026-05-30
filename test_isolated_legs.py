"""
test_isolated_legs.py — Isolated single-leg fill test (REAL money, tiny size).

Verifies each venue's order leg fills at the slippage-buffered price, WITHOUT
pairing them — so there is no arb and no naked-hedge risk. Each leg leaves at
most a tiny standalone position you can flatten manually.

Tests:
  1. Kalshi IOC buy at (ask + ARB_KALSHI_SLIPPAGE) — confirms the slippage fix
     makes the second leg actually cross.
  2. Polymarket FOK buy at (ask + ARB_TOLERANCE) — confirms Poly buys fill.

Usage:
    python test_isolated_legs.py kalshi   # test Kalshi leg only
    python test_isolated_legs.py poly      # test Poly leg only
    python test_isolated_legs.py both      # both (default)

Nothing fires unless you pass --fire. Without it, it only PRICES the orders and
shows what it WOULD send (a dry preview), so you can eyeball before committing.
"""
import sys
import time
from unittest.mock import MagicMock
sys.modules.setdefault("websocket", MagicMock())

from dotenv import load_dotenv
load_dotenv("/Users/klinsman.lau/Kalshi-Poly/.env", override=True)
sys.path.insert(0, "/Users/klinsman.lau/Kalshi-Poly")

import engine
import arb_trader
import polymarket

FIRE = "--fire" in sys.argv
KALSHI_SLIPPAGE = arb_trader.ARB_KALSHI_SLIPPAGE
POLY_TOLERANCE = arb_trader.ARB_TOLERANCE


def log(icon, msg):
    print(f"{icon} {msg}")


def test_kalshi():
    print("\n=== KALSHI LEG TEST ===")
    mkt = engine.discover_market("BTC", on_log=lambda i, m: None)
    if not mkt:
        print("  could not discover BTC market"); return
    yes_ask = mkt["yes_ask"]
    no_ask = mkt["no_ask"]
    print(f"  ticker={mkt['ticker']}  YES_ask={yes_ask}c  NO_ask={no_ask}c  secs_left={mkt['secs_left']}")
    # Pick the cheaper side to minimize cost; buy 1 contract.
    side = "yes" if (yes_ask or 99) <= (no_ask or 99) else "no"
    ask = yes_ask if side == "yes" else no_ask
    if ask is None:
        print("  no ask available — skip"); return
    buy_price = min(ask + KALSHI_SLIPPAGE, 99)
    print(f"  WOULD BUY {side.upper()} 1 contract @ {buy_price}c "
          f"(ask {ask}c + {KALSHI_SLIPPAGE}c slippage)  cost≈${buy_price/100:.2f}")
    if not FIRE:
        print("  [preview only — pass --fire to actually place]"); return

    # Build a minimal snapshot (only .ticker is used by _kalshi_ioc).
    snap = MagicMock(); snap.ticker = mkt["ticker"]
    filled = arb_trader._kalshi_ioc(snap, side, buy_price, 1, timeout=3.0)
    print(f"  RESULT: filled={filled}  "
          f"{'✅ CROSSED (slippage works)' if filled > 0 else '❌ no fill (ask moved further or thin)'}")


def test_poly():
    print("\n=== POLYMARKET LEG TEST ===")
    c = polymarket.PolyClient()
    now = int(time.time()); ws = now - (now % 900)
    info = None
    for w in [ws, ws + 900, ws - 900, ws + 1800]:
        info = polymarket.get_market_for_window("BTC", w)
        if info and info.accepting_orders:
            break
    if not info:
        print("  no accepting Poly market found"); return
    import requests
    bk = requests.get(f"{polymarket.POLY_REST}/book",
                      params={"token_id": info.yes_token_id}, timeout=5).json()
    asks = bk.get("asks") or []
    best_ask = float(asks[-1]["price"]) if asks else None
    if best_ask is None:
        print("  no Poly asks — skip"); return
    ask_c = round(best_ask * 100)
    buy_price = min(ask_c + POLY_TOLERANCE, 99)
    size = arb_trader.ARB_TRADE_SIZE
    order_usd = size * buy_price / 100.0
    print(f"  YES token best_ask={ask_c}c  WOULD BUY {size} @ {buy_price}c  order≈${order_usd:.2f}")
    if order_usd < arb_trader.ARB_POLY_MIN_ORDER_USD:
        print(f"  ⚠ below ${arb_trader.ARB_POLY_MIN_ORDER_USD} Poly min — would be skipped by the gate")
    if not FIRE:
        print("  [preview only — pass --fire to actually place]"); return

    polymarket.DRY_RUN = False
    filled = c.place_fok(info.yes_token_id, buy_price, size, info.fee_bps)
    print(f"  RESULT: filled={filled}  "
          f"{'✅ FILLED' if filled > 0 else '❌ no fill (FOK killed — book moved/thin)'}")


if __name__ == "__main__":
    which = next((a for a in sys.argv[1:] if not a.startswith("--")), "both")
    print(f"FIRE={'YES (real orders!)' if FIRE else 'no (preview only)'}  "
          f"kalshi_slippage={KALSHI_SLIPPAGE}c  poly_tolerance={POLY_TOLERANCE}c")
    if which in ("kalshi", "both"):
        test_kalshi()
    if which in ("poly", "both"):
        test_poly()
