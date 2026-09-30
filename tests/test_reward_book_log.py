"""The reward-market book recorder.

It exists because `feeds/poly_rewards.score_market` was measured predicting
$2.4157 against $0.1177 actually paid — 20.5x too high — and the causes that
remain after two fixes are sampling problems a snapshot cannot solve. So the
job here is to record the inputs faithfully, including the cases that look
like nothing.
"""
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from modules import reward_book_log as R
from modules.reward_book_log import RewardBookLogger, qualifying_size

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _market(cid="0xabc", rate=400.0, band=3.5, minsz=200.0):
    return {"condition_id": cid, "question": "Will X happen?", "slug": "will-x",
            "rate_per_day": rate, "max_spread_c": band, "min_size": minsz,
            "competitiveness": 0.4,
            "tokens": [{"token_id": "tokYES", "outcome": "Yes", "price": 0.4},
                       {"token_id": "tokNO", "outcome": "No", "price": 0.6}]}


def _books():
    return {"tokYES": ([[39.0, 500.0], [38.0, 900.0], [30.0, 4000.0]],
                       [[41.0, 600.0], [42.0, 100.0]]),
            "tokNO":  ([[58.0, 700.0]], [[61.0, 800.0]])}


# ── qualifying size: the raw competition number ──────────────────────────────

def test_only_orders_inside_the_band_and_over_the_cutoff_qualify():
    lv = [[39.0, 500.0],     # 1c from mid, big enough  -> counts
          [30.0, 4000.0],    # 10c away, outside a 3.5c band -> no
          [38.5, 50.0]]      # inside, but under the 200 cutoff -> no
    assert qualifying_size(lv, 40.0, 3.5, 200.0) == 500.0


def test_the_band_is_symmetric_around_the_mid():
    lv = [[36.6, 300.0], [43.4, 300.0], [36.4, 300.0], [43.6, 300.0]]
    assert qualifying_size(lv, 40.0, 3.5, 100.0) == 600.0


def test_no_band_means_nothing_qualifies():
    assert qualifying_size([[40.0, 999.0]], 40.0, 0.0, 1.0) == 0.0


def test_missing_mid_is_zero_not_an_exception():
    assert qualifying_size([[40.0, 999.0]], None, 3.5, 1.0) == 0.0


# ── the record ───────────────────────────────────────────────────────────────

def test_both_tokens_are_recorded_because_the_complement_is_half_the_field():
    """Every NO-side competitor was invisible until the scanner started folding
    in the complement book, which is one of the two causes of the 20.5x."""
    lg = RewardBookLogger()
    rec = lg._record(_market(), _books(), {"mid_c": 40.0}, NOW)
    assert rec["yes_token"] == "tokYES" and rec["no_token"] == "tokNO"
    assert rec["yes_bid_levels"] and rec["no_bid_levels"]
    assert rec["qual_no_bid"] > 0, "NO-side competition must be counted"


def test_the_pool_and_its_terms_are_recorded():
    lg = RewardBookLogger()
    rec = lg._record(_market(rate=2003.0, band=5.5, minsz=50.0), _books(),
                     {"mid_c": 40.0}, NOW)
    assert (rec["rate_per_day"], rec["max_spread_c"], rec["min_size"]) == (2003.0, 5.5, 50.0)


def test_the_estimate_is_kept_with_its_honesty_flags():
    """An estimate without its flags is worse than none — `no_competition`
    and `below_min_payout` are what stop a 100%-share reading being mistaken
    for an opportunity."""
    lg = RewardBookLogger()
    score = {"mid_c": 40.0, "yield_per_dollar_per_day": 0.004,
             "confidence": "low", "no_competition_detected": True,
             "below_min_payout": True, "est_daily_usd": 0.4}
    rec = lg._record(_market(), _books(), score, NOW)
    assert rec["confidence"] == "low"
    assert rec["no_competition_detected"] is True
    assert rec["below_min_payout"] is True


def test_a_market_that_could_not_be_priced_still_records_its_book():
    """score_market returning None must not lose the raw levels — they are the
    evidence, and the estimate is the thing under suspicion."""
    lg = RewardBookLogger()
    rec = lg._record(_market(), _books(), None, NOW)
    assert rec["yield_per_dollar_per_day"] is None
    assert rec["yes_bid_levels"], "the book is still worth keeping"


def test_levels_are_capped_so_one_deep_book_cannot_bloat_the_file():
    lg = RewardBookLogger()
    deep = {"tokYES": ([[40 - i * 0.1, 100.0] for i in range(40)], []),
            "tokNO": ([], [])}
    rec = lg._record(_market(), deep, {"mid_c": 40.0}, NOW)
    assert len(rec["yes_bid_levels"]) == R.MAX_LEVELS


def test_an_empty_book_records_as_empty_not_as_missing():
    lg = RewardBookLogger()
    rec = lg._record(_market(), {"tokYES": ([], []), "tokNO": ([], [])},
                     {"mid_c": 40.0}, NOW)
    assert rec["qual_total"] == 0.0
    assert rec["yes_bid_levels"] == []


# ── selection ────────────────────────────────────────────────────────────────

def test_the_cursor_advances_so_the_tail_of_the_universe_is_reached():
    lg = RewardBookLogger()
    old, R.MAX_MARKETS = R.MAX_MARKETS, 2
    try:
        ms = [_market(cid=f"0x{i}") for i in range(6)]
        assert [m["condition_id"] for m in lg._due(ms, 1000.0)] == ["0x0", "0x1"]
        assert [m["condition_id"] for m in lg._due(ms, 1000.0)] == ["0x2", "0x3"]
    finally:
        R.MAX_MARKETS = old


def test_a_recently_captured_market_is_skipped():
    lg = RewardBookLogger()
    m = _market()
    lg._last["0xabc"] = 1000.0
    assert lg._due([m], 1000.0 + R.SAMPLE_SEC - 1) == []
    assert lg._due([m], 1000.0 + R.SAMPLE_SEC + 1) == [m]


def test_state_survives_the_process_restart_cron_gives_it(tmp_path):
    p = tmp_path / "s.json"
    a = RewardBookLogger(state_path=p)
    a._last["0xabc"] = 1234.0
    a._cursor = 5
    a._save_state()
    b = RewardBookLogger(state_path=p)
    assert b._last == {"0xabc": 1234.0} and b._cursor == 5


# ── failure handling ─────────────────────────────────────────────────────────

def test_a_failed_book_fetch_returns_none_not_empty():
    """A market with no competitors and an unreachable API look identical
    otherwise — and the first is exactly what we are hunting for."""
    with patch("requests.post", side_effect=RuntimeError("timeout")):
        assert R.fetch_books_batch(["t1"]) is None


def test_no_tokens_is_empty_not_a_failure():
    assert R.fetch_books_batch([]) == {}
    assert R.fetch_books_batch(None) == {}


def test_a_venue_outage_does_not_stamp_the_throttle(tmp_path, monkeypatch):
    lg = RewardBookLogger(state_path=tmp_path / "s.json")
    monkeypatch.setattr(R, "fetch_books_batch", lambda *a, **k: None)
    m = _market()
    assert lg.snapshot([m]) == 0
    assert "0xabc" not in lg._last, "a failed fetch must stay due"


def test_snapshot_never_raises_into_the_caller(monkeypatch):
    lg = RewardBookLogger()
    monkeypatch.setattr(lg, "_due", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lg.snapshot([_market()]) == 0
    assert "boom" in lg.last_error


def test_the_complement_book_is_converted_into_yes_space_before_measuring():
    """A NO bid at 58c is a YES ask at 42c. Measured raw against a YES mid of
    40c it sits 18c away and reports as no competition — which is exactly the
    blind spot that made the scanner think it had the whole pool to itself."""
    lg = RewardBookLogger()
    books = {"tokYES": ([], []),
             "tokNO": ([[58.0, 700.0]], [[61.0, 800.0]])}
    rec = lg._record(_market(band=3.5, minsz=200.0), books, {"mid_c": 40.0}, NOW)
    assert rec["qual_no_bid"] == 700.0, "NO bid 58c = YES ask 42c, 2c from mid"
    assert rec["qual_no_ask"] == 800.0, "NO ask 61c = YES bid 39c, 1c from mid"


def test_a_complement_level_outside_the_band_still_does_not_qualify():
    lg = RewardBookLogger()
    books = {"tokYES": ([], []), "tokNO": ([[90.0, 700.0]], [])}
    rec = lg._record(_market(band=3.5, minsz=200.0), books, {"mid_c": 40.0}, NOW)
    assert rec["qual_no_bid"] == 0.0, "NO 90c = YES 10c, far outside the band"


def test_complement_levels_are_stored_unconverted():
    """Store what the venue said; convert only for the arithmetic."""
    lg = RewardBookLogger()
    books = {"tokYES": ([], []), "tokNO": ([[58.0, 700.0]], [])}
    rec = lg._record(_market(), books, {"mid_c": 40.0}, NOW)
    assert rec["no_bid_levels"] == [[58.0, 700.0]]
