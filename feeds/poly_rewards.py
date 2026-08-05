"""
feeds/poly_rewards.py — scanner for Polymarket liquidity-provider (LP) rewards.

Origin: a Chinese-language tweet (x.com/094551YY, 2026-06-09) pitched "farming"
Polymarket's daily-temperature markets for LP rebates — sort by reward pool,
quote near-certain buckets, collect the daily USDC payout. Verified against
Polymarket's own docs (docs.polymarket.com/programs/liquidity-rewards): real
program, but the tweet's "low price = single-leg" claim is wrong — markets
with a midpoint outside [0.10, 0.90] require BOTH sides resting to score at
all (Q_min = min(Q_one, Q_two), no single-sided floor). Nearly every "safe"
near-100c/near-0c bucket in the tweet's screenshots is in that dead zone.

This module answers the question the tweet skips: how much does a market
actually pay per dollar of resting capital, given who's already quoting it?
It is a READ-ONLY estimator — it places no orders. Two calls:

  fetch_reward_markets()  — bulk pull of every market with an active reward
                             config, one paginated CLOB call, no per-market
                             requests.
  score_market(m)          — fetches that market's book and estimates
                             $/day per $1 of resting capital.

Scoring approximation (see docstring on score_market for the exact formula
and where it diverges from Polymarket's methodology).
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

log = logging.getLogger("feeds.poly_rewards")

CLOB_MULTI = "https://clob.polymarket.com/rewards/markets/multi"
CLOB_BOOK = "https://clob.polymarket.com/book"
TIMEOUT = 15
PAGE_SIZE = 500

# Scaling factor from Polymarket's scoring formula (docs.polymarket.com/
# programs/liquidity-rewards): single-sided liquidity scores at 1/c when the
# midpoint is inside [0.10, 0.90]; outside that band it scores nothing at all.
SINGLE_SIDE_SCALE = 3.0
SINGLE_SIDE_BAND = (0.10, 0.90)
# "The minimum reward payout is $1; amounts below this will not be paid."
# (docs.polymarket.com/programs/liquidity-rewards). Anything under this is
# economically zero, however good the yield percentage looks.
MIN_PAYOUT_USD = 1.0
# Above this computed share of a market's qualifying liquidity, the estimate
# is treated as unmeasurable rather than excellent — see `dominant` in
# score_market. Every real payment observed so far implies a share of
# 0.03-0.12; the readings that measured 20.5x too high all implied ~1.0.
DOMINANCE_SUSPECT = 0.5


_MARKETS_CACHE = {}   # (tag_slug, min_rate) -> (fetched_at, rows)
# Multiple callers pull this same bulk list within a single monitor cycle
# (scan(), the two-leg entry lookup, and its still-earning check). Each pull is
# up to 20 pages x 500 markets, and duplicated heavy scans have OOM-killed this
# 512MB droplet before. A TTL well under the cycle period (300s) means every
# cycle still sees fresh data — including intraday min_size and reward-pool
# changes, which this strategy depends on noticing — while within-cycle repeats
# are free.
_MARKETS_TTL_SEC = float(os.getenv("POLY_REWARDS_MARKETS_TTL_SEC", "60"))


def fetch_reward_markets(tag_slug="weather", min_rate=0.0, max_pages=20, use_cache=True):
    """Bulk-pull every active market with a reward config under `tag_slug`.

    One paginated endpoint (`/rewards/markets/multi`), no per-market calls —
    this is the cheap, always-safe half of the scanner. Returns a list of
    dicts: condition_id, question, slug, rate_per_day (summed across reward
    configs on the market), max_spread_c, min_size, competitiveness, tokens
    ([{token_id, outcome, price}, ...]).

    Results are cached for _MARKETS_TTL_SEC so repeat callers inside one
    monitor cycle share a single pull; pass use_cache=False to force a fresh
    one. Callers must treat the returned rows as READ-ONLY — they are shared.
    """
    key = (tag_slug, min_rate)
    if use_cache:
        hit = _MARKETS_CACHE.get(key)
        if hit and (time.monotonic() - hit[0]) < _MARKETS_TTL_SEC:
            return hit[1]

    out, cursor = [], None
    for _ in range(max_pages):
        params = {"tag_slug": tag_slug, "page_size": PAGE_SIZE,
                   "order_by": "rate_per_day", "position": "DESC"}
        if cursor:
            params["next_cursor"] = cursor
        r = requests.get(CLOB_MULTI, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json()
        for m in page.get("data") or []:
            rate = sum(float(c.get("rate_per_day") or 0) for c in (m.get("rewards_config") or []))
            if rate < min_rate:
                continue
            tokens = m.get("tokens") or []
            out.append({
                "condition_id": m.get("condition_id"),
                "question": m.get("question"),
                "slug": m.get("market_slug"),
                "rate_per_day": rate,
                "max_spread_c": float(m.get("rewards_max_spread") or 0),
                "min_size": float(m.get("rewards_min_size") or 0),
                "competitiveness": m.get("market_competitiveness"),
                "tokens": [{"token_id": t.get("token_id"), "outcome": t.get("outcome"),
                            "price": float(t.get("price") or 0)} for t in tokens],
            })
        cursor = page.get("next_cursor")
        if not cursor or cursor == "LTE=" or len(page.get("data") or []) < PAGE_SIZE:
            break
    _MARKETS_CACHE[key] = (time.monotonic(), out)
    return out


def _fetch_book(token_id, timeout=6):
    """Aggregated book for `token_id`: (bids, asks), each [(price_c, size)]."""
    r = requests.get(CLOB_BOOK, params={"token_id": token_id}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    bids = sorted(((float(b["price"]) * 100.0, float(b["size"])) for b in (data.get("bids") or [])),
                  reverse=True)
    asks = sorted((float(a["price"]) * 100.0, float(a["size"])) for a in (data.get("asks") or []))
    return bids, asks


def _side_score(levels, mid_c, v_c, min_size_shares):
    """Sum of S(v,s)*size over book levels within the reward band.

    Levels below `min_size_shares` are dropped — the book only reports
    aggregated size per price tick, not individual orders, so a level is
    treated as one qualifying order iff its total size clears the cutoff.
    A level thinner than that is assumed to be sub-minimum noise; this can
    undercount a level that is actually several qualifying orders stacked
    at the same tick, but never overcounts non-qualifying dust.
    """
    score = 0.0
    if v_c <= 0:
        return score
    for price_c, size in levels:
        s = abs(price_c - mid_c)
        if s > v_c or size < min_size_shares:
            continue
        score += ((v_c - s) / v_c) ** 2 * size
    return score


def _adjusted_mid_c(bids, asks, min_size_shares):
    """The 'size-cutoff-adjusted midpoint' the official methodology scores
    against (docs.polymarket.com/programs/liquidity-rewards, variable `s`):
    the midpoint computed from qualifying liquidity only, NOT the raw
    best-bid/best-ask touch.

    We were scoring spreads against the raw touch, which on a thin weather
    book can sit many cents away from the adjusted midpoint — and every
    order's score depends quadratically on that distance. Falls back to the
    raw touch when neither side has qualifying depth (nothing better is
    knowable from an aggregated book).
    """
    qb = [p for p, s in bids if s >= min_size_shares]
    qa = [p for p, s in asks if s >= min_size_shares]
    best_qbid = max(qb) if qb else None
    best_qask = min(qa) if qa else None
    if best_qbid is not None and best_qask is not None:
        return (best_qbid + best_qask) / 2.0, True
    raw_bid = bids[0][0] if bids else None
    raw_ask = asks[0][0] if asks else None
    if raw_bid is not None and raw_ask is not None:
        return (raw_bid + raw_ask) / 2.0, False
    if raw_bid is not None:
        return raw_bid, False
    if raw_ask is not None:
        return raw_ask, False
    return None, False


def _complement_as_yes(no_bids, no_asks):
    """Re-express the NO book in YES price space.

    The scoring formula's Q_one is `bids on m + asks on m'` and Q_two is
    `asks on m + bids on m'` — competitors quoting the NO token score
    against the SAME pool we do. We were only ever fetching the YES book,
    so every NO-side maker was invisible to us and our computed share of
    the pool came out far too high (measured 20.5x too high against a real
    payment, 2026-08-01).

    A NO ask at price q is economically a YES bid at (100 - q), and because
    mid_NO = 100 - mid_YES the distance from the midpoint is preserved
    exactly, so converted levels can be scored directly in YES space.
    """
    as_yes_bids = [(100.0 - p, s) for p, s in no_asks]
    as_yes_asks = [(100.0 - p, s) for p, s in no_bids]
    return as_yes_bids, as_yes_asks


def score_market(market, our_spread_c=0.0, book_fetcher=_fetch_book):
    """Estimate $/day per $1 of resting capital for one reward market.

    CALIBRATION STATUS (measured, not assumed): on 2026-08-01 this estimator
    predicted $2.4157 for a real resting order and Polymarket actually paid
    $0.1177 — 20.5x too high. See scripts/poly_rewards_calibrate.py, which
    re-measures this against the CLOB's own earnings endpoint; re-run it as
    real earning days accumulate. Two structural causes of that gap are now
    FIXED below (complement book, adjusted midpoint); the remaining ones are
    listed after. Do not trust any absolute figure from this until the
    calibration script reports close to 1.0x on n > 1 days.

    Fixed since that measurement:
      - Q_one/Q_two mix a market's own bid/ask with its COMPLEMENT market's
        ask/bid. We now fetch the NO book and fold it in (_complement_as_yes);
        previously every NO-side competitor was invisible, which inflated our
        computed share of the pool.
      - Spreads are now measured from the size-cutoff-adjusted midpoint the
        methodology specifies (_adjusted_mid_c), not the raw touch.
      - A book with no qualifying competition no longer silently reports a
        ~100% share as if it were an opportunity; it is flagged
        confidence="low" and sorted DOWN, not up.
      - Estimates below the program's $1 minimum payout are flagged
        below_min_payout — they are economically zero.

    Known-remaining approximations:
      - Rewards are pro-rated across ALL makers and re-sampled over a
        10,080-sample epoch; this is a single point-in-time snapshot, not a
        time-weighted average. A maker resting all day accumulates far more
        samples than a short-lived order, which this does not model.
      - Ignores the epoch-level cross-market normalization (step 7) — assumes
        this market's reward pool is fixed and only your share within it
        shifts, which is right for "how does one more dollar change my cut"
        but not for absolute cross-market comparisons.
      - An aggregated book cannot reveal individual order sizes, so a price
        level is still treated as one order for the min-size cutoff.

    Returns None if the book can't be fetched or has no usable mid.
    Otherwise a dict with capital_usd, est_daily_usd, yield_per_dollar_per_day,
    and the two-sided requirement flag.
    """
    tokens = market.get("tokens") or []
    yes = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), tokens[0] if tokens else None)
    no = next((t for t in tokens if t.get("outcome", "").lower() == "no"), None)
    if not yes or not yes.get("token_id"):
        return None
    price = yes.get("price") or 0.0

    try:
        bids, asks = book_fetcher(yes["token_id"])
    except Exception as e:  # noqa: BLE001
        log.debug("book fetch failed for %s: %s", market.get("slug"), e)
        return None
    if not bids and not asks:
        return None

    # The complement book counts toward the same reward pool (see
    # _complement_as_yes). Missing it was the single largest source of
    # over-estimation. Best-effort: if the NO book can't be fetched we fall
    # back to YES-only and say so via complement_seen, rather than silently
    # reporting a number computed from half the competition.
    complement_seen = False
    c_bids, c_asks = [], []
    if no and no.get("token_id"):
        try:
            no_bids, no_asks = book_fetcher(no["token_id"])
            c_bids, c_asks = _complement_as_yes(no_bids, no_asks)
            complement_seen = True
        except Exception as e:  # noqa: BLE001
            log.debug("complement book fetch failed for %s: %s", market.get("slug"), e)

    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None

    v_c = market["max_spread_c"]
    min_size = market["min_size"]

    # Score against the size-cutoff-adjusted midpoint the methodology
    # specifies, not the raw touch.
    all_bids = sorted(bids + c_bids, reverse=True)
    all_asks = sorted(asks + c_asks)
    mid_c, mid_is_adjusted = _adjusted_mid_c(all_bids, all_asks, min_size)
    if mid_c is None:
        return None
    # Judge sidedness off the FRESH book mid (mid_c, just computed above), not
    # the bulk-endpoint `price` snapshot — the two can disagree by the time we
    # re-fetch the book (caught live: a market showing price=0.055 from the
    # snapshot had a real mid_c of 16, well inside the single-sided band, so
    # this was flagging two_sided_required=True on a market that no longer
    # needed it).
    two_sided_required = not (SINGLE_SIDE_BAND[0] <= mid_c / 100.0 <= SINGLE_SIDE_BAND[1])

    # Q_one = bids on m + asks on m'; Q_two = asks on m + bids on m'
    q_bid_existing = _side_score(all_bids, mid_c, v_c, min_size)
    q_ask_existing = _side_score(all_asks, mid_c, v_c, min_size)

    our_shares = min_size  # smallest resting order that still qualifies
    our_score = ((v_c - our_spread_c) / v_c) ** 2 * our_shares if v_c > 0 else 0.0

    bid_share = our_score / (q_bid_existing + our_score) if (q_bid_existing + our_score) > 0 else 0.0
    ask_share = our_score / (q_ask_existing + our_score) if (q_ask_existing + our_score) > 0 else 0.0

    if two_sided_required:
        our_share = min(bid_share, ask_share)
    else:
        # single-sided still scores, at 1/c — quoting both sides and taking
        # the min (uncapped) beats quoting one side at 1/c whenever the two
        # single-side shares aren't wildly lopsided, so use whichever is better.
        our_share = max(min(bid_share, ask_share), max(bid_share, ask_share) / SINGLE_SIDE_SCALE)

    # ── how much to trust this number ───────────────────────────────────────
    # An aggregated book cannot show us orders that don't qualify individually
    # but stack into a qualifying level, and it cannot show competitors who
    # join AFTER this snapshot. When we detect NO qualifying competition at
    # all, our_share pins to ~1.0 — "we'd take the entire pool" — which is
    # exactly the case that measured 20.5x too high against a real payment.
    # That reading is not evidence of an opportunity, it's evidence that this
    # book is too thin to estimate from, so flag it instead of ranking on it.
    no_competition = (q_bid_existing <= 0 and q_ask_existing <= 0)
    # ...and the same failure mode arrives by degrees, not just at exactly
    # zero: a book with a token amount of qualifying depth still lets our own
    # min-size order dominate the denominator and pushes our_share toward 1.
    # Every real payment we have seen corresponds to a share in the 0.03-0.12
    # range. A claim that we would be MORE THAN HALF of all qualifying
    # liquidity in a market is not a signal that we found an empty market —
    # it's a signal we cannot see the market properly.
    dominant = our_share > DOMINANCE_SUSPECT
    confidence = "low" if (no_competition or dominant or not complement_seen) else "ok"

    est_daily_usd = market["rate_per_day"] * our_share
    # Rewards below the program's minimum are never actually paid out
    # (docs: "The minimum reward payout is $1; amounts below this will not be
    # paid"). Our only real earning day accrued $0.2469 and paid nothing.
    below_min_payout = est_daily_usd < MIN_PAYOUT_USD

    # Capital: resting bid ties up shares*price in USDC; resting ask ties up
    # shares*(1-price) worth of the token itself (must already be held or
    # bought — this estimator doesn't cost that acquisition).
    p = price if price <= 1 else price / 100.0
    capital_usd = our_shares * p + our_shares * (1 - p)  # both sides resting
    yield_per_dollar = (est_daily_usd / capital_usd) if capital_usd > 0 else 0.0

    return {
        "condition_id": market.get("condition_id"),
        "question": market.get("question"),
        "slug": market.get("slug"),
        "rate_per_day": market["rate_per_day"],
        "mid_c": mid_c,
        # honesty flags — see `confidence` above. Anything not "ok" must not
        # be treated as a real yield estimate.
        "confidence": confidence,
        "no_competition_detected": no_competition,
        "complement_seen": complement_seen,
        "mid_is_adjusted": mid_is_adjusted,
        "below_min_payout": below_min_payout,
        # RAW book, not just the derived mid — so a caller can independently
        # verify a real gap exists between real quotes, rather than trusting
        # mid_c/yield alone (both are already real, but this is the actual
        # evidence, not a re-derivation of it).
        "best_bid_c": best_bid, "best_ask_c": best_ask,
        "max_spread_c": v_c,
        "min_size": min_size,
        "two_sided_required": two_sided_required,
        "existing_bid_score": q_bid_existing,
        "existing_ask_score": q_ask_existing,
        "our_share": our_share,
        "est_daily_usd": est_daily_usd,
        "capital_usd": capital_usd,
        "yield_per_dollar_per_day": yield_per_dollar,
    }


def _find_reward_market(condition_id, tag_slug="weather"):
    """Locate one market's raw reward config by condition_id. There is no
    single-market lookup on the rewards API, so this pages the same bulk
    endpoint fetch_reward_markets uses until it finds the id."""
    cursor = None
    for _ in range(20):
        params = {"tag_slug": tag_slug, "page_size": PAGE_SIZE,
                   "order_by": "rate_per_day", "position": "DESC"}
        if cursor:
            params["next_cursor"] = cursor
        r = requests.get(CLOB_MULTI, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json()
        for m in page.get("data") or []:
            if m.get("condition_id") == condition_id:
                return m
        cursor = page.get("next_cursor")
        if not cursor or cursor == "LTE=" or len(page.get("data") or []) < PAGE_SIZE:
            break
    return None


def get_twoleg_plan(condition_id, tag_slug="weather", band_fraction=0.5,
                    book_fetcher=_fetch_book, market=None):
    """Build a TWO-LEGGED quote plan: a resting BID on YES and a resting BID
    on NO, both inside the reward band.

    Why two bids rather than a bid and an ask: YES and NO always redeem to
    exactly $1.00 together, so a resting NO bid IS the sell side of YES —
    without owning any inventory or shorting anything. And because the
    scoring formula counts bids on m under Q_one and bids on m' under Q_two,
    those two buy orders make us two-sided, which is worth up to 3x the
    single-sided rate and is the ONLY way to score at all once the midpoint
    leaves [0.10, 0.90] (where every near-decided weather bucket sits).

    band_fraction places each bid that fraction of the max spread BELOW its
    own midpoint. It is the strategy's main risk knob and it is a genuine
    trade-off, because the score function is S = ((v - s)/v)^2:

        0.0  -> quote at the midpoint: full score, hit constantly
        0.5  -> quarter score, meaningfully out of the way
        1.0  -> quote at the band edge: score is exactly ZERO

    score_weight is returned so a paper run can compare reward earned against
    fill rate at different settings rather than guessing.

    Returns None if either book is missing or the plan prices out of range.
    """
    m = market or _find_reward_market(condition_id, tag_slug)
    if not m:
        return None
    tokens = m.get("tokens") or []
    yes = next((t for t in tokens if (t.get("outcome") or "").lower() == "yes"), None)
    no = next((t for t in tokens if (t.get("outcome") or "").lower() == "no"), None)
    if not yes or not no or not yes.get("token_id") or not no.get("token_id"):
        return None

    try:
        y_bids, y_asks = book_fetcher(yes["token_id"])
        n_bids, n_asks = book_fetcher(no["token_id"])
    except Exception as e:  # noqa: BLE001
        log.debug("two-leg book fetch failed for %s: %s", m.get("market_slug"), e)
        return None
    if not (y_bids and y_asks and n_bids and n_asks):
        return None

    # Accept either shape: the RAW market dict from the rewards API (what
    # _find_reward_market returns) or the NORMALIZED one fetch_reward_markets
    # produces. They use different key names for the same three fields, and
    # silently reading the wrong ones yields 0 and makes every market look
    # non-viable.
    v = float(m.get("rewards_max_spread") or m.get("max_spread_c") or 0)
    size = float(m.get("rewards_min_size") or m.get("min_size") or 0)
    if v <= 0 or size <= 0:
        return None

    y_mid = (y_bids[0][0] + y_asks[0][0]) / 2.0
    n_mid = (n_bids[0][0] + n_asks[0][0]) / 2.0
    offset = v * band_fraction
    # Floor to the 1c tick: rounding UP could push a bid above where we
    # intended and, at small band_fractions, across the touch into a
    # marketable order that post_only would reject outright.
    y_bid = float(int(y_mid - offset))
    n_bid = float(int(n_mid - offset))
    if y_bid < 1 or n_bid < 1:
        return None
    # Must stay a genuine maker quote on both sides.
    if y_bid >= y_asks[0][0] or n_bid >= n_asks[0][0]:
        return None

    total_c = y_bid + n_bid
    score_weight = ((v - offset) / v) ** 2 if v > 0 else 0.0
    return {
        "condition_id": condition_id,
        "question": m.get("question"),
        "slug": m.get("market_slug") or m.get("slug"),
        "rate_per_day": (sum(float(c.get("rate_per_day") or 0)
                             for c in (m.get("rewards_config") or []))
                         or float(m.get("rate_per_day") or 0)),
        "yes_token": yes["token_id"], "no_token": no["token_id"],
        "yes_mid_c": y_mid, "no_mid_c": n_mid,
        "yes_bid_c": y_bid, "no_bid_c": n_bid,
        "yes_best_ask_c": y_asks[0][0], "no_best_ask_c": n_asks[0][0],
        "yes_best_bid_c": y_bids[0][0], "no_best_bid_c": n_bids[0][0],
        "size": size, "max_spread_c": v, "band_fraction": band_fraction,
        "score_weight": round(score_weight, 4),
        "total_c": total_c,
        "capital_usd": round(total_c / 100.0 * size, 2),
        # Both legs filling leaves a complete set, worth exactly $1.00/share
        # however the weather turns out.
        "both_fill_usd": round((100.0 - total_c) / 100.0 * size, 2),
        "two_sided_required": not (SINGLE_SIDE_BAND[0] <= y_mid / 100.0 <= SINGLE_SIDE_BAND[1]),
    }


def get_band(condition_id, tag_slug="weather"):
    """On-demand band fetch for ONE market — the reference mid, reward-
    eligible price range, real book depth, and min_size, re-scored against a
    FRESH book (not a cached scan result). This replaces hand-copying these
    numbers out of a scan() run into a one-off script's hardcoded constants
    every time a market's reference price drifts — call this right before
    placing/repricing an order instead.

    Scans the same paginated bulk endpoint fetch_reward_markets uses (there
    is no single-market lookup on Polymarket's rewards API) until it finds
    condition_id, then re-fetches that market's own book fresh. Returns None
    if the market has no active reward config or its book can't be fetched.
    """
    market = None
    cursor = None
    for _ in range(20):
        params = {"tag_slug": tag_slug, "page_size": PAGE_SIZE,
                   "order_by": "rate_per_day", "position": "DESC"}
        if cursor:
            params["next_cursor"] = cursor
        r = requests.get(CLOB_MULTI, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json()
        for m in page.get("data") or []:
            if m.get("condition_id") == condition_id:
                market = m
                break
        if market:
            break
        cursor = page.get("next_cursor")
        if not cursor or cursor == "LTE=" or len(page.get("data") or []) < PAGE_SIZE:
            break
    if not market:
        return None

    rate = sum(float(c.get("rate_per_day") or 0) for c in (market.get("rewards_config") or []))
    tokens = market.get("tokens") or []
    m_dict = {
        "condition_id": market.get("condition_id"), "question": market.get("question"),
        "slug": market.get("market_slug"), "rate_per_day": rate,
        "max_spread_c": float(market.get("rewards_max_spread") or 0),
        "min_size": float(market.get("rewards_min_size") or 0),
        "tokens": [{"token_id": t.get("token_id"), "outcome": t.get("outcome"),
                    "price": float(t.get("price") or 0)} for t in tokens],
    }
    band = score_market(m_dict)
    if not band:
        return None
    v_c = band["max_spread_c"]
    band["band_lo_c"] = round(band["mid_c"] - v_c, 2)
    band["band_hi_c"] = round(band["mid_c"] + v_c, 2)
    yes = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), tokens[0] if tokens else None)
    band["token_id"] = yes.get("token_id") if yes else None
    return band


def scan(tag_slug="weather", min_rate=1.0, max_workers=8, question_filter=None):
    """Fetch reward markets, score each one's book in parallel, return sorted
    by yield_per_dollar_per_day descending. Markets whose book can't be
    fetched are silently dropped (transient errors, not scanner bugs).
    """
    markets = fetch_reward_markets(tag_slug=tag_slug, min_rate=min_rate)
    if question_filter:
        markets = [m for m in markets if question_filter(m.get("question") or "")]

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(score_market, m): m for m in markets}
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                log.debug("score failed for %s: %s", futures[fut].get("slug"), e)
                continue
            if res:
                results.append(res)

    # Rank trustworthy estimates first. Sorting purely on yield put the
    # SATURATED rows on top — the ones where an empty book made our_share
    # pin to 1.0 — so the head of this list was systematically the markets
    # the estimator had failed on, which is how three of them got traded.
    results.sort(key=lambda r: (r.get("confidence") != "ok",
                                r.get("below_min_payout", False),
                                -r["yield_per_dollar_per_day"]))
    return results
