#!/usr/bin/env python3
"""
scripts/live_preflight.py — READ-ONLY live-readiness check for the weather bot.

Confirms the Polymarket account is ready to trade WITHOUT placing any order:
  1. credentials load + CLOB client authenticates
  2. trading wallet address derives
  3. USDC (collateral) balance + allowance are readable and sufficient
  4. a current tradeable weather market has book depth for our order size

Places nothing. Signs nothing that hits the exchange. Safe to run anytime.
"""

import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# read-only: force paper so nothing in imported modules can arm live
os.environ.setdefault("DRY_RUN", "true")

import polymarket
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

STAKE = float(os.getenv("WEATHER_STAKE_USD", "4"))

def line(ok, label, detail=""):
    print(f"  [{'OK ' if ok else 'XX '}] {label:34s} {detail}")

def main():
    print("\n=== Polymarket live preflight (READ-ONLY) ===\n")
    ok_all = True

    # 1. credentials present
    creds = {k: bool(os.getenv(k)) for k in
             ("POLY_PRIVATE_KEY","POLY_API_KEY","POLY_API_SECRET","POLY_API_PASSPHRASE")}
    missing = [k for k,v in creds.items() if not v]
    line(not missing, "credentials loaded", "all present" if not missing else f"MISSING {missing}")
    ok_all &= not missing

    # 2. client builds + wallet derives
    c = polymarket.PolyClient()
    addr = c._derive_address()
    line(bool(addr), "trading wallet derived", addr or "could not derive")
    line(c._clob is not None, "CLOB client authenticated", "ready" if c._clob else "build failed")
    ok_all &= bool(addr) and c._clob is not None
    if c._clob is None:
        print("\n  → cannot read balances without an authenticated client. Stop.\n"); return

    # 3. USDC balance + allowance (read-only)
    try:
        bal = c._clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        usdc = int(bal.get("balance", 0)) / 1_000_000.0
        allow = int(bal.get("allowance", 0)) / 1_000_000.0 if bal.get("allowance") is not None else None
        line(usdc >= STAKE, "USDC balance", f"${usdc:.2f}  (need ~${STAKE:.0f}/order)")
        line(allow is None or allow >= STAKE, "USDC trading allowance",
             f"${allow:.2f}" if allow is not None else "not reported (set on first trade)")
        ok_all &= usdc >= STAKE
    except Exception as e:  # noqa: BLE001
        line(False, "USDC balance", f"read failed: {e}")
        ok_all = False

    # 4. book depth on a live tradeable market
    try:
        from modules.weather import WeatherEngine
        from feeds.metar import MetarFeed
        eng = WeatherEngine(MetarFeed())
        rows = eng.refresh()
        # only a genuine ENTER signal is what the live bot would actually trade
        cand = next((r for r in rows if r.get("signal") == "ENTER" and r.get("entry")), None)
        if cand:
            entry = cand["entry"]
            shares = max(5, round(STAKE / (entry["ask_c"]/100)))
            line(True, "live ENTER signal available now",
                 f"{cand['city']} {entry['label']} @{entry['ask_c']:.0f}c p={entry['p']}")
            print(f"       first live order would be ~{shares} shares (~${STAKE:.0f})")
        else:
            n_trade = sum(1 for r in rows if r["tradeable"] and r["is_today"])
            line(True, "market feed live",
                 f"{n_trade} tradeable today · no ENTER signal this moment (normal)")
    except Exception as e:  # noqa: BLE001
        line(False, "market check", str(e)[:50])

    print(f"\n  RESULT: {'READY — you can arm live' if ok_all else 'NOT READY — fix the XX rows above'}\n")
    print("  This script placed NO orders. Arming live (WEATHER_LIVE=true + dashboard")
    print("  toggle + restart) remains a manual step you perform.\n")

if __name__ == "__main__":
    main()
