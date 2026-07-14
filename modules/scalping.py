"""
modules/scalping.py — crypto scalping signal engine (monitor + dry-run paper).

For each asset it compares the Kalshi 15-min up/down contract price (the market's
implied probability) against a simple spot-vs-strike model probability, nets out
the round-trip fee, and emits a signal. In dry-run it opens a paper position on
ENTER and resolves it at settlement (spot vs strike), logging real-ish P&L so the
edge can be backtested without sending orders.

Model is deliberately simple and HONEST (zero-drift lognormal, configurable vol):
the weather-bot lesson is that an over-confident model just donates fees. Vol is
surfaced as a tunable so you can calibrate rather than trust a default.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("modules.scalping")

PAPER_LOG = Path(os.getenv("SCALP_PAPER_LOG", "scalping_paper.jsonl"))
SECONDS_PER_YEAR = 365 * 24 * 3600

# Annualised vol per asset (tunable). 0.6 = 60% — a sane starting point for crypto.
DEFAULT_VOL = {
    "BTC": float(os.getenv("SCALP_VOL_BTC", "0.6")),
    "ETH": float(os.getenv("SCALP_VOL_ETH", "0.75")),
    "SOL": float(os.getenv("SCALP_VOL_SOL", "0.9")),
}
MIN_EDGE_CENTS = float(os.getenv("SCALP_MIN_EDGE_CENTS", "1.0"))
MIN_SECS_LEFT = int(os.getenv("SCALP_MIN_SECS_LEFT", "30"))  # don't enter at the bell


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def kalshi_fee_cents(price_cents: float) -> float:
    """Kalshi taker fee per contract = ceil(0.07 * P * (1-P)) in cents, P in dollars."""
    if price_cents is None:
        return 2.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return math.ceil(7.0 * p * (1.0 - p))


class ScalpEngine:
    def __init__(self, dry_run=True, on_log=None):
        self.dry_run = dry_run
        self.on_log = on_log or (lambda i, m: None)
        self._paper = {}      # asset -> open paper position dict
        self._state = {}      # asset -> last computed view
        self.session = {"trades": 0, "wins": 0, "pnl": 0.0}

    # ── model ────────────────────────────────────────────────────────────────
    def model_prob_up(self, asset, spot, strike, secs_left):
        if not spot or not strike or secs_left is None or secs_left <= 0:
            return None
        vol = DEFAULT_VOL.get(asset, 0.7)
        sigma = vol * math.sqrt(secs_left / SECONDS_PER_YEAR)
        if sigma <= 0:
            return 1.0 if spot > strike else 0.0
        z = math.log(spot / strike) / sigma
        return _norm_cdf(z)

    # ── per-update compute ──────────────────────────────────────────────────
    def compute(self, snapshots: dict, spot_snap: dict) -> dict:
        """snapshots: asset -> engine MarketSnapshot.to_dict(); spot_snap from SpotFeed."""
        out = {}
        for asset, snap in (snapshots or {}).items():
            if not snap:
                out[asset] = self._state.get(asset)
                continue
            sp = (spot_snap or {}).get(asset) or {}
            spot = sp.get("spot")
            strike = snap.get("floor_strike")
            secs = snap.get("secs_left")
            yes_bid, yes_ask = snap.get("yes_bid"), snap.get("yes_ask")
            no_bid, no_ask = snap.get("no_bid"), snap.get("no_ask")

            # market implied P(up) = mid of YES in cents / 100
            mkt_prob = None
            if yes_bid is not None and yes_ask is not None:
                mkt_prob = (yes_bid + yes_ask) / 2.0 / 100.0
            elif yes_ask is not None:
                mkt_prob = yes_ask / 100.0

            model = self.model_prob_up(asset, spot, strike, secs)
            edge_up = (model - mkt_prob) if (model is not None and mkt_prob is not None) else None

            # choose side + entry cost. UP -> buy YES at yes_ask. DOWN -> buy NO at no_ask.
            side, entry_cost, edge_prob = None, None, None
            if edge_up is not None:
                if edge_up >= 0 and yes_ask is not None:
                    side, entry_cost, edge_prob = "UP", yes_ask, edge_up
                elif edge_up < 0 and no_ask is not None:
                    side, entry_cost, edge_prob = "DOWN", no_ask, -edge_up

            fee = kalshi_fee_cents(entry_cost) if entry_cost is not None else None
            gross_edge_c = edge_prob * 100 if edge_prob is not None else None
            net_edge_c = (gross_edge_c - 2 * fee) if (gross_edge_c is not None and fee is not None) else None

            # signal
            signal = "NO-DATA"
            if net_edge_c is None:
                signal = "NO-DATA"
            elif secs is not None and secs < MIN_SECS_LEFT:
                signal = "SETTLING"
            elif net_edge_c >= MIN_EDGE_CENTS:
                signal = f"ENTER-{side}"
            elif gross_edge_c is not None and gross_edge_c > 0:
                signal = "FEE-BLOCKED"
            else:
                signal = "NO-EDGE"

            view = {
                "asset": asset, "spot": spot,
                "vel30": sp.get("vel30"), "vel60": sp.get("vel60"),
                "strike": strike, "secs_left": secs,
                "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask,
                "mkt_prob": round(mkt_prob, 4) if mkt_prob is not None else None,
                "model_prob": round(model, 4) if model is not None else None,
                "edge_up": round(edge_up, 4) if edge_up is not None else None,
                "side": side, "entry_cost": entry_cost,
                "fee_c": fee, "gross_edge_c": round(gross_edge_c, 2) if gross_edge_c is not None else None,
                "net_edge_c": round(net_edge_c, 2) if net_edge_c is not None else None,
                "signal": signal,
                "window_pct": round(100 * (1 - secs / 900.0), 1) if secs is not None else None,
                "vol": DEFAULT_VOL.get(asset),
                "paper": self._paper.get(asset),
            }
            self._state[asset] = view
            out[asset] = view
            self._update_paper(asset, view, spot, strike, secs)
        return out

    # ── dry-run paper trades ─────────────────────────────────────────────────
    def _update_paper(self, asset, view, spot, strike, secs):
        pos = self._paper.get(asset)
        sig = view["signal"]
        # open on ENTER if flat
        if pos is None and sig.startswith("ENTER") and self.dry_run:
            self._paper[asset] = {
                "asset": asset, "side": view["side"], "entry_cost": view["entry_cost"],
                "strike": strike, "model_prob": view["model_prob"],
                "opened_ts": datetime.now(timezone.utc).isoformat(),
                "secs_at_entry": secs,
            }
            self.on_log("→", f"[scalp] PAPER ENTER {asset} {view['side']} @ {view['entry_cost']}c "
                             f"(edge {view['net_edge_c']}c, model {view['model_prob']})")
            return
        # resolve at settlement
        if pos is not None and secs is not None and secs <= 1 and spot is not None and strike is not None:
            won_up = spot > strike
            won = (pos["side"] == "UP" and won_up) or (pos["side"] == "DOWN" and not won_up)
            entry = pos["entry_cost"]
            gross = (100 - entry) if won else (-entry)
            fee = kalshi_fee_cents(entry)
            pnl_c = gross - fee  # single settlement fee (no exit trade for binary)
            self.session["trades"] += 1
            self.session["wins"] += 1 if won else 0
            self.session["pnl"] += pnl_c / 100.0
            row = {
                "type": "scalp_paper", "ts": datetime.now(timezone.utc).isoformat(),
                **pos, "settle_spot": spot, "won": won, "pnl_cents": round(pnl_c, 2),
            }
            try:
                with open(PAPER_LOG, "a") as f:
                    f.write(json.dumps(row) + "\n")
            except Exception as e:  # noqa: BLE001
                log.debug("paper log write: %s", e)
            self.on_log("✅" if won else "✗",
                        f"[scalp] PAPER SETTLE {asset} {pos['side']} {'WIN' if won else 'LOSS'} "
                        f"{pnl_c:+.1f}c")
            self._paper.pop(asset, None)

    def state(self):
        return {
            "assets": self._state,
            "session": self.session,
            "config": {"min_edge_c": MIN_EDGE_CENTS, "min_secs": MIN_SECS_LEFT, "vol": DEFAULT_VOL},
        }
