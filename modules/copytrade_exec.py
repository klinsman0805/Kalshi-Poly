"""
modules/copytrade_exec.py — copy-trade EXECUTOR + forward monitor (Polymarket).

Mirrors new BUY trades from followed wallets. This is also the ONLY honest test
of whether copy-trading has edge: it paper-fills each copy at the CURRENT market
price (not the trader's price), so the recorded entry already eats the latency
slippage a real follower suffers. As copied markets resolve it books win/loss and
realized P&L, and persists everything — so a loop can accumulate the *strategy's*
own winrate over time (past-winrate of a trader ≠ forward edge of copying them —
see the scalping n=135 variance lesson).

SAFETY — real orders need BOTH gates (a stray toggle can't go live):
  1. env  COPYTRADE_LIVE=true      (operator arms at launch)
  2. runtime mode == "live"        (default "paper")
Anything less → PAPER: identical selection/sizing/logging, simulated fill.

Config (env):
  COPYTRADE_FOLLOW          comma wallet list to force-follow (else: copyable
                            wallets handed in from the scanner)
  COPYTRADE_EXEC_STAKE_USD  legacy flat-stake $ (unused; RISK_USD sizes now)
  COPYTRADE_EXEC_MAX_OPEN   max concurrent open copies    (default 20)
  COPYTRADE_EXEC_MAX_USD    only copy trades whose usdcSize ≤ this (default 5000)
  COPYTRADE_EXEC_MIN_C      hard price floor (default 5)
  COPYTRADE_EXEC_MAX_C      hard price ceiling (default 95)
  COPYTRADE_EXEC_ENTRY_MIN_C EV band floor — skip cheaper (default = MIN_C)
  COPYTRADE_EXEC_ENTRY_MAX_C EV band ceiling — skip deep favourites (default 85)
  COPYTRADE_EXEC_RISK_USD    $ risked per copy; shares = RISK_USD/price (default 5)
  COPYTRADE_EXEC_MAX_SLIP_C  reject copy if price ran > this vs their fill (default 3)
  COPYTRADE_EXEC_MAX_SHARES  hard share cap per copy (default 200)
  COPYTRADE_EXEC_LOOKBACK   activity events scanned per wallet per poll (default 40)
  COPYTRADE_LIVE            true|false  arm real Poly orders (default false)
  COPYTRADE_EXEC_LOG        positions jsonl path (default copytrade_positions.jsonl)

Run standalone poller:  python -m modules.copytrade_exec
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds import poly_leaderboard

log = logging.getLogger("modules.copytrade_exec")

POS_LOG = Path(os.getenv("COPYTRADE_EXEC_LOG", "copytrade_positions.jsonl"))
ENV_ARMED = os.getenv("COPYTRADE_LIVE", "false").strip().lower() == "true"
STAKE_USD = float(os.getenv("COPYTRADE_EXEC_STAKE_USD", "5"))
MAX_OPEN = int(os.getenv("COPYTRADE_EXEC_MAX_OPEN", "20"))
MAX_TRADE_USD = float(os.getenv("COPYTRADE_EXEC_MAX_USD", "5000"))
MIN_C = int(os.getenv("COPYTRADE_EXEC_MIN_C", "5"))
MAX_C = int(os.getenv("COPYTRADE_EXEC_MAX_C", "95"))
LOOKBACK = int(os.getenv("COPYTRADE_EXEC_LOOKBACK", "40"))

# ── EV trade filter + risk sizing ────────────────────────────────────────────
# The n=317 paper test lost because copies were flat-STAKE: a 90c favourite and a
# 32c longshot both risked ~$5, but the favourite's upside is tiny ($0.10/share)
# while its downside is large ($0.90/share). Winning 52% of favourite-heavy bets
# can't pay for that. Two fixes:
#   1. EV price band — only copy entries where wins can pay for losses. Skip deep
#      favourites (>MAX_ENTRY_C) whose payoff is asymmetric against you.
#   2. Flat-NOTIONAL-RISK sizing — size so each YES buy risks the same dollars.
#      Risk per share of a YES buy at price p ($/share) is p (you lose entry on a
#      NO resolution), so shares = RISK_USD / p. This replaces shares = stake / p,
#      which under-sized longshots and over-sized favourites — the exact bleed.
ENTRY_MIN_C = int(os.getenv("COPYTRADE_EXEC_ENTRY_MIN_C", str(MIN_C)))
ENTRY_MAX_C = int(os.getenv("COPYTRADE_EXEC_ENTRY_MAX_C", "85"))
RISK_USD = float(os.getenv("COPYTRADE_EXEC_RISK_USD", "5"))
MAX_SLIP_C = int(os.getenv("COPYTRADE_EXEC_MAX_SLIP_C", "3"))
MAX_SHARES = int(os.getenv("COPYTRADE_EXEC_MAX_SHARES", "200"))


def _env_follow():
    raw = os.getenv("COPYTRADE_FOLLOW", "").strip()
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


class CopyTradeExecutor:
    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.mode = "paper"                      # "paper" | "live" (live also needs ENV_ARMED)
        self.stake_usd = STAKE_USD
        self.follow = set(_env_follow())         # wallets we mirror
        self.open = []                           # open copied positions
        self.closed = []                         # settled copies (bounded, for UI)
        self._seen = set()                       # dedup by trade transactionHash
        self._cursor = {}                        # wallet -> last-seen timestamp
        self._skips = {}                         # skip-reason -> count (visibility)
        self._poly = None
        self.session = {
            "copied": 0, "skipped": 0, "settled": 0, "wins": 0, "losses": 0,
            "staked_usd": 0.0, "realized_pnl": 0.0, "slippage_c_sum": 0,
            "lookup_fails": 0,
        }
        self._rehydrate()

    # ── crash/restart recovery ───────────────────────────────────────────────
    def _rehydrate(self):
        """Rebuild state from the jsonl so a restart doesn't orphan open copies.

        Without this the forward test silently resets on every restart: copies
        recorded on disk stay unsettled forever because `open` starts empty.
        Replays copy_open / copy_settle records to restore open positions,
        settled tallies, dedup keys and per-wallet cursors.
        """
        if not POS_LOG.exists():
            return
        settled_tokens = set()
        opens = {}
        try:
            for line in POS_LOG.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                t, tok = rec.get("type"), rec.get("token")
                if t == "copy_open":
                    opens[(tok, rec.get("ts"))] = rec
                    self.session["copied"] += 1
                    self.session["staked_usd"] = round(
                        self.session["staked_usd"] + float(rec.get("cost_usd") or 0), 2)
                    self.session["slippage_c_sum"] += int(rec.get("slippage_c") or 0)
                elif t == "copy_settle":
                    settled_tokens.add((tok, rec.get("ts")))
                    pnl = float(rec.get("pnl_usd") or 0)
                    self.session["settled"] += 1
                    self.session["wins" if rec.get("won") else "losses"] += 1
                    self.session["realized_pnl"] = round(self.session["realized_pnl"] + pnl, 2)
                    self.closed.append(rec)
        except Exception as e:  # noqa: BLE001
            log.warning("rehydrate failed: %s", e)
            return

        for key, rec in opens.items():
            if key in settled_tokens:
                continue
            rec.pop("type", None)   # don't let the on-disk tag ride along in memory
            self.open.append(rec)
            if rec.get("wallet"):
                self.follow.add(rec["wallet"])
        self.closed = self.closed[-200:]
        if self.open or self.closed:
            self.on_log("→", f"[copyexec] recovered {len(self.open)} open / "
                             f"{self.session['settled']} settled copies from {POS_LOG}")

    # ── mode control ─────────────────────────────────────────────────────────
    def set_mode(self, mode):
        mode = (mode or "paper").lower()
        if mode == "live" and not ENV_ARMED:
            self.on_log("!", "[copyexec] LIVE requested but COPYTRADE_LIVE!=true — staying PAPER")
            self.mode = "paper"
            return self.mode
        self.mode = "live" if mode == "live" else "paper"
        self.on_log("⚙", f"[copyexec] mode = {self.mode.upper()}"
                         + (" (REAL ORDERS)" if self.mode == "live" else ""))
        return self.mode

    @property
    def is_live(self):
        return self.mode == "live" and ENV_ARMED

    def follow_from_scan(self, rows):
        """Adopt the scanner's copyable wallets as follow targets (unless env pins them)."""
        if _env_follow():
            return
        for r in rows or []:
            if r.get("copyable") and r.get("wallet"):
                self.follow.add(r["wallet"].lower())

    # ── main poll: detect + copy new BUYs, then settle resolved copies ────────
    def poll(self):
        for wallet in list(self.follow):
            try:
                self._poll_wallet(wallet)
            except Exception as e:  # noqa: BLE001
                self.on_log("✗", f"[copyexec] poll {wallet[:8]} error: {e}")
        self.mark_resolutions()
        return self.state()

    def _poll_wallet(self, wallet):
        acts = poly_leaderboard.fetch_activity(wallet, limit=LOOKBACK)
        newest = max((a.get("timestamp", 0) for a in acts), default=0)
        # First sight of a wallet: baseline the cursor and copy NOTHING retroactively.
        # A forward test can only mirror trades that occur after we start following.
        if wallet not in self._cursor:
            self._cursor[wallet] = newest
            return
        cursor = self._cursor[wallet]
        for a in sorted(acts, key=lambda x: x.get("timestamp", 0)):  # oldest-first
            ts = a.get("timestamp", 0)
            if ts <= cursor:
                continue
            if a.get("type") == "TRADE" and a.get("side") == "BUY":
                self._consider(wallet, a)
        self._cursor[wallet] = max(newest, cursor)

    def _skip(self, reason):
        """Tally why a candidate was skipped (bucketed by leading token) for visibility."""
        key = reason.split()[0] if reason else "other"
        self._skips[key] = self._skips.get(key, 0) + 1

    def _consider(self, wallet, trade):
        txh = trade.get("transactionHash")
        if txh and txh in self._seen:
            return
        if txh:
            self._seen.add(txh)

        their_price = round(float(trade.get("price") or 0) * 100)
        usd = float(trade.get("usdcSize") or 0)
        token = trade.get("asset")
        title = trade.get("title") or trade.get("slug") or "?"

        # filters — copyability band, EV price band, capacity
        reason = None
        if not token:
            reason = "no token"
        elif usd > MAX_TRADE_USD:
            reason = f"size ${usd:,.0f}>cap"
        elif not (ENTRY_MIN_C <= their_price <= ENTRY_MAX_C):
            reason = f"their {their_price}c out of EV band [{ENTRY_MIN_C},{ENTRY_MAX_C}]"
        elif len(self.open) >= MAX_OPEN:
            reason = "max_open"
        if reason:
            self.session["skipped"] += 1
            self._skip(reason)
            return

        # fill at CURRENT market ask — this is where latency slippage shows up
        cur_ask = poly_leaderboard.token_price(token, side="buy")
        if cur_ask is None:
            self.session["skipped"] += 1
            self._skip("no live book")
            self.on_log("!", f"[copyexec] skip {title[:32]} — no live book "
                            f"(their {their_price}c)")
            return
        # by the time we fill, the price may have run past the EV band — re-check it
        if not (ENTRY_MIN_C <= cur_ask <= ENTRY_MAX_C):
            self.session["skipped"] += 1
            self._skip(f"cur {cur_ask}c out of EV band")
            return
        # reject copies where latency already moved the price too far against us
        if cur_ask - their_price > MAX_SLIP_C:
            self.session["skipped"] += 1
            self._skip(f"slip {cur_ask - their_price}c>{MAX_SLIP_C}")
            return

        # flat-NOTIONAL-RISK sizing: risk per share of a YES buy at p is p, so
        # shares = RISK_USD / p equalises dollar-at-risk across favourites/longshots.
        p = cur_ask / 100.0
        shares = int(round(RISK_USD / p)) if p > 0 else 0
        shares = min(shares, MAX_SHARES)
        if shares <= 0:
            self.session["skipped"] += 1
            self._skip("zero shares")
            return
        filled, detail = self._place(token, cur_ask, shares)
        if filled <= 0:
            self.session["skipped"] += 1
            self.on_log("✗", f"[copyexec] {self.mode.upper()} no fill {title[:32]} "
                            f"@{cur_ask}c ({detail})")
            return

        slip = cur_ask - their_price   # +ve = we paid more than they did (latency cost)
        cost = round(filled * cur_ask / 100.0, 2)
        pos = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode, "wallet": wallet, "title": title,
            "token": token, "outcome": trade.get("outcome"),
            "their_price": their_price, "entry": cur_ask, "slippage_c": slip,
            "filled": filled, "cost_usd": cost, "risk_usd": round(RISK_USD, 2),
            "detail": detail, "phase": "open",
        }
        self.open.append(pos)
        self.session["copied"] += 1
        self.session["staked_usd"] = round(self.session["staked_usd"] + cost, 2)
        self.session["slippage_c_sum"] += slip
        self._persist({**pos, "type": "copy_open"})   # type LAST — pos may carry a stale one
        self.on_log("✅", f"[copyexec] {self.mode.upper()} COPIED {wallet[:8]} → "
                         f"{title[:36]} {trade.get('outcome')} @{cur_ask}c ×{filled} "
                         f"(${cost}, slip {slip:+d}c vs {their_price}c)")

    # ── settlement: book win/loss as copied markets resolve ──────────────────
    def mark_resolutions(self):
        still_open = []
        lookup_fails = 0
        for pos in self.open:
            res = poly_leaderboard.market_resolution(pos["token"])
            if res is None:
                # LOOKUP FAILED — this is NOT "still open". Conflating the two
                # silently stalls the forward test whenever the API is down.
                lookup_fails += 1
                pos["lookup_fails"] = pos.get("lookup_fails", 0) + 1
                still_open.append(pos)
                continue
            pos.pop("lookup_fails", None)   # recovered
            if not res.get("closed") or res.get("price") is None:
                still_open.append(pos)      # genuinely unresolved
                continue
            payout_c = 100 if res["price"] >= 0.5 else 0   # this token won?
            proceeds = round(pos["filled"] * payout_c / 100.0, 2)
            pnl = round(proceeds - pos["cost_usd"], 2)
            won = pnl > 0
            pos.update({"phase": "settled", "resolved_price": round(res["price"], 3),
                        "proceeds_usd": proceeds, "pnl_usd": pnl, "won": won,
                        "settled_ts": datetime.now(timezone.utc).isoformat()})
            self.session["settled"] += 1
            self.session["wins" if won else "losses"] += 1
            self.session["realized_pnl"] = round(self.session["realized_pnl"] + pnl, 2)
            self.closed.append(pos)
            self.closed = self.closed[-200:]
            # type LAST: a rehydrated pos still carries "type": "copy_open", and
            # {"type": x, **pos} would let it overwrite x and rewrite a fake open.
            self._persist({**pos, "type": "copy_settle"})
            self.on_log("✅" if won else "✗",
                        f"[copyexec] SETTLED {'WIN' if won else 'LOSS'} "
                        f"{pos['title'][:36]} pnl ${pnl:+.2f} "
                        f"(entry {pos['entry']}c → {payout_c}c)")
        self.open = still_open
        self.session["lookup_fails"] = lookup_fails
        if lookup_fails:
            # Loud, not silent: an unreachable API must never look like "nothing resolved".
            self.on_log("!", f"[copyexec] {lookup_fails}/{len(self.open)} resolution "
                             f"lookups FAILED — settlements stalled (API unreachable?)")

    # ── order placement ──────────────────────────────────────────────────────
    def _place(self, token, ask, shares):
        if not self.is_live:
            return shares, "paper-fill"
        try:
            import polymarket
            if self._poly is None:
                polymarket.DRY_RUN = False
                self._poly = polymarket.PolyClient()
            filled = self._poly.place_fok(token, int(ask), int(shares), fee_bps=0)
            return int(filled), f"poly fok @{ask}c"
        except Exception as e:  # noqa: BLE001
            log.exception("copy order failed")
            return 0, f"error: {e}"

    # ── persistence + state ──────────────────────────────────────────────────
    def _persist(self, rec):
        try:
            with open(POS_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("copy pos log write: %s", e)

    def state(self):
        s = dict(self.session)
        s["win_rate"] = round(s["wins"] / s["settled"], 4) if s["settled"] else None
        s["avg_slippage_c"] = round(s["slippage_c_sum"] / s["copied"], 2) if s["copied"] else None
        return {
            "mode": self.mode, "live": self.is_live, "env_armed": ENV_ARMED,
            "stake_usd": self.stake_usd,
            "follow": sorted(self.follow),
            "open": self.open, "closed": self.closed[-25:], "session": s,
            "skips": dict(self._skips),
            "config": {"max_open": MAX_OPEN, "max_trade_usd": MAX_TRADE_USD,
                       "entry_min_c": ENTRY_MIN_C, "entry_max_c": ENTRY_MAX_C,
                       "risk_usd": RISK_USD, "max_slip_c": MAX_SLIP_C,
                       "max_shares": MAX_SHARES, "log": str(POS_LOG)},
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    interval = int(os.getenv("COPYTRADE_EXEC_INTERVAL", "60"))
    ex = CopyTradeExecutor(on_log=lambda i, m: print(i, m))
    if not ex.follow:
        # seed from the scanner's copyable leaderboard wallets
        rows = poly_leaderboard.scan(top_n=int(os.getenv("COPYTRADE_TOP_N", "25")),
                                     max_copy_trade_usd=MAX_TRADE_USD, deep=False)
        ex.follow_from_scan(rows)
    print(f"Following {len(ex.follow)} wallets, paper stake ${ex.stake_usd}, "
          f"poll {interval}s → {POS_LOG}")
    while True:
        st = ex.poll()
        s = st["session"]
        print(f"  copied={s['copied']} open={len(st['open'])} settled={s['settled']} "
              f"win={s['win_rate']} pnl=${s['realized_pnl']:+.2f} "
              f"avgslip={s['avg_slippage_c']}c")
        time.sleep(interval)
