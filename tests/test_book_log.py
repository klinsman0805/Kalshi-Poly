"""The full-ladder book recorder.

This exists because `book_depth` was recorded on 0 of 1,608 gate-refused rows,
which left the only promising backtest — selling those rows at their bid for
+22.3c a contract after fees — indistinguishable from a one-share phantom
quote. Every test here guards a way that measurement could silently fail again.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from modules import book_log as B
from modules.book_log import BookLogger

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _event(city="London", d=None, kind="low", n=3, tokens=None):
    d = d if d is not None else date(2026, 9, 1)
    toks = tokens or [f"tok{i}" for i in range(n)]
    return {
        "venue": "poly", "city": city, "date": d, "kind": kind,
        "slug": f"{city}-{kind}", "unit": "C",
        "buckets": [{"lo": 10 + i, "hi": 10 + i, "unit": "C",
                     "label": f"{10+i}°C", "token_yes": t}
                    for i, t in enumerate(toks)],
    }


def _book(bids, asks, ts="1756728000000"):
    return {"bid_levels": bids, "ask_levels": asks, "book_ts": ts,
            "tick_size": 0.001, "min_order_size": 5.0, "last_trade_c": 40.0}


# ── depth, the number this recorder exists for ───────────────────────────────

def test_depth_sums_only_within_the_band_of_the_touch():
    """One share at the touch with a wall five cents back is not depth."""
    lv = [[50.0, 10], [49.0, 20], [40.0, 5000]]
    assert B._depth_within(lv, 5.0) == 30.0


def test_depth_of_an_empty_side_is_zero_not_none():
    assert B._depth_within([], 5.0) == 0.0


def test_a_book_with_no_bids_is_recorded_not_skipped():
    """An empty bid side is the finding that would kill the sell strategy, so
    it must reach the file rather than being dropped as uninteresting."""
    lg = BookLogger("poly")
    rows = lg._rows(_event(n=1), {"tok0": _book([], [[60.0, 100]])}, "snap", NOW)
    assert len(rows) == 1
    assert rows[0]["bid_c"] is None
    assert rows[0]["bid_size"] == 0.0
    assert rows[0]["n_bid_levels"] == 0
    assert rows[0]["ask_c"] == 60.0


def test_top_of_book_and_sizes_are_captured():
    lg = BookLogger("poly")
    rows = lg._rows(_event(n=1),
                    {"tok0": _book([[45.0, 300], [44.0, 500]],
                                   [[47.0, 120], [48.0, 90]])}, "snap", NOW)
    r = rows[0]
    assert (r["bid_c"], r["ask_c"], r["spread_c"]) == (45.0, 47.0, 2.0)
    assert (r["bid_size"], r["ask_size"]) == (300, 120)
    assert (r["bid_depth"], r["ask_depth"]) == (800.0, 210.0)


# ── snap_id: the thing whose absence made ladder-sum untestable ──────────────

def test_every_bucket_of_one_ladder_shares_a_snap_id():
    """Ladder arbitrage needs a whole ladder at one instant. Grouping the old
    capture by timestamp reassembled nothing, so identity is stamped instead."""
    lg = BookLogger("poly")
    books = {f"tok{i}": _book([[10.0 * i, 5]], [[10.0 * i + 1, 5]]) for i in range(3)}
    rows = lg._rows(_event(n=3), books, "abc123", NOW)
    assert len(rows) == 3
    assert {r["snap_id"] for r in rows} == {"abc123"}
    assert {r["ladder_n"] for r in rows} == {3}


def test_two_snapshots_of_the_same_ladder_do_not_share_an_id(tmp_path):
    lg = BookLogger("poly", state_path=tmp_path / "s.json")
    a = lg._rows(_event(n=2), {"tok0": _book([], []), "tok1": _book([], [])}, "one", NOW)
    b = lg._rows(_event(n=2), {"tok0": _book([], []), "tok1": _book([], [])}, "two", NOW)
    assert {r["snap_id"] for r in a} != {r["snap_id"] for r in b}


# ── round robin, so the tail of a 300-event feed is ever reached ─────────────

def test_the_cursor_advances_instead_of_refetching_the_head():
    lg = BookLogger("poly")
    B.MAX_EVENTS, old = 2, B.MAX_EVENTS
    try:
        evs = [_event(city=f"C{i}") for i in range(6)]
        first = lg._due(evs, 1000.0)
        assert [e["city"] for e in first] == ["C0", "C1"]
        second = lg._due(evs, 1000.0)
        assert [e["city"] for e in second] == ["C2", "C3"], "must not repeat the head"
    finally:
        B.MAX_EVENTS = old


def test_the_cursor_wraps_at_the_end():
    lg = BookLogger("poly")
    B.MAX_EVENTS, old = 2, B.MAX_EVENTS
    try:
        evs = [_event(city=f"C{i}") for i in range(3)]
        lg._due(evs, 1000.0)
        lg._due(evs, 1000.0)
        assert [e["city"] for e in lg._due(evs, 1000.0)] == ["C0", "C1"]
    finally:
        B.MAX_EVENTS = old


# ── throttling and the date window ───────────────────────────────────────────

def test_a_recently_captured_event_is_skipped():
    lg = BookLogger("poly")
    e = _event()
    lg._last[lg._event_key(e)] = 1000.0
    assert lg._due([e], 1000.0 + B.SAMPLE_SEC - 1) == []
    assert lg._due([e], 1000.0 + B.SAMPLE_SEC + 1) == [e]


def test_events_further_ahead_than_the_window_are_ignored():
    far = _event(d=date(2026, 9, 1) + timedelta(days=B.MAX_DAYS_OUT + 1))
    assert BookLogger("poly")._due([far], 1000.0) == []


def test_past_events_are_ignored():
    lg = BookLogger("poly")
    old = _event(d=datetime.now(timezone.utc).date() - timedelta(days=1))
    assert lg._due([old], 1000.0) == []


def test_the_three_to_five_day_window_is_captured_and_labelled():
    """The published edge is 3-5 days out, which our old capture never saw."""
    lg = BookLogger("poly")
    today = datetime.now(timezone.utc).date()
    e = _event(d=today + timedelta(days=4))
    assert lg._due([e], 1000.0) == [e]
    rows = lg._rows(e, {"tok0": _book([[1.0, 1]], [[2.0, 1]])}, "s", NOW)
    assert rows[0]["days_out"] == 4


# ── failures must not be mistaken for empty books ────────────────────────────

def test_a_failed_fetch_does_not_stamp_the_throttle(tmp_path, monkeypatch):
    """If a venue outage marked events as freshly captured, the recorder would
    go quiet for half an hour per event and look healthy doing it."""
    lg = BookLogger("poly", state_path=tmp_path / "s.json")
    import feeds.order_books as OB
    monkeypatch.setattr(OB, "fetch_poly_books", lambda *a, **k: None)
    fh = (tmp_path / "o.jsonl").open("a", encoding="utf-8")
    monkeypatch.setattr(lg, "_fh_for_today", lambda: fh)
    e = _event()
    assert lg.snapshot([e]) == 0
    assert lg._event_key(e) not in lg._last, "a failed fetch must stay due"
    fh.close()


def test_observe_never_raises_into_the_caller(monkeypatch):
    lg = BookLogger("poly")
    monkeypatch.setattr(lg, "_due", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lg.snapshot([_event()]) == 0
    assert "boom" in lg.last_error


def test_a_bucket_with_no_book_returned_is_skipped_quietly():
    lg = BookLogger("poly")
    rows = lg._rows(_event(n=3), {"tok1": _book([[1.0, 1]], [[2.0, 1]])}, "s", NOW)
    assert len(rows) == 1 and rows[0]["token"] == "tok1"


# ── state persistence, because cron restarts the process every 15 minutes ────

def test_state_survives_a_process_restart(tmp_path):
    p = tmp_path / "state.json"
    a = BookLogger("poly", state_path=p)
    a._last["London|2026-09-01|low"] = 1234.0
    a._cursor = 7
    a._save_state()
    b = BookLogger("poly", state_path=p)
    assert b._last == {"London|2026-09-01|low": 1234.0}
    assert b._cursor == 7


def test_corrupt_state_starts_fresh_rather_than_crashing(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    lg = BookLogger("poly", state_path=p)
    assert lg._last == {} and lg._cursor == 0


# ── book age, so a stale quote is distinguishable from a live one ────────────

def test_polymarket_epoch_millis_are_parsed():
    ts = str(int((NOW - timedelta(seconds=30)).timestamp() * 1000))
    assert B._age_s(ts, NOW) == pytest.approx(30.0, abs=1.0)


def test_kalshi_iso_timestamps_are_parsed():
    ts = (NOW - timedelta(seconds=90)).isoformat()
    assert B._age_s(ts, NOW) == pytest.approx(90.0, abs=1.0)


def test_an_unparseable_timestamp_is_none_not_an_exception():
    assert B._age_s("not-a-time", NOW) is None
    assert B._age_s(None, NOW) is None
