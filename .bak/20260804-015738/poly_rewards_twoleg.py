"""
modules/poly_rewards_twoleg.py — two-legged LP quoting with auto-complete.

THE PROBLEM THIS SOLVES. Every LP position this project took was a single
resting BUY on one side of one bucket. That shape is indefensible: a fill
hands you a directional weather bet at the full entry price, against an LP
reward worth a fraction of a cent. Buenos Aires (85c), NYC (81c) and Phoenix
(70c) all died exactly that way, and the measured LP income against them was
$0.2469 — below the program's $1 minimum, so nothing was ever actually paid.

THE STRUCTURE THAT FIXES IT. YES and NO always redeem to exactly $1.00
together. Two consequences, both verified against live books (mid_sum came
back 100.0 on every market checked):

  1. Resting a BID on YES and a BID on NO is not two bets. If both fill we
     hold a complete set, worth exactly $1.00 per share no matter what the
     weather does. Buying the pair for less than $1.00 is profit that does
     not depend on being right about anything.

  2. If only ONE leg fills, the complement can be bought at market for
     almost exactly the remainder, because NO_ask = 100 - YES_bid
     structurally. Completing is break-even (minus the taker fee), so an
     unwanted fill costs ~0 instead of the entry price. THIS is the
     auto-complete path, and it is the entire risk control.

And because the scoring formula counts bids on m under Q_one and bids on m'
under Q_two, those same two buy orders make us two-sided for rewards — up to
3x the single-sided rate, and the only way to score anything once the
midpoint leaves [0.10, 0.90], which is where every near-decided weather
bucket lives. Our old single-leg quotes on those buckets were earning a
structural zero.

PAPER BY DEFAULT. POLY_TWOLEG_LIVE=false (the default) places nothing: fills
are inferred from real books (a resting BUY at p is treated as filled once
the opposing best ask trades down to p) and every action is written to the
ledger as it would have happened. The point of the paper phase is to measure
the two numbers the design actually rests on — how often BOTH legs fill, and
what auto-complete really costs when only one does — before any capital.

One position at a time, deliberately: the auto-complete path must never be
competing with itself for attention while a leg sits naked.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds.poly_rewards import get_twoleg_plan, fetch_reward_markets, _fetch_book
from modules.poly_rewards_live import place_limit, cancel_limit

log = logging.getLogger("modules.poly_rewards_twoleg")

TWOLEG_LOG = Path("poly_twoleg.jsonl")
POLY_TWOLEG_LIVE = os.getenv("POLY_TWOLEG_LIVE", "false").strip().lower() == "true"
# Fraction of the max spread to sit BELOW each midpoint. See get_twoleg_plan:
# 0.0 is at the mid (max reward, filled constantly), 1.0 is the band edge
# (zero reward). This is the knob the paper phase exists to calibrate.
BAND_FRACTION = float(os.getenv("POLY_TWOLEG_BAND_FRACTION", "0.5"))
# Give up and re-quote if neither leg has traded in this long.
MAX_QUOTE_MIN = float(os.getenv("POLY_TWOLEG_MAX_QUOTE_MIN", "90"))
# Weather taker fee rate — what auto-complete pays to cross the spread.
# fee = shares * rate * p * (1-p), symmetric around 50c, cheapest at the
# extremes, which is the real reason to prefer low-priced buckets.
TAKER_FEE_RATE = float(os.getenv("POLY_TAKER_FEE_RATE", "0.05"))


def _taker_fee_usd(shares, price_c):
    p = price_c / 100.0
    return shares * TAKER_FEE_RATE * p * (1.0 - p)


class PolyTwoLeg:
    """Single active two-leg quote at a time.

    State is replayed from TWOLEG_LOG so a restart never loses track of a
    position — critically, never loses track of a leg that filled and still
    needs completing.
    """

    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.pos = self._load_open()

    # ── ledger ───────────────────────────────────────────────────────────────
    def _persist(self, rec):
        try:
            with open(TWOLEG_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # noqa: BLE001
            log.warning("two-leg log write failed: %s", e)

    def _load_open(self):
        if not TWOLEG_LOG.exists():
            return None
        pos = None
        for line in open(TWOLEG_LOG):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = rec.get("type")
            if t == "twoleg_placed":
                pos = dict(rec)
                pos["yes_filled"] = False
                pos["no_filled"] = False
            elif pos and rec.get("id") == pos.get("id"):
                if t == "twoleg_fill":
                    pos[f"{rec['side']}_filled"] = True
                elif t == "twoleg_completed":
                    pos["completed"] = True
                elif t == "twoleg_closed":
                    pos = None
        return pos

    # ── main loop ────────────────────────────────────────────────────────────
    def cycle(self, scan_results=None):
        if self.pos is None:
            self._maybe_enter(scan_results)
            return
        if self.pos.get("completed"):
            # A complete set needs no management at all — it redeems at
            # exactly $1.00/share at settlement, so there is nothing to
            # hedge, sell, or watch. Close the slot and move on.
            self._close("complete_set_held")
            return
        self._check_legs()

    # ── entry ────────────────────────────────────────────────────────────────
    def _maybe_enter(self, scan_results):
        if not scan_results:
            return
        # Only markets whose yield estimate we actually trust (see
        # score_market's confidence flag — the saturated readings were the
        # ones that measured 20.5x hot).
        cands = [r for r in scan_results
                 if r.get("confidence") == "ok" and not r.get("below_min_payout")]
        if not cands:
            return
        top = sorted(cands, key=lambda x: -x["yield_per_dollar_per_day"])[:5]
        # Resolve all five markets from ONE bulk pull. get_twoleg_plan will
        # otherwise re-page the entire rewards API per candidate (up to 20
        # pages x 500 each), which is exactly the kind of duplicated heavy
        # scan that has OOM-killed this 512MB droplet before.
        try:
            wanted = {r["condition_id"] for r in top}
            by_cid = {m["condition_id"]: m for m in fetch_reward_markets(tag_slug="weather")
                      if m.get("condition_id") in wanted}
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[twoleg] market lookup failed: {e}")
            return
        for r in top:
            m = by_cid.get(r["condition_id"])
            if not m:
                continue
            plan = get_twoleg_plan(r["condition_id"], band_fraction=BAND_FRACTION, market=m)
            if not plan or plan["both_fill_usd"] <= 0:
                continue
            self._enter(plan)
            return

    def _enter(self, plan):
        pid = f"{plan['condition_id']}-{int(time.time())}"
        yes_oid = place_limit(plan["yes_token"], plan["yes_bid_c"], plan["size"],
                              side="BUY", post_only=True, live=POLY_TWOLEG_LIVE)
        no_oid = place_limit(plan["no_token"], plan["no_bid_c"], plan["size"],
                             side="BUY", post_only=True, live=POLY_TWOLEG_LIVE)
        if POLY_TWOLEG_LIVE and not (yes_oid and no_oid):
            # A half-placed pair is the exact naked-leg state this design exists
            # to avoid. The leg that DID place is resting live and directional —
            # actually cancel it (the previous version only logged that it
            # would, and orphaned the survivor off the ledger entirely).
            survivor = yes_oid or no_oid
            if survivor:
                which = "YES" if yes_oid else "NO"
                if cancel_limit(survivor, live=POLY_TWOLEG_LIVE):
                    self.on_log("!", f"[twoleg] one leg failed to place — cancelled the resting "
                                     f"{which} leg, no naked exposure left")
                else:
                    # Cancel failed too: the survivor is genuinely stuck live.
                    # Record it so it is never silently lost — a human or a
                    # later sweep can cancel it by id.
                    self._persist({"type": "twoleg_orphan",
                                  "ts": datetime.now(timezone.utc).isoformat(),
                                  "order_id": survivor, "side": "yes" if yes_oid else "no",
                                  "token": plan["yes_token"] if yes_oid else plan["no_token"],
                                  "note": "leg placed, sibling failed AND cancel failed — MANUAL CANCEL REQUIRED"})
                    self.on_log("✗", f"[twoleg] ORPHAN — {which} leg {str(survivor)[:16]} is resting "
                                     f"live and could not be cancelled; recorded for manual cancel")
            return

        rec = {
            "type": "twoleg_placed", "id": pid,
            "ts": datetime.now(timezone.utc).isoformat(),
            "condition_id": plan["condition_id"], "question": plan["question"],
            "yes_token": plan["yes_token"], "no_token": plan["no_token"],
            "yes_bid_c": plan["yes_bid_c"], "no_bid_c": plan["no_bid_c"],
            "size": plan["size"], "total_c": plan["total_c"],
            "capital_usd": plan["capital_usd"], "both_fill_usd": plan["both_fill_usd"],
            "band_fraction": plan["band_fraction"], "score_weight": plan["score_weight"],
            "rate_per_day": plan["rate_per_day"],
            "two_sided_required": plan["two_sided_required"],
            "yes_order_id": yes_oid, "no_order_id": no_oid,
            "mode": "live" if POLY_TWOLEG_LIVE else "paper",
        }
        self._persist(rec)
        self.pos = dict(rec, yes_filled=False, no_filled=False)
        self.on_log("→", f"[twoleg] {'LIVE' if POLY_TWOLEG_LIVE else 'PAPER'} quoted "
                         f"{(plan['question'] or '')[:38]} — YES {plan['yes_bid_c']:.0f}c + "
                         f"NO {plan['no_bid_c']:.0f}c = {plan['total_c']:.0f}c "
                         f"({plan['size']:.0f} sh, ${plan['capital_usd']:.2f} capital); "
                         f"both fill → +${plan['both_fill_usd']:.2f} risk-free, "
                         f"score weight {plan['score_weight']:.2f}")

    # ── fill detection + auto-complete ───────────────────────────────────────
    def _check_legs(self):
        pos = self.pos
        try:
            y_bids, y_asks = _fetch_book(pos["yes_token"])
            n_bids, n_asks = _fetch_book(pos["no_token"])
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[twoleg] book fetch failed: {e}")
            return

        # A resting BUY at p is filled once the opposing ask trades down to p.
        # (In live mode this is still the cheap check; a real order-status or
        # websocket fill event should supersede it once wired.)
        y_hit = bool(y_asks) and y_asks[0][0] <= pos["yes_bid_c"] and not pos.get("yes_filled")
        n_hit = bool(n_asks) and n_asks[0][0] <= pos["no_bid_c"] and not pos.get("no_filled")

        for side, hit in (("yes", y_hit), ("no", n_hit)):
            if not hit:
                continue
            pos[f"{side}_filled"] = True
            self._persist({"type": "twoleg_fill", "id": pos["id"],
                          "ts": datetime.now(timezone.utc).isoformat(),
                          "side": side, "price_c": pos[f"{side}_bid_c"],
                          "size": pos["size"]})
            self.on_log("→", f"[twoleg] {side.upper()} leg filled @ "
                             f"{pos[f'{side}_bid_c']:.0f}c ({pos['size']:.0f} sh)")

        if pos.get("yes_filled") and pos.get("no_filled"):
            self._persist({"type": "twoleg_completed", "id": pos["id"],
                          "ts": datetime.now(timezone.utc).isoformat(),
                          "how": "both_legs_filled",
                          "cost_c": pos["total_c"], "pnl_usd": pos["both_fill_usd"]})
            self.on_log("✓", f"[twoleg] COMPLETE SET both legs — paid {pos['total_c']:.0f}c, "
                             f"redeems 100c → +${pos['both_fill_usd']:.2f} risk-free")
            pos["completed"] = True
            return

        if pos.get("yes_filled") or pos.get("no_filled"):
            filled = "yes" if pos.get("yes_filled") else "no"
            asks = n_asks if filled == "yes" else y_asks
            self._auto_complete(filled, asks)
            return

        opened = datetime.fromisoformat(pos["ts"])
        age_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
        if age_min >= MAX_QUOTE_MIN:
            self._close("quote_expired_unfilled")

    def _auto_complete(self, filled_side, other_asks):
        """One leg filled and the other is still resting — we are directionally
        exposed RIGHT NOW. Buy the complement at market immediately.

        Because NO_ask = 100 - YES_bid structurally, this lands at roughly
        break-even rather than at the mercy of the weather. The only genuine
        costs are the taker fee and whatever the book moved between the fill
        and this call, which is exactly the latency a websocket fill feed
        would shrink.
        """
        pos = self.pos
        other = "no" if filled_side == "yes" else "yes"
        if not other_asks:
            self.on_log("!", f"[twoleg] {other.upper()} side has no ask to complete against "
                             f"— STILL NAKED, will retry next cycle")
            return
        ask_c = other_asks[0][0]
        size = pos["size"]
        paid_c = pos[f"{filled_side}_bid_c"]
        set_cost_c = paid_c + ask_c
        fee = _taker_fee_usd(size, ask_c)
        pnl = (100.0 - set_cost_c) / 100.0 * size - fee

        # Cancel our OWN still-resting bid on the completing side FIRST. We are
        # about to buy that side at market; if the resting bid below also fills
        # we would be over-completed (2x one side + 1x the other = net
        # directional again, the very thing this path removes). Cancel before
        # the market buy so the two can't race.
        cancel_limit(pos.get(f"{other}_order_id"), live=POLY_TWOLEG_LIVE)

        oid = place_limit(pos[f"{other}_token"], ask_c, size, side="BUY",
                          post_only=False, live=POLY_TWOLEG_LIVE)
        if POLY_TWOLEG_LIVE and not oid:
            self.on_log("!", f"[twoleg] auto-complete FAILED — {filled_side.upper()} leg is "
                             f"naked, retrying next cycle")
            return

        pos[f"{other}_filled"] = True
        pos["completed"] = True
        self._persist({"type": "twoleg_completed", "id": pos["id"],
                      "ts": datetime.now(timezone.utc).isoformat(),
                      "how": f"auto_complete_after_{filled_side}",
                      "filled_leg_c": paid_c, "completed_at_c": ask_c,
                      "cost_c": set_cost_c, "fee_usd": round(fee, 4),
                      "pnl_usd": round(pnl, 4), "order_id": oid})
        self.on_log("✓" if pnl >= 0 else "!",
                   f"[twoleg] AUTO-COMPLETED after {filled_side.upper()} fill — "
                   f"{paid_c:.0f}c + {ask_c:.0f}c = {set_cost_c:.0f}c for a $1.00 set, "
                   f"fee ${fee:.3f} → {pnl:+.3f} (directional risk closed)")

    def _close(self, reason):
        pos = self.pos
        # Cancel any leg still resting live before dropping the slot. On the
        # expiry path both legs are still on the book (a fill would have routed
        # to the complete/auto-complete branches, not here), and leaving them
        # resting would leak orders that stack up as duplicate quotes across
        # re-entries — the same failure mode that caused a real multi-order
        # loss in the arb bot. A completed/both-filled set has nothing resting,
        # so cancel only the legs that never filled.
        for side in ("yes", "no"):
            if not pos.get(f"{side}_filled"):
                cancel_limit(pos.get(f"{side}_order_id"), live=POLY_TWOLEG_LIVE)
        self._persist({"type": "twoleg_closed", "id": pos["id"],
                      "ts": datetime.now(timezone.utc).isoformat(), "reason": reason})
        self.on_log("→", f"[twoleg] closed ({reason})")
        self.pos = None
