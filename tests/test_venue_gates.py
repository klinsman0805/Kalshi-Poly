"""Per-venue gates. One number must never govern both venues.

MAX_OPEN was a module-level global until 2026-08-18, so raising it to trade
Polymarket silently armed Kalshi — which trades real money on its own
KALSHI_WEATHER_LIVE gate — and closing one closed the other. Nothing in the
variable's name said so.

These import the modules fresh under controlled environments, because the
values resolve at class-definition time. That timing is itself load-bearing:
both entrypoints must call load_dotenv BEFORE importing modules.*, or the
classes read a bare environment and silently fall back to their defaults.
"""
import importlib
import sys

import pytest


def _reimport(env, monkeypatch):
    """Import both executors fresh with exactly this environment."""
    for key in ("WEATHER_MAX_OPEN", "KALSHI_WEATHER_MAX_OPEN",
                "WEATHER_MAX_REENTRIES", "KALSHI_WEATHER_MAX_REENTRIES",
                "WEATHER_DECLINE_GATE_DEG", "KALSHI_WEATHER_DECLINE_GATE_DEG",
                "WEATHER_MAX_WIN_ADDS", "KALSHI_WEATHER_MAX_WIN_ADDS"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("KALSHI_WEATHER_LIVE", "true")

    # load_dotenv walks up to the repo .env, which would reintroduce whatever
    # the live box happens to be set to. Neutralise it so the test controls the
    # environment completely; load_dotenv(override=False) means anything set
    # above still wins, but absent keys must fall through to the code defaults.
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)

    for name in [m for m in list(sys.modules)
                 if m.startswith(("modules.", "feeds.", "engine"))]:
        del sys.modules[name]
    wx = importlib.import_module("modules.weather_exec")
    kx = importlib.import_module("modules.kalshi_weather_exec")
    return wx.WeatherExecutor, kx.KalshiWeatherExecutor


@pytest.mark.parametrize("env,poly_expected,kalshi_expected", [
    ({"WEATHER_MAX_OPEN": "0"}, 0, 0),
    ({"WEATHER_MAX_OPEN": "0", "KALSHI_WEATHER_MAX_OPEN": "3"}, 0, 3),
    ({"WEATHER_MAX_OPEN": "3", "KALSHI_WEATHER_MAX_OPEN": "0"}, 3, 0),
    ({}, 10, 10),
])
def test_max_open_resolves_per_venue(env, poly_expected, kalshi_expected, monkeypatch):
    poly, kalshi = _reimport(env, monkeypatch)
    assert poly.MAX_OPEN == poly_expected
    assert kalshi.MAX_OPEN == kalshi_expected


def test_kalshi_falls_back_to_the_poly_key_when_unset(monkeypatch):
    """Removing KALSHI_WEATHER_MAX_OPEN must restore the old shared behaviour
    rather than silently reverting Kalshi to the default of 10."""
    poly, kalshi = _reimport({"WEATHER_MAX_OPEN": "7"}, monkeypatch)
    assert (poly.MAX_OPEN, kalshi.MAX_OPEN) == (7, 7)


def test_zero_is_a_hard_off_switch_not_unlimited():
    """The gate is len(open) >= MAX_OPEN, so 0 blocks every entry. Reading it as
    "unlimited" would arm a venue the operator believed was shut."""
    from modules.weather_exec import WeatherExecutor
    ex = WeatherExecutor.__new__(WeatherExecutor)
    ex.open = []
    ex.MAX_OPEN = 0
    assert len(ex.open) >= ex.MAX_OPEN, "MAX_OPEN=0 must block even with nothing open"
    ex.MAX_OPEN = 3
    assert not len(ex.open) >= ex.MAX_OPEN


def test_the_other_per_venue_gates_stay_independent(monkeypatch):
    poly, kalshi = _reimport(
        {"WEATHER_DECLINE_GATE_DEG": "0", "KALSHI_WEATHER_DECLINE_GATE_DEG": "1.0",
         "WEATHER_MAX_REENTRIES": "0", "KALSHI_WEATHER_MAX_REENTRIES": "1"},
        monkeypatch)
    assert (poly.DECLINE_GATE_DEG, kalshi.DECLINE_GATE_DEG) == (0.0, 1.0)
    assert (poly.MAX_REENTRIES, kalshi.MAX_REENTRIES) == (0, 1)


def test_win_adds_can_be_disabled_for_kalshi_alone(monkeypatch):
    """0 disables (the gate is adds >= MAX_WIN_ADDS). Each add stakes another
    tranche, so 3 adds quadruple a position's exposure."""
    poly, kalshi = _reimport(
        {"WEATHER_MAX_WIN_ADDS": "3", "KALSHI_WEATHER_MAX_WIN_ADDS": "0"},
        monkeypatch)
    assert poly.MAX_WIN_ADDS == 3
    assert kalshi.MAX_WIN_ADDS == 0
