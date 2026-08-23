"""Scoring, calibration, and the acceptance test.

The bar these encode is deliberately hostile: a model ships only if it beats
both the base rate and the market price. The live model_p fails the first, and
every price band fails the second, which is why the sector is currently flat.
"""
import math

import pytest

from modules import confidence as C


# ── metrics ──────────────────────────────────────────────────────────────────

def test_brier_is_zero_for_perfect_forecasts():
    assert C.brier([1.0, 0.0, 1.0], [True, False, True]) == 0.0


def test_brier_is_one_for_perfectly_wrong_forecasts():
    assert C.brier([0.0, 1.0], [True, False]) == 1.0


def test_a_coin_flip_scores_a_quarter():
    assert C.brier([0.5] * 4, [True, False, True, False]) == 0.25


def test_the_live_models_actual_numbers_reproduce():
    """45 settled entries, mean model_p 0.956, 62.2% won. The model's Brier
    must come out worse than simply predicting the base rate — that is the
    finding that says the score carries no information."""
    outcomes = [True] * 28 + [False] * 17          # 62.2%
    model = [0.956] * 45
    br = C.base_rate(outcomes)
    assert round(br, 3) == 0.622
    assert C.brier(model, outcomes) > C.brier([br] * 45, outcomes)


def test_base_rate_of_an_empty_set_is_none():
    assert C.base_rate([]) is None


def test_log_loss_punishes_confident_errors():
    mild = C.log_loss([0.6], [False])
    severe = C.log_loss([0.99], [False])
    assert severe > mild


# ── reliability ──────────────────────────────────────────────────────────────

def test_reliability_detects_overconfidence():
    """Predicting 0.9 while winning half the time must show a positive gap."""
    probs = [0.9] * 10
    outcomes = [True] * 5 + [False] * 5
    bins = C.reliability(probs, outcomes)
    assert len(bins) == 1
    assert bins[0]["predicted"] == 0.9
    assert bins[0]["actual"] == 0.5
    assert bins[0]["gap"] == pytest.approx(0.4)


def test_reliability_of_a_calibrated_forecaster_sits_on_the_diagonal():
    probs = [0.7] * 10
    outcomes = [True] * 7 + [False] * 3
    assert C.reliability(probs, outcomes)[0]["gap"] == pytest.approx(0.0)


def test_reliability_skips_empty_bins():
    assert all(b["n"] > 0 for b in C.reliability([0.95] * 5, [True] * 5))


# ── Platt scaling ────────────────────────────────────────────────────────────

def test_platt_pulls_an_overconfident_score_down():
    scores = [0.95] * 60
    outcomes = ([True] * 36) + ([False] * 24)      # really 60%
    params = C.fit_platt(scores, outcomes)
    cal = C.apply_platt(0.95, params)
    assert cal < 0.95, "calibration should reduce an overconfident score"
    assert C.brier([cal] * 60, outcomes) < C.brier(scores, outcomes)


def test_platt_is_the_identity_when_it_cannot_learn():
    """A single-class calibration set must do no harm rather than invent a fit."""
    assert C.fit_platt([0.8, 0.7], [True, True]) == (1.0, 0.0)
    assert C.apply_platt(0.8, (1.0, 0.0)) == pytest.approx(0.8, abs=1e-4)


def test_platt_output_stays_a_probability():
    params = C.fit_platt([0.9, 0.2, 0.7, 0.4], [True, False, True, False])
    for s in (0.001, 0.5, 0.999):
        assert 0.0 < C.apply_platt(s, params) < 1.0


# ── the market as a forecaster ───────────────────────────────────────────────

def test_price_converts_to_a_probability():
    assert C.market_prob({"ask_c": 80.0}) == pytest.approx(0.80)


def test_missing_price_has_no_probability():
    assert C.market_prob({"ask_c": None}) is None


def test_expected_value_is_zero_at_a_fair_price():
    """Buying at 70c with a true 70% chance is a break-even trade before fees."""
    assert C.expected_value_c(0.70, 70.0) == pytest.approx(0.0)


def test_expected_value_goes_negative_once_fees_are_charged():
    assert C.expected_value_c(0.70, 70.0, fee_c=1.2) < 0


def test_expected_value_rewards_a_genuine_edge():
    assert C.expected_value_c(0.80, 70.0) == pytest.approx(10.0)


# ── acceptance ───────────────────────────────────────────────────────────────

def test_a_model_that_cannot_beat_the_base_rate_is_rejected():
    outcomes = [True] * 28 + [False] * 17
    ev = C.evaluate([0.956] * 45, [0.7] * 45, outcomes)
    assert ev["beats_base_rate"] is False
    assert "does not beat the base rate" in ev["verdict"]


def test_beating_the_base_rate_but_not_the_price_is_still_rejected():
    """The market is the benchmark. Informative but worse than the price means
    no edge, which is the situation every price band is in today."""
    outcomes = [True, True, True, False, False, False]
    model = [0.7, 0.7, 0.7, 0.4, 0.4, 0.4]
    market = [0.95, 0.95, 0.95, 0.05, 0.05, 0.05]
    ev = C.evaluate(model, market, outcomes)
    assert ev["beats_base_rate"] is True
    assert ev["beats_market"] is False
    assert "not the price" in ev["verdict"]


def test_beating_both_passes():
    outcomes = [True, True, True, False, False, False]
    model = [0.95, 0.95, 0.95, 0.05, 0.05, 0.05]
    market = [0.6, 0.6, 0.6, 0.4, 0.4, 0.4]
    ev = C.evaluate(model, market, outcomes)
    assert ev["beats_base_rate"] and ev["beats_market"]
    assert ev["verdict"].startswith("PASS")


def test_evaluate_reports_no_data_rather_than_dividing_by_zero():
    assert C.evaluate([], [], [])["n"] == 0


def test_overconfidence_is_reported_signed():
    outcomes = [True] * 6 + [False] * 4
    ev = C.evaluate([0.9] * 10, [0.5] * 10, outcomes)
    assert ev["overconfidence"] == pytest.approx(0.3)


# ── break-even by band ───────────────────────────────────────────────────────

def test_break_even_flags_a_band_that_wins_less_than_its_price_requires():
    records = [{"ask_c": 80.0}] * 10
    outcomes = [True] * 6 + [False] * 4            # 60% at a price needing 80%
    row = C.break_even_by_price(records, outcomes, bands=((80, 101),))[0]
    assert row["gap_pp"] == pytest.approx(-20.0)


def test_break_even_skips_bands_with_no_trades():
    assert C.break_even_by_price([{"ask_c": 50.0}], [True],
                                 bands=((80, 101),)) == []


def test_the_market_is_scored_as_a_benchmark_not_a_candidate():
    """Comparing the price against itself gives an identical Brier, which would
    otherwise print as a rejection of the thing being used as the bar."""
    outcomes = [True, True, False, False]
    market = [0.9, 0.8, 0.2, 0.3]
    ev = C.evaluate(market, market, outcomes, benchmark=True)
    assert ev["verdict"].startswith("BENCHMARK")
