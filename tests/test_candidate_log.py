"""The shadow feature recorder.

Its whole value is that it records the candidates the bot did NOT trade. If it
only captured entries it would reproduce the 45-row sample that is already too
small and too conditioned on the bot's own gates to learn from.
"""
import datetime as dt
import json
from unittest import mock

import pytest

import modules.candidate_log as cl
from modules.candidate_log import CandidateLogger
from modules.weather_exec import WeatherExecutor
from modules.kalshi_weather_exec import KalshiWeatherExecutor


@pytest.fixture
def logger(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "LOG_PATH", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(cl, "ENABLED", True)
    monkeypatch.setattr(cl, "SAMPLE_SEC", 900.0)
    lg = CandidateLogger("poly")
    yield lg
    lg.close()


def _executor(cls=WeatherExecutor):
    ex = cls.__new__(cls)
    ex.on_log = lambda i, m: None
    return ex


def _row(signal="ENTER", ask_c=70.0, p=0.95, **kw):
    row = {
        "city": "Seattle", "date": "2026-08-21", "kind": "low", "unit": "F",
        "station": "KSEA", "station_tz": "America/Los_Angeles",
        "station_local_date": "2026-08-21", "slug": "lowest-temp-seattle",
        "signal": signal, "why": "ok", "group": "actionable", "tradeable": True,
        "best_label": "61F", "best_p": p,
        "ext_c": 61.0, "temp_c": 62.0, "ext_age_min": 30.0, "obs_today": 20,
        "local_hour": 20.5,
        "conditions": {"cover": 0, "wind_kt": 8},
        "ensemble": {"anch_min_f": 58.0},
        "buckets": [{"label": "61F", "lo": 61, "hi": None, "ask_c": ask_c,
                     "bid_c": (ask_c - 3) if ask_c else None, "edge_c": 20.0,
                     "fee_c": 1.0, "shares_planned": 3, "book_depth": 3,
                     "token_yes": "TKR-1"}],
    }
    row.update(kw)
    return row


def _read(lg):
    lg.fh.flush()
    return [json.loads(l) for l in lg.path.read_text(encoding="utf-8").splitlines()]


def test_records_a_scored_candidate(logger):
    assert logger.observe([_row()], _executor()) == 1
    rec = _read(logger)[0]
    assert rec["venue"] == "poly"
    assert rec["key"] == "Seattle|2026-08-21|low"
    assert rec["model_p"] == 0.95 and rec["ask_c"] == 70.0


def test_records_blocked_candidates_not_just_entries(logger):
    """The counterfactuals are the point. A sample of entries alone cannot say
    whether the gates that blocked the rest were helping."""
    rows = [_row(signal="ENTER"),
            _row(signal="PRICED", city="Denver", why="ask 100c > 82c"),
            _row(signal="THIN-EDGE", city="Austin", why="edge 2c < 8c")]
    assert logger.observe(rows, _executor()) == 3
    assert {r["signal"] for r in _read(logger)} == {"ENTER", "PRICED", "THIN-EDGE"}


def test_skips_rows_with_no_model_probability(logger):
    assert logger.observe([_row(best_p=None)], _executor()) == 0


def test_skips_rows_with_no_live_quote(logger):
    assert logger.observe([_row(ask_c=None)], _executor()) == 0


def test_throttles_repeat_snapshots_of_the_same_market(logger):
    ex = _executor()
    assert logger.observe([_row()], ex) == 1
    assert logger.observe([_row()], ex) == 0, "should be throttled"


def test_a_change_of_verdict_always_records(logger):
    """Crossing into or out of ENTER is the decision boundary the model has to
    reproduce, so it must never be throttled away."""
    ex = _executor()
    assert logger.observe([_row(signal="PRICED")], ex) == 1
    assert logger.observe([_row(signal="ENTER")], ex) == 1
    assert [r["signal"] for r in _read(logger)] == ["PRICED", "ENTER"]


def test_carries_the_identifiers_needed_to_label_later(logger):
    """No label is written here. These fields are what make one resolvable
    against the venue's own settlement source afterwards."""
    logger.observe([_row()], _executor())
    rec = _read(logger)[0]
    for field in ("station", "settlement_date", "date", "kind", "lo", "hi",
                  "slug", "token_yes", "label", "unit"):
        assert field in rec, field
    assert "won" not in rec and "label_outcome" not in rec


def test_keeps_the_two_unused_feature_blocks(logger):
    logger.observe([_row()], _executor())
    rec = _read(logger)[0]
    assert rec["conditions"]["cover"] == 0
    assert rec["ensemble"]["anch_min_f"] == 58.0


def test_hours_left_uses_the_venues_own_clock():
    """Kalshi's CLI day closes an hour after Polymarket's, so the same instant
    leaves the two venues with different time remaining."""
    row = _row()
    real = dt.datetime

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            t = real.fromisoformat("2026-08-21T22:00:00+00:00")
            return t.astimezone(tz) if tz else t

    with mock.patch.object(cl, "datetime", Frozen):
        import modules.weather_exec as wx
        with mock.patch.object(wx, "datetime", Frozen):
            poly_h = CandidateLogger._hours_left(_executor(), row)
            kal_h = CandidateLogger._hours_left(_executor(KalshiWeatherExecutor), row)
    assert poly_h is not None and kal_h is not None
    assert round(kal_h - poly_h, 2) == 1.0, "Kalshi should have exactly one more hour"


def test_a_closed_window_reports_zero_hours_left():
    row = _row(station_local_date="2026-08-23", date="2026-08-21")
    assert CandidateLogger._hours_left(_executor(), row) == 0.0


def test_recorder_never_raises_into_the_caller(logger):
    """A recorder fault must not disturb trading."""
    broken = _row()
    broken["buckets"] = "not-a-list"
    assert logger.observe([broken], _executor()) == 0


def test_disabled_recorder_writes_nothing(logger, monkeypatch):
    monkeypatch.setattr(cl, "ENABLED", False)
    assert logger.observe([_row()], _executor()) == 0


def test_files_roll_by_utc_day(logger):
    logger.observe([_row()], _executor())
    assert logger.day in logger.path.name
    assert logger.path.name.startswith("candidates-")
