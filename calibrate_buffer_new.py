"""
calibrate_buffer_new.py — Basis-risk study for the candidate new assets
(XRP, DOGE, BNB). Same methodology as calibrate_buffer.py: pull settled
Kalshi windows, match Polymarket outcomes, measure venue-agreement rate and
how a strike-distance buffer affects it. Read-only.
"""
import sys
from unittest.mock import MagicMock
sys.modules.setdefault("websocket", MagicMock())
from dotenv import load_dotenv
load_dotenv("/Users/klinsman.lau/Kalshi-Poly/.env", override=True)
sys.path.insert(0, "/Users/klinsman.lau/Kalshi-Poly")

import requests
from datetime import datetime
import engine

KSERIES = {"XRP": "KXXRP15M", "DOGE": "KXDOGE15M", "BNB": "KXBNB15M"}
PSLUG   = {"XRP": "xrp", "DOGE": "doge", "BNB": "bnb"}
REF     = {"XRP": 1.33, "DOGE": 0.0994, "BNB": 716.0}
N_WINDOWS = 200
WIN_C, LOSS_C = 8.0, -86.0


def kalshi_settled(asset, n):
    h = engine._auth_headers("GET", "/trade-api/v2/markets")
    out, cursor = [], None
    while len(out) < n:
        p = {"series_ticker": KSERIES[asset], "status": "settled", "limit": 1000}
        if cursor: p["cursor"] = cursor
        j = requests.get(engine.API_BASE + "/markets", headers=h, params=p, timeout=20).json()
        ms = j.get("markets", [])
        out += ms
        cursor = j.get("cursor")
        if not cursor or not ms: break
    return out[:n]


def poly_outcome(asset, window_ts):
    slug = f"{PSLUG[asset]}-updown-15m-{window_ts}"
    try:
        ev = requests.get("https://gamma-api.polymarket.com/events",
                          params={"slug": slug}, timeout=10).json()
        if not ev: return None
        m = ev[0]["markets"][0]
        if m.get("umaResolutionStatus") != "resolved": return None
        op = m.get("outcomePrices", "")
        return "Up" if op == '["1", "0"]' else ("Down" if op == '["0", "1"]' else None)
    except Exception:
        return None


def run():
    for asset in ["XRP", "DOGE", "BNB"]:
        mkts = kalshi_settled(asset, N_WINDOWS)
        rows = []
        for m in mkts:
            try:
                close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            except Exception:
                continue
            wts = int(close_dt.timestamp()) - 900
            p_out = poly_outcome(asset, wts)
            if p_out is None:
                continue
            k_out = "Up" if m["result"] == "yes" else "Down"
            try:
                margin = abs(float(m["expiration_value"]) - float(m["floor_strike"]))
            except Exception:
                continue
            rows.append((k_out == p_out, margin))

        if not rows:
            print(f"\n{asset}: no matched windows (Poly may not have history)"); continue
        n = len(rows)
        base = sum(1 for a, _ in rows if a) / n
        ref = REF[asset]
        print(f"\n=== {asset}  (n={n})  baseline agree={base*100:.1f}%  "
              f"EV={base*WIN_C+(1-base)*LOSS_C:+.1f}c ===")
        print(f"  {'%price':>7} {'kept':>6} {'agree%':>7} {'EV/ct':>8}")
        for pct in (0, 0.0001, 0.0002, 0.0003, 0.0005, 0.001, 0.0015):
            B = pct * ref
            kept = [r for r in rows if r[1] >= B]
            if not kept: continue
            ka = sum(1 for a, _ in kept if a) / len(kept)
            ev = ka * WIN_C + (1 - ka) * LOSS_C
            print(f"  {pct*100:>6.3f}% {len(kept)/n*100:>5.0f}% {ka*100:>6.1f}% {ev:>+7.1f}")


if __name__ == "__main__":
    run()
