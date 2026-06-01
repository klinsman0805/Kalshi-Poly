"""
pnl_report.py — TRUE realized P&L per arb, reconciled from BOTH venues' APIs.

The bot's trades.jsonl logs `net_est` = the GROSS intended edge at detection
(pre-fee, pre-slippage, assumes a perfect equal-share hedge). It is NOT realized
P&L. This report ignores net_est and instead pulls the actual money from each
venue:

  Kalshi  : /portfolio/fills (real fill price + fee_cost) + /portfolio/settlements (payout)
  Polymarket: data-api /activity  TRADE usdcSize (cost) + REDEEM usdcSize (payout)

For each arb entry it shows: real Kalshi cost/fee/payout, real Poly cost/payout,
realized net, the logged net_est for comparison, and the gap (slippage+fees+
overfill). Read-only.

Usage: python pnl_report.py [YYYY-MM-DD]   (default: all entries in trades.jsonl)
"""
import sys, json
from unittest.mock import MagicMock
sys.modules.setdefault("websocket", MagicMock())
from dotenv import load_dotenv
load_dotenv("/Users/klinsman.lau/Kalshi-Poly/.env", override=True)
sys.path.insert(0, "/Users/klinsman.lau/Kalshi-Poly")

import os
import requests
from datetime import datetime, timezone
import engine

FUNDER = os.getenv("POLY_FUNDER", "")
DATE_FILTER = sys.argv[1] if len(sys.argv) > 1 else None


# ── Kalshi ──────────────────────────────────────────────────────────────────
def kalshi_fills_by_ticker():
    """All taker buy fills, summed per ticker: (total_cost_$, total_fee_$, count)."""
    h = engine._auth_headers("GET", "/trade-api/v2/portfolio/fills")
    out, cursor = {}, None
    for _ in range(20):
        p = {"limit": 1000}
        if cursor: p["cursor"] = cursor
        j = requests.get(engine.API_BASE + "/portfolio/fills", headers=h,
                         params=p, timeout=20).json()
        for f in j.get("fills", []):
            if f.get("action") != "buy":
                continue
            tk = f.get("ticker") or f.get("market_ticker")
            cnt = float(f.get("count_fp", 0) or 0)
            side = f.get("side")
            px = float(f.get("yes_price_dollars") if side == "yes"
                       else f.get("no_price_dollars") or 0)
            fee = float(f.get("fee_cost", 0) or 0) / 100.0  # fee_cost is in cents
            e = out.setdefault(tk, {"cost": 0.0, "fee": 0.0, "count": 0.0, "side": side})
            e["cost"] += px * cnt
            e["fee"]  += fee
            e["count"] += cnt
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def kalshi_settlements_by_ticker():
    h = engine._auth_headers("GET", "/trade-api/v2/portfolio/settlements")
    out = {}
    cursor = None
    for _ in range(20):
        p = {"limit": 1000}
        if cursor: p["cursor"] = cursor
        j = requests.get(engine.API_BASE + "/portfolio/settlements", headers=h,
                         params=p, timeout=20).json()
        for s in j.get("settlements", []):
            out[s["ticker"]] = float(s.get("revenue", 0)) / 100.0
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


# ── Polymarket ──────────────────────────────────────────────────────────────
def poly_activity():
    """Per conditionId: total TRADE-buy cost and total REDEEM payout."""
    out = {}
    r = requests.get("https://data-api.polymarket.com/activity",
                     params={"user": FUNDER, "limit": 500}, timeout=20)
    for a in r.json():
        cid = a.get("conditionId")
        if not cid:
            continue
        e = out.setdefault(cid, {"cost": 0.0, "payout": 0.0, "title": a.get("title", "")})
        if a.get("type") == "TRADE" and a.get("side") == "BUY":
            e["cost"] += float(a.get("usdcSize", 0) or 0)
        elif a.get("type") == "REDEEM":
            e["payout"] += float(a.get("usdcSize", 0) or 0)
    return out


def run():
    print("Loading venue data…")
    kf = kalshi_fills_by_ticker()
    ks = kalshi_settlements_by_ticker()
    pa = poly_activity()
    # build a slug->conditionId map from poly activity titles isn't reliable;
    # instead match each entry's poly leg by conditionId via the activity 'asset'
    # field (token id). Build token->cid from a second pass:
    tok2cid = {}
    r = requests.get("https://data-api.polymarket.com/activity",
                     params={"user": FUNDER, "limit": 500}, timeout=20)
    for a in r.json():
        if a.get("asset") and a.get("conditionId"):
            tok2cid[str(a["asset"])] = a["conditionId"]

    rows = [json.loads(l) for l in open("trades.jsonl")]
    entries = [r for r in rows if r.get("type") == "arb_entry"]
    if DATE_FILTER:
        entries = [r for r in entries if r.get("ts", "").startswith(DATE_FILTER)]

    print(f"\n{'WINDOW':<26} {'K cost':>7} {'K fee':>6} {'K pay':>6} "
          f"{'P cost':>7} {'P pay':>7} {'REALIZED':>9} {'net_est':>8} {'gap':>7}")
    print("-" * 96)
    tot_real = tot_est = 0.0
    matched = 0
    for e in entries:
        tk = e.get("kalshi_ticker")
        k = kf.get(tk)
        kpay = ks.get(tk)
        tok = str(e.get("poly_token_id", ""))
        cid = tok2cid.get(tok)
        p = pa.get(cid) if cid else None
        if not k or kpay is None or not p:
            continue  # only show fully-reconcilable arbs
        matched += 1
        realized = (kpay - k["cost"] - k["fee"]) + (p["payout"] - p["cost"])
        # logged net_est is cents/contract over ~5 pairs
        est = (e.get("net_profit_per_contract") or 0) / 100.0 * (e.get("count") or 5)
        gap = realized - est
        tot_real += realized
        tot_est += est
        label = tk.replace("KX", "").replace("15M-", " ")[:25]
        print(f"{label:<26} {k['cost']:>7.2f} {k['fee']:>6.2f} {kpay:>6.2f} "
              f"{p['cost']:>7.2f} {p['payout']:>7.2f} {realized:>+9.2f} "
              f"{est:>+8.2f} {gap:>+7.2f}")
    print("-" * 96)
    print(f"{'TOTAL (' + str(matched) + ' arbs)':<26} "
          f"{'':>7} {'':>6} {'':>6} {'':>7} {'':>7} "
          f"{tot_real:>+9.2f} {tot_est:>+8.2f} {tot_real-tot_est:>+7.2f}")
    print(f"\nREALIZED net P&L: ${tot_real:+.2f}   (logged net_est said ${tot_est:+.2f})")
    print(f"Gap (slippage + fees + overfill tail): ${tot_real-tot_est:+.2f}")


if __name__ == "__main__":
    run()
