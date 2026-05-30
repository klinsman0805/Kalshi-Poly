"""
analyze_basis.py — Historical cross-venue outcome study.

For each recent 15-min window on BTC/ETH/SOL, compare how Kalshi and Polymarket
resolved (Up/Down). Quantifies:
  - agreement rate per asset (how often a hold-to-resolution arb would have worked)
  - strike gap between venues
  - whether DISAGREEMENTS cluster when the final price is near the strike
    (i.e. confirms basis risk concentrates in the "dead zone")
Read-only; no orders.
"""
import sys
from unittest.mock import MagicMock
sys.modules.setdefault("websocket", MagicMock())
from dotenv import load_dotenv
load_dotenv("/Users/klinsman.lau/Kalshi-Poly/.env", override=True)
sys.path.insert(0, "/Users/klinsman.lau/Kalshi-Poly")

import requests
from datetime import datetime, timezone
import engine

KSERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M"}
PSLUG   = {"BTC": "btc", "ETH": "eth", "SOL": "sol"}
LIMIT   = 60  # windows per asset


def kalshi_settled(asset):
    h = engine._auth_headers("GET", "/trade-api/v2/markets")
    out, cursor = [], None
    while len(out) < LIMIT:
        params = {"series_ticker": KSERIES[asset], "status": "settled", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(engine.API_BASE + "/markets", headers=h, params=params, timeout=20)
        j = r.json()
        out += j.get("markets", [])
        cursor = j.get("cursor")
        if not cursor:
            break
    return out[:LIMIT]


def poly_outcome(asset, window_ts):
    slug = f"{PSLUG[asset]}-updown-15m-{window_ts}"
    try:
        ev = requests.get("https://gamma-api.polymarket.com/events",
                          params={"slug": slug}, timeout=10).json()
        if not ev:
            return None
        m = ev[0]["markets"][0]
        if m.get("umaResolutionStatus") != "resolved":
            return None
        # outcomes order is ["Up","Down"]; outcomePrices "1"=winner
        op = m.get("outcomePrices", "")
        return "Up" if op == '["1", "0"]' else ("Down" if op == '["0", "1"]' else None)
    except Exception:
        return None


def run():
    for asset in ["BTC", "ETH", "SOL"]:
        mkts = kalshi_settled(asset)
        rows = []
        for m in mkts:
            try:
                close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            except Exception:
                continue
            window_ts = int(close_dt.timestamp()) - 900
            p_out = poly_outcome(asset, window_ts)
            if p_out is None:
                continue
            k_out = "Up" if m["result"] == "yes" else "Down"
            strike = m.get("floor_strike")
            final  = m.get("expiration_value")
            try:
                margin = abs(float(final) - float(strike)) if (final and strike) else None
            except Exception:
                margin = None
            rows.append((k_out, p_out, strike, final, margin))

        if not rows:
            print(f"{asset}: no matched windows"); continue
        n = len(rows)
        agree = sum(1 for r in rows if r[0] == r[1])
        disagree_rows = [r for r in rows if r[0] != r[1]]
        # margin stats: how close to strike on agree vs disagree
        def mstats(rs):
            ms = [r[4] for r in rs if r[4] is not None]
            if not ms:
                return None
            ms.sort()
            return (min(ms), ms[len(ms)//2], max(ms))
        ag_m = mstats([r for r in rows if r[0] == r[1]])
        dis_m = mstats(disagree_rows)
        # express margin as % of price for comparability
        print(f"\n=== {asset}  (n={n} matched windows) ===")
        print(f"  AGREE: {agree}/{n} = {agree/n*100:.1f}%   DISAGREE: {len(disagree_rows)} ({len(disagree_rows)/n*100:.1f}%)")
        if ag_m:  print(f"  |final-strike| on AGREE   : min={ag_m[0]:.4f} median={ag_m[1]:.4f} max={ag_m[2]:.4f}")
        if dis_m: print(f"  |final-strike| on DISAGREE: min={dis_m[0]:.4f} median={dis_m[1]:.4f} max={dis_m[2]:.4f}")
        if disagree_rows:
            print(f"  disagreement examples (k_out,p_out,strike,final,|gap|):")
            for r in disagree_rows[:5]:
                print(f"    K={r[0]} P={r[1]} strike={r[2]} final={r[3]} gap={r[4]}")


if __name__ == "__main__":
    run()
