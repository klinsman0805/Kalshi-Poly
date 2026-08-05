#!/usr/bin/env python3
"""
scripts/poly_my_orders.py — read-only status board for MANUAL LP quoting.

Answers the four questions you need after placing a pair by hand, none of
which the Polymarket UI shows clearly together:

  1. Are my orders actually EARNING?  Resting inside the reward band is NOT
     enough — an order below the market's `rewards_min_size` scores nothing
     while still carrying full fill risk (the worst of both worlds). And
     min_size CHANGES INTRADAY: observed 2026-08-04, Busan's jumped 20 -> 100
     minutes after a 20-share pair was placed, silently zeroing it. The
     authoritative answer is the CLOB's own `is_order_scoring`, which this
     prints per order as SCORING=True/False.
  2. Where do my bids sit vs the CURRENT band? (mid drifts; a quote that
     qualified an hour ago may be outside now)
  3. Did a leg fill? — held positions, so a filled leg can't go unnoticed.
  4. How much USDC is actually free? Resting BUY orders reserve collateral,
     and on this wallet the live weather bot competes for the same balance.

Places and cancels NOTHING. Run it right after placing a pair, and again
after every re-quote.

Run (droplet):  cd /opt/kalshi-poly && venv/bin/python scripts/poly_my_orders.py
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_PATH = os.getenv("POLY_ENV_PATH", str(REPO / ".env"))

# ── SDK half (must NOT have the repo on sys.path — `polymarket` would bind to
# the repo's own polymarket.py instead of the installed SDK, see
# scripts/poly_sdk_runner.py). Run as a subprocess from a neutral cwd.
_SDK_PROBE = r'''
import asyncio, json
from polymarket.clients import AsyncSecureClient
def _env(n, p=%r):
    for line in open(p):
        if line.startswith(n + "="):
            return line.split("=", 1)[1].strip()
async def main():
    c = await AsyncSecureClient.create(private_key=_env("POLY_PRIVATE_KEY"),
                                       wallet=_env("POLY_FUNDER"))
    bal = await c.get_balance_allowance(asset_type="COLLATERAL")
    out = {"wallet": _env("POLY_FUNDER"), "balance_usd": bal.balance / 1e6, "orders": []}
    async for page in c.list_open_orders():
        for o in (getattr(page, "items", None) or []):
            d = o.model_dump() if hasattr(o, "model_dump") else dict(o)
            oid = d.get("id") or d.get("order_id")
            try:
                scoring = bool(await c.get_order_scoring(order_id=oid))
            except Exception:
                scoring = None
            out["orders"].append({
                "id": oid, "token_id": str(d.get("asset_id") or d.get("token_id") or ""),
                "market": d.get("market"), "outcome": d.get("outcome"),
                "price_c": float(d.get("price") or 0) * 100,
                "size": float(d.get("original_size") or 0),
                "matched": float(d.get("size_matched") or 0),
                "scoring": scoring,
            })
    print(json.dumps(out))
asyncio.run(main())
''' % ENV_PATH


def _sdk_state():
    proc = subprocess.run([sys.executable, "-c", _SDK_PROBE], capture_output=True,
                          text=True, cwd="/tmp", timeout=90)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-400:])
    import json
    return json.loads([l for l in proc.stdout.splitlines() if l.strip()][-1])


def main():
    try:
        state = _sdk_state()
    except Exception as e:  # noqa: BLE001
        print(f"could not read account state: {e}")
        return

    sys.path.insert(0, str(REPO))
    import requests
    from feeds.poly_rewards import fetch_reward_markets, get_twoleg_plan

    orders = state["orders"]
    reserved = sum(o["price_c"] / 100.0 * (o["size"] - o["matched"]) for o in orders)
    print(f"USDC balance : ${state['balance_usd']:.2f}")
    print(f"  reserved by {len(orders)} resting order(s): ${reserved:.2f}")
    print(f"  free        : ${state['balance_usd'] - reserved:.2f}   "
          f"(the live weather bot draws on this same wallet)")
    print()

    if not orders:
        print("no open orders")
    else:
        by_market = {}
        for o in orders:
            by_market.setdefault(o["market"], []).append(o)
        rewards = {m["condition_id"]: m for m in fetch_reward_markets(tag_slug="weather")}
        for cid, group in by_market.items():
            m = rewards.get(cid)
            title = (m or {}).get("question") or cid[:24]
            print(title)
            if m:
                plan = get_twoleg_plan(cid, band_fraction=0.5, market=m)
                v = m["max_spread_c"]
                print(f"  pool ${m['rate_per_day']:.0f}/day | min qualifying size "
                      f"{m['min_size']:.0f} sh | max spread {v}c")
                if plan:
                    print(f"  mids: YES {plan['yes_mid_c']:.1f}c / NO {plan['no_mid_c']:.1f}c "
                          f"-> a fresh pair right now would be YES {plan['yes_bid_c']:.0f}c "
                          f"+ NO {plan['no_bid_c']:.0f}c ({plan['size']:.0f} sh each)")
                for o in group:
                    small = m["min_size"] and o["size"] < m["min_size"]
                    mid = None
                    if plan:
                        mid = plan["yes_mid_c"] if (o["outcome"] or "").lower() == "yes" else plan["no_mid_c"]
                    band = ""
                    if mid is not None:
                        d = abs(o["price_c"] - mid)
                        band = f", {d:.1f}c from mid ({'in' if d <= v else 'OUT OF'} band)"
                    verdict = ("EARNING" if o["scoring"] else
                               "NOT EARNING" + (f" — {o['size']:.0f} sh is below the "
                                                f"{m['min_size']:.0f} sh minimum" if small else ""))
                    print(f"    {(o['outcome'] or '?'):>3} BUY {o['size']:.0f} sh @ "
                          f"{o['price_c']:.0f}c  matched={o['matched']:.0f}{band}")
                    print(f"        -> {verdict}")
            print()

    # held positions — a filled leg must never go unnoticed
    try:
        r = requests.get("https://data-api.polymarket.com/positions",
                         params={"user": state["wallet"], "sizeThreshold": 0.9},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        pos = [p for p in r.json() if not p.get("redeemable")]
    except Exception as e:  # noqa: BLE001
        print(f"(position check failed: {e})")
        return
    live = [p for p in pos if "temperature" in (p.get("title") or "").lower()]
    if live:
        print("open weather positions (a filled leg shows up here):")
        for p in live:
            print(f"  {p.get('outcome'):>3} {float(p.get('size') or 0):.0f} sh @ "
                  f"{float(p.get('avgPrice') or 0) * 100:.1f}c | {(p.get('title') or '')[:58]}")


if __name__ == "__main__":
    main()
