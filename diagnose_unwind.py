"""
diagnose_unwind.py — read-only forensic for the 2026-06-02 naked-leg incident.

Answers three questions before we change unwind code:
  1. Did the Polymarket buy actually settle on-chain? (transactionHash present)
  2. Where did the 5.065 shares end up — funder address, or somewhere else?
  3. Does the signer derived from POLY_PRIVATE_KEY match POLY_FUNDER's
     configured proxy? (Mismatch = sells signed for an empty wallet.)

Reads only. Never places orders. Masks the private key.
"""
import os, sys, requests, json
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

TOKEN     = "18608600788269925019302166552813534397283701878951386923741501984569188757470"
TS_UTC    = datetime(2026, 6, 2, 15, 21, 56, tzinfo=timezone.utc)
WINDOW    = timedelta(minutes=10)

funder    = os.getenv("POLY_FUNDER", "")
pk        = os.getenv("POLY_PRIVATE_KEY", "")
sig_type  = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

if not funder:
    print("POLY_FUNDER not set in .env — abort"); sys.exit(1)
if not pk:
    print("POLY_PRIVATE_KEY not set in .env — abort"); sys.exit(1)

# ── 1. Signer address derivation ───────────────────────────────────────────
try:
    from eth_account import Account
    signer = Account.from_key(pk).address
except Exception as e:
    print(f"Could not derive signer address: {e}"); sys.exit(1)

print("=" * 72)
print("CONFIG")
print("=" * 72)
print(f"POLY_FUNDER         : {funder}")
print(f"Signer (from PK)    : {signer}")
print(f"POLY_SIGNATURE_TYPE : {sig_type}  (1=Email/Magic proxy, 2=Browser Safe)")
if sig_type == 1:
    print("  → For sigType=1, funder MUST be the Magic/email proxy address,")
    print("    NOT the signer. Positions land in funder. Signer just signs.")
    if signer.lower() == funder.lower():
        print("  ⚠  signer == funder. For sigType=1 these should DIFFER.")
print()

# ── 2. Current position on funder ──────────────────────────────────────────
print("=" * 72)
print("CURRENT POSITION on POLY_FUNDER for token …757470")
print("=" * 72)
r = requests.get("https://data-api.polymarket.com/positions",
                 params={"user": funder, "sizeThreshold": 0}, timeout=20)
hit = None
for p in r.json():
    if str(p.get("asset", "")) == TOKEN:
        hit = p
        break
if hit:
    print(f"  size      : {hit.get('size')}")
    print(f"  avgPrice  : {hit.get('avgPrice')}")
    print(f"  realizedPnl: {hit.get('realizedPnl')}")
    print(f"  cashPnl   : {hit.get('cashPnl')}")
    print(f"  title     : {hit.get('title', '')[:60]}")
else:
    print("  No open position found on funder for this token.")
    print("  (You said you manually closed it — this is expected.)")
print()

# ── 3. Activity around the failure window ──────────────────────────────────
print("=" * 72)
print(f"ACTIVITY on POLY_FUNDER around {TS_UTC.isoformat()} (±10 min)")
print("=" * 72)
r = requests.get("https://data-api.polymarket.com/activity",
                 params={"user": funder, "limit": 500}, timeout=20)
rows = r.json()
hits = []
for a in rows:
    if str(a.get("asset", "")) != TOKEN:
        continue
    ts = a.get("timestamp")
    if ts is None:
        continue
    t = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    if abs(t - TS_UTC) <= WINDOW:
        hits.append((t, a))

if not hits:
    print(f"  ⚠  No activity rows for this token in ±10 min of failure.")
    print(f"     If empty even after manual close, double-check the funder")
    print(f"     address — the position may be on a different wallet.")
else:
    for t, a in sorted(hits):
        print(f"  [{t.isoformat()}]  {a.get('type'):<7} {a.get('side','-'):<4} "
              f"size={a.get('size')} price={a.get('price')} "
              f"usdc={a.get('usdcSize')}")
        txh = a.get("transactionHash") or a.get("transaction_hash")
        print(f"     txHash   : {txh or '⚠ NONE — buy did not settle on-chain'}")
        print(f"     proxyWallet (from row): {a.get('proxyWallet','-')}")
print()

# ── 4. Wallet-address cross-check ──────────────────────────────────────────
print("=" * 72)
print("DIAGNOSIS HINTS")
print("=" * 72)
print("If the activity row above shows a transactionHash AND proxyWallet")
print("matches POLY_FUNDER above → the buy settled, position was on the")
print("right wallet, and the sell-side balance check failed transiently.")
print("Verdict: race condition. A balance-checked retry in place_sell_fok")
print("is justified.")
print()
print("If proxyWallet in the activity row DIFFERS from POLY_FUNDER →")
print("the buy went to a different proxy than the sell signs against.")
print("Verdict: config drift. Fix POLY_FUNDER, do NOT add a retry.")
print()
print("If the activity row has no transactionHash → buy never settled.")
print("Verdict: matching-engine glitch. Halt-on-failure is correct as-is.")
