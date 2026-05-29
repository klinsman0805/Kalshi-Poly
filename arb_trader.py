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
import json
import math
import time
import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict

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

# Phase-2 safety knobs (all in seconds / cents / multiples)
ARB_K_SNAP_MAX_AGE   = float(os.getenv("ARB_K_SNAP_MAX_AGE",   "1.5"))
ARB_P_SNAP_MAX_AGE   = float(os.getenv("ARB_P_SNAP_MAX_AGE",   "1.5"))
ARB_KALSHI_IOC_TIMEOUT = float(os.getenv("ARB_KALSHI_IOC_TIMEOUT", "3.0"))
# Unwind cost ceiling: if expected_loss_on_unwind > N × expected_profit, abort.
ARB_UNWIND_RATIO_MAX = float(os.getenv("ARB_UNWIND_RATIO_MAX", "3.0"))
# Periodic stats summary: seconds between per-window stats dumps.
ARB_STATS_INTERVAL   = float(os.getenv("ARB_STATS_INTERVAL",   "60"))

TRADES_FILE = Path(os.getenv("TRADES_FILE", "trades.jsonl"))

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

# ── Trade log ─────────────────────────────────────────────────────────────────

def _log_trade(record: dict):
    try:
        with TRADES_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.error("arb trade log: %s", e)

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

    path    = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("POST", path)
    url     = API_BASE + "/portfolio/orders"
    try:
        r = SESSION.post(url, json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        order = r.json().get("order", {})
        return float(order.get("fill_count_fp", "0") or "0")
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

    path    = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("POST", path)
    url     = API_BASE + "/portfolio/orders"
    try:
        r = SESSION.post(url, json=body, headers=headers, timeout=ORDER_TIMEOUT)
        r.raise_for_status()
        order = r.json().get("order", {})
        return float(order.get("fill_count_fp", "0") or "0")
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
    poly_price:     int
    count:          float               # matched contracts (min of both fills)
    k_filled:       float               # actual Kalshi fill
    p_filled:       float               # actual Polymarket fill
    expected_profit: float              # ¢ per contract after fees
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

        self._position: Optional[ArbPosition] = None
        self._attempted  = False
        self._last_attempt_ts: float = 0.0
        self._market_info: Optional[PolyMarketInfo] = None
        self._lock = threading.Lock()
        self._last_price_log_ts: float = 0.0   # rate-limit combined price log

        # Phase-3 observability: per-window decision counters + last stats emit.
        self._stats: Counter = Counter()
        self._last_stats_log_ts: float = 0.0
        self._window_start_ts: float = time.time()

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
            self._position      = None
            self._attempted     = False
            self._last_attempt_ts = 0.0
            self._market_info   = None
            self._stats.clear()
            self._last_stats_log_ts = 0.0
            self._window_start_ts = time.time()

    @property
    def position(self) -> Optional[ArbPosition]:
        return self._position

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
        with self._lock:
            if self._attempted or self._position is not None:
                return
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
            # REST-only seed: give WS 2s to catch up, fail if older than 5s.
            if p_age < 2.0 or p_age > 5.0:
                return
        else:
            if p_age > ARB_P_SNAP_MAX_AGE:
                return

        k_yes = kalshi_snap.yes_ask
        k_no  = kalshi_snap.no_ask
        p_yes = poly_snap.yes_ask
        p_no  = poly_snap.no_ask

        if None in (k_yes, k_no, p_yes, p_no):
            return

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
            if k_depth is None or k_depth < ARB_TRADE_SIZE:
                self._on_log("📉", (
                    f"ARB {self.asset} {k_side.upper()}: insufficient Kalshi depth "
                    f"({k_depth}) < {ARB_TRADE_SIZE} at top of ask — skipping"
                ))
                self._log_attempt(ctx, decision="skip", reason="thin_kalshi_depth")
                continue
            if p_depth is None or p_depth < ARB_TRADE_SIZE:
                self._on_log("📉", (
                    f"ARB {self.asset} {p_side.upper()}: insufficient Poly depth "
                    f"({p_depth}) < {ARB_TRADE_SIZE} at top of ask — skipping"
                ))
                self._log_attempt(ctx, decision="skip", reason="thin_poly_depth")
                continue

            profit = _net_profit(k_price, p_price, info.fee_bps, ARB_TRADE_SIZE)
            ctx["net_profit_per_contract"] = round(profit, 4)

            # Bounded unwind cost.
            poly_order_price = min(p_price + ARB_TOLERANCE, 99)
            implied_bid      = 100 - p_opposite_ask
            unwind_slip      = max(0, poly_order_price - implied_bid)
            unwind_fee       = _poly_fee_per_contract(max(implied_bid, 1), info.fee_bps)
            unwind_cost_per  = unwind_slip + unwind_fee
            expected_total   = profit * ARB_TRADE_SIZE
            unwind_total     = unwind_cost_per * ARB_TRADE_SIZE
            ctx["poly_order_price"] = poly_order_price
            ctx["implied_unwind_bid"] = implied_bid
            ctx["unwind_cost_total_cents"] = round(unwind_total, 4)
            ctx["expected_total_cents"] = round(expected_total, 4)

            if expected_total <= 0 or unwind_total > ARB_UNWIND_RATIO_MAX * expected_total:
                self._on_log("🛡", (
                    f"ARB {self.asset} {k_side.upper()}: unwind risk too high "
                    f"(unwind={unwind_total:.2f}¢, profit={expected_total:.2f}¢, "
                    f"ratio>{ARB_UNWIND_RATIO_MAX}× implied_bid={implied_bid}¢) — skipping"
                ))
                self._log_attempt(ctx, decision="skip", reason="unwind_risk")
                continue

            if profit < ARB_MIN_PROFIT:
                self._on_log("💡", (
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
                      poly_order_price, implied_bid, k_age, p_age),
                daemon=True,
            ).start()
            return

        # End of candidate loop: emit periodic stats summary.
        self._maybe_log_stats()

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

        def do_k_sell():
            nonlocal k_sold
            k_sold = _kalshi_ioc_sell(kalshi_snap, k_side, k_filled)

        def do_p_sell():
            nonlocal p_sold
            p_sold = self._poly.place_sell_fok(p_token, p_filled, fee_bps)

        threads = []
        if k_filled > 0:
            threads.append(threading.Thread(target=do_k_sell, daemon=True))
        if p_filled > 0:
            threads.append(threading.Thread(target=do_p_sell, daemon=True))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        unwind_ms = int((time.time() - unwind_start) * 1000)
        naked_ms  = (int((time.time() - naked_since_ts) * 1000)
                     if naked_since_ts is not None else None)

        parts = []
        if k_filled > 0:
            parts.append(f"K-{k_side.upper()} sold={k_sold}/{k_filled}")
        if p_filled > 0:
            parts.append(f"P-{p_side.upper()} sold={p_sold}/{p_filled}")

        ok = (k_filled == 0 or k_sold == k_filled) and (p_filled == 0 or p_sold == p_filled)
        self._on_log(
            "🔄" if ok else "⚠",
            f"ARB {self.asset} UNWIND: {' | '.join(parts)} "
            f"(unwind_ms={unwind_ms}, naked_ms={naked_ms})"
        )

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
                 p_age_at_detect: float):
        """
        Sequential two-leg execution: Poly FOK first, Kalshi IOC only if Poly fills.
        If Poly misses → nothing was touched on Kalshi → zero loss, allow retry.
        If Poly fills but Kalshi misses → unwind Poly, lock window.
        """
        n = ARB_TRADE_SIZE

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
            })
            with self._lock:
                self._attempted = False
            return

        # ── Step 1: Poly FOK ─────────────────────────────────────────────────
        poly_ts0 = time.time()
        p_filled: float = 0.0
        p_err: Optional[str] = None
        try:
            p_filled = self._poly.place_fok(p_token, poly_order_price, n, fee_bps)
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
            })
            with self._lock:
                self._attempted = False
            return

        poly_fill_ts = time.time()

        # ── Step 2: Kalshi IOC (only reached if Poly filled) ─────────────────
        k_ts0 = time.time()
        k_filled: float = 0.0
        k_err: Optional[str] = None
        if DRY_RUN:
            k_filled = float(n)
        else:
            try:
                k_filled = _kalshi_ioc(
                    kalshi_snap, k_side, k_price, n,
                    timeout=ARB_KALSHI_IOC_TIMEOUT,
                )
            except Exception as e:
                k_err = str(e)
                self._on_log("✗", f"ARB {self.asset} Kalshi leg error: {e}")
        kalshi_ms = int((time.time() - k_ts0) * 1000)
        naked_ms  = int((time.time() - poly_fill_ts) * 1000)

        if k_filled == 0:
            # Poly filled but Kalshi missed → unwind Poly, lock window.
            self._stats["kalshi_miss_naked"] += 1
            self._on_log("⚠", (
                f"ARB {self.asset} PARTIAL FILL: "
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
            phase=phase,
        )
        with self._lock:
            self._position = pos
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
