"""
modules/confidence.py — scoring, calibration, and the acceptance test.

The point of this module is not to produce a probability. It is to decide
whether a probability is worth acting on, and the bar is deliberately hostile:

  1. Beat the base rate.   A Brier score above "always predict the historical
     win rate" means the model is contributing nothing. The live model_p fails
     this today: 0.3516 against a base rate score of 0.2351.

  2. Beat the market.      The ask price IS a competing forecast — an 80c
     contract is the market saying 80%. Across all three price bands the market
     currently beats the in-house model by 4 to 16 points. A model that cannot
     out-predict the price has no edge, however well calibrated it is.

  3. Be calibrated.        If it says 0.70 it must win about 70% of the time.
     An uncalibrated score cannot be compared against a price at all, because
     the EV gate subtracts one from the other.

Calibration uses Platt scaling: a two-parameter logistic fitted on held-out
data. Isotonic regression is the more flexible choice and the wrong one here —
it is non-parametric and overfits small calibration sets, which is exactly the
regime this is in.

No sklearn dependency. The fits are small enough to do directly, and adding a
heavy wheel to a 512MB box that runs live trading is not worth it.
"""

import math
from collections import Counter


# ── metrics ──────────────────────────────────────────────────────────────────

def brier(probs, outcomes):
    """Mean squared error of probabilistic forecasts. Lower is better.
    0.25 is a coin flip; a constant base-rate forecast scores p(1-p)."""
    if not probs:
        return None
    return sum((p - (1.0 if o else 0.0)) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def log_loss(probs, outcomes, eps=1e-6):
    if not probs:
        return None
    total = 0.0
    for p, o in zip(probs, outcomes):
        p = min(max(p, eps), 1 - eps)
        total += -(math.log(p) if o else math.log(1 - p))
    return total / len(probs)


def base_rate(outcomes):
    return (sum(1 for o in outcomes if o) / len(outcomes)) if outcomes else None


def reliability(probs, outcomes, bins=5):
    """Reliability diagram as data: predicted vs actual, per confidence bin.

    A calibrated model sits on the diagonal. Overconfidence shows as actual
    falling below predicted — which is the shape the current model makes.
    """
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        idx = [j for j, p in enumerate(probs)
               if (p >= lo and p < hi) or (i == bins - 1 and p == 1.0)]
        if not idx:
            continue
        pred = sum(probs[j] for j in idx) / len(idx)
        act = sum(1 for j in idx if outcomes[j]) / len(idx)
        out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                    "predicted": round(pred, 4), "actual": round(act, 4),
                    "gap": round(pred - act, 4)})
    return out


# ── Platt scaling ────────────────────────────────────────────────────────────

def _logit(p, eps=1e-6):
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def fit_platt(scores, outcomes, iters=200, lr=0.1):
    """Fit p_cal = sigmoid(A * logit(s) + B) by gradient descent.

    Two parameters, which is the whole reason to prefer it here: it produces a
    usable fit from a calibration set far too small for anything richer.
    """
    if not scores or len(set(bool(o) for o in outcomes)) < 2:
        return (1.0, 0.0)          # degenerate: identity, do no harm
    xs = [_logit(s) for s in scores]
    ys = [1.0 if o else 0.0 for o in outcomes]
    a, b, n = 1.0, 0.0, len(xs)
    for _ in range(iters):
        ga = gb = 0.0
        for x, y in zip(xs, ys):
            pred = 1.0 / (1.0 + math.exp(-(a * x + b)))
            err = pred - y
            ga += err * x
            gb += err
        a -= lr * ga / n
        b -= lr * gb / n
    return (a, b)


def apply_platt(score, params):
    a, b = params
    return 1.0 / (1.0 + math.exp(-(a * _logit(score) + b)))


# ── the competing forecast ───────────────────────────────────────────────────

def market_prob(rec):
    """The price as a probability. This is the benchmark to beat, not a cost."""
    ask = rec.get("ask_c")
    if ask is None:
        return None
    return min(max(ask / 100.0, 0.001), 0.999)


def expected_value_c(p, ask_c, fee_c=0.0):
    """Cents of EV per share for buying at ask_c with probability p.

    Win returns (100 - ask); loss costs ask. Fees are charged on entry.
    """
    if p is None or ask_c is None:
        return None
    return p * (100.0 - ask_c) - (1.0 - p) * ask_c - (fee_c or 0.0)


# ── acceptance ───────────────────────────────────────────────────────────────

def evaluate(model_probs, market_probs, outcomes, label="model"):
    """Score a set of forecasts against outcomes and against the market.

    Returns the three numbers that decide whether the model ships, plus the
    reliability table that explains why.
    """
    n = len(outcomes)
    if not n:
        return {"n": 0, "verdict": "no labelled data"}
    br = base_rate(outcomes)
    b_model = brier(model_probs, outcomes)
    b_base = brier([br] * n, outcomes)
    b_market = brier(market_probs, outcomes) if market_probs else None

    beats_base = b_model is not None and b_base is not None and b_model < b_base
    beats_market = b_market is not None and b_model is not None and b_model < b_market

    if not beats_base:
        verdict = "REJECT — does not beat the base rate; the score carries no information"
    elif not beats_market:
        verdict = "REJECT — beats the base rate but not the price; no edge over the market"
    else:
        verdict = "PASS — beats both the base rate and the market"

    return {
        "label": label, "n": n,
        "base_rate": round(br, 4),
        "brier_model": round(b_model, 4) if b_model is not None else None,
        "brier_base_rate": round(b_base, 4) if b_base is not None else None,
        "brier_market": round(b_market, 4) if b_market is not None else None,
        "log_loss_model": round(log_loss(model_probs, outcomes), 4),
        "mean_predicted": round(sum(model_probs) / n, 4),
        "overconfidence": round(sum(model_probs) / n - br, 4),
        "beats_base_rate": beats_base,
        "beats_market": beats_market,
        "verdict": verdict,
        "reliability": reliability(model_probs, outcomes),
    }


def break_even_by_price(records, outcomes, bands=((0, 70), (70, 80), (80, 101))):
    """Realised win rate against the rate the price implies, per band.

    This is the check that first showed the strategy had no edge: every band
    won less often than its own price required.
    """
    rows = []
    for lo, hi in bands:
        idx = [i for i, r in enumerate(records)
               if r.get("ask_c") is not None and lo <= r["ask_c"] < hi]
        if not idx:
            continue
        wins = sum(1 for i in idx if outcomes[i])
        avg_ask = sum(records[i]["ask_c"] for i in idx) / len(idx)
        wr = wins / len(idx)
        rows.append({"band": f"{lo}-{hi}c", "n": len(idx),
                     "win_rate": round(wr, 4),
                     "break_even": round(avg_ask / 100.0, 4),
                     "gap_pp": round(100 * wr - avg_ask, 2)})
    return rows


def signal_mix(records):
    return dict(Counter(r.get("signal") for r in records).most_common())
