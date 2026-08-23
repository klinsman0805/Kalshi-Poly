"""Riot livestats: cold-search backoff and frame flattening.

Coverage is patchy — 4 of 321 observed games ever published a frame — and a
game with none is a NORMAL steady state that can last the whole match, not a
transient to retry around. Without backoff, one uncovered game costs a full
30-request walk every 10 seconds, forever.
"""
import time

import pytest

from feeds import riot_livestats as R

FRAME = {
    "rfc460Timestamp": "2026-08-06T14:00:00.000Z",
    "gameState": "in_game",
    "blueTeam": {"totalGold": 5000, "totalKills": 3, "towers": 1,
                 "inhibitors": 0, "barons": 0, "dragons": [1]},
    "redTeam": {"totalGold": 4200, "totalKills": 1, "towers": 0,
                "inhibitors": 0, "barons": 0, "dragons": []},
}


@pytest.fixture
def feed():
    return R.RiotLivestatsFeed()


def _counting(feed, result):
    calls = {"n": 0}

    def window(game_id, at=None):
        calls["n"] += 1
        return result

    feed.window = window
    return calls


def test_cold_search_walks_the_whole_window(feed):
    calls = _counting(feed, ([], "no_data"))
    assert feed.latest_frame("g1") is None
    assert calls["n"] >= 25, "a cold search should sweep the full lag range"


def test_backoff_suppresses_repeat_searches(feed):
    _counting(feed, ([], "no_data"))
    feed.latest_frame("g1")
    calls = _counting(feed, ([], "no_data"))
    for _ in range(5):
        assert feed.latest_frame("g1") is None
    assert calls["n"] == 0, "backoff must cost nothing while it is in effect"


def test_backoff_expires_and_doubles(feed):
    _counting(feed, ([], "no_data"))
    feed.latest_frame("g1")
    assert feed._miss["g1"][0] == 1
    feed._miss["g1"] = (1, time.monotonic() - 1)          # expire it
    calls = _counting(feed, ([], "no_data"))
    feed.latest_frame("g1")
    assert calls["n"] >= 25
    assert feed._miss["g1"][0] == 2, "consecutive misses should accumulate"


def test_a_hit_clears_the_backoff_and_installs_a_hint(feed):
    _counting(feed, ([FRAME], "ok"))
    result = feed.latest_frame("g1")
    assert result is not None
    assert "g1" not in feed._miss
    assert feed._lag_hint.get("g1")


def test_the_warm_path_costs_one_request(feed):
    _counting(feed, ([FRAME], "ok"))
    feed.latest_frame("g1")
    calls = _counting(feed, ([FRAME], "ok"))
    feed.latest_frame("g1")
    assert calls["n"] == 1, "a known lag should be found on the first try"


def test_a_transport_error_is_not_treated_as_absent_data(feed):
    """error means "we could not ask", which must not be recorded as a miss and
    must not start a backoff."""
    _counting(feed, ([], "error"))
    assert feed.latest_frame("g1") is None
    assert "g1" not in feed._miss


def test_summarize_flattens_a_frame():
    s = R.RiotLivestatsFeed.summarize(FRAME)
    assert s["gold_diff_blue"] == 800
    assert s["blue"]["dragons"] == 1 and s["red"]["dragons"] == 0
    assert s["blue"]["kills"] == 3
    assert s["game_state"] == "in_game"


def test_summarize_handles_no_frame():
    assert R.RiotLivestatsFeed.summarize(None) is None


def test_summarize_survives_missing_counters():
    s = R.RiotLivestatsFeed.summarize({"blueTeam": {}, "redTeam": {}})
    assert s["gold_diff_blue"] is None
    assert s["blue"]["dragons"] == 0
