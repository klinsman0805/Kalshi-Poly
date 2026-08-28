"""The 6-hourly max/min groups, and the ones that decode to nonsense.

Those groups exist to catch an extreme that fell between hourly observations,
so a genuine one sits within a degree or two of what we saw. Ankara emitted
minT=-0.4 and maxT=-0.8 on a day whose 37 observations ran 15-27C. Folding the
min in dropped the running minimum to -0.4, and because a bogus MAX is only
folded when it is higher, the corruption hit low markets exclusively — the
model then read the day's minimum as locked and returned p=1.0 on a bucket that
could not win.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from feeds.metar import MetarFeed, SIX_HOURLY_TOLERANCE_C

TZ = ZoneInfo("Europe/Istanbul")
DAY = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)   # 06:00 local


def _obs(hour_offset, temp, maxT=None, minT=None):
    """One observation, `hour_offset` hours after 06:00 local."""
    t = DAY + timedelta(hours=hour_offset)
    o = {"icaoId": "LTAC", "temp": temp, "reportTime": t.isoformat(),
         "lat": 40.1, "lon": 33.0}
    if maxT is not None:
        o["maxT"] = maxT
    if minT is not None:
        o["minT"] = minT
    return o


def _reduce(rows, now=None):
    return MetarFeed()._reduce("LTAC", rows, TZ,
                               now or (DAY + timedelta(hours=12)))


def test_the_real_ankara_case_is_rejected():
    """37 observations from 15 to 27C, plus groups decoding to -0.4 and -0.8."""
    rows = [_obs(i, t) for i, t in enumerate([15, 18, 21, 24, 26, 27, 26, 24])]
    rows[4]["minT"] = -0.4
    rows[3]["maxT"] = -0.8
    v = _reduce(rows)
    assert v["min_c"] == 15.0, "the bogus 6-hourly min must not become the day's min"
    assert v["max_c"] == 27.0


def test_a_plausible_group_is_still_folded_in():
    """The whole point of these groups: an extreme that fell between hourly
    observations. One degree below what we saw is exactly that."""
    rows = [_obs(i, t) for i, t in enumerate([15, 18, 21, 24])]
    rows[2]["minT"] = 14.0
    assert _reduce(rows)["min_c"] == 14.0


def test_a_plausible_max_group_is_still_folded_in():
    rows = [_obs(i, t) for i, t in enumerate([15, 18, 21, 24])]
    rows[2]["maxT"] = 26.0
    assert _reduce(rows)["max_c"] == 26.0


def test_the_boundary_is_inclusive():
    rows = [_obs(i, t) for i, t in enumerate([20, 22, 24])]
    rows[1]["minT"] = 20.0 - SIX_HOURLY_TOLERANCE_C
    assert _reduce(rows)["min_c"] == pytest.approx(20.0 - SIX_HOURLY_TOLERANCE_C)


def test_just_beyond_the_boundary_is_rejected():
    rows = [_obs(i, t) for i, t in enumerate([20, 22, 24])]
    rows[1]["minT"] = 20.0 - SIX_HOURLY_TOLERANCE_C - 0.1
    assert _reduce(rows)["min_c"] == 20.0


def test_an_absurd_max_group_is_rejected_too():
    """The max side was only ever spared by luck — a bogus max below the real
    one fails the `> max_c` test. A bogus max ABOVE it would corrupt highs the
    same way."""
    rows = [_obs(i, t) for i, t in enumerate([20, 22, 24])]
    rows[1]["maxT"] = 60.0
    assert _reduce(rows)["max_c"] == 24.0


def test_a_day_with_no_groups_is_unaffected():
    rows = [_obs(i, t) for i, t in enumerate([15, 18, 21, 24, 20])]
    v = _reduce(rows)
    assert (v["min_c"], v["max_c"]) == (15.0, 24.0)


def test_rejection_does_not_disturb_the_extreme_timestamps():
    """A discarded group must not stamp an age onto an extreme it did not set."""
    rows = [_obs(i, t) for i, t in enumerate([15, 18, 21, 24])]
    rows[2]["minT"] = -0.4
    v = _reduce(rows)
    assert v["min_c"] == 15.0
    assert v["min_age_min"] is not None
