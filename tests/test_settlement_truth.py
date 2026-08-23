"""Resolving what a market actually settled to.

The rule that matters here: "not knowable yet" must come back as None and never
as a loss. A false label is undetectable downstream — it would silently teach
the model the wrong thing — whereas a missing one just gets retried tomorrow.
"""
import json

import pytest

import modules.settlement_truth as st
from modules.settlement_truth import SettlementTruth


class _Resp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


CLI_TEXT = """
CLIMATE SUMMARY FOR AUGUST 21 2026

TEMPERATURE (F)
 MAXIMUM         78
 MINIMUM         60
"""


@pytest.fixture
def truth():
    return SettlementTruth()


def _stub_cli(monkeypatch, product_text=CLI_TEXT, listing=None):
    calls = {"n": 0}
    listing = listing if listing is not None else {"@graph": [{"id": "p1"}]}

    def get(url, params=None, timeout=None):
        calls["n"] += 1
        if "/types/CLI/" in url:
            return _Resp(listing)
        return _Resp({"productText": product_text})

    monkeypatch.setattr(st._session, "get", get)
    return calls


# ── bucket arithmetic ────────────────────────────────────────────────────────

def test_bucket_contains_a_two_sided_range():
    assert SettlementTruth.bucket_contains(78, 77, 79) is True
    assert SettlementTruth.bucket_contains(80, 77, 79) is False


def test_catch_all_buckets_carry_only_one_bound():
    """A ">=60F" bucket has no upper bound. Demanding both once mislabelled a
    real San Francisco win as a mismatch."""
    assert SettlementTruth.bucket_contains(75, 60, None) is True
    assert SettlementTruth.bucket_contains(55, 60, None) is False
    assert SettlementTruth.bucket_contains(90, None, 98) is True
    assert SettlementTruth.bucket_contains(99, None, 98) is False


def test_bucket_contains_is_none_without_an_extreme():
    assert SettlementTruth.bucket_contains(None, 60, 70) is None


# ── Kalshi via the CLI report ────────────────────────────────────────────────

def test_cli_parses_both_extremes(truth, monkeypatch):
    _stub_cli(monkeypatch)
    assert truth.cli_extremes("KSEA", "2026-08-21") == {"high": 78, "low": 60}


def test_cli_result_is_cached(truth, monkeypatch):
    calls = _stub_cli(monkeypatch)
    truth.cli_extremes("KSEA", "2026-08-21")
    first = calls["n"]
    truth.cli_extremes("KSEA", "2026-08-21")
    assert calls["n"] == first, "a station-day should be fetched once"


def test_cli_for_a_different_day_is_not_yet_published(truth, monkeypatch):
    _stub_cli(monkeypatch)
    assert truth.cli_extremes("KSEA", "2026-08-22") is None


def test_a_transport_failure_is_not_cached_as_absent(truth, monkeypatch):
    """A network blip must not permanently mark a day unresolvable."""
    def boom(url, params=None, timeout=None):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(st._session, "get", boom)
    assert truth.cli_extremes("KSEA", "2026-08-21") is None
    assert ("KSEA", "2026-08-21") not in truth._cli


def test_label_resolves_a_kalshi_low(truth, monkeypatch):
    _stub_cli(monkeypatch)
    rec = {"venue": "kalshi", "station": "KSEA", "date": "2026-08-21",
           "kind": "low", "lo": 61, "hi": None}
    out = truth.label(rec)
    assert out["actual_extreme"] == 60
    assert out["won"] is False, "the minimum came in at 60, below a >=61 bucket"


def test_label_resolves_a_kalshi_high(truth, monkeypatch):
    _stub_cli(monkeypatch)
    rec = {"venue": "kalshi", "station": "KSEA", "date": "2026-08-21",
           "kind": "high", "lo": 77, "hi": 79}
    assert truth.label(rec)["won"] is True


def test_unpublished_cli_labels_nothing(truth, monkeypatch):
    _stub_cli(monkeypatch, listing={"@graph": []})
    rec = {"venue": "kalshi", "station": "KSEA", "date": "2026-08-21",
           "kind": "high", "lo": 77, "hi": 79}
    out = truth.label(rec)
    assert out["won"] is None and "not published" in out["reason"]


def test_a_malformed_report_labels_nothing(truth, monkeypatch):
    _stub_cli(monkeypatch, product_text="CLIMATE SUMMARY FOR AUGUST 21 2026\ngarbage")
    assert truth.cli_extremes("KSEA", "2026-08-21") is None


# ── Polymarket via its own resolution ────────────────────────────────────────

def _stub_gamma(monkeypatch, payload):
    monkeypatch.setattr(st._session, "get",
                        lambda url, params=None, timeout=None: _Resp(payload))


def test_gamma_reports_a_win(truth, monkeypatch):
    _stub_gamma(monkeypatch, [{"umaResolutionStatus": "resolved",
                               "outcomePrices": json.dumps(["1", "0"])}])
    assert truth.gamma_resolved("some-slug") is True


def test_gamma_reports_a_loss(truth, monkeypatch):
    _stub_gamma(monkeypatch, [{"umaResolutionStatus": "resolved",
                               "outcomePrices": ["0", "1"]}])
    assert truth.gamma_resolved("some-slug") is False


def test_an_unresolved_market_labels_nothing(truth, monkeypatch):
    _stub_gamma(monkeypatch, [{"umaResolutionStatus": "pending"}])
    out = truth.label({"venue": "poly", "slug": "s", "kind": "high"})
    assert out["won"] is None and "not resolved" in out["reason"]


def test_an_unresolved_market_is_not_cached(truth, monkeypatch):
    _stub_gamma(monkeypatch, [{"umaResolutionStatus": "pending"}])
    truth.gamma_resolved("s")
    assert "s" not in truth._gamma, "must be retried once it settles"


def test_an_unknown_venue_labels_nothing(truth):
    out = truth.label({"venue": "betfair"})
    assert out["won"] is None and "unknown venue" in out["reason"]


def test_missing_identity_labels_nothing(truth):
    assert truth.label({"venue": "kalshi", "kind": "high"})["won"] is None
    assert truth.label({"venue": "poly", "kind": "high"})["won"] is None
