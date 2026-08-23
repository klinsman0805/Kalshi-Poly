"""Startup reconciliation: compare the rehydrated ledger against what the
exchange says we actually hold.

_rehydrate replays the journal and never asks the exchange anything, so a fill
lost to a crash leaves an orphan the bot cannot see. This reports divergence —
it deliberately never mutates self.open, because an automatic "correction"
against a bad read would be worse than the divergence itself.
"""
from modules.weather_exec import WeatherExecutor
from modules.kalshi_weather_exec import KalshiWeatherExecutor


def _executor():
    ex = WeatherExecutor.__new__(WeatherExecutor)
    ex.open = []
    ex.on_log = lambda icon, msg: None
    return ex


def _live(token, **kw):
    d = {"mode": "live", "token_yes": token, "city": "NYC",
         "date": "2026-08-01", "label": "30C"}
    d.update(kw)
    return d


def _held(token, title="Highest temperature in NYC", size=5):
    return {"asset": token, "title": title, "size": size}


def test_ledger_and_exchange_agree():
    ex = _executor()
    ex.open = [_live("aaa")]
    r = ex.reconcile_against_exchange([_held("aaa")])
    assert r["ok"] and not r["orphans"] and not r["ghosts"]


def test_orphan_is_held_on_the_exchange_but_absent_from_the_ledger():
    ex = _executor()
    r = ex.reconcile_against_exchange([_held("bbb")])
    assert not r["ok"]
    assert len(r["orphans"]) == 1 and not r["ghosts"]


def test_ghost_is_in_the_ledger_but_gone_from_the_exchange():
    ex = _executor()
    ex.open = [_live("ccc")]
    r = ex.reconcile_against_exchange([])
    assert not r["ok"]
    assert len(r["ghosts"]) == 1 and not r["orphans"]


def test_non_temperature_holdings_are_not_this_executors_business():
    """The two-leg LP strategy trades from the same wallet. Its holdings must
    not surface here as orphans."""
    ex = _executor()
    r = ex.reconcile_against_exchange([_held("ddd", title="Will BTC hit 100k")])
    assert r["ok"]


def test_paper_positions_are_never_ghosts():
    ex = _executor()
    ex.open = [{"mode": "paper", "token_yes": "eee"}]
    assert ex.reconcile_against_exchange([])["ok"]


def test_token_ids_compare_as_strings():
    """Ledger and API disagree on int vs str. A type mismatch would invent a
    ghost and an orphan for the same real position."""
    ex = _executor()
    ex.open = [_live(12345)]
    assert ex.reconcile_against_exchange([_held("12345")])["ok"]


def test_token_id_key_is_accepted_as_well_as_asset():
    ex = _executor()
    ex.open = [_live("fff")]
    held = {"token_id": "fff", "title": "temperature", "size": 1}
    assert ex.reconcile_against_exchange([held])["ok"]


def test_reconcile_on_start_is_skipped_when_not_live(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(type(ex), "is_live", property(lambda self: False))
    assert ex.reconcile_on_start() is None


def test_unreachable_exchange_reports_skipped_rather_than_ghosts(monkeypatch):
    """None means "could not check" and must stay distinct from [] — an empty
    list would turn every real open position into a ghost."""
    ex = _executor()
    monkeypatch.setattr(type(ex), "is_live", property(lambda self: True))
    ex.fetch_exchange_positions = lambda: None
    ex.open = [_live("ggg")]
    msgs = []
    ex.on_log = lambda icon, msg: msgs.append(msg)
    assert ex.reconcile_on_start() is None
    assert any("SKIPPED" in m for m in msgs)


def test_reconcile_on_start_surfaces_an_orphan(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(type(ex), "is_live", property(lambda self: True))
    ex.fetch_exchange_positions = lambda: [_held("hhh")]
    msgs = []
    ex.on_log = lambda icon, msg: msgs.append(msg)
    result = ex.reconcile_on_start()
    assert not result["ok"]
    assert any("ORPHAN" in m for m in msgs)


def test_kalshi_refuses_to_answer_with_polymarket_data():
    """The inherited fetch queries Polymarket's API against a Polymarket wallet.
    For Kalshi that is the wrong exchange and the wrong account, so "could not
    check" is the only honest answer — [] would ghost every real position."""
    k = KalshiWeatherExecutor.__new__(KalshiWeatherExecutor)
    assert k.fetch_exchange_positions() is None
