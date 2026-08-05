#!/usr/bin/env python3
"""
scripts/poly_lp_roundtrip_test.py — the GO-LIVE GATE TEST for the two-leg LP
executor. Run BY HAND, on the droplet, before ever setting POLY_TWOLEG_LIVE=true:

    cd /opt/kalshi-poly && venv/bin/python scripts/poly_lp_roundtrip_test.py
    cd /opt/kalshi-poly && venv/bin/python scripts/poly_lp_roundtrip_test.py --matched

It exercises, with the smallest real order the CLOB accepts, the exact
primitives the executor trusts (modules/poly_rewards_live -> scripts/
poly_sdk_runner.py) and prints an explicit safe/NOT-safe-to-arm verdict.

GATE 1 (default phase): place -> status -> cancel -> status.
  A resting post-only bid at 1c, priced BELOW the market's own best bid so it
  should never trade (~$1 notional at risk only if the book collapses to 1c
  while it rests). Proves: placement returns an order_id; get_order returns
  a readable size_matched for an OPEN order; cancel confirms; and records how
  get_order behaves on a CANCELLED order.

GATE 2 (--matched): the question the review flagged and only a real fill can
  answer — what does get_order return for a FULLY-MATCHED order? On a
  NONEXISTENT id it raises (UnexpectedResponseError -> our status() reports
  UNKNOWN, which is safe). If a MATCHED order does the same, a filled leg
  would read UNKNOWN forever and the executor would stall until the 90-min
  expiry closed the slot with shares held naked — in that case DO NOT arm
  live until a fallback lands. This phase deliberately BUYS ~$1-2 of the
  cheapest ask on a weather bucket (post_only=False) and asks for typed
  confirmation first. The shares are kept (pennies; they either expire
  worthless or pay $1 each).

Auto-picks a target from live weather reward markets; override with
--token/--price/--size. This script places REAL orders when confirmed — it is
the operator's tool, deliberately not wired into any automated loop.
"""

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from feeds.poly_rewards import fetch_reward_markets, _fetch_book  # noqa: E402
from modules.poly_rewards_live import (place_limit, cancel_limit,  # noqa: E402
                                       order_status)

MIN_NOTIONAL_USD = 1.05   # CLOB minimum order value is $1 — leave headroom


def _pick_market(want_cheap_ask):
    """Find a temperature market to test against.

    Gate 1 wants a YES book whose best bid is comfortably ABOVE 1c (our 1c
    resting bid then sits behind it and should never trade). Gate 2 wants the
    cheapest real ask (1-12c) so the deliberate fill costs pennies.
    """
    markets = fetch_reward_markets(tag_slug="weather", min_rate=1.0)
    markets = [m for m in markets if "temperature in" in (m.get("question") or "").lower()]
    best = None
    for m in markets:
        tokens = [t for t in (m.get("tokens") or []) if t.get("token_id")]
        yes = next((t for t in tokens if (t.get("outcome") or "").lower() == "yes"), None)
        if want_cheap_ask:
            # A cheap ask can live on EITHER token (the NO of a near-certain
            # bucket is as good a probe target as the YES of a long shot).
            # Measured 2026-08-04: weather books quote WIDE — across 120
            # markets only one token had a best ask under 12c, so anything
            # tighter than that finds nothing. Total cost stays ~$1.1 by
            # construction (size = $1.05 notional / price). Depth is counted
            # across [ask, ask+1c] because the gate-2 buy crosses at ask+1
            # and needs to FULLY match — a partial leaves the order LIVE,
            # which answers nothing about matched-order readability.
            for t in tokens:
                try:
                    _, asks = _fetch_book(t["token_id"])
                except Exception:  # noqa: BLE001
                    continue
                if not asks:
                    continue
                ask_c = asks[0][0]
                if not (1.0 <= ask_c <= 12.0):
                    continue
                needed = math.ceil(MIN_NOTIONAL_USD / (ask_c / 100.0))
                cum = sum(sz for p, sz in asks if p <= ask_c + 1)
                if cum < needed:
                    continue
                if best is None or ask_c < best[2]:
                    best = (m, t["token_id"], ask_c)
        else:
            if not yes:
                continue
            try:
                bids, _ = _fetch_book(yes["token_id"])
            except Exception:  # noqa: BLE001
                continue
            # need our 1c bid to sit BELOW a real standing bid
            if bids and bids[0][0] >= 3.0:
                return m, yes["token_id"], bids[0][0]
    return best


def gate1(args):
    print("=" * 72)
    print("GATE 1 — place / status / cancel / status  (resting, should not trade)")
    print("=" * 72)
    if args.token:
        token, price_c = args.token, args.price or 1.0
        question = "(operator-supplied token)"
    else:
        picked = _pick_market(want_cheap_ask=False)
        if not picked:
            print("FAIL: no suitable market found (need YES best bid >= 3c); rerun or pass --token")
            return False
        m, token, best_bid = picked
        question, price_c = m.get("question"), 1.0
        print(f"target: {question}")
        print(f"  best bid {best_bid:.0f}c — our resting bid goes at {price_c:.0f}c, below it")
    size = args.size or math.ceil(MIN_NOTIONAL_USD / (price_c / 100.0))
    print(f"  placing BUY {size} sh @ {price_c:.0f}c post_only (${size * price_c / 100:.2f} notional)")
    if input("type PLACE to continue (anything else aborts): ").strip() != "PLACE":
        print("aborted — nothing placed")
        return False

    oid = place_limit(token, price_c, size, side="BUY", post_only=True, live=True)
    if not oid:
        print("FAIL: placement returned no order_id — check logs above")
        return False
    print(f"PASS: placed, order_id={oid}")

    time.sleep(2)
    st = order_status(oid, live=True)
    if st is None:
        print("FAIL: get_order could not read our own OPEN order — executor would see")
        print("      every leg as UNKNOWN. DO NOT ARM LIVE.")
        cancel_limit(oid, live=True)
        return False
    print(f"PASS: open-order status readable: status={st['status']} "
          f"matched={st['size_matched']}/{st['original_size']}")
    if st["size_matched"] > 0:
        print("  NOTE: order matched immediately?! book moved — treat as gate 2 data")

    ok = cancel_limit(oid, live=True)
    print(("PASS" if ok else "FAIL") + f": cancel confirmed={ok}")
    if not ok:
        print(f"  MANUAL CANCEL REQUIRED for {oid}")
        return False

    time.sleep(2)
    st2 = order_status(oid, live=True)
    if st2 is None:
        print("INFO: get_order on a CANCELLED order raises/unreadable -> reads UNKNOWN.")
        print("      Same is likely true for MATCHED orders — run --matched to confirm.")
    else:
        print(f"INFO: cancelled order still readable: status={st2['status']} "
              f"matched={st2['size_matched']}")
    return True


def gate2(args):
    print("=" * 72)
    print("GATE 2 — get_order behavior on a FULLY-MATCHED order (real ~$1-2 buy)")
    print("=" * 72)
    if args.token:
        token, ask_c = args.token, args.price
        if not ask_c:
            print("FAIL: --matched with --token requires --price (the ask to cross)")
            return False
        question = "(operator-supplied token)"
    else:
        picked = _pick_market(want_cheap_ask=True)
        if not picked:
            print("FAIL: no 1-12c ask with enough depth found right now; rerun later or pass --token/--price")
            return False
        m, token, ask_c = picked
        question = m.get("question")
    size = args.size or math.ceil(MIN_NOTIONAL_USD / (ask_c / 100.0))
    cost = size * ask_c / 100.0
    print(f"target: {question}")
    print(f"  will BUY {size} sh @ up to {ask_c + 1:.0f}c (crossing the {ask_c:.0f}c ask)")
    print(f"  real cost ~${cost:.2f}; shares are kept (expire worthless or pay $1 each)")
    if input("type BUY to spend this (anything else aborts): ").strip() != "BUY":
        print("aborted — nothing placed")
        return False

    oid = place_limit(token, min(ask_c + 1, 99), size, side="BUY", post_only=False, live=True)
    if not oid:
        print("FAIL: marketable order returned no order_id — check logs")
        return False
    print(f"placed marketable order {oid}; waiting 5s for the match to settle...")
    time.sleep(5)

    st = order_status(oid, live=True)
    if st is None:
        print("VERDICT: get_order on a MATCHED order is UNREADABLE (raises).")
        print("  A filled leg would read UNKNOWN forever -> the executor would stall")
        print("  until the 90-min expiry closes the slot with shares held naked.")
        print("  DO NOT set POLY_TWOLEG_LIVE=true until a fallback for this lands")
        print("  (e.g. treat status-gone + order-absent-from-open-list as FILLED).")
        return False
    print(f"VERDICT: matched order READABLE: status={st['status']} "
          f"matched={st['size_matched']}/{st['original_size']}")
    if st["size_matched"] >= size:
        print("  Fill detection via size_matched works end-to-end. GATE 2 PASSES —")
        print("  safe to arm POLY_TWOLEG_LIVE from the fill-detection standpoint.")
        return True
    print("  Partial/no match — book may have moved; re-run, or inspect by hand.")
    return False


def main():
    ap = argparse.ArgumentParser(description="Two-leg LP go-live gate test (real orders, run by hand)")
    ap.add_argument("--matched", action="store_true", help="run GATE 2 (deliberate ~$1-2 fill)")
    ap.add_argument("--token", help="override: token_id to test against")
    ap.add_argument("--price", type=float, help="override: price in cents")
    ap.add_argument("--size", type=float, help="override: share count")
    args = ap.parse_args()
    ok = gate2(args) if args.matched else gate1(args)
    print()
    print("RESULT:", "PASS" if ok else "NOT PASSED — see above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
