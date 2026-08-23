"""Settlement day boundaries. The two venues bound their day on different clocks.

Kalshi settles on the NWS Climatological Report, midnight-to-midnight in local
STANDARD time. Polymarket settles from Wunderground's daily history page — "the
highest temperature recorded for all times on this day" — which is the station's
local CLOCK day. Reading the DST-aware date for Kalshi declared the outcome
fixed an hour early, which disarms take-profit and stop management and, while
win-adds were enabled, invited adding to a position that could still lose.

Both of August's large Kalshi losses landed inside that hour. They are replayed
below at the exact instant their win-add fired.
"""
import datetime as dt
from unittest import mock

import pytest

import modules.weather_exec as wx
from modules.weather_exec import WeatherExecutor
from modules.kalshi_weather_exec import KalshiWeatherExecutor

POLY = WeatherExecutor.__new__(WeatherExecutor)
KALSHI = KalshiWeatherExecutor.__new__(KalshiWeatherExecutor)


def closed_at(utc_iso, executor, station_tz, station_local_date, market_date):
    """Evaluate _window_closed as if the clock read utc_iso."""
    real = dt.datetime

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            t = real.fromisoformat(utc_iso)
            return t.astimezone(tz) if tz else t

    row = {"station_local_date": station_local_date, "station_tz": station_tz}
    with mock.patch.object(wx, "datetime", Frozen):
        return executor._window_closed({"date": market_date}, row)


# ── the two real losses ──────────────────────────────────────────────────────

def test_seattle_window_was_still_open_when_the_add_fired():
    """2026-08-22T07:02:51Z is 00:02 PDT, which is 23:02 PST. 58 minutes of the
    CLI day remained and the overnight minimum kept falling. Cost -$8.30."""
    assert closed_at("2026-08-22T07:02:51+00:00", KALSHI,
                     "America/Los_Angeles", "2026-08-22", "2026-08-21") is False


def test_seattle_window_closes_once_standard_midnight_passes():
    assert closed_at("2026-08-22T08:30:00+00:00", KALSHI,
                     "America/Los_Angeles", "2026-08-22", "2026-08-21") is True


def test_minneapolis_window_was_still_open_when_the_add_fired():
    """2026-08-23T05:02:04Z is 00:02 CDT, which is 23:02 CST. About -$8.00."""
    assert closed_at("2026-08-23T05:02:04+00:00", KALSHI,
                     "America/Chicago", "2026-08-23", "2026-08-22") is False


def test_minneapolis_window_closes_once_standard_midnight_passes():
    assert closed_at("2026-08-23T06:30:00+00:00", KALSHI,
                     "America/Chicago", "2026-08-23", "2026-08-22") is True


# ── Polymarket must be untouched ─────────────────────────────────────────────

def test_poly_uses_the_local_clock_day():
    assert closed_at("2026-08-22T07:02:51+00:00", POLY,
                     "America/Los_Angeles", "2026-08-22", "2026-08-21") is True


def test_poly_same_day_is_still_open():
    assert closed_at("2026-08-22T07:02:51+00:00", POLY,
                     "America/Los_Angeles", "2026-08-22", "2026-08-22") is False


@pytest.mark.parametrize("tz", ["Asia/Shanghai", "Asia/Tokyo", "Europe/Istanbul"])
def test_non_dst_cities_are_unaffected(tz):
    """28 of the 49 tracked cities observe no DST, so the two clocks coincide
    and the question cannot arise."""
    assert closed_at("2026-08-22T16:02:00+00:00", POLY, tz,
                     "2026-08-23", "2026-08-22") is True


@pytest.mark.parametrize("executor", [POLY, KALSHI])
def test_in_winter_both_venues_agree(executor):
    """No DST anywhere in the US in January, so standard time is clock time."""
    assert closed_at("2026-01-15T06:30:00+00:00", executor,
                     "America/Chicago", "2026-01-15", "2026-01-14") is True


def test_a_dst_zone_on_the_kalshi_path_still_shifts():
    """Defensive. Kalshi is US-only today, but the rule is about the clock, not
    the country."""
    assert closed_at("2026-08-22T22:30:00+00:00", KALSHI,
                     "Europe/Paris", "2026-08-23", "2026-08-22") is False


# ── a bad read must never disarm risk management ─────────────────────────────

def test_missing_zone_reads_as_still_open():
    row = {"station_local_date": "2026-08-22"}
    assert KALSHI._window_closed({"date": "2026-08-21"}, row) is False


def test_unparseable_zone_reads_as_still_open():
    row = {"station_local_date": "2026-08-22", "station_tz": "Not/AZone"}
    assert KALSHI._window_closed({"date": "2026-08-21"}, row) is False


def test_missing_market_date_reads_as_still_open():
    row = {"station_local_date": "2026-08-22", "station_tz": "America/Chicago"}
    assert KALSHI._window_closed({}, row) is False


def test_venue_flags_match_how_the_venues_actually_settle():
    assert WeatherExecutor.SETTLEMENT_STANDARD_TIME is False
    assert KalshiWeatherExecutor.SETTLEMENT_STANDARD_TIME is True
