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

LIVE fills are NEVER inferred from the book: every leg's state comes from the
CLOB's own order status (size_matched via get_order), because acting on a
false book inference would cancel a real bid and market-buy the other side —
manufacturing the naked position this design exists to prevent. A leg whose
status can't be read in a cycle is UNKNOWN and left alone until it can.

One position at a time, deliberately: the auto-complete path must never be
competing with itself for attention while a leg sits naked.
"""

import json
import logging
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds.poly_rewards import get_twoleg_plan, fetch_reward_markets, _fetch_book
from modules.poly_rewards_exec import PolyRewardsExec
from modules.poly_rewards_live import (place_limit, cancel_limit, order_status,
                                       cancel_token_orders)

log = logging.getLogger("modules.poly_rewards_twoleg")

TWOLEG_LOG = Path("poly_twoleg.jsonl")
POLY_TWOLEG_LIVE = os.getenv("POLY_TWOLEG_LIVE", "false").strip().lower() == "true"
# Fraction of the max spread to sit BELOW each midpoint. See get_twoleg_plan:
# 0.0 is at the mid (max reward, filled constantly), 1.0 is the band edge
# (zero reward). This is the knob the paper phase exists to calibrate.
BAND_FRACTION = float(os.getenv("POLY_TWOLEG_BAND_FRACTION", "0.5"))
# Give up and re-quote if neither leg has traded in this long.
MAX_QUOTE_MIN = float(os.getenv("POLY_TWOLEG_MAX_QUOTE_MIN", "90"))
# ── METAR-print quote-pulling ────────────────────────────────────────────────
# A locked temperature market only ever gets new information at its station's
# hourly METAR print (each station reports at a fixed minute; we infer it from
# the observation history). Between prints fair value cannot move and a resting
# order is near-safe; in the minutes around the print the whole book reprices
# and stale quotes get picked off. So: cancel both legs PULL_BEFORE_MIN minutes
# before the expected print, re-place RESUME_AFTER_MIN minutes after it (fresh
# band prices). Costs ~13 quoting-minutes per hour; removes the window where
# most fills on gated buckets actually happen. PULL_BEFORE_MIN must be >= the
# monitor's cycle period in minutes (default cycle 300s = 5min) or a cycle can
# miss the window entirely.
PULL_BEFORE_MIN = float(os.getenv("POLY_TWOLEG_PULL_BEFORE_MIN", "7"))
RESUME_AFTER_MIN = float(os.getenv("POLY_TWOLEG_RESUME_AFTER_MIN", "6"))
# ── reprice / abandon ────────────────────────────────────────────────────────
# Rewards score against the CURRENT midpoint, so when the mid drifts our
# resting quotes earn less (quadratically) and eventually nothing. Small drift
# -> follow it (cancel + re-place at fresh band prices). A LARGE move is not
# drift: it means new information invalidated the "this bucket is decided"
# thesis we entered on — chasing the mid down is catching a falling knife, so
# cancel everything and never re-enter this market (blacklist survives restart
# via ledger replay).
REPRICE_C = float(os.getenv("POLY_TWOLEG_REPRICE_C", "2"))
ABANDON_C = float(os.getenv("POLY_TWOLEG_ABANDON_C", "8"))
# ── market-consensus entry filter ────────────────────────────────────────────
# The METAR gate says the DATA thinks the bucket is decided; this requires the
# MARKET to agree — one side's mid must already be at/above this. Both paper
# losses on gated buckets (2026-08-04: Tokyo 30C quoted at 13:15 local with
# YES mid ~25c, fill+rescue -$1.13; a 13c fill completing at 108c, -$1.65)
# were mid-priced CONTESTED buckets, which rank highest on yield exactly
# because they're contested. A bucket the market prices 50/50 is not "decided"
# no matter what the plateau check says.
CONSENSUS_MIN_C = float(os.getenv("POLY_TWOLEG_CONSENSUS_MIN_C", "85"))
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

    def __init__(self, on_log=None, exec_=None):
        self.on_log = on_log or (lambda i, m: None)
        # Own PolyRewardsExec instance, NOT shared with the single-leg pipeline
        # (mirrors PolyRewardsPaperSim's default) — check()'s dedup set marks a
        # (condition_id, date) as logged the first time it's seen in a cycle,
        # so a shared instance called twice in one cycle would return nothing
        # on the second call and starve this module of candidates.
        self.exec = exec_ or PolyRewardsExec(on_log=self.on_log)
        self._print_minute = {}   # station -> inferred METAR report minute (cached)
        self.pos = self._load_open()
        # Markets abandoned because the mid moved through ABANDON_C are never
        # re-entered — rebuilt from the ledger so a restart can't forget one.
        # Weather markets are daily, so condition_ids never recur; a permanent
        # set is both safe and simpler than expiring entries.
        self._blacklist = self._load_blacklist()

    def _load_blacklist(self):
        if not TWOLEG_LOG.exists():
            return set()
        placed_cid = {}
        black = set()
        for line in open(TWOLEG_LOG):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") == "twoleg_placed":
                placed_cid[rec.get("id")] = rec.get("condition_id")
            elif (rec.get("type") == "twoleg_closed"
                  and str(rec.get("reason", "")).startswith("thesis_broke")):
                cid = placed_cid.get(rec.get("id"))
                if cid:
                    black.add(cid)
        return black

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
                    if rec.get("size_matched") is not None:
                        pos[f"{rec['side']}_matched"] = rec["size_matched"]
                elif t == "twoleg_pulled":
                    pos["pulled"] = True
                elif t == "twoleg_requoted":
                    pos["pulled"] = False
                    for k in ("yes_bid_c", "no_bid_c", "yes_mid_c", "no_mid_c",
                              "total_c", "capital_usd", "both_fill_usd",
                              "yes_order_id", "no_order_id"):
                        if k in rec:
                            pos[k] = rec[k]
                elif t == "twoleg_completed":
                    pos["completed"] = True
                elif t == "twoleg_closed":
                    pos = None
        return pos

    # ── main loop ────────────────────────────────────────────────────────────
    def cycle(self, scan_results=None):
        # A position replayed from the ledger carries the MODE it was opened
        # under. Managing it under the OTHER mode is never safe, in either
        # direction:
        #  - paper position in a live process: its "fills" are book inference
        #    with no real orders behind them — the auto-complete would spend
        #    REAL money completing a phantom fill. No real orders exist, so
        #    dropping the slot costs nothing.
        #  - live position in a paper process: real orders/shares may exist,
        #    and every management action here would be a paper no-op that only
        #    PRETENDS to cancel/complete. Refuse to touch it (and to open
        #    anything new) until the operator re-arms or cancels by hand.
        mode = "live" if POLY_TWOLEG_LIVE else "paper"
        if self.pos is not None and (self.pos.get("mode") or "paper") != mode:
            if self.pos.get("mode") == "paper":
                self.on_log("!", "[twoleg] dropping PAPER position carried into a LIVE "
                                 "process — no real orders behind it, nothing to unwind")
                self._close("mode_mismatch_paper_dropped")
            else:
                self.on_log("✗", f"[twoleg] LIVE position {self.pos.get('id')} found but "
                                 f"POLY_TWOLEG_LIVE is off — real orders may be resting; "
                                 f"refusing to paper-manage them. Re-arm live or cancel "
                                 f"manually (yes={self.pos.get('yes_order_id')}, "
                                 f"no={self.pos.get('no_order_id')})")
            return
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
        # NEAR-LOCK gate — the check every OTHER execution path in this
        # project runs before touching a bucket (poly_rewards_exec.py,
        # autoexec, papersim) and this module skipped entirely until now.
        # Traced live 2026-08-03: the two worst auto-complete losses in
        # paper (-$2.79 Atlanta high, -$4.42 NYC low) both fired on buckets
        # that were still LIVE — the temperature was actively moving. A fill
        # on a moving bucket usually means fresh information just arrived,
        # and NO_ask stops tracking 100-YES_bid exactly when that happens —
        # the auto-complete pnl model assumes a calm book, not one that's
        # actively repricing. check() also verifies a genuine two-sided gap
        # exists on the REAL book (see its own docstring), which
        # yield_per_dollar_per_day alone does not.
        cands = self.exec.check(scan_results)
        cands = [c for c in cands if c["condition_id"] not in self._blacklist]
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
            if max(plan["yes_mid_c"], plan["no_mid_c"]) < CONSENSUS_MIN_C:
                self.on_log("→", f"[twoleg] {r.get('city')} {r.get('kind')} passed the "
                                 f"METAR gate but the market prices it contested "
                                 f"(mids {plan['yes_mid_c']:.0f}/{plan['no_mid_c']:.0f}c, "
                                 f"need one >= {CONSENSUS_MIN_C:.0f}c) — skipping")
                continue
            self._enter(plan, r)
            return

    def _enter(self, plan, cand=None):
        pid = f"{plan['condition_id']}-{int(time.time())}"
        yes_oid = place_limit(plan["yes_token"], plan["yes_bid_c"], plan["size"],
                              side="BUY", post_only=True, live=POLY_TWOLEG_LIVE)
        no_oid = place_limit(plan["no_token"], plan["no_bid_c"], plan["size"],
                             side="BUY", post_only=True, live=POLY_TWOLEG_LIVE)
        if POLY_TWOLEG_LIVE and not (yes_oid and no_oid):
            # A half-placed pair is the exact naked-leg state this design exists
            # to avoid — and a leg whose place call TIMED OUT may have been
            # accepted server-side with no order_id known to us, which a per-id
            # cancel can never reach. Sweep-cancel every order of ours on BOTH
            # tokens: that clears the survivor (id known) and any id-less ghost
            # in one confirmable call. Safe to over-sweep — this module runs
            # one position at a time and nothing else rests maker orders on
            # these tokens (the weather bot is FOK/FAK only).
            ok_y = cancel_token_orders(plan["yes_token"], live=POLY_TWOLEG_LIVE)
            ok_n = cancel_token_orders(plan["no_token"], live=POLY_TWOLEG_LIVE)
            if ok_y and ok_n:
                self.on_log("!", "[twoleg] one leg failed to place — swept both tokens "
                                 "clean, no naked exposure left")
            else:
                # Even the sweep couldn't confirm a clean book. Record what we
                # know so it is never silently lost — manual cancel required.
                self._persist({"type": "twoleg_orphan",
                              "ts": datetime.now(timezone.utc).isoformat(),
                              "yes_order_id": yes_oid, "no_order_id": no_oid,
                              "yes_token": plan["yes_token"], "no_token": plan["no_token"],
                              "note": "half-placed pair AND token sweep unconfirmed — MANUAL CANCEL REQUIRED"})
                self.on_log("✗", "[twoleg] ORPHAN RISK — half-placed pair and the token sweep "
                                 "could not be confirmed; recorded for manual cancel")
            return

        cand = cand or {}
        rec = {
            "type": "twoleg_placed", "id": pid,
            "ts": datetime.now(timezone.utc).isoformat(),
            "condition_id": plan["condition_id"], "question": plan["question"],
            # station drives the METAR-print pull windows; the rest is context
            # for reports. Older ledger rows lack these — every reader treats
            # them as optional (missing station = fail-open, no pulling).
            "station": cand.get("station"), "city": cand.get("city"),
            "kind": cand.get("kind"), "date": cand.get("date"),
            "yes_token": plan["yes_token"], "no_token": plan["no_token"],
            "yes_bid_c": plan["yes_bid_c"], "no_bid_c": plan["no_bid_c"],
            "yes_mid_c": plan["yes_mid_c"], "no_mid_c": plan["no_mid_c"],
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
        in_window = self._print_window(pos.get("station"))

        # Pulled state: nothing is resting, so there is nothing to fill-check
        # (paper book inference would otherwise "fill" quotes that don't
        # exist). Either the print window has passed and we re-quote at fresh
        # prices, or we sit tight. Expiry still applies — a position that ages
        # out while flat just closes.
        if pos.get("pulled"):
            if self._age_min() >= MAX_QUOTE_MIN:
                self._close("quote_expired_unfilled")
            elif in_window is not True:
                self._requote("post_print", cancel_first=False)
            return

        try:
            y_bids, y_asks = _fetch_book(pos["yes_token"])
            n_bids, n_asks = _fetch_book(pos["no_token"])
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[twoleg] book fetch failed: {e}")
            return

        # Fill detection differs by mode, and the difference is load-bearing.
        #
        # LIVE: ask the CLOB for the order's real size_matched. Book inference
        # is NOT acceptable here — "best ask <= our bid" can be true while our
        # order sat queued behind others and never traded, and acting on that
        # false positive would cancel our real bid and market-buy the OTHER
        # side: a naked directional position bought at the expensive end,
        # manufactured by the very mechanism meant to prevent one. A leg whose
        # status can't be read this cycle is UNKNOWN — do nothing to it.
        #
        # PAPER: nothing is resting anywhere, so the book inference (the
        # opposing best ask trading down to our price) is the only signal
        # there is, and optimistic-but-directionally-right is fine for what
        # paper is measuring.
        for side, asks in (("yes", y_asks), ("no", n_asks)):
            if pos.get(f"{side}_filled"):
                continue
            matched = 0.0
            if POLY_TWOLEG_LIVE and pos.get(f"{side}_order_id"):
                st = order_status(pos[f"{side}_order_id"], live=True)
                if st is None:
                    continue  # unknown this cycle — never act on a guess, live
                matched = st["size_matched"]
                if matched <= 0:
                    continue
            else:
                if not (bool(asks) and asks[0][0] <= pos[f"{side}_bid_c"]):
                    continue
                matched = pos["size"]
            pos[f"{side}_filled"] = True
            pos[f"{side}_matched"] = matched
            self._persist({"type": "twoleg_fill", "id": pos["id"],
                          "ts": datetime.now(timezone.utc).isoformat(),
                          "side": side, "price_c": pos[f"{side}_bid_c"],
                          "size": pos["size"], "size_matched": matched})
            self.on_log("→", f"[twoleg] {side.upper()} leg filled @ "
                             f"{pos[f'{side}_bid_c']:.0f}c ({matched:.0f}/{pos['size']:.0f} sh)")

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

        # ── neither leg filled: the resting-quote management stack ──────────
        # 1) METAR print approaching — get flat before the information event.
        if in_window is True:
            self._pull_quotes()
            return

        # 2) Mid drift. Small: follow it (we stop scoring if we don't). Large:
        #    the entry thesis (bucket already decided) just got invalidated by
        #    real data — abandon and never re-enter this market.
        cur_mid = ((y_bids[0][0] + y_asks[0][0]) / 2.0
                   if y_bids and y_asks else None)
        entry_mid = pos.get("yes_mid_c")
        if cur_mid is not None and entry_mid is not None:
            drift = abs(cur_mid - entry_mid)
            if drift >= ABANDON_C:
                self._blacklist.add(pos["condition_id"])
                self.on_log("✗", f"[twoleg] THESIS BROKE — mid moved "
                                 f"{entry_mid:.1f}c -> {cur_mid:.1f}c "
                                 f"(>{ABANDON_C:.0f}c); abandoning and "
                                 f"blacklisting this market, not chasing")
                self._close("thesis_broke_mid_moved")
                return
            if drift >= REPRICE_C:
                self._requote("mid_drift", cancel_first=True)
                return

        if self._age_min() >= MAX_QUOTE_MIN:
            self._close("quote_expired_unfilled")

    def _age_min(self):
        opened = datetime.fromisoformat(self.pos["ts"])
        return (datetime.now(timezone.utc) - opened).total_seconds() / 60.0

    # ── METAR print windows ──────────────────────────────────────────────────
    def _print_minutes_of(self, station):
        """The SET of minutes-of-hour this station's METAR reports land at,
        inferred from recent observation timestamps. A set, not a single mode,
        for two reasons found by probing real stations (2026-08-04): some
        report more than once an hour (WMKK prints :00 AND :30 — a
        single-minute model sits flat for one print and quotes straight
        through the other), and aviationweather's reportTime is sometimes
        rounded to the hour (KSEA shows :00 for its real :53 obs — harmless,
        since a window around :00 still covers a :53 print's publication lag,
        but it means the inferred minute is a report label, not gospel).

        Minutes seen >=2 times in the last 12 obs are kept (drops one-off
        SPECIs); if nothing repeats, every seen minute is kept. Cached once
        >=3 obs back the inference; with 1-2 obs (station just past local
        midnight — today_obs resets by local date) the result is provisional
        and NOT cached, so it re-infers as history accumulates."""
        if station in self._print_minute:
            return self._print_minute[station]
        tz = self.exec._climo_tz.get(station)
        if not tz:
            return None   # not in the climo table — can't poll it cleanly
        try:
            self.exec.metar.set_stations({station: tz})
            self.exec.metar.poll()
            snap = self.exec.metar.snapshot().get(station) or {}
            obs = snap.get("today_obs") or []
            if not obs:
                return None
            minutes = [datetime.fromisoformat(ts).astimezone(timezone.utc).minute
                       for ts, _t in obs[-12:]]
            counts = {m: minutes.count(m) for m in set(minutes)}
            repeated = {m for m, n in counts.items() if n >= 2}
            result = repeated or set(counts)
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[twoleg] print-minute inference failed for {station}: {e}")
            return None
        if len(obs) >= 3:
            self._print_minute[station] = result
        return result

    def _print_window(self, station):
        """True = inside the pull window around ANY of the station's print
        minutes; False = safely between prints; None = unknown (no station on
        this position, or minutes not inferable) — treated as fail-OPEN, i.e.
        keep quoting: the NEAR-LOCK gate is still the primary risk control and
        a position must not be starved of rewards by a metadata gap."""
        if not station:
            return None
        minutes = self._print_minutes_of(station)
        if not minutes:
            return None
        now = datetime.now(timezone.utc)
        now_min = now.minute + now.second / 60.0
        for m in minutes:
            delta = ((now_min - m + 30.0) % 60.0) - 30.0   # signed, [-30, 30)
            if -PULL_BEFORE_MIN <= delta <= RESUME_AFTER_MIN:
                return True
        return False

    def _cancel_and_verify(self, side):
        """Cancel one resting leg and find out what really happened to it.
        Returns 'cancelled' (gone, nothing filled — also the paper case),
        'filled' (it matched before the cancel landed; the fill is recorded
        and the caller must stop and let the rescue path run), or 'failed'
        (cancel unconfirmed — order may still be resting, caller must not
        proceed as if flat)."""
        pos = self.pos
        oid = pos.get(f"{side}_order_id")
        if not POLY_TWOLEG_LIVE or not oid:
            return "cancelled"
        if not cancel_limit(oid, live=True):
            return "failed"
        st = order_status(oid, live=True)
        if st and st["size_matched"] > 0:
            pos[f"{side}_filled"] = True
            pos[f"{side}_matched"] = st["size_matched"]
            self._persist({"type": "twoleg_fill", "id": pos["id"],
                          "ts": datetime.now(timezone.utc).isoformat(),
                          "side": side, "price_c": pos[f"{side}_bid_c"],
                          "size": pos["size"], "size_matched": st["size_matched"]})
            self.on_log("→", f"[twoleg] {side.upper()} leg had FILLED before the "
                             f"cancel landed ({st['size_matched']:.0f} sh) — "
                             f"switching to the fill-handling path")
            return "filled"
        return "cancelled"

    def _pull_quotes(self):
        """Print window opening — get flat before the new observation prints.
        Any leg discovered filled during the cancel routes to the rescue path
        next cycle instead."""
        pos = self.pos
        for side in ("yes", "no"):
            res = self._cancel_and_verify(side)
            if res == "filled":
                return   # fill recorded; _check_legs handles it next cycle
            if res == "failed":
                self.on_log("!", f"[twoleg] pull cancel unconfirmed on {side.upper()} — "
                                 f"legs may still be resting, retrying next cycle")
                return
        pos["pulled"] = True
        minutes = sorted(self._print_minute.get(pos.get("station")) or [])
        self._persist({"type": "twoleg_pulled", "id": pos["id"],
                      "ts": datetime.now(timezone.utc).isoformat(),
                      "print_minutes": minutes})
        self.on_log("→", f"[twoleg] PULLED quotes ahead of {pos.get('station')}'s "
                         f"METAR print (minutes {minutes or '?'}) — flat through "
                         f"the information window")

    def _requote(self, reason, cancel_first):
        """Cancel whatever still rests (unless already flat from a pull) and
        re-place both legs at FRESH band prices. Used after a print window
        passes and when the mid has drifted enough that our quotes stopped
        scoring meaningfully."""
        pos = self.pos
        if cancel_first:
            for side in ("yes", "no"):
                res = self._cancel_and_verify(side)
                if res == "filled":
                    return   # rescue path next cycle
                if res == "failed":
                    self.on_log("!", f"[twoleg] reprice cancel unconfirmed on "
                                     f"{side.upper()} — leaving quotes as they "
                                     f"are, retrying next cycle")
                    return

        plan = get_twoleg_plan(pos["condition_id"], band_fraction=BAND_FRACTION)
        if not plan:
            self._close("requote_market_gone")
            return
        yes_oid = place_limit(plan["yes_token"], plan["yes_bid_c"], plan["size"],
                              side="BUY", post_only=True, live=POLY_TWOLEG_LIVE)
        no_oid = place_limit(plan["no_token"], plan["no_bid_c"], plan["size"],
                             side="BUY", post_only=True, live=POLY_TWOLEG_LIVE)
        if POLY_TWOLEG_LIVE and not (yes_oid and no_oid):
            # Same half-placed-pair discipline as _enter: sweep both tokens
            # (covers a timed-out place with no known id) and free the slot.
            cancel_token_orders(plan["yes_token"], live=True)
            cancel_token_orders(plan["no_token"], live=True)
            self._close("requote_replace_failed")
            return
        pos.update({"yes_bid_c": plan["yes_bid_c"], "no_bid_c": plan["no_bid_c"],
                    "yes_mid_c": plan["yes_mid_c"], "no_mid_c": plan["no_mid_c"],
                    "total_c": plan["total_c"], "capital_usd": plan["capital_usd"],
                    "both_fill_usd": plan["both_fill_usd"],
                    "yes_order_id": yes_oid, "no_order_id": no_oid,
                    "pulled": False})
        self._persist({"type": "twoleg_requoted", "id": pos["id"],
                      "ts": datetime.now(timezone.utc).isoformat(), "reason": reason,
                      "yes_bid_c": plan["yes_bid_c"], "no_bid_c": plan["no_bid_c"],
                      "yes_mid_c": plan["yes_mid_c"], "no_mid_c": plan["no_mid_c"],
                      "total_c": plan["total_c"], "capital_usd": plan["capital_usd"],
                      "both_fill_usd": plan["both_fill_usd"],
                      "yes_order_id": yes_oid, "no_order_id": no_oid})
        self.on_log("→", f"[twoleg] REQUOTED ({reason}) — YES {plan['yes_bid_c']:.0f}c + "
                         f"NO {plan['no_bid_c']:.0f}c = {plan['total_c']:.0f}c "
                         f"(mid now {plan['yes_mid_c']:.1f}c)")

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
        # Complete only what actually FILLED. A partial fill (live: matched <
        # size) hedged at full size would over-buy the complement — the excess
        # is a fresh directional position on the other side.
        size = pos.get(f"{filled_side}_matched") or pos["size"]
        paid_c = pos[f"{filled_side}_bid_c"]
        set_cost_c = paid_c + ask_c
        fee = _taker_fee_usd(size, ask_c)
        pnl = (100.0 - set_cost_c) / 100.0 * size - fee

        # Cancel BOTH resting orders BEFORE the market buy — the sibling bid on
        # the completing side (if it also filled we'd be over-completed: 2x one
        # side + 1x the other = net directional again), and the filled leg's
        # own remainder if the fill was partial. A cancel of an already-fully-
        # matched order comes back "already matched" and counts as success.
        if not cancel_limit(pos.get(f"{other}_order_id"), live=POLY_TWOLEG_LIVE):
            # Could NOT confirm the sibling is off the book. Buying now could
            # race its own fill into an over-complete — do nothing this cycle
            # and retry; the sibling filling in the meantime is caught below.
            self.on_log("!", f"[twoleg] sibling {other.upper()} cancel unconfirmed — "
                             f"NOT market-buying into a possible race, retrying next cycle")
            return
        cancel_limit(pos.get(f"{filled_side}_order_id"), live=POLY_TWOLEG_LIVE)

        if POLY_TWOLEG_LIVE and pos.get(f"{other}_order_id"):
            # The cancel "succeeding" can mean the sibling was ALREADY GONE —
            # including gone-because-it-filled in the gap since our status
            # check. Buying at market on top of that would over-complete, so
            # re-read the sibling's real matched size now that it can no
            # longer change (the order is off the book either way).
            st = order_status(pos[f"{other}_order_id"], live=True)
            if st is None:
                self.on_log("!", f"[twoleg] sibling {other.upper()} state UNKNOWN after cancel "
                                 f"— not buying blind, retrying next cycle")
                return
            if st["size_matched"] > 0:
                pos[f"{other}_filled"] = True
                pos[f"{other}_matched"] = st["size_matched"]
                pos["completed"] = True
                self._persist({"type": "twoleg_completed", "id": pos["id"],
                              "ts": datetime.now(timezone.utc).isoformat(),
                              "how": "both_legs_filled_race",
                              "cost_c": pos["total_c"],
                              "yes_matched": pos.get("yes_matched"),
                              "no_matched": pos.get("no_matched"),
                              "pnl_usd": pos["both_fill_usd"]})
                self.on_log("✓", f"[twoleg] sibling {other.upper()} had ALREADY filled — "
                                 f"complete set, no market buy needed")
                return

        # Cross with a slippage buffer: a limit at the SNAPSHOTTED ask can miss
        # a moved book and quietly REST instead of filling — which would mark
        # this position complete while actually adding a fourth live order.
        # +3c crosses through small moves; pnl above is still computed at the
        # snapshot ask (the expected cost), and the fill lands at the real
        # best ask regardless of the buffered limit.
        cross_c = min(ask_c + 3, 99)
        oid = place_limit(pos[f"{other}_token"], cross_c, size, side="BUY",
                          post_only=False, live=POLY_TWOLEG_LIVE)
        if POLY_TWOLEG_LIVE and not oid:
            self.on_log("!", f"[twoleg] auto-complete FAILED — {filled_side.upper()} leg is "
                             f"naked, retrying next cycle")
            return
        # Track the completion order id in the sibling's slot (the original
        # sibling is confirmed off the book above), so if this buy only
        # partially crossed, _close's unconditional sweep cancels the rest.
        pos[f"{other}_order_id"] = oid

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
        # Cancel BOTH tracked orders, unconditionally, before dropping the
        # slot. Leaked resting orders stack up as duplicate quotes across
        # re-entries — the failure mode behind the arb bot's real multi-order
        # loss. Unconditional is deliberate: a partially-filled leg still has
        # remainder resting even though it is marked filled, and the
        # auto-complete path stores its (possibly partially-crossed) buy in
        # the sibling's slot. Cancelling an order that is already fully
        # matched/cancelled reports "already gone" and counts as success, so
        # over-cancelling costs nothing.
        for side in ("yes", "no"):
            cancel_limit(pos.get(f"{side}_order_id"), live=POLY_TWOLEG_LIVE)
        self._persist({"type": "twoleg_closed", "id": pos["id"],
                      "ts": datetime.now(timezone.utc).isoformat(), "reason": reason})
        self.on_log("→", f"[twoleg] closed ({reason})")
        self.pos = None
