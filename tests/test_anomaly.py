"""Detectors for the ways this pipeline has actually broken.

Each test corresponds to a failure that happened, was invisible to the summary
statistics, and was caught only by reading raw numbers.
"""
from datetime import datetime, timedelta, timezone

import pytest

from modules import anomaly as A

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _rec(lo=77, hi=79, unit="F", **kw):
    d = {"key": "Austin|2026-08-23|high", "lo": lo, "hi": hi, "unit": unit}
    d.update(kw)
    return d


# ── bucket arithmetic ────────────────────────────────────────────────────────

def test_a_settled_value_inside_the_bucket_has_no_miss():
    assert A.bucket_miss(_rec(), 78) == 0.0


def test_miss_is_signed_by_direction():
    assert A.bucket_miss(_rec(), 82) == pytest.approx(3.0)
    assert A.bucket_miss(_rec(), 74) == pytest.approx(-3.0)


def test_open_tailed_buckets_only_have_the_bound_they_have():
    assert A.bucket_miss(_rec(lo=60, hi=None), 90) == 0.0
    assert A.bucket_miss(_rec(lo=60, hi=None), 55) == pytest.approx(-5.0)
    assert A.bucket_miss(_rec(lo=None, hi=98), 99) == pytest.approx(1.0)


def test_no_settled_value_means_no_miss():
    assert A.bucket_miss(_rec(), None) is None


# ── the preliminary-CLI failure ──────────────────────────────────────────────

def test_a_twenty_degree_miss_is_flagged_as_a_label_problem():
    """Austin 2026-08-23: labelled 84F from a mid-day preliminary report when
    the day actually reached 105F. Weather does not move that far past a
    forecast; the label was wrong, not the model."""
    assert A.implausible_miss(_rec(lo=104, hi=105, unit="F"), 84) is True


def test_an_ordinary_miss_is_not_flagged():
    assert A.implausible_miss(_rec(), 82) is False


def test_celsius_uses_a_tighter_threshold_than_fahrenheit():
    """Seven Celsius degrees is about thirteen Fahrenheit; the same physical
    limit has to be expressed per unit."""
    assert A.implausible_miss(_rec(lo=30, hi=31, unit="C"), 40) is True
    assert A.implausible_miss(_rec(lo=30, hi=31, unit="C"), 34) is False
    assert A.implausible_miss(_rec(lo=86, hi=88, unit="F"), 94) is False


def test_find_implausible_returns_only_the_bad_rows():
    pairs = [(_rec(lo=104, hi=105), 84), (_rec(), 78), (_rec(), 81)]
    assert len(A.find_implausible(pairs)) == 1


# ── unit mixing ──────────────────────────────────────────────────────────────

def test_mixed_units_are_detected():
    """Averaging a miss across Celsius and Fahrenheit produces a number that
    means nothing — a mistake I made before splitting the table."""
    assert A.mixed_units([_rec(unit="C"), _rec(unit="F")]) is True
    assert A.mixed_units([_rec(unit="C"), _rec(unit="C")]) is False


def test_unit_summary_never_merges_units():
    pairs = [(_rec(unit="C", lo=30, hi=30), 30), (_rec(unit="C", lo=30, hi=30), 32),
             (_rec(unit="F", lo=80, hi=81), 80)]
    out = A.unit_summary(pairs)
    assert {d["unit"] for d in out} == {"C", "F"}
    c = next(d for d in out if d["unit"] == "C")
    assert c["n"] == 2 and c["inside"] == 1 and c["above"] == 1


def test_unit_summary_counts_direction():
    pairs = [(_rec(), 82), (_rec(), 83), (_rec(), 74)]
    d = A.unit_summary(pairs)[0]
    assert d["above"] == 2 and d["below"] == 1 and d["inside"] == 0


# ── capture and labelling stalls ─────────────────────────────────────────────

def test_a_quiet_recorder_is_flagged():
    old = (NOW - timedelta(hours=5)).isoformat()
    stalled, age = A.capture_stalled(old, now=NOW)
    assert stalled is True and age == pytest.approx(5.0, abs=0.1)


def test_a_live_recorder_is_not_flagged():
    fresh = (NOW - timedelta(minutes=10)).isoformat()
    assert A.capture_stalled(fresh, now=NOW)[0] is False


def test_a_missing_timestamp_is_not_treated_as_a_stall():
    assert A.capture_stalled(None, now=NOW)[0] is False


def test_a_large_standing_label_backlog_is_flagged():
    """Every non-Kalshi market went unlabelled for two days because an event
    slug was queried against the markets endpoint. From outside, that is what
    a broken resolver looks like."""
    assert A.labels_stalled(100, 10)[0] is True


def test_ordinary_settlement_lag_is_not_flagged():
    assert A.labels_stalled(100, 80)[0] is False
    assert A.labels_stalled(10, 2)[0] is False, "small counts are just lag"


def test_nothing_settleable_is_not_a_stall():
    assert A.labels_stalled(0, 0)[0] is False


# ── the scan ─────────────────────────────────────────────────────────────────

def test_a_clean_system_reports_no_issues():
    fresh = (NOW - timedelta(minutes=5)).isoformat()
    pairs = [(_rec(), 78), (_rec(), 79)]
    assert A.scan(pairs, fresh, 10, 9, now=NOW) == []


def test_scan_surfaces_the_bad_rows_for_eyeballing():
    fresh = (NOW - timedelta(minutes=5)).isoformat()
    issues = A.scan([(_rec(lo=104, hi=105), 84)], fresh, 10, 9, now=NOW)
    assert issues[0]["code"] == "implausible_miss"
    assert issues[0]["level"] == "ERROR"
    assert issues[0]["rows"][0]["settled"] == 84


def test_scan_reports_several_problems_at_once():
    stale = (NOW - timedelta(hours=6)).isoformat()
    issues = A.scan([(_rec(lo=104, hi=105), 84)], stale, 100, 5, now=NOW)
    assert {i["code"] for i in issues} == {"implausible_miss", "capture_stalled",
                                           "labels_stalled"}


def test_the_stall_detector_fires_at_the_ratios_it_is_meant_to():
    """Guard for the off-by-one that once made settleable come out zero, which
    silently disabled this detector entirely."""
    assert A.labels_stalled(100, 10)[0] is True      # 90% backlog
    assert A.labels_stalled(100, 49)[0] is True      # 51% backlog
    assert A.labels_stalled(100, 51)[0] is False     # 49% backlog
    assert A.labels_stalled(40, 19)[0] is True       # >20 rows outstanding
    assert A.labels_stalled(30, 15)[0] is False      # exactly 15, under the floor
