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


# Austin's real 2026-08-23 sequence: a morning preliminary reporting the max so
# far, then the final issued after the climate day closed. Taking the first
# date match labelled a 105F day as 84F.
PRELIM_TEXT = CLI_TEXT.replace("MAXIMUM         78", "MAXIMUM         60")
# KSEA is UTC-8 standard, so the 2026-08-21 climate day closes at 08:00Z on
# the 22nd. "final" clears that boundary; "prelim" is issued inside the day.
FINAL_LISTING = {"@graph": [
    {"id": "final",  "issuanceTime": "2026-08-22T09:00:00+00:00"},
    {"id": "prelim", "issuanceTime": "2026-08-21T13:00:00+00:00"},
]}


def _stub_cli(monkeypatch, product_text=CLI_TEXT, listing=None, bodies=None):
    calls = {"n": 0}
    listing = listing if listing is not None else FINAL_LISTING

    def get(url, params=None, timeout=None):
        calls["n"] += 1
        if "/types/CLI/" in url:
            return _Resp(listing)
        if bodies:
            pid = url.rsplit("/", 1)[-1]
            return _Resp({"productText": bodies.get(pid, product_text)})
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
    assert truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles") == {"high": 78, "low": 60}


def test_cli_result_is_cached(truth, monkeypatch):
    calls = _stub_cli(monkeypatch)
    truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles")
    first = calls["n"]
    truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles")
    assert calls["n"] == first, "a station-day should be fetched once"


def test_cli_for_a_different_day_is_not_yet_published(truth, monkeypatch):
    _stub_cli(monkeypatch)
    assert truth.cli_extremes("KSEA", "2026-08-22", "America/Los_Angeles") is None


def test_a_transport_failure_is_not_cached_as_absent(truth, monkeypatch):
    """A network blip must not permanently mark a day unresolvable."""
    def boom(url, params=None, timeout=None):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(st._session, "get", boom)
    assert truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles") is None
    assert ("KSEA", "2026-08-21") not in truth._cli


def test_label_resolves_a_kalshi_low(truth, monkeypatch):
    _stub_cli(monkeypatch)
    rec = {"venue": "kalshi", "station": "KSEA", "date": "2026-08-21",
           "kind": "low", "lo": 61, "hi": None,
           "station_tz": "America/Los_Angeles"}
    out = truth.label(rec)
    assert out["actual_extreme"] == 60
    assert out["won"] is False, "the minimum came in at 60, below a >=61 bucket"


def test_label_resolves_a_kalshi_high(truth, monkeypatch):
    _stub_cli(monkeypatch)
    rec = {"venue": "kalshi", "station": "KSEA", "date": "2026-08-21",
           "kind": "high", "lo": 77, "hi": 79,
           "station_tz": "America/Los_Angeles"}
    assert truth.label(rec)["won"] is True


def test_unpublished_cli_labels_nothing(truth, monkeypatch):
    _stub_cli(monkeypatch, listing={"@graph": []})
    rec = {"venue": "kalshi", "station": "KSEA", "date": "2026-08-21",
           "kind": "high", "lo": 77, "hi": 79,
           "station_tz": "America/Los_Angeles"}
    out = truth.label(rec)
    assert out["won"] is None and "no FINAL CLI" in out["reason"]


def test_a_malformed_report_labels_nothing(truth, monkeypatch):
    _stub_cli(monkeypatch, product_text="CLIMATE SUMMARY FOR AUGUST 21 2026\ngarbage")
    assert truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles") is None


# ── Polymarket via its own resolution ────────────────────────────────────────

def _stub_gamma(monkeypatch, payload):
    monkeypatch.setattr(st._session, "get",
                        lambda url, params=None, timeout=None: _Resp(payload))


def _event(markets):
    return [{"markets": markets}]


def _mkt(token, won, question, resolved=True):
    return {"umaResolutionStatus": "resolved" if resolved else "pending",
            "outcomePrices": json.dumps(["1", "0"] if won else ["0", "1"]),
            "clobTokenIds": json.dumps([token, token + "-no"]),
            "question": question}


LADDER = _event([
    _mkt("t30", False, "Will the highest temperature in Ankara be 30°C or below on August 23?"),
    _mkt("t31", False, "Will the highest temperature in Ankara be 31°C on August 23?"),
    _mkt("t32", True,  "Will the highest temperature in Ankara be 32°C on August 23?"),
    _mkt("t33", False, "Will the highest temperature in Ankara be 33°C on August 23?"),
])


def test_a_temperature_event_resolves_the_whole_ladder(truth, monkeypatch):
    """The recorded slug is the EVENT slug. Querying it against /markets
    silently returned nothing, which left every non-Kalshi market permanently
    unlabelled - 73% of everything captured."""
    _stub_gamma(monkeypatch, LADDER)
    res = truth.gamma_event("highest-temperature-in-ankara-on-august-23-2026")
    assert res["by_token"]["t32"] is True
    assert res["by_token"]["t31"] is False


def test_the_winning_bucket_recovers_the_settled_temperature(truth, monkeypatch):
    """Outside the US there is no NWS climate report, so the bucket that
    settled true is the only route to a settled temperature."""
    _stub_gamma(monkeypatch, LADDER)
    res = truth.gamma_event("slug")
    assert res["settled"] == 32


def test_an_open_tailed_winner_has_no_midpoint(truth, monkeypatch):
    _stub_gamma(monkeypatch, _event([
        _mkt("t30", True, "Will the highest temperature in Ankara be 30°C or below on August 23?"),
    ]))
    assert truth.gamma_event("slug")["settled"] is None


def test_label_reads_our_own_bucket_out_of_the_ladder(truth, monkeypatch):
    _stub_gamma(monkeypatch, LADDER)
    win = truth.label({"venue": "poly", "slug": "s", "token_yes": "t32", "kind": "high"})
    lose = truth.label({"venue": "poly", "slug": "s", "token_yes": "t31", "kind": "high"})
    assert win["won"] is True and win["actual_extreme"] == 32
    assert lose["won"] is False


def test_a_token_absent_from_the_ladder_labels_nothing(truth, monkeypatch):
    _stub_gamma(monkeypatch, LADDER)
    out = truth.label({"venue": "poly", "slug": "s", "token_yes": "nope", "kind": "high"})
    assert out["won"] is None and "not in the resolved ladder" in out["reason"]


def test_an_unresolved_event_labels_nothing(truth, monkeypatch):
    _stub_gamma(monkeypatch, _event([
        _mkt("t31", False, "Will the highest temperature be 31°C?", resolved=False),
    ]))
    out = truth.label({"venue": "poly", "slug": "s", "token_yes": "t31", "kind": "high"})
    assert out["won"] is None and "not resolved" in out["reason"]


def test_an_unresolved_event_is_not_cached(truth, monkeypatch):
    _stub_gamma(monkeypatch, [])
    truth.gamma_event("s")
    assert "s" not in truth._gamma, "must be retried once it settles"


def test_an_unknown_venue_labels_nothing(truth):
    out = truth.label({"venue": "betfair"})
    assert out["won"] is None and "unknown venue" in out["reason"]


def test_missing_identity_labels_nothing(truth):
    assert truth.label({"venue": "kalshi", "kind": "high"})["won"] is None
    assert truth.label({"venue": "poly", "kind": "high"})["won"] is None


# ── preliminary vs final ─────────────────────────────────────────────────────

def test_a_preliminary_report_is_never_used(truth, monkeypatch):
    """The bug this caught: NWS publishes the max SO FAR during the day. Austin
    2026-08-23 read 84F at 07:35 local and 105F after midnight. Only a report
    issued after the climate day closed can be final."""
    _stub_cli(monkeypatch, bodies={"prelim": PRELIM_TEXT, "final": CLI_TEXT})
    ext = truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles")
    assert ext == {"high": 78, "low": 60}, "must take the final, not the preliminary"


def test_only_same_day_reports_means_not_yet_resolvable(truth, monkeypatch):
    """Every product still issued inside the day it describes."""
    listing = {"@graph": [{"id": "prelim",
                           "issuanceTime": "2026-08-21T13:00:00+00:00"}]}
    _stub_cli(monkeypatch, listing=listing, bodies={"prelim": PRELIM_TEXT})
    assert truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles") is None


def test_without_a_timezone_nothing_can_be_called_final(truth, monkeypatch):
    _stub_cli(monkeypatch)
    assert truth.cli_extremes("KSEA", "2026-08-21", None) is None


def test_the_labeller_refuses_a_record_with_no_timezone(truth, monkeypatch):
    _stub_cli(monkeypatch)
    out = truth.label({"venue": "kalshi", "station": "KSEA",
                       "date": "2026-08-21", "kind": "high", "lo": 77, "hi": 79})
    assert out["won"] is None and "timezone" in out["reason"]


def test_a_later_correction_supersedes_an_earlier_final(truth, monkeypatch):
    corrected = CLI_TEXT.replace("MAXIMUM         78", "MAXIMUM         79")
    listing = {"@graph": [
        {"id": "corr",  "issuanceTime": "2026-08-23T07:00:00+00:00"},
        {"id": "final", "issuanceTime": "2026-08-22T07:00:00+00:00"},
    ]}
    _stub_cli(monkeypatch, listing=listing,
              bodies={"corr": corrected, "final": CLI_TEXT})
    ext = truth.cli_extremes("KSEA", "2026-08-21", "America/Los_Angeles")
    assert ext["high"] == 79, "newest eligible report should win"
