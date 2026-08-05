"""
feeds/kalshi_order.py — Kalshi order placement for the weather strategy.

Uses the V2 order-creation endpoint (POST /portfolio/events/orders). The V1
endpoint this originally targeted (/portfolio/orders, the same one trader.py's
retired scalper used) returned 410 Gone on first live fire on 2026-07-24 —
"deprecated_v1_order_endpoint" — caught safely: the failed request raised,
_place_live's caller logged a miss, and zero contracts/dollars moved. Endpoint,
request, and response shapes below were confirmed against
https://docs.kalshi.com/api-reference/orders/create-order-v2 (fetched twice,
cross-checked) but NOT yet fired live — this fix itself is still unverified
against the real API. Watch the next live attempt closely.

V2 schema differences from V1 (this module, not the crypto trader):
  • side is "bid"/"ask" (buy/sell direction), not "yes"/"no" + a separate
    action field — there's no explicit outcome-side field at all, so a ticker
    always refers to a single yes-contract and bid=buy-yes, ask=sell-yes. This
    matches how our normalized buckets already work (always "buy/sell YES on
    this specific range").
  • count and price are STRINGS ("10.00", "0.5600"), price in DOLLARS not cents.
  • self_trade_prevention_type is required; "taker_at_cross" is the sensible
    default (execute as taker if we'd otherwise cross our own resting order —
    we don't rest orders here, so this never actually triggers).
  • the response is flat (order_id/fill_count/average_fill_price at the top
    level), not nested under "order" — and V2 gives a REAL average_fill_price
    when there's a fill, so unlike the V1-era plan this reports the actual
    average price, not an assumed limit-price fill.

Kalshi's IOC (immediate_or_cancel) fills what's available immediately at-or-
better than the limit and cancels the rest — FAK semantics (partial fills
allowed), not Polymarket's stricter FOK. Fine for entries (a partial fill is
just a smaller position, same as the existing miss/cooldown handling assumes);
for exits it's exactly the intended "sell what's there" behavior.
"""

import json
import logging
import time
import uuid

from engine import kalshi_get, _auth_headers, API_BASE, SESSION, ORDER_TIMEOUT

log = logging.getLogger("feeds.kalshi_order")


def _post_order(body: dict) -> dict:
    path = "/trade-api/v2/portfolio/events/orders"
    headers = _auth_headers("POST", path)
    url = f"{API_BASE}/portfolio/events/orders"
    t0 = time.perf_counter()
    r = SESSION.post(url, json=body, headers=headers, timeout=ORDER_TIMEOUT)
    ms = (time.perf_counter() - t0) * 1000
    log.info("KALSHI-WX ORDER rtt=%.0fms side=%s ticker=%s status=%d",
             ms, body.get("side"), body.get("ticker"), r.status_code)
    if not r.ok:
        log.error("KALSHI-WX ORDER body: %s", json.dumps(body))
        log.error("KALSHI-WX ORDER error: %s", r.text)
    r.raise_for_status()
    return r.json()


def place_ioc(ticker: str, action: str, price_c: float, count: int) -> tuple:
    """Place an immediate-or-cancel order. action = "buy" | "sell" (mapped to
    side "bid"/"ask"), always on the market's yes-contract.

    Returns (filled_count, avg_fill_price_c) — the REAL volume-weighted average
    fill price from the response, not the submitted limit. (0, None) if nothing
    filled.
    """
    count = int(count)
    if count <= 0:
        return 0, None
    price_c = max(1, min(99, int(round(price_c))))
    side = "bid" if action == "buy" else "ask"
    body = {
        "ticker": ticker,
        "side": side,
        "count": f"{count:.2f}",
        "price": f"{price_c / 100.0:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4()),
        # matching the documented example's full field set exactly, even though
        # these are described as optional/defaulted — the previous attempt's
        # bare-required-fields body got a bare "invalid_parameters" with no
        # detail, so this rules out a stricter-than-documented validator.
        "post_only": False,
        "cancel_order_on_pause": False,
        "reduce_only": False,
        "subaccount": 0,
        "exchange_index": 0,
    }
    resp = _post_order(body)
    filled = float(resp.get("fill_count", "0") or "0")
    if filled <= 0:
        return 0, None
    avg_price = resp.get("average_fill_price")
    fill_c = round(float(avg_price) * 100.0, 2) if avg_price is not None else float(price_c)
    return filled, fill_c


def held_contracts(ticker: str):
    """Current position count on this ticker, or None if unavailable. Used the
    same way Polymarket's _held_shares is — a source of truth if a sell needs
    to know what's actually left to unwind. NOTE: unlike order creation, this
    read endpoint was not flagged as deprecated — left as originally written,
    but equally unfired against the real API until observed working."""
    try:
        d = kalshi_get("/portfolio/positions", params={"ticker": ticker})
        for p in d.get("market_positions", []):
            if p.get("ticker") == ticker:
                return int(float(p.get("position", 0)))
        return 0
    except Exception as e:  # noqa: BLE001
        log.debug("held_contracts(%s) failed: %s", ticker, e)
        return None
