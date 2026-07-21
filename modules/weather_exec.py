"""
modules/weather_exec.py — forward-test executor for the NEAR-LOCK weather engine.

PAPER by default: fills ENTER signals at the quoted ask, persists every event
to weather_positions.jsonl (rehydrated on restart), and settles positions from
the market's own resolution (Gamma umaResolutionStatus == "resolved") — never
from our METAR read. That way the forward test also validates that our
observation feed matches the actual settlement source.

One position per event (city+date): the near-lock trade buys the single
highest-probability bucket, not a ladder.

LIVE mode exists but is double-gated (WEATHER_LIVE=true env AND set_mode) and
routes through polymarket.PolyClient.place_fok — same pattern as
copytrade_exec. Do not arm until the paper forward-test is calibrated
(target: n ≥ 100 settlements, win rate within a few points of model p).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from feeds.poly_weather import taker_fee_c, fetch_book_asks, fetch_book_bid_c, vwap_for_size

log = logging.getLogger("modules.weather_exec")

POS_LOG = Path(os.getenv("WEATHER_EXEC_LOG", "weather_positions.jsonl"))
# Every live FOK miss is recorded here with the book at the miss instant, so we
# can later tell "market ran away for a reason" (big gap, bucket often loses)
# from "we under-priced by a cent and missed a still-good trade" (small gap).
# The second kind is the argument for loosening the FOK; the first is not.
MISS_LOG = Path(os.getenv("WEATHER_MISS_LOG", "weather_misses.jsonl"))
ENV_ARMED = os.getenv("WEATHER_LIVE", "false").strip().lower() == "true"
# Boot straight into LIVE instead of waiting for the dashboard toggle. Only for
# UNATTENDED hosts: a crash-restart or reboot otherwise comes back PAPER while
# live positions are still open, and a paper executor refuses to close them
# (see _exit_position) — so they'd sit with no dead-exit and no take-profit.
# Still gated on WEATHER_LIVE; this cannot arm live trading on its own.
START_LIVE = os.getenv("WEATHER_START_LIVE", "false").strip().lower() == "true"
STAKE_USD = float(os.getenv("WEATHER_STAKE_USD", "5"))
MAX_OPEN = int(os.getenv("WEATHER_MAX_OPEN", "10"))
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
# USDC balance immediately BEFORE live trading began. Set this to capture P&L
# from the very first live trade; otherwise the baseline is snapped on first read
# (which would silently exclude any profit already banked).
BASELINE_ENV = os.getenv("WEATHER_LIVE_BASELINE_USD", "").strip()
ACCT_REFRESH_SEC = 60
# Re-run the entry reasoning against open positions on this cadence. Entry is a
# snapshot; the world moves. Warnings fire IMMEDIATELY regardless of cadence.
RECHECK_SEC = float(os.getenv("WEATHER_RECHECK_SEC", "1200"))     # 20 min
# Close once the market has converged this far. The edge is the convergence; the
# last few cents carry the entire downside. 0 disables.
TAKE_PROFIT_BID_C = float(os.getenv("WEATHER_TAKE_PROFIT_BID_C", "90"))
# mirrored from the engine's gates (read from env, not imported, to stay decoupled)
P_MIN = float(os.getenv("WEATHER_P_MIN", "0.92"))
MIN_MAX_AGE_MIN = float(os.getenv("WEATHER_MIN_MAX_AGE_MIN", "120"))


class WeatherExecutor:
    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.mode = "live" if (ENV_ARMED and START_LIVE) else "paper"
        self.stake_usd = STAKE_USD
        self.open = []            # position dicts
        self.closed = []
        # realized_pnl is NET of taker fees; realized_gross and fees_paid are
        # tracked alongside so the forward test can report both.
        self.session = {"opened": 0, "settled": 0, "wins": 0,
                        "staked_usd": 0.0, "realized_pnl": 0.0,
                        "realized_gross": 0.0, "fees_paid": 0.0}
        # REAL on-chain account tracking (live only) — the ledger's P&L is
        # modeled; USDC is truth. See _refresh_account.
        self._live_baseline = float(BASELINE_ENV) if BASELINE_ENV else None
        self._acct = None
        self._acct_ts = 0.0
        self._lock = threading.Lock()
        # live FOK misses, seeded from the log so the count survives restarts
        self._misses = self._count_misses()
        self._rehydrate()

    def _count_misses(self):
        try:
            with open(MISS_LOG) as f:
                return sum(1 for line in f if line.strip())
        except FileNotFoundError:
            return 0
        except Exception:  # noqa: BLE001
            return 0

    # ── persistence ──────────────────────────────────────────────────────────
    def _rehydrate(self):
        if not POS_LOG.exists():
            return
        by_key = {}
        try:
            for line in POS_LOG.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("type") == "baseline":
                    # env override wins; else recover the persisted baseline
                    if not BASELINE_ENV:
                        self._live_baseline = rec.get("live_baseline_usd")
                elif rec.get("type") == "open":
                    by_key[rec["key"]] = rec
                elif rec.get("type") == "settle" and rec.get("key") in by_key:
                    pos = by_key.pop(rec["key"])
                    # backfill net/gross/fee for pre-fee-accounting settle records
                    gross = rec.get("gross_pnl")
                    if gross is None:
                        gross = rec.get("pnl_usd", 0.0)   # old records stored gross here
                    fee = rec.get("fee_usd")
                    if fee is None:
                        fee = round(pos.get("shares", 0) * taker_fee_c(pos.get("entry_c")) / 100.0, 2)
                    net = round(gross - fee, 2)
                    self.closed.append({**pos, **rec, "gross_pnl": gross,
                                        "fee_usd": fee, "pnl_usd": net})
                    self.session["settled"] += 1
                    self.session["wins"] += 1 if rec.get("won") else 0
                    # keep the calibration counters correct across restarts:
                    # early exits don't reveal if the bucket was right; only
                    # positions HELD to resolution count toward win_rate_held.
                    if rec.get("closed_early"):
                        self.session["early_exits"] = self.session.get("early_exits", 0) + 1
                    elif rec.get("won"):
                        self.session["wins_held"] = self.session.get("wins_held", 0) + 1
                    self.session["realized_pnl"] += net
                    self.session["realized_gross"] += gross
                    self.session["fees_paid"] += fee
        except Exception as e:  # noqa: BLE001
            self.on_log("✗", f"[weatherexec] rehydrate failed: {e}")
            return
        self.open = list(by_key.values())
        self.session["opened"] = self.session["settled"] + len(self.open)
        self.session["staked_usd"] = round(
            sum(p.get("cost_usd", 0.0) for p in self.open), 2)
        self.closed = self.closed[-200:]
        if self.open or self.closed:
            self.on_log("→", f"[weatherexec] recovered {len(self.open)} open / "
                             f"{self.session['settled']} settled from {POS_LOG}")

    def _persist(self, rec):
        try:
            with open(POS_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # noqa: BLE001
            log.warning("persist failed: %s", e)

    # ── mode control ─────────────────────────────────────────────────────────
    def set_mode(self, mode):
        mode = (mode or "paper").lower()
        if mode == "live" and not ENV_ARMED:
            self.on_log("!", "[weatherexec] LIVE requested but WEATHER_LIVE!=true — staying PAPER")
            mode = "paper"
        if mode != self.mode:
            self.mode = mode
            self.on_log("◆", f"[weatherexec] mode → {mode.upper()}")

    @property
    def is_live(self):
        return self.mode == "live" and ENV_ARMED

    # ── entries (called by WeatherEngine.refresh) ────────────────────────────
    def on_refresh(self, rows):
        self._mark_open(rows)           # mark positions to market before anything else
        self._recheck_open(rows)        # is the thesis we bought on still true?
        self._close_dead(rows)          # bail out of provably-lost buckets
        self._take_profit(rows)         # and out of ones the market already agrees with
        for row in rows:
            entry = row.get("entry")
            if not entry:
                continue
            self._consider(entry)

    # ── mark to market ───────────────────────────────────────────────────────
    def _mark_open(self, rows):
        """Stamp each open position with the current BID — what it's actually
        worth right now, not what we paid.

        Valuing open positions at cost silently reports a dead position as if it
        still held its purchase value: with Miami worthless the dashboard showed
        real P&L +$1.55 when the truth was -$6.50. Equity must mark to market.
        """
        by_key = {f"{r['city']}|{r['date']}|{r['kind']}": r for r in rows}
        with self._lock:
            for pos in self.open:
                row = by_key.get(pos["key"])
                if not row:
                    continue
                b = next((x for x in row.get("buckets", [])
                          if x.get("label") == pos.get("label")), None)
                if b is not None and b.get("bid_c") is not None:
                    pos["mark_c"] = b["bid_c"]

    # ── position health re-check ─────────────────────────────────────────────
    def _recheck_open(self, rows):
        """Re-run the ENTRY reasoning (thermometer -> lock -> probability) against
        every open position.

        Entry is a snapshot and the world moves. BA and Miami both went from a
        confident buy to worthless inside an hour as the temperature resumed
        climbing — and by the time they were arithmetically dead, every bid had
        already gone. Waiting for the dead-exit is waiting too long: it fires
        when the position is provably lost, which is exactly when nobody will
        buy it. This re-reads the thermometer each refresh so a broken thesis is
        visible while there is still a bid to sell into.

        Health is logged on RECHECK_SEC cadence, but a BREAK warns immediately.
        """
        by_key = {f"{r['city']}|{r['date']}|{r['kind']}": r for r in rows}
        now = time.time()
        with self._lock:
            open_pos = list(self.open)
        for pos in open_pos:
            row = by_key.get(pos["key"])
            if not row:
                continue
            kind = pos.get("kind", "high")
            ext, age = row.get("ext_c"), row.get("ext_age_min")
            bucket = next((b for b in row.get("buckets", [])
                           if b.get("label") == pos.get("label")), None)
            p_now = (bucket or {}).get("p")
            lo, hi = pos.get("lo"), pos.get("hi")
            # how many degrees of room before the bucket dies
            headroom = None
            if ext is not None:
                if kind == "high" and hi is not None:
                    headroom = hi - ext
                elif kind == "low" and lo is not None:
                    headroom = ext - lo
            p_entry = pos.get("model_p")
            locked = (age is not None and age >= MIN_MAX_AGE_MIN)
            reasons = []
            if p_now is not None and p_now < P_MIN:
                reasons.append(f"confidence {p_now:.2f} < entry bar {P_MIN}")
            if not locked and age is not None:
                reasons.append(f"lock BROKEN — new extreme {age:.0f}min ago, moving again")
            # headroom == 0 is NORMAL for an exact-value bucket (lo==hi): sitting
            # on your number is how you win it (Manila did exactly that). Only a
            # NEGATIVE headroom means the extreme has passed us and it's dead.
            if headroom is not None and headroom < 0:
                reasons.append(f"extreme passed the bucket ({headroom:+.0f}°) — dead")
            health = {
                "p_now": round(p_now, 4) if p_now is not None else None,
                "p_entry": p_entry,
                "p_delta": (round(p_now - p_entry, 4)
                            if (p_now is not None and p_entry is not None) else None),
                "ext_now": ext, "headroom": headroom,
                "age_min": round(age, 1) if age is not None else None,
                "locked": locked, "mark_c": pos.get("mark_c"),
                "breaks": reasons, "checked": now,
            }
            pos["health"] = health
            broke = bool(reasons)
            was_broken = pos.get("_broken", False)
            due = (now - pos.get("_recheck_ts", 0)) >= RECHECK_SEC
            if broke and not was_broken:
                # fire the moment it breaks — a bid may still exist right now
                mk = pos.get("mark_c")
                self.on_log("!", f"[weatherexec] ⚠ THESIS BREAK {pos['city']} {pos['label']}: "
                                 f"{'; '.join(reasons)} | bid {mk if mk is not None else '?'}c "
                                 f"vs entry {pos['entry_c']:.0f}c — exit window is NOW, "
                                 f"dead-exit will be too late")
                pos["_recheck_ts"] = now
            elif due:
                self.on_log("→", f"[weatherexec] recheck {pos['city']} {pos['label']}: "
                                 f"max {ext}° headroom {headroom if headroom is not None else '?'}° "
                                 f"| p {p_entry}→{health['p_now']} | "
                                 f"{'locked' if locked else 'UNLOCKED'} {health['age_min']}min "
                                 f"| bid {pos.get('mark_c')}c vs entry {pos['entry_c']:.0f}c")
                pos["_recheck_ts"] = now
            pos["_broken"] = broke

    # ── dead-position exit ───────────────────────────────────────────────────
    def _close_dead(self, rows):
        """Exit buckets that CANNOT win any more.

        A daily extreme is monotonic: the max only rises, the min only falls.
        So once the observed max exceeds a high-bucket's ceiling (or the observed
        min drops below a low-bucket's floor), that bucket is arithmetically dead
        — it will settle 0. Riding it to settlement burns the slot and forfeits
        whatever bid is still standing. (Live BA 23°C died this way: 24°C printed
        and the bids vanished within minutes. Salvage beats hope.)
        """
        by_key = {f"{r['city']}|{r['date']}|{r['kind']}": r for r in rows}
        with self._lock:
            open_pos = list(self.open)
        for pos in open_pos:
            row = by_key.get(pos["key"])
            if not row or row.get("ext_c") is None:
                continue
            ext, lo, hi = row["ext_c"], pos.get("lo"), pos.get("hi")
            kind = pos.get("kind", "high")
            if kind == "high" and hi is not None and ext > hi:
                self._exit_dead(pos, row, f"max {ext:.0f}° > bucket ceiling {hi}°")
            elif kind == "low" and lo is not None and ext < lo:
                self._exit_dead(pos, row, f"min {ext:.0f}° < bucket floor {lo}°")

    # ── take profit once the market has converged ────────────────────────────
    def _take_profit(self, rows):
        """Close positions the market has already repriced to (near) certainty.

        The edge is in the CONVERGENCE, not the settlement. We buy a lagging
        bucket and get paid when the market catches up — after that the trade is
        over, but holding to settlement silently swaps a good asymmetry for a
        terrible one. Istanbul: bought 82c, market moved to 91c (9 of the 18c
        captured); holding the last 9c risks the whole 91c — a 10:1 bet to earn
        pennies. Chengdu at 99c bid has literally ~1c left to win and $3.80 to lose.

        This is a VARIANCE trade, not free money: if the book is fair, selling at
        the bid gives up ~half the spread versus holding. With a small bankroll,
        a demonstrably overconfident model, and ~2.7 wins needed per loss, that
        is a price worth paying.
        """
        if TAKE_PROFIT_BID_C <= 0:
            return
        by_key = {f"{r['city']}|{r['date']}|{r['kind']}": r for r in rows}
        with self._lock:
            open_pos = list(self.open)
        for pos in open_pos:
            row = by_key.get(pos["key"])
            if not row:
                continue
            bid = pos.get("mark_c")
            if bid is None or bid < TAKE_PROFIT_BID_C:
                continue
            entry = pos.get("entry_c") or 0
            captured = bid - entry
            if captured <= 0:                     # never "take profit" at a loss
                continue
            upside, risk = 100 - bid, bid
            self._exit_position(
                pos, row, won=True, tag="TAKE-PROFIT",
                reason=(f"bid {bid:.0f}c >= {TAKE_PROFIT_BID_C:.0f}c — converged "
                        f"({captured:+.0f}c of {100-entry:.0f}c captured); holding "
                        f"risks {risk:.0f}c to win {upside:.0f}c"))

    def _exit_dead(self, pos, row, reason):
        """Sell out of a dead bucket (or write it off when nothing bids)."""
        self._exit_position(pos, row, won=False, tag="DEAD-EXIT", reason=reason)

    def _exit_position(self, pos, row, won, tag, reason):
        """Shared exit: sell into whatever bids exist and book the REAL proceeds."""
        bucket = next((b for b in row.get("buckets", [])
                       if b.get("label") == pos.get("label")), {})
        bid_c = bucket.get("bid_c") or 0.0
        sold, proceeds, fill_c = 0.0, 0.0, None
        # A LIVE position holds real shares. It may only be closed by a real sale
        # (or by real settlement). If the executor has since been flipped to
        # paper, the live-sell branch below is skipped — and paper-marking it out
        # would book profit that does not exist while the shares sit untouched in
        # the wallet. That happened: Chengdu+Istanbul booked a phantom +$1.96
        # against zero USDC movement. Refuse, and say so.
        if pos.get("mode") == "live" and not self.is_live:
            if not pos.get("_exit_blocked"):
                pos["_exit_blocked"] = True
                self.on_log("!", f"[weatherexec] {tag} SKIPPED {pos['city']} {pos['label']} — "
                                 f"live position, executor is PAPER. Not booking a "
                                 f"simulated exit against real shares. Arm live to "
                                 f"sell, or close it manually. ({reason})")
            return
        if pos.get("mode") == "live" and self.is_live:
            try:
                import polymarket
                client = self._poly()
                fee = polymarket.fetch_live_fee_bps(pos["token_yes"]) or 0
                sold = client.place_sell_fok(pos["token_yes"], float(pos["shares"]),
                                             fee, neg_risk=pos.get("neg_risk"))
                # Use the REAL USDC received, not shares x a cached book quote.
                # The quote can be badly stale in our favour: a dead Shenzhen
                # bucket swept stale 90c bids for $6.30 while the cached bid
                # implied $5.54 (booked +$1.13 against a real +$1.89).
                real = getattr(client, "_last_sell_proceeds_usd", None)
                fill_c = getattr(client, "_last_fill_price_cents", None)
                proceeds = round(real if real is not None else sold * bid_c / 100.0, 2)
            except Exception as e:  # noqa: BLE001
                self.on_log("✗", f"[weatherexec] {tag} sell failed {pos['city']}: {e}")
                return                       # keep the position; retry next refresh
        else:
            sold = float(pos["shares"])          # paper: mark out at the bid
            proceeds = round(sold * bid_c / 100.0, 2)
        pnl = round(proceeds - pos.get("cost_usd", 0.0), 2)
        rec = {"type": "settle", "key": pos["key"], "won": won,
               "mode": pos.get("mode"),
               "closed_early": True, "exit": tag, "reason": reason,
               "salvage_usd": proceeds, "sold_shares": round(sold, 6),
               "sold_at_c": round(fill_c, 2) if fill_c is not None else None,
               "gross_pnl": pnl, "fee_usd": 0.0, "pnl_usd": pnl,
               "settled": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.open = [p for p in self.open if p["key"] != pos["key"]]
            self.closed.append({**pos, **rec})
            self.closed = self.closed[-200:]
            self.session["settled"] += 1
            self.session["wins"] += 1 if won else 0
            # early exits never reveal whether the BUCKET was right, so they must
            # not be counted as evidence for/against the model's calibration
            self.session["early_exits"] = self.session.get("early_exits", 0) + 1
            self.session["realized_pnl"] = round(self.session["realized_pnl"] + pnl, 2)
            self.session["realized_gross"] = round(self.session["realized_gross"] + pnl, 2)
            self.session["staked_usd"] = round(
                max(0.0, self.session["staked_usd"] - pos.get("cost_usd", 0.0)), 2)
        self._persist(rec)
        self.on_log("✅" if pnl >= 0 else "✗",
                    f"[weatherexec] {tag} {pos['city']} {pos['label']} — {reason}; "
                    f"got ${proceeds:.2f} of ${pos.get('cost_usd',0):.2f} ({pnl:+.2f})")

    def _consider(self, entry):
        key = f"{entry['city']}|{entry['date']}|{entry.get('kind', 'high')}"
        with self._lock:
            if len(self.open) >= MAX_OPEN:
                return
            if any(p["key"] == key for p in self.open):
                return
            if any(c.get("key") == key for c in self.closed):
                return
        ask_c = entry["ask_c"]
        if ask_c is None or ask_c <= 0:
            return
        # Prefer the size the engine actually verified depth for on the real
        # book; fall back to stake/price only if the book wasn't confirmed.
        shares = entry.get("shares_planned") or max(
            entry.get("min_size") or 5, round(self.stake_usd / (ask_c / 100.0)))
        filled_c = ask_c          # paper fills at the (book-confirmed) ask
        mode = "paper"
        if self.is_live:
            # Send the LIMIT that clears the worst level we'd touch (ask_c is the
            # VWAP we expect to pay — using it as the limit under-prices the FOK
            # and gets it killed).
            limit_c = entry.get("limit_c") or ask_c
            filled, fill_c = self._place_live(entry["token_yes"], limit_c, shares,
                                              entry.get("neg_risk"))
            if filled <= 0:
                self._record_miss(entry, limit_c, shares)
                return
            shares, mode = filled, "live"
            # record the ACTUAL average fill price the exchange gave us (a FOK
            # often fills below the limit), not the limit ask — otherwise live
            # cost/P&L is mis-stated. fill_c is None only if the resp lacked amounts.
            if fill_c is not None and fill_c > 0:
                filled_c = round(fill_c, 2)
        cost = round(shares * filled_c / 100.0, 2)
        pos = {
            "type": "open", "key": key, "mode": mode, "kind": entry.get("kind", "high"),
            "city": entry["city"], "date": entry["date"], "station": entry["station"],
            "label": entry["label"], "condition_id": entry["condition_id"],
            "token_yes": entry["token_yes"], "slug": entry["slug"],
            "lo": entry.get("lo"), "hi": entry.get("hi"),
            "neg_risk": entry.get("neg_risk"),
            "entry_c": filled_c, "shares": shares, "cost_usd": cost,
            "model_p": entry["p"], "edge_c": entry["edge_c"],
            "opened": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.open.append(pos)
            self.session["opened"] += 1
            self.session["staked_usd"] = round(self.session["staked_usd"] + cost, 2)
        self._persist(pos)
        self.on_log("◆", f"[weatherexec] {mode.upper()} ENTER {entry['city']} {entry['label']} "
                         f"@ {filled_c:.0f}c ×{shares} (p={entry['p']}, edge +{entry['edge_c']}c)")

    def _poly(self):
        """Lazily build and REUSE one PolyClient — avoids re-authenticating (and
        the couple-seconds latency that costs) on every live order."""
        if getattr(self, "_client", None) is None:
            import polymarket
            self._client = polymarket.PolyClient()
        return self._client

    def _place_live(self, token_id, ask_c, shares, neg_risk=None):
        """Place a live FOK buy. Returns (filled_shares, avg_fill_price_cents);
        fill price is None if the fill response lacked amounts."""
        try:
            import polymarket
            client = self._poly()
            fee = polymarket.fetch_live_fee_bps(token_id) or 0
            filled = client.place_fok(token_id, int(round(ask_c)), float(shares), fee,
                                      neg_risk=neg_risk)
            return filled, getattr(client, "_last_fill_price_cents", None)
        except Exception as e:  # noqa: BLE001
            self.on_log("✗", f"[weatherexec] live order failed: {e}")
            return 0.0, None

    def _record_miss(self, entry, limit_c, shares):
        """A live FOK returned nothing. Re-read the book RIGHT NOW and persist the
        gap, so we can later separate a market that ran away (chase = buy losers)
        from a fill we lost by a cent (the real 'missed opportunity').

        gap_c = live best ask − our limit. Positive means the ask climbed above
        our limit (we'd have needed to pay more); depth_ok says the size we
        wanted is still there at all. The re-read is ~milliseconds after the
        kill, so it is the closest picture we get of why it died — but it is
        AFTER the fact, so treat it as diagnostic, not the exact fill book.
        """
        token = entry["token_yes"]
        asks = fetch_book_asks(token)
        bid = fetch_book_bid_c(token)
        now_ask = asks[0][0] if asks else None
        _, got, _ = vwap_for_size(asks, shares) if asks else (None, 0.0, None)
        gap_c = round(now_ask - limit_c, 2) if now_ask is not None else None
        rec = {
            "type": "miss", "ts": datetime.now(timezone.utc).isoformat(),
            "key": f"{entry['city']}|{entry['date']}|{entry.get('kind', 'high')}",
            "city": entry.get("city"),
            "label": entry.get("label"), "kind": entry.get("kind", "high"),
            "p": entry.get("p"), "edge_c": entry.get("edge_c"),
            "limit_c": round(limit_c, 2), "shares_wanted": shares,
            # book at the miss instant
            "now_ask_c": round(now_ask, 2) if now_ask is not None else None,
            "now_bid_c": round(bid, 2) if bid is not None else None,
            "gap_c": gap_c,                       # >0: ask climbed past our limit
            "depth_ok": bool(asks) and got + 1e-9 >= shares,
        }
        try:
            with open(MISS_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self._misses += 1
        except Exception as e:  # noqa: BLE001
            log.warning("miss-log write failed: %s", e)
        g = f"gap {gap_c:+.1f}c" if gap_c is not None else "book gone"
        self.on_log("!", f"[weatherexec] LIVE FOK missed {entry['city']} "
                         f"{entry['label']} — limit {limit_c:.0f}c, now ask "
                         f"{'—' if now_ask is None else f'{now_ask:.0f}c'} ({g})")

    # ── real on-chain account (live truth, not modeled) ──────────────────────
    def _refresh_account(self):
        """Snapshot the REAL USDC balance so the dashboard shows actual P&L.

        equity = USDC + cost of still-open live positions (valuing open ones at
        cost, so tied-up capital isn't mistaken for a loss). Therefore
        real_pnl = equity − baseline is pure REALIZED profit, inclusive of
        everything the modeled ledger can miss: true fill prices, real fees,
        slippage. Cached to one read a minute.
        """
        if not (self.is_live or self._live_baseline is not None
                or any(p.get("mode") == "live" for p in self.open)):
            return
        now = time.time()
        if now - self._acct_ts < ACCT_REFRESH_SEC:
            return
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            client = self._poly()
            bal = client._clob.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            usdc = int(bal.get("balance", 0)) / 1_000_000.0
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[weatherexec] account read failed: {e}")
            return
        self._acct_ts = now
        if self._live_baseline is None:
            self._live_baseline = usdc
            self._persist({"type": "baseline", "live_baseline_usd": usdc,
                           "ts": datetime.now(timezone.utc).isoformat()})
            self.on_log("◆", f"[weatherexec] live USDC baseline set = ${usdc:.2f}")
        with self._lock:
            live = [p for p in self.open if p.get("mode") == "live"]
            open_cost = sum(p.get("cost_usd", 0.0) for p in live)
            # MARK TO MARKET: a position is worth its current bid, not its cost.
            # Costing it would report a dead position at face value (Miami showed
            # +$1.55 against a true -$6.50). Fall back to cost only when unmarked.
            open_value = sum(
                (p["shares"] * p["mark_c"] / 100.0) if p.get("mark_c") is not None
                else p.get("cost_usd", 0.0)
                for p in live)
            unmarked = sum(1 for p in live if p.get("mark_c") is None)
        equity = usdc + open_value
        self._acct = {
            "usdc": round(usdc, 2),
            "open_cost": round(open_cost, 2),
            "open_value": round(open_value, 2),      # marked to the bid
            "unrealized": round(open_value - open_cost, 2),
            "unmarked": unmarked,                    # >0 => open_value part-guessed
            "equity": round(equity, 2),
            "baseline": round(self._live_baseline, 2),
            "real_pnl": round(equity - self._live_baseline, 2),
        }

    # ── settlement (poll Gamma for resolutions) ──────────────────────────────
    def poll(self):
        self._refresh_account()
        with self._lock:
            open_pos = list(self.open)
        if not open_pos:
            return
        # Gamma needs repeated condition_ids params (comma-joining returns [])
        # AND closed=true — the endpoint silently filters out closed markets by
        # default, which is precisely the state a settling position is in.
        ids = [p["condition_id"] for p in open_pos if p.get("condition_id")]
        try:
            r = requests.get(GAMMA_MARKETS,
                             params={"condition_ids": ids, "closed": "true"},
                             timeout=15)
            r.raise_for_status()
            markets = {m.get("conditionId"): m for m in r.json()}
        except Exception as e:  # noqa: BLE001
            self.on_log("!", f"[weatherexec] settle poll failed: {e}")
            return
        for pos in open_pos:
            m = markets.get(pos.get("condition_id"))
            if not m or m.get("umaResolutionStatus") != "resolved":
                continue
            try:
                prices = m.get("outcomePrices")
                prices = json.loads(prices) if isinstance(prices, str) else prices
                yes = float(prices[0])
            except (TypeError, ValueError, IndexError):
                continue
            won = yes >= 0.5
            gross = round(pos["shares"] * ((100 - pos["entry_c"]) if won else -pos["entry_c"]) / 100.0, 2)
            fee = round(pos["shares"] * taker_fee_c(pos["entry_c"]) / 100.0, 2)
            net = round(gross - fee, 2)
            rec = {"type": "settle", "key": pos["key"], "won": won,
                   "mode": pos.get("mode"),
                   "gross_pnl": gross, "fee_usd": fee, "pnl_usd": net,
                   "settled": datetime.now(timezone.utc).isoformat()}
            with self._lock:
                self.open = [p for p in self.open if p["key"] != pos["key"]]
                self.closed.append({**pos, **rec})
                self.closed = self.closed[-200:]
                self.session["settled"] += 1
                self.session["wins"] += 1 if won else 0
                self.session["wins_held"] = self.session.get("wins_held", 0) + (1 if won else 0)
                self.session["realized_pnl"] = round(self.session["realized_pnl"] + net, 2)
                self.session["realized_gross"] = round(self.session["realized_gross"] + gross, 2)
                self.session["fees_paid"] = round(self.session["fees_paid"] + fee, 2)
                self.session["staked_usd"] = round(
                    max(0.0, self.session["staked_usd"] - pos.get("cost_usd", 0.0)), 2)
            self._persist(rec)
            self.on_log("✅" if won else "✗",
                        f"[weatherexec] SETTLE {pos['city']} {pos['date']} {pos['label']} "
                        f"{'WIN' if won else 'LOSS'} net {net:+.2f} USD (gross {gross:+.2f} − fee {fee:.2f}, "
                        f"model p was {pos['model_p']})")

    # ── state ────────────────────────────────────────────────────────────────
    def state(self):
        with self._lock:
            s = dict(self.session)
            s["win_rate"] = (s["wins"] / s["settled"]) if s["settled"] else None
            # calibration must be judged ONLY on positions held to resolution —
            # an early exit never reveals whether the bucket was actually right
            held = s["settled"] - s.get("early_exits", 0)
            s["settled_held"] = held
            s["win_rate_held"] = (s.get("wins_held", 0) / held) if held else None
            avg_p = ([p for p in (c.get("model_p") for c in self.closed) if p is not None])
            # PER-MODE breakdown — paper and live are different books and must be
            # counted separately, even though they share this ledger. Derived
            # from the position lists (each carries its own mode), so it is always
            # consistent with what's actually open/closed.
            by_mode = {}
            for m in ("live", "paper"):
                op = [p for p in self.open if p.get("mode") == m]
                cl = [c for c in self.closed if c.get("mode") == m]
                held_c = [c for c in cl if not c.get("closed_early")]
                by_mode[m] = {
                    "open": len(op),
                    "settled": len(cl),
                    "wins": sum(1 for c in cl if c.get("won")),
                    "settled_held": len(held_c),
                    "wins_held": sum(1 for c in held_c if c.get("won")),
                    "realized_pnl": round(sum(c.get("pnl_usd", 0.0) for c in cl), 2),
                    "staked_usd": round(sum(p.get("cost_usd", 0.0) for p in op), 2),
                }
            return {
                "mode": self.mode, "live": self.is_live, "env_armed": ENV_ARMED,
                "start_live": START_LIVE,   # boots live unattended? (see START_LIVE)
                "stake_usd": self.stake_usd, "max_open": MAX_OPEN, "session": s,
                "misses": self._misses,     # live FOKs that found nothing to fill
                "by_mode": by_mode,         # {live:{...}, paper:{...}}
                "account": self._acct,      # REAL on-chain USDC / equity / P&L

                "avg_model_p": round(sum(avg_p) / len(avg_p), 3) if avg_p else None,
                "open": [{k: p.get(k) for k in
                          ("mode", "city", "kind", "date", "label", "entry_c", "shares",
                           "cost_usd", "model_p", "edge_c", "opened", "mark_c", "health")}
                         for p in self.open],
                "recent": [{k: c.get(k) for k in
                            ("city", "kind", "date", "label", "entry_c", "model_p",
                             "won", "pnl_usd", "gross_pnl", "fee_usd")}
                           for c in self.closed[-15:]][::-1],
            }
