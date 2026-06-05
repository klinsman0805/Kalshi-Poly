"""
arb_trader.py — Cross-platform Arbitrage: Kalshi × Polymarket

STRATEGY:
  Monitor YES (Up) and NO (Down) ask prices on both platforms.
  When the combined cost of covering both outcomes < ARB_THRESHOLD cents (after fees),
  buy both legs simultaneously — guaranteed profit at resolution regardless of direction.

ENTRY CONDITIONS (checked on every Kalshi price tick):
  Leg A:  Kalshi YES ask  + Polymarket NO ask  < ARB_THRESHOLD
          → buy YES on Kalshi,  buy NO  on Polymarket
  Leg B:  Kalshi NO  ask  + Polymarket YES ask < ARB_THRESHOLD
          → buy NO  on Kalshi,  buy YES on Polymarket

TOLERANCE:  ARB_TOLERANCE allows up to this many extra cents of slippage on fills.
            Detection gate:  combined < ARB_THRESHOLD                (strict)
            Execution gate:  combined < ARB_THRESHOLD + ARB_TOLERANCE (fill drift ok)

FEE MODEL (per docs.kalshi.com/getting_started/fee_rounding and
            docs.polymarket.com — CLOB fee curve):
  Kalshi taker:     ceil(0.07 × size × p × (1−p), to $0.01) — whole-order, retail precision.
  Polymarket taker: (fee_bps / 10_000) × p × (1 − p) × 100  cents per contract.
                    Makers pay 0 and earn a rebate (Crypto: ~20% of taker fees,
                    paid daily in USDC). The taker-side formula has the
                    SAME p×(1−p) symmetric shape as Kalshi — earlier versions
                    of this bot missed this factor and over-estimated Poly
                    fees by up to 80× near mid-price.
  Both fees are subtracted from gross profit before the ARB_MIN_PROFIT gate.

  Example — YES@88¢ Kalshi + NO@8¢ Polymarket, 5 contracts, Poly fee 200 bps:
    gross profit = (100 − 96) × 5 = 20¢ total = 4¢/contract
    Kalshi fee   = ceil(0.07 × 5 × 0.88 × 0.12 × 100¢) = ceil(3.696) = 4¢ total → 0.8¢/contract
    Poly fee     = 0.02 × 0.08 × 0.92 × 100 = 0.147¢/contract
    net profit   = 4 − 0.8 − 0.147 ≈ 3.05¢ per contract ✓

MARKET MATCHING (automatic, per window):
  Polymarket slug = {asset}-updown-15m-{window_ts}
  window_ts is the window START time in Unix seconds — identical to
  MarketSnapshot.window_ts from the Kalshi engine, so no manual mapping needed.
  get_market_for_window() is called once at the start of each window.

EXECUTION:
  Both legs fire simultaneously via two threads.
  Kalshi: IOC buy.  Polymarket: FOK buy (EIP-712 signed).
  Partial fill (one leg only) logs a naked-position warning.
"""

import os
import csv
import json
import math
import time
import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

from engine import (
    MarketSnapshot, _auth_headers, API_BASE, SESSION, ORDER_TIMEOUT, DRY_RUN,
)
from polymarket import PolyClient, PolySnap, PolyMarketInfo, get_market_for_window

log = logging.getLogger("kalshi.arb")

# ── Config ────────────────────────────────────────────────────────────────────

ARB_THRESHOLD    = int(os.getenv("ARB_THRESHOLD",        "99"))
ARB_TOLERANCE    = int(os.getenv("ARB_TOLERANCE",         "2"))
ARB_TRADE_SIZE   = int(os.getenv("ARB_TRADE_SIZE",         "5"))
ARB_MIN_PROFIT   = int(os.getenv("ARB_MIN_PROFIT_CENTS",   "1"))
ARB_ORDER_COOLDOWN = float(os.getenv("ARB_ORDER_COOLDOWN", "30"))
# Max arb ENTRIES per asset per 15-min window. Multiple orders/window are allowed
# (same or opposite side) to capture more opportunities, but each entry stacks the
# SAME venue-disagreement basis risk: if the window resolves oppositely, you lose
# ~N× a single arb. This cap is the per-window exposure ceiling — total worst-case
# loss ≈ cap × single-arb loss. Spacing is enforced by ARB_ORDER_COOLDOWN.
# (Added 2026-06-04 after an uncapped re-arm placed 9 stacked SOL-UP buys.)
ARB_MAX_WINDOW_ENTRIES = int(os.getenv("ARB_MAX_WINDOW_ENTRIES", "2"))
# Multi-order toggle (for A/B testing two ways to deploy size in a window):
#   true  → up to ARB_MAX_WINDOW_ENTRIES entries/window, each ARB_TRADE_SIZE.
#   false → exactly ONE entry/asset/window, sized ARB_SINGLE_ORDER_SIZE (lets you
#           test "one bigger trade" vs "several small trades"). Defaults to
#           ARB_TRADE_SIZE when unset.
ARB_MULTI_ORDER = os.getenv("ARB_MULTI_ORDER", "true").lower() != "false"
ARB_SINGLE_ORDER_SIZE = int(os.getenv("ARB_SINGLE_ORDER_SIZE", str(ARB_TRADE_SIZE)))
# Polymarket rejects orders worth < this dollar amount (min order value). Kalshi
# has no such floor. Arb needs equal share counts on both legs, so we can't pad
# the Poly leg alone — instead skip when ARB_TRADE_SIZE × poly_price < this.
ARB_POLY_MIN_ORDER_USD = float(os.getenv("ARB_POLY_MIN_ORDER_USD", "1.0"))
# Kalshi IOC slippage buffer (cents): the Poly leg fills first (~300-800ms), so
# by the time the Kalshi IOC fires the ask may have ticked up. Bid this many
# cents above the detected ask so the IOC still crosses. Bounded — too high and
# the arb edge is lost (re-validated against combined cost before firing).
ARB_KALSHI_SLIPPAGE = int(os.getenv("ARB_KALSHI_SLIPPAGE", "2"))
# Poly limit-BUYs over-fill (the dollar budget buys extra shares at better
# prices), so the Poly leg fills a fractional count like 5.55 while Kalshi only
# trades whole contracts. We hedge floor(poly_fill) on Kalshi and accept the
# sub-share remainder as "dust" — too small to be worth unwinding or halting on.
ARB_DUST_SHARES = float(os.getenv("ARB_DUST_SHARES", "1.0"))

# Naked Poly exposure ≤ this USD amount is HELD to resolution instead of being
# sold via FAK. Auto-redeems on market settlement. Threshold is on the buy-side
# cost basis, not mark-to-market, so it never depends on a live quote at unwind
# time.
#
# DEFAULT 0 (2026-06-05, by user decision): ALWAYS unwind a naked Poly leg when
# Kalshi misses — never hold a directional position to resolution. We accept the
# sell-side risks (ledger race, thin-book partials) the hold policy avoided;
# partial unwinds now RETRY (ARB_UNWIND_RETRIES) and then log+continue rather
# than halt. Set >0 to re-enable holding small legs.
ARB_POLY_HOLD_NAKED_USD = float(os.getenv("ARB_POLY_HOLD_NAKED_USD", "0"))
# How many extra times to retry selling a naked Poly leg that only PARTIALLY
# unwound (thin near-expiry book). Each retry re-sends a FAK sell for the
# remaining shares. After the last retry, residual dust is logged and trading
# CONTINUES (no kill-switch halt) — by user decision 2026-06-05.
ARB_UNWIND_RETRIES = int(os.getenv("ARB_UNWIND_RETRIES", "3"))
ARB_UNWIND_RETRY_DELAY = float(os.getenv("ARB_UNWIND_RETRY_DELAY", "1.0"))

# ── Global kill-switch ─────────────────────────────────────────────────────────
# Set True after any failed/partial unwind. While set, NO new arb fires on ANY
# asset — one unwind bug must not be able to bleed repeatedly. Cleared only by
# restarting the process (deliberate: a human should inspect the naked position).
TRADING_HALTED = False
HALT_REASON    = ""

def halt_trading(reason: str):
    """Trip the global kill-switch. Idempotent."""
    global TRADING_HALTED, HALT_REASON
    if not TRADING_HALTED:
        TRADING_HALTED = True
        HALT_REASON    = reason
        log.critical("ARB TRADING HALTED: %s", reason)

# Phase-2 safety knobs (all in seconds / cents / multiples)
ARB_K_SNAP_MAX_AGE   = float(os.getenv("ARB_K_SNAP_MAX_AGE",   "1.5"))
ARB_P_SNAP_MAX_AGE   = float(os.getenv("ARB_P_SNAP_MAX_AGE",   "1.5"))
# Max age for a REST-only (not WS-confirmed) Poly snapshot. Must exceed the
# POLY_RESEED_INTERVAL plus network/processing margin so re-seeded markets stay
# eligible; kept tight enough that genuinely stale prices are still rejected.
ARB_REST_MAX_AGE     = float(os.getenv("ARB_REST_MAX_AGE",     "4.0"))
ARB_KALSHI_IOC_TIMEOUT = float(os.getenv("ARB_KALSHI_IOC_TIMEOUT", "3.0"))
# Unwind cost ceiling: if expected_loss_on_unwind > N × expected_profit, abort.
ARB_UNWIND_RATIO_MAX = float(os.getenv("ARB_UNWIND_RATIO_MAX", "3.0"))
# Periodic stats summary: seconds between per-window stats dumps.
ARB_STATS_INTERVAL   = float(os.getenv("ARB_STATS_INTERVAL",   "60"))

# ── Basis-risk controls (added 2026-05-30 after −$3.63 BTC venue-disagreement) ──
# Kalshi and Polymarket use DIFFERENT price feeds (Poly=Chainlink) and strikes,
# so they can resolve oppositely when the price settles near the strike. A
# 200-window study showed disagreements live within ~0.05% of strike; requiring
# the price to be farther than that pushed agreement to ~100%.
#
# Only trade these assets (BTC excluded for now — costliest miss, widest dead-zone).
ARB_ASSETS = [a.strip().upper() for a in
              os.getenv("ARB_ASSETS", "ETH,SOL").split(",") if a.strip()]
# Entry gate: skip if |spot − Kalshi strike| < this % of spot.
#
# This is the percentage safeguard against the venue-disagreement / opposite-
# resolution risk. The two venues use DIFFERENT "price to beat" strikes and feeds:
# Kalshi publishes floor_strike; Polymarket's strike is the underlying captured at
# window open from Chainlink — a number Poly NEVER sends over its feed (we only
# receive odds), so we cannot read Poly's strike to measure the cross-venue gap
# directly. Instead we gate on distance from the strike we DO trust (Kalshi's): if
# spot is within X% of it, the resolution price could plausibly land between the
# two strikes (which differ by the cross-feed offset, ~$0.30 on ETH) and resolve
# the legs oppositely → total loss on both.
#
# The % auto-scales per asset, which matters: at 0.20%, ETH≈$3.57 but SOL≈$0.28 —
# SOL is the tightest because of its low price, so the % must stay safe for SOL.
# Raised 0.15→0.20 (2026-06-04) after repeated near-strike opposite-resolution
# losses; gated on ENTRY only (no early-exit), so headroom also absorbs post-entry
# drift toward the strike before resolution.
ARB_STRIKE_BUFFER_PCT = float(os.getenv("ARB_STRIKE_BUFFER_PCT", "0.20"))
# Early-exit: in the last N seconds, if price has drifted to within this % of the
# strike (the danger zone), sell BOTH legs rather than hold into a risky
# resolution. 0 disables early-exit (hold to resolution).
#
# DISABLED by default (2026-06-01): early-exit must sell into the SAME thin
# near-expiry liquidity it's trying to escape, so the Poly FAK sell often only
# partially fills — leaving a worse-hedged residual tail than just holding, plus
# tripping the kill-switch. Decision: once both legs are filled, HOLD to
# resolution. The strike-distance ENTRY gate already keeps us out of the
# danger zone in the first place; that's where basis risk is controlled.
ARB_EXIT_BUFFER_PCT   = float(os.getenv("ARB_EXIT_BUFFER_PCT", "0"))
ARB_EXIT_WINDOW_SECS  = float(os.getenv("ARB_EXIT_WINDOW_SECS", "90"))

TRADES_FILE = Path(os.getenv("TRADES_FILE", "trades.jsonl"))
# Clean, separate CSV of SUCCESSFUL trades only (entries + exits/resolutions),
# for easy review/debugging alongside the Polymarket export. trades.jsonl stays
# the full firehose (attempts, skips, misses, aborts); this is the signal.
ARB_TRADES_CSV = Path(os.getenv("ARB_TRADES_CSV", "arb_trades.csv"))

# ── Fee helpers ───────────────────────────────────────────────────────────────
#
# Both venues use the symmetric p×(1−p) curve, peaking at p=0.5. Kalshi rounds
# the per-fill fee UP to $0.0001 (centicent) with a per-order accumulator that
# issues $0.01 rebates as rounding accumulates; non-direct (retail) accounts
# settle at $0.01 balance precision. We model the retail case: ceil-to-whole-
# cent on the order, applied once for a single IOC fill (the common case for
# this bot's arb size).

def _kalshi_fee_total(price_cents: int, size: int) -> float:
    """Kalshi taker fee for the whole order, in cents, rounded UP to 1¢."""
    p = price_cents / 100.0
    raw_dollars = 0.07 * size * p * (1.0 - p)
    return math.ceil(raw_dollars * 100.0)  # cents, integer-valued

def _poly_fee_per_contract(price_cents: int, fee_bps: int) -> float:
    """Polymarket taker fee per contract in cents: (bps/10_000) × p × (1−p) × 100."""
    p = price_cents / 100.0
    return (fee_bps / 10_000.0) * p * (1.0 - p) * 100.0

def _net_profit(k_price: int, p_price: int, poly_fee_bps: int,
                size: int) -> float:
    """
    Net profit per contract pair after both platform fees (cents).

    Order size is required because Kalshi's whole-order rounding means the
    amortized per-contract fee depends on `size`.
    """
    if size <= 0:
        return 0.0
    gross_per_pair = 100 - (k_price + p_price)
    k_fee_per_pair = _kalshi_fee_total(k_price, size) / size
    p_fee_per_pair = _poly_fee_per_contract(p_price, poly_fee_bps)
    return gross_per_pair - k_fee_per_pair - p_fee_per_pair

# ── Spot price (for the strike-distance / basis-risk gate) ──────────────────────
_SPOT_SYMBOL = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
_spot_cache: Dict[str, tuple] = {}   # asset -> (price, ts)
_SPOT_TTL = 2.0

def get_spot(asset: str) -> Optional[float]:
    """
    Current spot price for the strike-distance gate. Cached ~2s. Uses Coinbase
    spot (fast, no key). Returns None on failure so the gate fails CLOSED (skip).
    Note: this is a proxy for the venues' own feeds; the gate keeps a wide buffer
    to absorb the small difference between spot and Kalshi/Chainlink references.
    """
    import requests
    now = time.time()
    cached = _spot_cache.get(asset)
    if cached and now - cached[1] < _SPOT_TTL:
        return cached[0]
    sym = _SPOT_SYMBOL.get(asset.upper())
    if not sym:
        return None
    try:
        r = requests.get(f"https://api.coinbase.com/v2/prices/{sym}/spot", timeout=3)
        r.raise_for_status()
        px = float(r.json()["data"]["amount"])
        _spot_cache[asset] = (px, now)
        return px
    except Exception as e:
        log.debug("spot fetch %s: %s", asset, e)
        return None

# ── Trade log ─────────────────────────────────────────────────────────────────

def _log_trade(record: dict):
    try:
        with TRADES_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.error("arb trade log: %s", e)

# Fixed column order for the clean success CSV. Entries and exits share one schema;
# fields irrelevant to a row are left blank. Append-only; header written once.
_ARB_CSV_COLUMNS = [
    "ts", "event", "asset", "window_ts", "kalshi_ticker", "secs_left",
    "kalshi_side", "kalshi_price", "kalshi_filled",
    "poly_side", "poly_order_price", "poly_fill_price", "poly_filled",
    "count", "combined_cost",
    "net_profit_per_contract", "exec_profit_per_contract",
    "fully_exited", "k_sold", "p_sold", "spot", "strike",
]
_arb_csv_lock = threading.Lock()

def _log_success_csv(row: dict):
    """
    Append one successful-trade row (event='entry' or 'exit') to ARB_TRADES_CSV.
    Writes the header on first use. Only confirmed fills land here — this is the
    clean signal, separate from the trades.jsonl firehose. Never raises.
    """
    try:
        with _arb_csv_lock:
            new_file = not ARB_TRADES_CSV.exists() or ARB_TRADES_CSV.stat().st_size == 0
            with ARB_TRADES_CSV.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=_ARB_CSV_COLUMNS, extrasaction="ignore")
                if new_file:
                    w.writeheader()
                w.writerow(row)
    except Exception as e:
        log.error("arb success CSV log: %s", e)

# ── Kalshi order placement ────────────────────────────────────────────────────

def _reconcile_kalshi_fill(cid: str) -> float:
    """
    After a Kalshi POST that failed/timed out, query for the order by
    client_order_id to discover whether it actually filled. Returns filled
    contract count (float to preserve fractional fills), or 0.0 if no order
    exists or the query also fails.
    """
    path = "/trade-api/v2/portfolio/orders"
    try:
        headers = _auth_headers("GET", path)
        url = API_BASE + "/portfolio/orders"
        r = SESSION.get(url, params={"client_order_id": cid},
                        headers=headers, timeout=5)
        r.raise_for_status()
        orders = r.json().get("orders", []) or []
        if not orders:
            return 0.0
        order = orders[0]
        filled = float(order.get("fill_count_fp", "0") or "0")
        log.warning("Kalshi reconciliation cid=%s → filled=%s status=%s",
                    cid, filled, order.get("status"))
        return filled
    except Exception as e:
        log.error("Kalshi reconciliation FAILED for cid=%s: %s — "
                  "ASSUMING NOT FILLED (risk: silent naked position)",
                  cid, e)
        return 0.0


def _kalshi_ioc(snap: MarketSnapshot, side: str, price: int, size: int,
                timeout: Optional[float] = None) -> float:
    """
    Place an IOC buy on Kalshi. Returns filled count as float (preserves
    fractional fills if the market supports them; integer fills return as e.g. 5.0).

    On HTTP timeout or network error, performs a reconciliation lookup by
    client_order_id rather than blindly returning 0 — otherwise a successful
    fill whose response was lost would silently leave a naked Kalshi position.
    """
    if timeout is None:
        timeout = ORDER_TIMEOUT
    cid  = f"arb-k-{side}-{int(time.time() * 1000)}"
    body = {
        "ticker":          snap.ticker,
        "side":            side,
        "action":          "buy",
        "count":           size,
        "time_in_force":   "immediate_or_cancel",
        "client_order_id": cid,
    }
    body["yes_price" if side == "yes" else "no_price"] = price

    log.info("Kalshi IOC buy %s @%dc x%d ticker=%s — placing",
             side.upper(), price, size, snap.ticker)
    path    = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("POST", path)
    url     = API_BASE + "/portfolio/orders"
    try:
        r = SESSION.post(url, json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        order = r.json().get("order", {})
        filled = float(order.get("fill_count_fp", "0") or "0")
        if filled == 0:
            log.warning("Kalshi IOC buy %s @%dc x%d → NOT FILLED (status=%s)",
                        side.upper(), price, size, order.get("status"))
        else:
            log.info("Kalshi IOC buy %s @%dc x%d → filled=%s",
                     side.upper(), price, size, filled)
        return filled
    except Exception as e:
        log.warning("Kalshi IOC %s POST failed (%s) — reconciling cid=%s",
                    side, e, cid)
        return _reconcile_kalshi_fill(cid)


def _kalshi_ioc_sell(snap: MarketSnapshot, side: str, size: float) -> float:
    """
    Emergency market-sell: IOC sell `size` contracts on Kalshi at price=1¢
    (accept any bid).  Used for naked-leg unwind after a partial arb fill.
    Returns sold count as float.
    """
    if DRY_RUN:
        log.info("[DRY RUN] Kalshi IOC sell %s x%s", side.upper(), size)
        return float(size)

    cid  = f"arb-k-unwind-{side}-{int(time.time() * 1000)}"
    body = {
        "ticker":          snap.ticker,
        "side":            side,
        "action":          "sell",
        "count":           size,
        "time_in_force":   "immediate_or_cancel",
        "client_order_id": cid,
    }
    # price=1 means accept any bid ≥ 1¢ — guarantees execution
    body["yes_price" if side == "yes" else "no_price"] = 1

    log.info("Kalshi IOC sell %s x%s ticker=%s — placing (unwind)",
             side.upper(), size, snap.ticker)
    path    = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("POST", path)
    url     = API_BASE + "/portfolio/orders"
    try:
        r = SESSION.post(url, json=body, headers=headers, timeout=ORDER_TIMEOUT)
        r.raise_for_status()
        order = r.json().get("order", {})
        sold = float(order.get("fill_count_fp", "0") or "0")
        log.info("Kalshi IOC sell %s x%s → sold=%s", side.upper(), size, sold)
        return sold
    except Exception as e:
        log.error("Kalshi IOC sell error (size=%s): %s — reconciling cid=%s",
                  size, e, cid)
        return _reconcile_kalshi_fill(cid)

# ── Position ──────────────────────────────────────────────────────────────────

@dataclass
class ArbPosition:
    asset:          str
    kalshi_ticker:  str
    kalshi_side:    str
    kalshi_price:   int
    poly_token_id:  str
    poly_side:      str
    poly_price:     int                 # limit price we SENT on the Poly leg (¢)
    count:          float               # matched contracts (min of both fills)
    k_filled:       float               # actual Kalshi fill
    p_filled:       float               # actual Polymarket fill
    expected_profit: float              # ¢ per contract after fees
    poly_fill_price: Optional[float] = None  # actual avg Poly fill (¢), if reported
    ts:    str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: str = "open"                 # open | partial | resolved


# ── Per-asset arb engine ──────────────────────────────────────────────────────

class ArbTrader:
    """
    Cross-platform arb for one asset (BTC / ETH / SOL).
    Wire into the Kalshi price loop: call update(snap) on each tick.
    Call reset() between 15-min windows.
    """

    def __init__(self, asset: str, poly_client: PolyClient, on_log=None):
        self.asset   = asset
        self._poly   = poly_client
        self._on_log = on_log or (lambda ic, msg: log.info("[%s] %s", ic, msg))

        # All positions opened THIS window (multiple entries per window allowed,
        # capped by ARB_MAX_WINDOW_ENTRIES). Every leg is tracked here so none is
        # orphaned/unhedged — the single-_position overwrite caused exactly that
        # (8 untracked SOL legs, 2026-06-04). reset() clears it each window.
        self._positions: List[ArbPosition] = []
        # In-flight guard: True only while an _execute() thread is running, so two
        # ticks can't fire concurrently. Cleared after each entry/abort/miss so the
        # trader can place MULTIPLE orders within one window (re-arm), up to the cap.
        self._attempted  = False
        self._last_attempt_ts: float = 0.0
        self._market_info: Optional[PolyMarketInfo] = None
        self._lock = threading.Lock()
        self._last_price_log_ts: float = 0.0   # rate-limit combined price log
        # Throttle repetitive per-tick skip logs (keyed by reason). Every skip is
        # still recorded to trades.jsonl + stats; only the on-screen line is muted.
        self._skip_log_ts: Dict[str, float] = {}
        self._skip_log_interval: float = 30.0

        # Phase-3 observability: per-window decision counters + last stats emit.
        self._stats: Counter = Counter()
        self._last_stats_log_ts: float = 0.0
        self._window_start_ts: float = time.time()

        # Latest observed prices for the dashboard (updated each evaluable tick).
        self._live: dict = {
            "k_yes": None, "k_no": None, "p_yes": None, "p_no": None,
            "spread_a": None,  # k_yes + p_no
            "spread_b": None,  # k_no  + p_yes
            "poly_linked": False, "fee_bps": None, "ws_confirmed": False,
            "secs_left": None, "updated_ts": None,
            "strike": None, "spot": None, "dist_pct": None,
        }

    def get_state(self) -> dict:
        """
        Dashboard snapshot of this asset's arb state. Safe to call from any
        thread. Returns live prices, per-window stat counters, and any open
        position — everything the UI needs for arb observability.
        """
        with self._lock:
            all_pos = [p.__dict__.copy() for p in self._positions]
            # Representative position for the existing single-position UI: most
            # recent open one, else most recent of any.
            pos = None
            if self._positions:
                pos = next((p.__dict__.copy() for p in reversed(self._positions)
                            if p.phase == "open"), self._positions[-1].__dict__.copy())
            stats = dict(self._stats)
            live = dict(self._live)
        return {
            "asset":     self.asset,
            "live":      live,
            "stats":     stats,
            "position":  pos,
            "positions": all_pos,
            "entries_this_window": len(all_pos),
            "max_window_entries":  ARB_MAX_WINDOW_ENTRIES,
            "attempted": self._attempted,
            "window_age_s": int(time.time() - self._window_start_ts),
        }

    def _log_skip_throttled(self, key: str, icon: str, msg: str):
        """Emit a skip log line at most once per _skip_log_interval seconds per key."""
        now = time.time()
        if now - self._skip_log_ts.get(key, 0.0) >= self._skip_log_interval:
            self._skip_log_ts[key] = now
            self._on_log(icon, msg)

    def _drift_probe(self, kalshi_snap: MarketSnapshot, k_side: str, p_side: str,
                     k_price_at_detect: int, p_price_at_detect: int,
                     detection_ts: float) -> dict:
        """
        Observability only — no trading effect. At fire time, re-read the freshest
        prices for both venues and measure how far they drifted from the values
        captured at detection. Lets us tell "the +2¢ pad killed it" (drift≈0) apart
        from "the book moved under us in flight" (drift>0 / book gone).

        Returns a flat dict folded into the fire/miss/abort log records.
        """
        probe = {
            "detect_to_fire_ms": int((time.time() - detection_ts) * 1000),
            "k_price_at_detect": k_price_at_detect,
            "p_price_at_detect": p_price_at_detect,
        }
        # Fresh Kalshi ask from the snapshot held by the engine (already updated
        # in-place each tick). Same side we intended to take.
        k_now = kalshi_snap.yes_ask if k_side == "yes" else kalshi_snap.no_ask
        probe["k_price_now"]  = k_now
        probe["k_drift"]      = (None if k_now is None else k_now - k_price_at_detect)
        k_now_age = (datetime.now(timezone.utc) - kalshi_snap.ts).total_seconds()
        probe["k_snap_age_ms_now"] = int(k_now_age * 1000)

        # Fresh Polymarket ask for the side we intended to take.
        p_now = None
        p_age_now = None
        info = self._market_info
        if info is not None:
            ps = self._poly.snap(info.condition_id)
            if ps is not None:
                p_now = ps.yes_ask if p_side == "yes" else ps.no_ask
                p_age_now = time.time() - ps.ts
        probe["p_price_now"]  = p_now
        probe["p_drift"]      = (None if p_now is None else p_now - p_price_at_detect)
        probe["p_snap_age_ms_now"] = (None if p_age_now is None else int(p_age_now * 1000))
        return probe

    # ── Observability helpers ────────────────────────────────────────────────

    def _log_attempt(self, ctx: dict, *, decision: str, reason: Optional[str]):
        """
        Write a per-candidate decision row to trades.jsonl. `ctx` contains the
        evaluation context (prices, depths, ages, computed costs); `decision`
        is one of "fire" or "skip"; `reason` names the rejection (or None for
        a fire).
        """
        # Track stats too — used by the periodic summary.
        key = "fire" if decision == "fire" else f"skip_{reason or 'unknown'}"
        self._stats[key] += 1
        self._stats["qualified"] += 1  # every logged attempt was past combined gate

        info = ctx.get("info")
        kalshi_snap = ctx.get("kalshi_snap")
        rec = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "type":        "arb_attempt",
            "asset":       self.asset,
            "decision":    decision,
            "reason":      reason,
            "window_ts":   kalshi_snap.window_ts if kalshi_snap else None,
            "secs_left":   kalshi_snap.secs_left if kalshi_snap else None,
            "k_side":      ctx.get("k_side"),
            "k_price":     ctx.get("k_price"),
            "k_depth":     ctx.get("k_depth"),
            "k_age_ms":    ctx.get("k_age_ms"),
            "p_side":      ctx.get("p_side"),
            "p_price":     ctx.get("p_price"),
            "p_depth":     ctx.get("p_depth"),
            "p_age_ms":    ctx.get("p_age_ms"),
            "combined":    ctx.get("combined"),
            "net_profit_per_contract": ctx.get("net_profit_per_contract"),
            "poly_order_price":        ctx.get("poly_order_price"),
            "implied_unwind_bid":      ctx.get("implied_unwind_bid"),
            "unwind_cost_total_cents": ctx.get("unwind_cost_total_cents"),
            "expected_total_cents":    ctx.get("expected_total_cents"),
            "fee_bps":     info.fee_bps if info else None,
            "fee_source":  info.fee_source if info else None,
            "trade_size":  ARB_TRADE_SIZE,
        }
        _log_trade(rec)

    def _maybe_log_stats(self):
        """Emit a per-window stats summary every ARB_STATS_INTERVAL seconds."""
        now = time.time()
        if now - self._last_stats_log_ts < ARB_STATS_INTERVAL:
            return
        self._last_stats_log_ts = now
        if not self._stats:
            return  # nothing qualified yet this window
        window_age = int(now - self._window_start_ts)
        parts = [f"{k}={v}" for k, v in sorted(self._stats.items())]
        self._on_log("📈", (
            f"ARB {self.asset} stats (window age {window_age}s): "
            + "  ".join(parts)
        ))

    # ── Public ───────────────────────────────────────────────────────────────

    def reset(self):
        """Call at the start of each new 15-min window."""
        with self._lock:
            self._positions     = []
            self._attempted     = False
            self._last_attempt_ts = 0.0
            self._market_info   = None
            self._stats.clear()
            self._last_stats_log_ts = 0.0
            self._window_start_ts = time.time()
            self._live.update({
                "k_yes": None, "k_no": None, "p_yes": None, "p_no": None,
                "spread_a": None, "spread_b": None, "poly_linked": False,
                "secs_left": None, "updated_ts": None,
            })

    @property
    def positions(self) -> List[ArbPosition]:
        """All positions opened this window."""
        with self._lock:
            return list(self._positions)

    @property
    def position(self) -> Optional[ArbPosition]:
        """Representative single position (back-compat): the most recent open one,
        else the most recent of any. None if no entries yet this window."""
        with self._lock:
            if not self._positions:
                return None
            for pos in reversed(self._positions):
                if pos.phase == "open":
                    return pos
            return self._positions[-1]

    @property
    def attempted(self) -> bool:
        return self._attempted

    # ── Main tick ────────────────────────────────────────────────────────────

    def update(self, kalshi_snap: MarketSnapshot):
        """
        Called on each Kalshi price tick.
        1. Lazily discovers the matching Polymarket market for this window.
        2. Checks both arb combinations against latest Poly prices.
        3. Executes if spread is profitable.
        """
        # Global kill-switch: a prior unwind left a naked position. Do not place
        # any new orders on any asset until the process is restarted by a human.
        if TRADING_HALTED:
            return

        # Asset filter: only trade configured assets (BTC excluded by default —
        # widest venue-disagreement dead-zone; see ARB_ASSETS / basis-risk study).
        if self.asset.upper() not in ARB_ASSETS:
            return

        # Run early-exit on every OPEN position held this window (price drifting
        # into the danger zone near expiry). We do NOT return here: multiple orders
        # per window are allowed, so the trader may fire again if a fresh arb
        # appears — up to ARB_MAX_WINDOW_ENTRIES. Every leg stays tracked in
        # self._positions so none is orphaned/unhedged.
        if self._positions:
            self._maybe_early_exit(kalshi_snap)

        with self._lock:
            # In-flight guard: block while an _execute() thread is running so two
            # ticks can't fire concurrently. Cleared after each entry/abort/miss.
            if self._attempted:
                return
            # Per-window exposure cap: stop entering once we've hit the max number
            # of entries. This bounds worst-case opposite-resolution loss to
            # ~cap × single-arb loss (the uncapped re-arm lost far more by stacking
            # the same SOL-UP arb 9×). Single-order mode caps at exactly 1.
            entry_cap = ARB_MAX_WINDOW_ENTRIES if ARB_MULTI_ORDER else 1
            if len(self._positions) >= entry_cap:
                self._log_skip_throttled("window_cap", "🧯", (
                    f"ARB {self.asset}: hit {entry_cap}-entry cap for this window "
                    f"({'multi' if ARB_MULTI_ORDER else 'single'}-order mode) "
                    f"— no more orders until next window"
                ))
                return
            # Cooldown between entries (prevents rapid same-arb repeats).
            if time.time() - self._last_attempt_ts < ARB_ORDER_COOLDOWN:
                return

        # ── Step 1: discover Poly market for this window (once per window) ──
        info = self._market_info
        if info is None:
            info = get_market_for_window(self.asset, kalshi_snap.window_ts)
            if info is None:
                return
            if not info.accepting_orders:
                log.debug("Poly market for %s/%d not accepting orders", self.asset, kalshi_snap.window_ts)
                return
            with self._lock:
                self._market_info = info
            self._poly.subscribe_market(info)
            self._on_log("🔗", (
                f"ARB {self.asset} — linked Poly market "
                f"cond={info.condition_id[:12]}...  fee={info.fee_bps}bps"
            ))

        # ── Observability: record what we know NOW, before any early-return ──
        # The card should reflect the Kalshi side + link status even when the
        # Poly snapshot is missing/stale (e.g. mid-WS-reconnect) — otherwise the
        # card goes blank and looks "not linked" when it actually is.
        with self._lock:
            self._live.update({
                "poly_linked": True,
                "fee_bps":     info.fee_bps,
                "k_yes":       kalshi_snap.yes_ask,
                "k_no":        kalshi_snap.no_ask,
                "secs_left":   kalshi_snap.secs_left,
                "updated_ts":  time.time(),
            })
            # Recompute spreads if both sides known, else clear (avoid stale).
            ky, kn = kalshi_snap.yes_ask, kalshi_snap.no_ask
            py, pn = self._live.get("p_yes"), self._live.get("p_no")
            self._live["spread_a"] = (ky + pn) if (ky is not None and pn is not None) else None
            self._live["spread_b"] = (kn + py) if (kn is not None and py is not None) else None

        # ── Step 2: get prices ───────────────────────────────────────────────
        poly_snap = self._poly.snap(info.condition_id)
        if poly_snap is None:
            return

        # Kalshi snapshot staleness (datetime → seconds).
        k_age = (datetime.now(timezone.utc) - kalshi_snap.ts).total_seconds()
        if k_age > ARB_K_SNAP_MAX_AGE:
            return

        # Polymarket snapshot staleness.
        p_age = time.time() - poly_snap.ts
        ws_confirmed = getattr(poly_snap, "_ws_confirmed", False)
        if not ws_confirmed:
            # REST-only feed (WS silent for this market): the periodic re-seed
            # keeps it fresh, so just require recency. Allow up to the re-seed
            # interval + network/processing margin; reject if staler.
            if p_age > ARB_REST_MAX_AGE:
                return
        else:
            if p_age > ARB_P_SNAP_MAX_AGE:
                return

        k_yes = kalshi_snap.yes_ask
        k_no  = kalshi_snap.no_ask
        p_yes = poly_snap.yes_ask
        p_no  = poly_snap.no_ask

        # Observability: record Poly prices as soon as we have the snapshot, even
        # if a price is missing — so the card shows the Poly side independently.
        with self._lock:
            self._live.update({
                "p_yes": p_yes, "p_no": p_no,
                "ws_confirmed": bool(getattr(poly_snap, "_ws_confirmed", False)),
                "updated_ts": time.time(),
            })

        if None in (k_yes, k_no, p_yes, p_no):
            return

        # Record live prices for the dashboard (both sides confirmed non-None).
        with self._lock:
            self._live.update({
                "k_yes": k_yes, "k_no": k_no, "p_yes": p_yes, "p_no": p_no,
                "spread_a": k_yes + p_no, "spread_b": k_no + p_yes,
                "poly_linked": True,
                "fee_bps": info.fee_bps,
                "ws_confirmed": bool(getattr(poly_snap, "_ws_confirmed", False)),
                "secs_left": kalshi_snap.secs_left,
                "updated_ts": time.time(),
            })

        # ── Periodic visibility log (both sides confirmed live) ──────────────
        now = time.time()
        if now - self._last_price_log_ts >= 30.0:
            self._last_price_log_ts = now
            s1 = k_yes + p_no
            s2 = k_no  + p_yes
            ws_ok = "✓WS" if getattr(poly_snap, "_ws_confirmed", False) else "REST-seed"
            self._on_log("📊", (
                f"ARB {self.asset} prices — "
                f"K YES:{k_yes}¢ NO:{k_no}¢  |  "
                f"P YES:{p_yes}¢ NO:{p_no}¢  [{ws_ok}]  "
                f"spreads: {s1}¢ / {s2}¢  (threshold {ARB_THRESHOLD}¢)"
            ))

        # ── Basis-risk gate: skip if price is near the strike (danger zone) ──
        # Kalshi & Polymarket use different price feeds, so when the price settles
        # near the strike they can resolve oppositely → both legs lose. Require
        # the spot price to be > ARB_STRIKE_BUFFER_PCT away from the Kalshi strike.
        strike = getattr(kalshi_snap, "floor_strike", None)
        spot   = get_spot(self.asset)
        if strike is None or spot is None:
            # Fail closed: without strike+spot we can't assess basis risk.
            self._log_skip_throttled("no_strike_or_spot", "🛡", (
                f"ARB {self.asset}: missing strike({strike}) or spot({spot}) "
                f"— can't assess basis risk, skipping"
            ))
            return
        dist_pct = abs(spot - float(strike)) / spot * 100.0
        with self._lock:
            self._live.update({"strike": float(strike), "spot": spot,
                               "dist_pct": round(dist_pct, 4)})
        if dist_pct < ARB_STRIKE_BUFFER_PCT:
            self._log_skip_throttled("near_strike", "🛡", (
                f"ARB {self.asset}: price {spot:.4f} only {dist_pct:.3f}% from "
                f"strike {strike} (< {ARB_STRIKE_BUFFER_PCT}% buffer) — basis-risk "
                f"danger zone, skipping"
            ))
            self._stats["skip_near_strike"] += 1
            return

        # Order size for this evaluation: single-order mode uses a (possibly
        # larger) dedicated size; multi-order mode uses the standard per-entry size.
        # Both legs use the same count (arb requires equal shares). Poly's 5-share
        # minimum still applies, so this is clamped to >= 5.
        trade_size = max(5, ARB_SINGLE_ORDER_SIZE if not ARB_MULTI_ORDER else ARB_TRADE_SIZE)

        # ── Step 3: check both arb legs ──────────────────────────────────────
        # candidate = (kalshi_side, k_price, k_depth, poly_side, p_price, p_depth,
        #              p_token_id, p_opposite_ask_for_implied_bid)
        candidates = [
            ("yes", k_yes, kalshi_snap.yes_ask_depth,
             "no",  p_no,  poly_snap.no_ask_depth,
             info.no_token_id,  p_yes),
            ("no",  k_no,  kalshi_snap.no_ask_depth,
             "yes", p_yes, poly_snap.yes_ask_depth,
             info.yes_token_id, p_no),
        ]

        for (k_side, k_price, k_depth,
             p_side, p_price, p_depth,
             p_token, p_opposite_ask) in candidates:
            combined = k_price + p_price

            if combined >= ARB_THRESHOLD + ARB_TOLERANCE:
                # Too far from arb — quiet path, no per-attempt log.
                continue

            ctx = {
                "kalshi_snap": kalshi_snap,
                "info":        info,
                "k_side":      k_side,
                "k_price":     k_price,
                "k_depth":     k_depth,
                "p_side":      p_side,
                "p_price":     p_price,
                "p_depth":     p_depth,
                "combined":    combined,
                "k_age_ms":    int(k_age * 1000),
                "p_age_ms":    int(p_age * 1000),
            }

            # Depth gate.
            if k_depth is None or k_depth < trade_size:
                self._log_skip_throttled("thin_kalshi_depth", "📉", (
                    f"ARB {self.asset} {k_side.upper()}: insufficient Kalshi depth "
                    f"({k_depth}) < {trade_size} at top of ask — skipping"
                ))
                self._log_attempt(ctx, decision="skip", reason="thin_kalshi_depth")
                continue
            if p_depth is None or p_depth < trade_size:
                self._log_skip_throttled("thin_poly_depth", "📉", (
                    f"ARB {self.asset} {p_side.upper()}: insufficient Poly depth "
                    f"({p_depth}) < {trade_size} at top of ask — skipping"
                ))
                self._log_attempt(ctx, decision="skip", reason="thin_poly_depth")
                continue

            # Polymarket min order value: trade_size × poly_price must clear
            # $1. Arb requires equal share counts on both legs, so a cheap Poly
            # leg can't be padded alone — skip instead. (Kalshi has no min.)
            poly_order_usd = trade_size * p_price / 100.0
            if poly_order_usd < ARB_POLY_MIN_ORDER_USD:
                self._log_skip_throttled("below_poly_min", "💵", (
                    f"ARB {self.asset} {p_side.upper()}: Poly order "
                    f"{trade_size}×{p_price}¢ = ${poly_order_usd:.2f} "
                    f"< ${ARB_POLY_MIN_ORDER_USD:.2f} min — skipping"
                ))
                self._log_attempt(ctx, decision="skip", reason="below_poly_min")
                continue

            profit = _net_profit(k_price, p_price, info.fee_bps, trade_size)
            ctx["net_profit_per_contract"] = round(profit, 4)

            # Bounded unwind cost.
            poly_order_price = min(p_price + ARB_TOLERANCE, 99)
            implied_bid      = 100 - p_opposite_ask
            unwind_slip      = max(0, poly_order_price - implied_bid)
            unwind_fee       = _poly_fee_per_contract(max(implied_bid, 1), info.fee_bps)
            unwind_cost_per  = unwind_slip + unwind_fee
            expected_total   = profit * trade_size
            unwind_total     = unwind_cost_per * trade_size
            ctx["poly_order_price"] = poly_order_price
            ctx["implied_unwind_bid"] = implied_bid
            ctx["unwind_cost_total_cents"] = round(unwind_total, 4)
            ctx["expected_total_cents"] = round(expected_total, 4)

            if expected_total <= 0 or unwind_total > ARB_UNWIND_RATIO_MAX * expected_total:
                self._log_skip_throttled("unwind_risk", "🛡", (
                    f"ARB {self.asset} {k_side.upper()}: unwind risk too high "
                    f"(unwind={unwind_total:.2f}¢, profit={expected_total:.2f}¢, "
                    f"ratio>{ARB_UNWIND_RATIO_MAX}× implied_bid={implied_bid}¢) — skipping"
                ))
                self._log_attempt(ctx, decision="skip", reason="unwind_risk")
                continue

            if profit < ARB_MIN_PROFIT:
                self._log_skip_throttled("below_min_profit", "💡", (
                    f"ARB {self.asset}: K-{k_side.upper()}@{k_price}¢ + P-{p_side.upper()}@{p_price}¢ "
                    f"= {combined}¢, net={profit:.2f}¢ < min={ARB_MIN_PROFIT}¢ after fees"
                ))
                self._log_attempt(ctx, decision="skip", reason="below_min_profit")
                continue

            self._on_log("🔎", (
                f"ARB OPPORTUNITY {self.asset}: "
                f"Kalshi {k_side.upper()}@{k_price}¢  +  Poly {p_side.upper()}@{p_price}¢ "
                f"= {combined}¢  →  net ~{profit:.2f}¢/contract after fees"
            ))
            self._log_attempt(ctx, decision="fire", reason=None)

            with self._lock:
                if self._attempted:
                    return
                self._attempted = True
                self._last_attempt_ts = time.time()

            detection_ts = time.time()
            threading.Thread(
                target=self._execute,
                args=(kalshi_snap, k_side, k_price, p_side, p_token, p_price,
                      info.fee_bps, info.fee_source, profit, detection_ts,
                      poly_order_price, implied_bid, k_age, p_age, trade_size),
                daemon=True,
            ).start()
            return

        # End of candidate loop: emit periodic stats summary.
        self._maybe_log_stats()

    # ── Early exit (basis-risk avoidance) ──────────────────────────────────────

    def _maybe_early_exit(self, kalshi_snap: MarketSnapshot):
        """
        Called each tick. If we're in the last ARB_EXIT_WINDOW_SECS and the price
        has drifted within ARB_EXIT_BUFFER_PCT of the strike (the venue-
        disagreement danger zone), SELL BOTH LEGS of EVERY open position now rather
        than hold into a resolution where Kalshi and Polymarket might settle
        oppositely (which can turn the locked arb into a double loss).

        Disabled when ARB_EXIT_BUFFER_PCT == 0 (hold to resolution).
        """
        if ARB_EXIT_BUFFER_PCT <= 0:
            return
        if kalshi_snap.secs_left > ARB_EXIT_WINDOW_SECS:
            return
        strike = getattr(kalshi_snap, "floor_strike", None)
        spot   = get_spot(self.asset)
        if strike is None or spot is None:
            return
        dist_pct = abs(spot - float(strike)) / spot * 100.0
        if dist_pct >= ARB_EXIT_BUFFER_PCT:
            return  # safely away from the strike — hold to resolution as normal

        # In the danger zone near expiry → flatten every open position. Snapshot
        # the list under lock; _early_exit_one re-checks each position's phase.
        for pos in self.positions:
            if pos.phase == "open":
                self._early_exit_one(pos, kalshi_snap, spot, strike, dist_pct)

    def _early_exit_one(self, pos: "ArbPosition", kalshi_snap: MarketSnapshot,
                        spot: float, strike, dist_pct: float):
        """Flatten both legs of a single open position (see _maybe_early_exit)."""
        with self._lock:
            if pos.phase != "open":
                return
            pos.phase = "exiting"
        self._on_log("🚪", (
            f"ARB {self.asset} EARLY EXIT: {kalshi_snap.secs_left}s left, price "
            f"{spot:.4f} only {dist_pct:.3f}% from strike {strike} — flattening "
            f"both legs to avoid venue-disagreement risk."
        ))
        k_side = pos.kalshi_side
        p_token = pos.poly_token_id
        p_side  = pos.poly_side
        n_k = pos.k_filled
        n_p = pos.p_filled
        fee_bps = 1000  # gamma default; sells are price-insensitive (FAK @ market)
        k_sold = {"v": 0.0}
        p_sold = {"v": 0.0}

        def do_k():
            k_sold["v"] = _kalshi_ioc_sell(kalshi_snap, k_side, n_k)
        def do_p():
            p_sold["v"] = self._poly.place_sell_fok(p_token, n_p, fee_bps)

        threads = [threading.Thread(target=do_k, daemon=True),
                   threading.Thread(target=do_p, daemon=True)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=8.0)

        ok = (k_sold["v"] >= n_k - ARB_DUST_SHARES) and (p_sold["v"] >= int(n_p) - ARB_DUST_SHARES)
        with self._lock:
            pos.phase = "closed" if ok else "exit_partial"
        _log_trade({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "arb_early_exit",
            "asset": self.asset,
            "secs_left": kalshi_snap.secs_left,
            "spot": spot, "strike": strike, "dist_pct": round(dist_pct, 4),
            "k_side": k_side, "k_filled": n_k, "k_sold": k_sold["v"],
            "p_side": p_side, "p_filled": n_p, "p_sold": p_sold["v"],
            "fully_exited": ok,
        })
        self._on_log("🚪" if ok else "⚠", (
            f"ARB {self.asset} early-exit {'done' if ok else 'PARTIAL'}: "
            f"K sold {k_sold['v']}/{n_k}, P sold {p_sold['v']}/{n_p}"
        ))
        # Clean success-CSV row for this exit.
        _log_success_csv({
            "ts": datetime.now(timezone.utc).isoformat(), "event": "exit",
            "asset": self.asset, "kalshi_ticker": kalshi_snap.ticker,
            "secs_left": kalshi_snap.secs_left,
            "kalshi_side": k_side, "kalshi_filled": n_k, "k_sold": k_sold["v"],
            "poly_side": p_side, "poly_filled": n_p, "p_sold": p_sold["v"],
            "fully_exited": ok, "spot": spot, "strike": strike,
        })
        if not ok:
            halt_trading(f"{self.asset} early-exit incomplete — possible residual "
                         f"position, inspect before restart.")

    # ── Unwind ───────────────────────────────────────────────────────────────

    def _unwind_naked(self, kalshi_snap: MarketSnapshot,
                      k_side: str, k_filled: float,
                      p_token: str, p_side: str, p_filled: float,
                      fee_bps: int, fee_source: str = "unknown",
                      poly_buy_price: Optional[int] = None,
                      implied_unwind_bid: Optional[int] = None,
                      naked_since_ts: Optional[float] = None,
                      kalshi_error: Optional[str] = None):
        """
        Immediately sell whichever leg filled when the other missed.
        Runs in its own thread so it never blocks the tick loop.
        """
        unwind_start = time.time()
        k_sold: float = 0.0
        p_sold: float = 0.0
        poly_held: bool = False

        # Hold-to-resolution check: small naked Poly legs auto-redeem at market
        # settlement; selling them via FAK introduces failure modes (ledger
        # race, thin-book partials) that have cost more than the held
        # directional risk would have over a ≤15-min window.
        poly_exposure_usd = (p_filled * (poly_buy_price or 0)) / 100.0
        if (p_filled > 0 and poly_buy_price is not None
                and poly_exposure_usd <= ARB_POLY_HOLD_NAKED_USD):
            poly_held = True

        def do_k_sell():
            nonlocal k_sold
            k_sold = _kalshi_ioc_sell(kalshi_snap, k_side, k_filled)

        def do_p_sell():
            nonlocal p_sold
            p_sold = self._poly.place_sell_fok(p_token, p_filled, fee_bps)

        threads = []
        if k_filled > 0:
            threads.append(threading.Thread(target=do_k_sell, daemon=True))
        if p_filled > 0 and not poly_held:
            threads.append(threading.Thread(target=do_p_sell, daemon=True))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Retry partial Poly unwinds: place_sell_fok already handles the ledger-lag
        # race internally, but a thin near-expiry book can leave the sell only
        # partially filled (sold < held) with no error. Re-send a FAK sell for the
        # remaining whole shares a few times before giving up. (Always-unwind policy
        # by user decision — we never hold a naked directional leg to resolution.)
        if p_filled > 0 and not poly_held:
            for attempt in range(1, ARB_UNWIND_RETRIES + 1):
                p_remaining_now = p_filled - p_sold
                if p_remaining_now <= ARB_DUST_SHARES:
                    break
                time.sleep(ARB_UNWIND_RETRY_DELAY)
                more = self._poly.place_sell_fok(p_token, p_remaining_now, fee_bps)
                p_sold += more
                self._on_log("🔁", (
                    f"ARB {self.asset} naked-unwind retry {attempt}/"
                    f"{ARB_UNWIND_RETRIES}: sold {more} more "
                    f"({p_filled - p_sold:.2f} still naked)"
                ))

        unwind_ms = int((time.time() - unwind_start) * 1000)
        naked_ms  = (int((time.time() - naked_since_ts) * 1000)
                     if naked_since_ts is not None else None)

        parts = []
        if k_filled > 0:
            parts.append(f"K-{k_side.upper()} sold={k_sold}/{k_filled}")
        if p_filled > 0:
            if poly_held:
                parts.append(f"P-{p_side.upper()} HELD={p_filled} "
                             f"(${poly_exposure_usd:.2f} ≤ ${ARB_POLY_HOLD_NAKED_USD:.2f})")
            else:
                parts.append(f"P-{p_side.upper()} sold={p_sold}/{p_filled}")

        # "ok" = remaining unsold exposure on each leg is within dust tolerance.
        # FAK sells whole shares only, so a fractional remainder ≤ ARB_DUST_SHARES
        # is expected and acceptable — it must NOT trip the kill-switch. A leg
        # we intentionally HELD to resolution is also acceptable (the position
        # auto-redeems at market close).
        k_remaining = k_filled - k_sold
        p_remaining = p_filled - p_sold
        ok = (k_remaining <= ARB_DUST_SHARES) and (
            poly_held or p_remaining <= ARB_DUST_SHARES
        )
        self._on_log(
            "🔄" if ok else "⚠",
            f"ARB {self.asset} UNWIND: {' | '.join(parts)} "
            f"(unwind_ms={unwind_ms}, naked_ms={naked_ms}, "
            f"remaining K={k_remaining:.2f} P={p_remaining:.2f})"
        )
        if not ok:
            # A leg could not be fully unwound even after retries → residual naked
            # exposure remains. By user decision (2026-06-05) we LOG and CONTINUE
            # rather than tripping the kill-switch: a small residual auto-redeems
            # at resolution and shouldn't stop the bot. The residual is recorded in
            # the arb_unwind trade-log row (fully_unwound=false) for manual review.
            self._on_log("⚠", (
                f"ARB {self.asset} unwind INCOMPLETE after {ARB_UNWIND_RETRIES} "
                f"retries — K sold {k_sold}/{k_filled}, P sold {p_sold}/{p_filled}. "
                f"Residual naked ~{p_remaining:.2f} shares left to resolution; "
                f"NOT halting (inspect/redeem manually)."
            ))

        # Estimated realized loss in cents: bought poly at poly_buy_price,
        # sold at implied bid (best-effort estimate; actual proceeds will
        # arrive from the Poly response if we add post-fill reconciliation).
        est_loss_cents = None
        if (poly_buy_price is not None and implied_unwind_bid is not None
                and p_sold > 0):
            slip = max(0, poly_buy_price - implied_unwind_bid)
            est_loss_cents = slip * p_sold

        _log_trade({
            "ts":            datetime.now(timezone.utc).isoformat(),
            "type":          "arb_unwind",
            "asset":         self.asset,
            "kalshi_ticker": kalshi_snap.ticker,
            "kalshi_side":   k_side,
            "kalshi_filled": k_filled,
            "kalshi_sold":   k_sold,
            "kalshi_error_on_buy": kalshi_error,
            "poly_side":     p_side,
            "poly_token_id": p_token,
            "poly_filled":   p_filled,
            "poly_sold":     p_sold,
            "poly_held_to_resolution": poly_held,
            "poly_exposure_usd": round(poly_exposure_usd, 4),
            "poly_buy_price": poly_buy_price,
            "implied_unwind_bid": implied_unwind_bid,
            "fee_bps":       fee_bps,
            "fee_source":    fee_source,
            "unwind_ms":     unwind_ms,
            "naked_ms":      naked_ms,
            "est_loss_cents": est_loss_cents,
            "fully_unwound": ok,
        })

    # ── Execution ────────────────────────────────────────────────────────────

    def _execute(self, kalshi_snap: MarketSnapshot,
                 k_side: str, k_price: int,
                 p_side: str, p_token: str, p_price: int,
                 fee_bps: int, fee_source: str, expected_profit: float,
                 detection_ts: float, poly_order_price: int,
                 implied_unwind_bid: int, k_age_at_detect: float,
                 p_age_at_detect: float, trade_size: int = ARB_TRADE_SIZE):
        """
        Sequential two-leg execution: Poly FOK first, Kalshi IOC only if Poly fills.
        If Poly misses → nothing was touched on Kalshi → zero loss, allow retry.
        If Poly fills but Kalshi misses → unwind Poly, lock window.
        """
        n = trade_size

        # Observability: measure price drift between detection and fire time.
        # Pure logging — does not affect the EV recheck or order placement.
        drift = self._drift_probe(kalshi_snap, k_side, p_side,
                                  k_price, p_price, detection_ts)

        # Re-check EV at the prices we're actually about to send. Detection used
        # k_price + p_price; we're firing Poly up to ARB_TOLERANCE¢ worse. If
        # that drift kills the edge, abort before touching either venue.
        exec_profit = _net_profit(k_price, poly_order_price, fee_bps, n)
        if exec_profit < ARB_MIN_PROFIT:
            self._on_log("⏸", (
                f"ARB {self.asset} EV recheck FAILED at exec prices: "
                f"K-{k_side.upper()}@{k_price}¢ + P-{p_side.upper()}@{poly_order_price}¢ "
                f"→ net={exec_profit:.2f}¢ < min={ARB_MIN_PROFIT}¢ "
                f"(detection net was {expected_profit:.2f}¢)"
            ))
            self._stats["skip_ev_recheck"] += 1
            _log_trade({
                "ts":          datetime.now(timezone.utc).isoformat(),
                "type":        "arb_ev_abort",
                "asset":       self.asset,
                "k_side":      k_side, "k_price": k_price,
                "p_side":      p_side, "p_price": p_price,
                "poly_order_price":         poly_order_price,
                "detection_net":            round(expected_profit, 4),
                "execution_net":            round(exec_profit, 4),
                "fee_bps":     fee_bps, "fee_source": fee_source,
                **drift,
            })
            with self._lock:
                self._attempted = False
            return

        # ── Step 1: Poly FOK ─────────────────────────────────────────────────
        poly_ts0 = time.time()
        p_filled: float = 0.0
        p_err: Optional[str] = None
        p_fill_price: Optional[float] = None  # actual avg fill cents (vs the limit)
        try:
            p_filled = self._poly.place_fok(p_token, poly_order_price, n, fee_bps)
            # Actual average fill price Poly reported — usually better than the
            # limit (poly_order_price) we sent, since FOK fills at the resting ask.
            p_fill_price = getattr(self._poly, "_last_fill_price_cents", None)
        except Exception as e:
            p_err = str(e)
            self._on_log("✗", f"ARB {self.asset} Poly leg error: {e}")
        poly_ms = int((time.time() - poly_ts0) * 1000)

        if p_filled == 0:
            self._stats["poly_miss"] += 1
            self._on_log("⏸", (
                f"ARB {self.asset} — Poly {p_side.upper()}@{poly_order_price}¢ not filled. "
                f"Kalshi not touched. Retrying next tick."
            ))
            _log_trade({
                "ts":          datetime.now(timezone.utc).isoformat(),
                "type":        "arb_poly_miss",
                "asset":       self.asset,
                "k_side":      k_side, "k_price": k_price,
                "p_side":      p_side, "p_price": p_price,
                "poly_order_price": poly_order_price,
                "poly_latency_ms":  poly_ms,
                "poly_error":  p_err,
                "fee_bps":     fee_bps, "fee_source": fee_source,
                **drift,
            })
            with self._lock:
                self._attempted = False
            return

        poly_fill_ts = time.time()

        # ── Step 2: Kalshi IOC (only reached if Poly filled) ─────────────────
        # Hedge the WHOLE-SHARE portion of the actual Poly fill. Poly can over-
        # fill (e.g. 5.55 shares from a 5-share request) because the dollar
        # budget buys extra at better prices; Kalshi trades whole contracts only.
        # Buying floor(p_filled) matches as much as possible; the sub-share
        # remainder is accepted as dust (see _unwind / kill-switch logic below).
        k_target = int(p_filled)  # floor → whole contracts to hedge
        if k_target < 1:
            # Poly filled less than one whole share — nothing hedgeable on Kalshi.
            # Treat the tiny fractional Poly position as dust; unwind it.
            self._on_log("⚠", (
                f"ARB {self.asset} Poly filled {p_filled} (<1 share) — "
                f"sub-share dust, unwinding Poly leg."
            ))
            threading.Thread(
                target=self._unwind_naked,
                args=(kalshi_snap, k_side, 0.0, p_token, p_side, p_filled, fee_bps,
                      fee_source, poly_order_price, implied_unwind_bid, poly_fill_ts,
                      None),
                daemon=True,
            ).start()
            return

        # Bid above the detected ask by ARB_KALSHI_SLIPPAGE so the IOC still
        # crosses if the ask ticked up during the Poly leg. Capped at 99¢. The
        # arb still profits as long as (k_buy_price + poly_order_price) < 100.
        k_buy_price = min(k_price + ARB_KALSHI_SLIPPAGE, 99)
        k_ts0 = time.time()
        k_filled: float = 0.0
        k_err: Optional[str] = None
        if DRY_RUN:
            k_filled = float(k_target)
        else:
            try:
                k_filled = _kalshi_ioc(
                    kalshi_snap, k_side, k_buy_price, k_target,
                    timeout=ARB_KALSHI_IOC_TIMEOUT,
                )
            except Exception as e:
                k_err = str(e)
                self._on_log("✗", f"ARB {self.asset} Kalshi leg error: {e}")
        kalshi_ms = int((time.time() - k_ts0) * 1000)
        naked_ms  = int((time.time() - poly_fill_ts) * 1000)

        # Surface the Kalshi leg result on the dashboard/terminal channel so it's
        # visible alongside the Poly lines (file logs alone weren't showing it).
        self._on_log(
            "🟢" if k_filled >= k_target else ("🟡" if k_filled > 0 else "🔴"),
            f"ARB {self.asset} Kalshi {k_side.upper()} buy @≤{k_buy_price}¢ "
            f"x{k_target} → filled {k_filled} ({kalshi_ms}ms)"
        )

        # Naked exposure = Poly shares not covered by Kalshi contracts.
        naked_shares = p_filled - k_filled

        if k_filled == 0:
            # Poly filled but Kalshi missed entirely → unwind all Poly.
            self._stats["kalshi_miss_naked"] += 1
            self._on_log("⚠", (
                f"ARB {self.asset} NAKED: "
                f"Poly {p_side.upper()}@{poly_order_price}¢ ×{p_filled} FILLED — "
                f"Kalshi MISSED. Unwinding Poly. Locked for this window."
            ))
            threading.Thread(
                target=self._unwind_naked,
                args=(kalshi_snap, k_side, 0.0, p_token, p_side, p_filled, fee_bps,
                      fee_source, poly_order_price, implied_unwind_bid, poly_fill_ts,
                      k_err),
                daemon=True,
            ).start()
            return

        if naked_shares > ARB_DUST_SHARES:
            # Kalshi only PARTIALLY hedged (e.g. got 3 of 5 while Poly has 5.55).
            # The whole-share hedge stays on; unwind only the uncovered Poly
            # excess so we're not left directionally exposed.
            self._stats["kalshi_partial_naked"] += 1
            self._on_log("⚠", (
                f"ARB {self.asset} PARTIAL HEDGE: Kalshi filled {k_filled}/{k_target}, "
                f"Poly {p_filled} → {naked_shares:.2f} shares naked. "
                f"Unwinding the uncovered Poly excess."
            ))
            threading.Thread(
                target=self._unwind_naked,
                args=(kalshi_snap, k_side, 0.0, p_token, p_side, naked_shares, fee_bps,
                      fee_source, poly_order_price, implied_unwind_bid, poly_fill_ts,
                      k_err),
                daemon=True,
            ).start()
            # Fall through: record the hedged portion (k_filled pairs) as a position.

        matched   = min(k_filled, p_filled)
        phase     = "open" if (k_filled > 0 and p_filled > 0) else "partial"
        combined  = k_price + p_price
        max_profit = (100 - combined) * max(matched, 1) / 100.0

        pos = ArbPosition(
            asset=self.asset,
            kalshi_ticker=kalshi_snap.ticker,
            kalshi_side=k_side,
            kalshi_price=k_price,
            poly_token_id=p_token,
            poly_side=p_side,
            poly_price=p_price,
            count=matched,
            k_filled=k_filled,
            p_filled=p_filled,
            expected_profit=expected_profit,
            poly_fill_price=p_fill_price,
            phase=phase,
        )
        with self._lock:
            # APPEND (never overwrite): every leg stays tracked so none is
            # orphaned/unhedged. The window-entry cap in update() bounds how many
            # accumulate.
            self._positions.append(pos)
            # Re-arm: clear the in-flight guard so the trader can place ANOTHER
            # order later this window if a fresh arb appears. Spacing is enforced
            # by ARB_ORDER_COOLDOWN; total entries by ARB_MAX_WINDOW_ENTRIES.
            self._attempted = False
            self._last_attempt_ts = time.time()
        self._stats["entries"] += 1

        _log_trade({
            "ts":            pos.ts,
            "type":          "arb_entry",
            "asset":         self.asset,
            "kalshi_ticker": kalshi_snap.ticker,
            "window_ts":     kalshi_snap.window_ts,
            "secs_left":     kalshi_snap.secs_left,
            "kalshi_side":   k_side,
            "kalshi_price":  k_price,
            "kalshi_filled": k_filled,
            "poly_side":     p_side,
            "poly_token_id": p_token,
            "poly_price":    p_price,
            "poly_order_price": poly_order_price,
            "poly_fill_price":  round(p_fill_price, 2) if p_fill_price is not None else None,
            "poly_filled":   p_filled,
            "count":         matched,
            "combined_cost": combined,
            "fee_bps":       fee_bps,
            "fee_source":    fee_source,
            "net_profit_per_contract": round(expected_profit, 4),
            "exec_profit_per_contract": round(exec_profit, 4),
            "phase":         phase,
            "k_snap_age_ms_at_detect": int(k_age_at_detect * 1000),
            "p_snap_age_ms_at_detect": int(p_age_at_detect * 1000),
            "detection_to_fire_ms":    int((poly_ts0 - detection_ts) * 1000),
            "poly_leg_latency_ms":     poly_ms,
            "kalshi_leg_latency_ms":   kalshi_ms,
            "naked_duration_ms":       naked_ms,
            **drift,
        })

        # Clean success-CSV row for this entry (both legs filled / hedged).
        _log_success_csv({
            "ts": pos.ts, "event": "entry", "asset": self.asset,
            "window_ts": kalshi_snap.window_ts,
            "kalshi_ticker": kalshi_snap.ticker, "secs_left": kalshi_snap.secs_left,
            "kalshi_side": k_side, "kalshi_price": k_price, "kalshi_filled": k_filled,
            "poly_side": p_side, "poly_order_price": poly_order_price,
            "poly_fill_price": round(p_fill_price, 2) if p_fill_price is not None else "",
            "poly_filled": p_filled, "count": matched, "combined_cost": combined,
            "net_profit_per_contract": round(expected_profit, 4),
            "exec_profit_per_contract": round(exec_profit, 4),
        })

        if phase == "open":
            self._on_log("✅", (
                f"ARB ENTERED {self.asset}: "
                f"Kalshi {k_side.upper()}@{k_price}¢  +  Poly {p_side.upper()}@{p_price}¢ "
                f"× {matched} contracts  combined={combined}¢  "
                f"max profit/pair ≈ ${max_profit:.4f}  (resolves at expiry)"
            ))
        if DRY_RUN:
            self._on_log("📋", (
                f"[DRY RUN] ARB {self.asset} "
                f"K:{k_side.upper()}@{k_price}¢  P:{p_side.upper()}@{p_price}¢  ×{n}"
            ))


# ── Factory ───────────────────────────────────────────────────────────────────

def build_arb_traders(assets: list, poly_client: PolyClient,
                      on_log=None) -> Dict[str, ArbTrader]:
    """
    Build one ArbTrader per asset. Market IDs are discovered lazily
    per window via get_market_for_window() — no static config file needed.
    """
    traders = {}
    for asset in assets:
        if asset.upper() not in ("BTC", "ETH", "SOL"):
            log.warning("No Polymarket 15-min market known for %s, skipping arb", asset)
            continue
        traders[asset] = ArbTrader(asset=asset, poly_client=poly_client, on_log=on_log)
        log.info("ArbTrader ready: %s (market discovered per window)", asset)
    return traders
