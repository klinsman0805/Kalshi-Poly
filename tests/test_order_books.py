"""Order book fetches for the two venues.

Both venues answer with shapes that are easy to trust and wrong to trust.
Polymarket returns price levels UNSORTED — a live probe came back with asks at
0.953, 0.952, 0.251 in that order — so "the first ask is the best ask" is
false. Kalshi renamed its price fields (`yes_bid` -> `yes_bid_dollars`) and the
old names now return None rather than erroring, which is the failure mode that
reads as a dead market instead of a broken parser.
"""
from unittest.mock import patch

import pytest

from feeds import order_books as OB


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


# ── level ordering: the venue does not sort for us ───────────────────────────

def test_asks_are_sorted_best_first_regardless_of_wire_order():
    """The real Polymarket response that motivated this: 0.953, 0.952, 0.251."""
    raw = [{"price": "0.953", "size": "30"}, {"price": "0.952", "size": "41"},
           {"price": "0.251", "size": "30"}]
    assert [p for p, _ in OB._levels(raw, descending=False)] == [25.1, 95.2, 95.3]


def test_bids_are_sorted_highest_first():
    raw = [{"price": "0.10", "size": "5"}, {"price": "0.40", "size": "7"}]
    assert [p for p, _ in OB._levels(raw, descending=True)] == [40.0, 10.0]


def test_levels_are_capped_so_one_deep_book_cannot_bloat_the_file():
    raw = [{"price": str(0.001 * i), "size": "1"} for i in range(1, 60)]
    assert len(OB._levels(raw, descending=False)) == OB.MAX_LEVELS


def test_malformed_levels_are_dropped_not_fatal():
    raw = [{"price": "0.5", "size": "10"}, {"price": None, "size": "9"},
           {"size": "3"}, {"price": "x", "size": "1"}]
    assert OB._levels(raw, descending=False) == [[50.0, 10.0]]


# ── Polymarket batch ─────────────────────────────────────────────────────────

def test_a_whole_ladder_comes_back_in_one_post():
    payload = [{"asset_id": "t1", "bids": [{"price": "0.4", "size": "9"}],
                "asks": [{"price": "0.42", "size": "3"}], "timestamp": "1756728000000",
                "tick_size": "0.001", "min_order_size": "5",
                "last_trade_price": "0.41"},
               {"asset_id": "t2", "bids": [], "asks": [], "timestamp": "1756728000000"}]
    with patch.object(OB.requests, "post", return_value=_Resp(payload)) as post:
        out = OB.fetch_poly_books(["t1", "t2"])
    assert post.call_count == 1, "one call per ladder, not one per bucket"
    assert set(out) == {"t1", "t2"}
    assert out["t1"]["bid_levels"] == [[40.0, 9.0]]
    assert out["t1"]["last_trade_c"] == 41.0
    assert out["t2"]["bid_levels"] == []


def test_a_failed_call_returns_none_not_an_empty_dict():
    """An unreachable API and a market with no orders mean opposite things."""
    with patch.object(OB.requests, "post", side_effect=RuntimeError("timeout")):
        assert OB.fetch_poly_books(["t1"]) is None


def test_no_tokens_is_an_empty_result_not_a_failure():
    assert OB.fetch_poly_books([]) == {}
    assert OB.fetch_poly_books(None) == {}


def test_large_ladders_are_chunked():
    toks = [f"t{i}" for i in range(OB.BATCH + 5)]
    with patch.object(OB.requests, "post", return_value=_Resp([])) as post:
        OB.fetch_poly_books(toks)
    assert post.call_count == 2


# ── Kalshi ───────────────────────────────────────────────────────────────────

def _kalshi_market(ticker="KXHIGHNY-26SEP01-B85.5"):
    """Fields as the API actually returns them today. The pre-rename names
    (`yes_bid`, `volume`) are absent, and reading them yields None."""
    return {"ticker": ticker, "yes_bid_dollars": "0.0100",
            "yes_ask_dollars": "0.0200", "yes_bid_size_fp": "966.41",
            "yes_ask_size_fp": "188.00", "volume_fp": "7436.39",
            "open_interest_fp": "5845.05", "last_price_dollars": "0.0100",
            "updated_time": "2026-09-01T12:00:00Z", "yes_sub_title": "85° to 86°"}


def test_kalshi_top_of_book_carries_its_size():
    """The size is the whole point — Kalshi publishes only the best level, but
    it publishes how much is there, which /orderbook does not give us."""
    with patch("engine.kalshi_get", return_value={"markets": [_kalshi_market()]}):
        out = OB.fetch_kalshi_ladder("KXHIGHNY-26SEP01")
    r = out["KXHIGHNY-26SEP01-B85.5"]
    assert r["bid_levels"] == [[1.0, 966.41]]
    assert r["ask_levels"] == [[2.0, 188.0]]
    assert r["volume"] == 7436.39


def test_a_kalshi_market_with_no_quote_yields_empty_sides():
    m = _kalshi_market()
    m["yes_bid_dollars"] = None
    with patch("engine.kalshi_get", return_value={"markets": [m]}):
        out = OB.fetch_kalshi_ladder("EV")
    assert out["KXHIGHNY-26SEP01-B85.5"]["bid_levels"] == []
    assert out["KXHIGHNY-26SEP01-B85.5"]["ask_levels"] == [[2.0, 188.0]]


def test_kalshi_fetch_failure_returns_none():
    with patch("engine.kalshi_get", side_effect=RuntimeError("401")):
        assert OB.fetch_kalshi_ladder("EV") is None


def test_no_event_ticker_is_empty_not_a_call():
    assert OB.fetch_kalshi_ladder(None) == {}
    assert OB.fetch_kalshi_ladder("") == {}


def test_kalshi_carries_no_book_timestamp():
    """`updated_time` is when the market RECORD changed, not when the book did.
    The first live slice read a median age of 93,176s from it on quotes that
    were plainly current, so it must not masquerade as a book timestamp."""
    with patch("engine.kalshi_get", return_value={"markets": [_kalshi_market()]}):
        out = OB.fetch_kalshi_ladder("EV")
    r = out["KXHIGHNY-26SEP01-B85.5"]
    assert r["book_ts"] is None, "a wrong age is worse than no age"
    assert r["market_updated"] == "2026-09-01T12:00:00Z"
