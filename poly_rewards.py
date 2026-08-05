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


def fetch_reward_markets(tag_slug="weather", min_rate=0.0, max_pages=20):
    """Bulk-pull every active market with a reward config under `tag_slug`.

    One paginated endpoint (`/rewards/markets/multi`), no per-market calls —
    this is the cheap, always-safe half of the scanner. Returns a list of
    dicts: condition_id, question, slug, rate_per_day (summed across reward
    configs on the market), max_spread_c, min_size, competitiveness, tokens
    ([{token_id, outcome, price}, ...]).
    """
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


def score_market(market, our_spread_c=0.0, book_fetcher=_fetch_book):
    """Estimate $/day per $1 of resting capital for one reward market.

    Approximation vs. Polymarket's actual formula (docs.polymarket.com/
    programs/liquidity-rewards):
      - Q_one/Q_two normally mix a market's own bid/ask with its complement
        market's ask/bid (YES bid + NO ask on one side, YES ask + NO bid on
        the other). We only fetch the YES book and treat its bid depth as
        one side, its ask depth as the other — correct when YES/NO books are
        mirror images (the common case), optimistic if they've decoupled.
      - Rewards are pro-rated across ALL makers and re-sampled every ~60s
        over a 10,080-sample epoch; this is a single point-in-time snapshot,
        not a time-weighted average.
      - Ignores the epoch-level cross-market normalization (step 7) — assumes
        this market's reward pool is fixed and only your share within it
        shifts, which is right for "how does one more dollar change my cut"
        but not for absolute cross-market comparisons.

    Returns None if the book can't be fetched or has no usable mid.
    Otherwise a dict with capital_usd, est_daily_usd, yield_per_dollar_per_day,
    and the two-sided requirement flag.
    """
    tokens = market.get("tokens") or []
    yes = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), tokens[0] if tokens else None)
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

    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    if best_bid is not None and best_ask is not None:
        mid_c = (best_bid + best_ask) / 2.0
    elif best_bid is not None:
        mid_c = best_bid
    elif best_ask is not None:
        mid_c = best_ask
    else:
        return None

    v_c = market["max_spread_c"]
    min_size = market["min_size"]
    # Judge sidedness off the FRESH book mid (mid_c, just computed above), not
    # the bulk-endpoint `price` snapshot — the two can disagree by the time we
    # re-fetch the book (caught live: a market showing price=0.055 from the
    # snapshot had a real mid_c of 16, well inside the single-sided band, so
    # this was flagging two_sided_required=True on a market that no longer
    # needed it).
    two_sided_required = not (SINGLE_SIDE_BAND[0] <= mid_c / 100.0 <= SINGLE_SIDE_BAND[1])

    q_bid_existing = _side_score(bids, mid_c, v_c, min_size)
    q_ask_existing = _side_score(asks, mid_c, v_c, min_size)

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

    est_daily_usd = market["rate_per_day"] * our_share

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

    results.sort(key=lambda r: r["yield_per_dollar_per_day"], reverse=True)
    return results
