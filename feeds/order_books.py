"""
feeds/order_books.py — full-ladder order book fetches, for measurement only.

Nothing on the trading path imports this. It exists because the candidate
recorder stores one bucket per event per cycle, with `book_depth` populated
only when a row got as far as _book_confirm — which happens exclusively on
ENTER. Every row a gate refused therefore carries a bid and ask of unknown
size, and across 1,608 EARLY rows `book_depth` was recorded 0 times.

That gap matters. Selling those rows at the recorded bid backtests at +9.4c a
share and +22.3c after fees, which is either a variance premium worth trading
or a phantom quote one share deep. Stored data cannot tell the two apart, so
this module captures what would settle it: every bucket in the ladder, both
sides, with sizes.

Two venues, two shapes, one call each per event:

  Polymarket  POST /books takes every token in the ladder at once and returns
              full depth per side. Levels come back UNSORTED — an early probe
              returned asks at 0.953, 0.952, 0.251 in that order — so they are
              sorted here rather than trusted.

  Kalshi      GET /markets?event_ticker= returns the whole strike ladder with
              yes_bid_size_fp / yes_ask_size_fp already on each market. The
              /orderbook endpoint answers 200 with an empty book even when
              authenticated and even when the market has 7,436 in volume, so
              it is not used.

Both return None on failure. A caller must treat that as "unknown", never as
"empty" — an empty book and an unreachable API mean opposite things here.
"""

import logging

import requests

log = logging.getLogger("feeds.order_books")

CLOB_BOOKS = "https://clob.polymarket.com/books"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TIMEOUT = 20
MAX_LEVELS = 10          # per side, deepest books are noise past this
BATCH = 40               # tokens per POST; a wide ladder is 11


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _levels(raw, descending):
    """[{price,size}] -> [[price_c, size]] sorted best-first, capped."""
    out = []
    for lv in raw or []:
        p, s = _f(lv.get("price")), _f(lv.get("size"))
        if p is None or s is None:
            continue
        out.append([round(p * 100.0, 4), s])
    out.sort(key=lambda x: -x[0] if descending else x[0])
    return out[:MAX_LEVELS]


def fetch_poly_books(token_ids, timeout=TIMEOUT):
    """token_id -> {bid_levels, ask_levels, book_ts, tick_size, min_order_size,
    last_trade_c}. None if the call failed; missing tokens simply absent."""
    ids = [t for t in (token_ids or []) if t]
    if not ids:
        return {}
    out = {}
    try:
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            r = requests.post(CLOB_BOOKS,
                              json=[{"token_id": t} for t in chunk],
                              timeout=timeout)
            r.raise_for_status()
            for b in r.json() or []:
                tok = b.get("asset_id")
                if not tok:
                    continue
                out[str(tok)] = {
                    # bids descend (best = highest), asks ascend (best = lowest)
                    "bid_levels": _levels(b.get("bids"), descending=True),
                    "ask_levels": _levels(b.get("asks"), descending=False),
                    "book_ts": b.get("timestamp"),
                    "tick_size": _f(b.get("tick_size")),
                    "min_order_size": _f(b.get("min_order_size")),
                    "last_trade_c": (lambda v: round(v * 100.0, 4) if v is not None else None)(
                        _f(b.get("last_trade_price"))),
                }
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("poly books fetch failed (%d tokens): %s", len(ids), e)
        return None


def fetch_kalshi_ladder(event_ticker, timeout=TIMEOUT):
    """ticker -> top-of-book with sizes, for every strike in the event.

    Kalshi publishes only the best level, but it publishes the SIZE there,
    which is the number this whole exercise turns on.
    """
    if not event_ticker:
        return {}
    try:
        from engine import kalshi_get
        data = kalshi_get("/markets", {"event_ticker": event_ticker,
                                       "limit": 200}) or {}
    except Exception as e:  # noqa: BLE001
        log.debug("kalshi ladder fetch failed %s: %s", event_ticker, e)
        return None
    out = {}
    for m in data.get("markets") or []:
        tk = m.get("ticker")
        if not tk:
            continue
        bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
        out[tk] = {
            "bid_levels": ([[round(bid * 100.0, 4),
                             _f(m.get("yes_bid_size_fp")) or 0.0]]
                           if bid is not None else []),
            "ask_levels": ([[round(ask * 100.0, 4),
                             _f(m.get("yes_ask_size_fp")) or 0.0]]
                           if ask is not None else []),
            # NOT a book timestamp. `updated_time` is when the market RECORD
            # last changed, and the first live slice read a median age of
            # 93,176s (26 hours) from it on quotes that were plainly current.
            # Kalshi publishes no per-book timestamp, so book_ts stays None and
            # the misleading field is kept under its own name.
            "book_ts": None,
            "market_updated": m.get("updated_time"),
            "tick_size": 0.01,          # price_level_structure: linear_cent
            "min_order_size": 1.0,
            "last_trade_c": (lambda v: round(v * 100.0, 4) if v is not None else None)(
                _f(m.get("last_price_dollars"))),
            "volume": _f(m.get("volume_fp")),
            "open_interest": _f(m.get("open_interest_fp")),
            "subtitle": m.get("yes_sub_title"),
        }
    return out
