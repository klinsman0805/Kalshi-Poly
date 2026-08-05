#!/usr/bin/env python3
"""
scripts/poly_lp_supervisor_report.py — oversight snapshot for the LP executor.

Fetches the exchange's own view (open orders + held positions) and reconciles
it against the ledger, then prints the circuit-breaker state and any open
held legs. This is the part of the daily report that can catch a belief/reality
gap — an order resting on the CLOB that nothing is managing, a leg the ledger
still thinks is resting, or shares no holding explains.

Read-only: fetches, compares, prints. Cancels and sells nothing.

Run (droplet):  cd /opt/kalshi-poly && venv/bin/python scripts/poly_lp_supervisor_report.py
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from modules import poly_rewards_supervisor as sup  # noqa: E402
from modules.poly_rewards_live import _RUNNER, _SDK_PYTHON  # noqa: E402

# The SDK half must run from a neutral cwd so `import polymarket` binds to the
# installed SDK, not this repo's polymarket.py (see scripts/poly_sdk_runner.py).
_PROBE = r'''
import asyncio, json
from decimal import Decimal
from polymarket.clients import AsyncSecureClient
def _env(n, p="/opt/kalshi-poly/.env"):
    for line in open(p):
        if line.startswith(n + "="):
            return line.split("=", 1)[1].strip()
def _plain(v):
    # SDK models carry Decimals and datetimes; JSON needs plain scalars.
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
async def main():
    c = await AsyncSecureClient.create(private_key=_env("POLY_PRIVATE_KEY"),
                                       wallet=_env("POLY_FUNDER"))
    bal = await c.get_balance_allowance(asset_type="COLLATERAL")
    keep = ("id", "asset_id", "market", "outcome", "price",
            "original_size", "size_matched", "status")
    orders = []
    async for page in c.list_open_orders():
        for o in (getattr(page, "items", None) or []):
            d = o.model_dump() if hasattr(o, "model_dump") else dict(o)
            orders.append({k: _plain(v) for k, v in d.items() if k in keep})
    print(json.dumps({"wallet": _env("POLY_FUNDER"),
                      "balance_usd": float(bal.balance) / 1e6, "orders": orders}))
asyncio.run(main())
'''


def _exchange_state():
    proc = subprocess.run([_SDK_PYTHON, "-c", _PROBE], capture_output=True,
                          text=True, cwd="/tmp", timeout=90)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-300:])
    return json.loads([l for l in proc.stdout.splitlines() if l.strip()][-1])


def main():
    records = sup._read_ledger()

    ok, reason = sup.check_breaker(records)
    print("=== circuit breaker ===")
    print(f"  {'OPEN — entries allowed' if ok else 'TRIPPED — no new entries'}: {reason}")
    print()

    holdings = {}
    for r in records:
        if r.get("type") == "twoleg_holding":
            holdings[r["hold_id"]] = r
        elif r.get("type") == "twoleg_holding_closed":
            holdings.pop(r.get("hold_id"), None)
    print("=== open held legs (un-hedged, riding to resolution) ===")
    if not holdings:
        print("  none")
    else:
        for h in holdings.values():
            print(f"  {h.get('city')} {h.get('kind')} {h['side'].upper()} "
                  f"{h['size']:.0f} sh @ {h['price_c']:.0f}c "
                  f"(bucket {h.get('lo')}-{h.get('hi')}{h.get('unit')}, "
                  f"${h['size'] * h['price_c'] / 100:.2f} at risk)")
    print()

    try:
        state = _exchange_state()
    except Exception as e:  # noqa: BLE001
        print(f"=== reconciliation ===\n  could not reach the exchange: {e}")
        return
    try:
        import requests
        r = requests.get("https://data-api.polymarket.com/positions",
                         params={"user": state["wallet"], "sizeThreshold": 0.9},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        positions = [p for p in r.json() if not p.get("redeemable")]
    except Exception as e:  # noqa: BLE001
        print(f"  (position fetch failed, reconciling orders only: {e})")
        positions = []

    result = sup.reconcile(state["orders"], positions, records)
    print("=== reconciliation (exchange truth vs ledger) ===")
    print(f"  USDC ${state['balance_usd']:.2f} | {len(state['orders'])} resting order(s) "
          f"| {len(positions)} open position(s)")
    findings = sup.describe(result)
    if not findings:
        print("  CLEAN — everything the exchange holds is explained by the ledger")
    else:
        for line in findings:
            print(f"  {line}")


if __name__ == "__main__":
    main()
