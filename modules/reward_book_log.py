"""
modules/reward_book_log.py — order-book recorder for Polymarket reward markets.

PLACES NO ORDERS. Records what a reward pool looks like and who is already
competing for it, so that "what does a resting dollar actually earn" can be
answered from observation instead of from a formula.

Why this and not just the existing scanner. `feeds/poly_rewards.score_market`
already implements Polymarket's scoring formula, and it carries its own
measured warning: on 2026-08-01 it predicted $2.4157 for a real resting order
against $0.1177 actually paid — 20.5x too high. Two structural causes were
fixed after that (the complement book, the adjusted midpoint), but the
remaining ones cannot be fixed by better arithmetic:

  - Rewards are re-sampled across a 10,080-sample epoch. A snapshot cannot see
    how long a competitor's order rested, and resting time is most of the
    score. Only a time series can.
  - The epoch-level cross-market normalisation is ignored outright.
  - An aggregated book cannot reveal individual order sizes, so a price level
    is treated as a single order against the min-size cutoff.

All three are sampling problems, not modelling problems. So this records the
inputs every fifteen minutes and lets the estimate be checked against what the
CLOB actually pays, rather than trusting the estimate.

What it captures per market per cycle: the pool, the qualification terms, BOTH
token books (the complement is half the competition and was invisible until
recently), the qualifying score already resting on each side, and the scanner's
own estimate with its honesty flags — so a later pass can ask how wrong the
estimate was, market by market, and in which direction.

Scope. The reward universe is 18,323 markets paying $224,149/day, but the
median pool is $3/day and the program's minimum payout is $1. Recording all of
it would be noise. The default targets pools at or above $50/day, which is
roughly the top decile and where nearly all the money is.

Env:
  REWARD_BOOK_LOG           default candidate_data/reward_books.jsonl
  REWARD_BOOK_LOG_ENABLED   default true
  REWARD_BOOK_MIN_RATE      default 50 — ignore pools smaller than this
  REWARD_BOOK_MAX_MARKETS   default 40 — markets fetched per cycle
  REWARD_BOOK_SAMPLE_SEC    default 900 — per-market throttle
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("modules.reward_book_log")

ENABLED = os.getenv("REWARD_BOOK_LOG_ENABLED", "true").strip().lower() == "true"
LOG_PATH = Path(os.getenv("REWARD_BOOK_LOG", "candidate_data/reward_books.jsonl"))
MIN_RATE = float(os.getenv("REWARD_BOOK_MIN_RATE", "50"))
MAX_MARKETS = int(os.getenv("REWARD_BOOK_MAX_MARKETS", "40"))
SAMPLE_SEC = float(os.getenv("REWARD_BOOK_SAMPLE_SEC", "900"))

MAX_LEVELS = 8          # per side per token; deeper is noise for this question
BOOKS_URL = "https://clob.polymarket.com/books"
BATCH = 40


def _path_for(day):
    return LOG_PATH.with_name(f"{LOG_PATH.stem}-{day}{LOG_PATH.suffix}")


def fetch_books_batch(token_ids, timeout=25):
    """token_id -> (bids, asks) as [(price_c, size)], best first.

    One POST for the whole slice. score_market() takes a `book_fetcher`, so
    this is injected there rather than letting it make 2 HTTP calls per market
    — 40 markets would otherwise be 80 round trips a cycle.

    Returns None on failure. A caller must treat that as "unknown", never as
    "empty": a market with no competition and an unreachable API look identical
    otherwise, and the first is the whole thing we are hunting for.
    """
    import requests
    ids = [str(t) for t in (token_ids or []) if t]
    if not ids:
        return {}
    out = {}
    try:
        for i in range(0, len(ids), BATCH):
            r = requests.post(BOOKS_URL,
                              json=[{"token_id": t} for t in ids[i:i + BATCH]],
                              timeout=timeout)
            r.raise_for_status()
            for b in r.json() or []:
                tok = b.get("asset_id")
                if not tok:
                    continue
                bids = sorted(((float(x["price"]) * 100.0, float(x["size"]))
                               for x in (b.get("bids") or [])), reverse=True)
                asks = sorted((float(x["price"]) * 100.0, float(x["size"]))
                              for x in (b.get("asks") or []))
                out[str(tok)] = (bids, asks)
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("reward books batch failed (%d tokens): %s", len(ids), e)
        return None


def qualifying_size(levels, mid_c, max_spread_c, min_size):
    """Shares resting inside the reward band that would actually score.

    Deliberately simpler than the scanner's quadratic S(v,s) weighting: this is
    the raw competition count, recorded so the weighting can be re-derived
    later without re-fetching. Levels under the min-size cutoff score nothing.
    """
    if max_spread_c <= 0 or mid_c is None:
        return 0.0
    return round(sum(sz for p, sz in (levels or [])
                     if abs(p - mid_c) <= max_spread_c and sz >= min_size), 4)


class RewardBookLogger:
    """Throttled reward-market book recorder. Never raises into the caller."""

    def __init__(self, on_log=None, state_path=None):
        self.on_log = on_log or (lambda i, m: None)
        self.day = None
        self.fh = None
        self.path = None
        self._last = {}      # condition_id -> unix seconds of last capture
        self._cursor = 0
        self.n_written = 0
        self.last_error = None
        self.state_path = Path(state_path) if state_path else None
        self._load_state()

    # ── state, because cron restarts this process every 15 minutes ──────────
    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._last = dict(d.get("last") or {})
            self._cursor = int(d.get("cursor") or 0)
        except (ValueError, OSError) as e:
            log.warning("reward book state unreadable (%s); starting fresh", e)

    def _save_state(self):
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last": self._last, "cursor": self._cursor}),
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            log.warning("reward book state not saved: %s", e)

    def _fh_for_today(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self.day or self.fh is None:
            if self.fh:
                self.fh.close()
            self.day = day
            self.path = _path_for(day)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = self.path.open("a", encoding="utf-8")
        return self.fh

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None

    # ── selection ───────────────────────────────────────────────────────────
    def _due(self, markets, now):
        """Markets to capture this cycle, resuming where the last run stopped.

        Round-robin rather than "the top N by pool": the biggest pools are also
        the most contested, and a recorder that only ever sees them cannot say
        whether a smaller, quieter pool pays better per dollar. That comparison
        is the entire question.
        """
        pool = [m for m in markets or []
                if now - self._last.get(m.get("condition_id"), 0) >= SAMPLE_SEC]
        if not pool:
            return []
        if self._cursor >= len(pool):
            self._cursor = 0
        picked = pool[self._cursor:self._cursor + MAX_MARKETS]
        self._cursor += len(picked)
        return picked

    # ── record ──────────────────────────────────────────────────────────────
    def _record(self, m, books, score, now):
        toks = m.get("tokens") or []
        yes = next((t for t in toks if (t.get("outcome") or "").lower() == "yes"), None)
        no = next((t for t in toks if (t.get("outcome") or "").lower() == "no"), None)
        v_c = m.get("max_spread_c") or 0.0
        msz = m.get("min_size") or 0.0
        mid = (score or {}).get("mid_c")

        def side(tok, idx, complement=False):
            """Levels as recorded, plus qualifying size measured in YES space.

            The NO book is quoted in NO prices, and `mid` is a YES midpoint. A
            NO bid at 58c IS a YES ask at 42c, so the complement must be
            converted before it can be compared to the band — measuring it raw
            silently reports zero competition on half the field, which is one
            of the two causes of the scanner's 20.5x overestimate. Levels are
            stored unconverted so nothing is lost; only the qualification
            arithmetic moves.
            """
            if not tok:
                return [], 0.0
            bk = books.get(str(tok.get("token_id")))
            if not bk:
                return [], 0.0
            raw = bk[idx]
            as_yes = [(100.0 - p, s) for p, s in raw] if complement else raw
            return ([[round(p, 4), s] for p, s in raw[:MAX_LEVELS]],
                    qualifying_size(as_yes, mid, v_c, msz))

        y_bids, y_bq = side(yes, 0)
        y_asks, y_aq = side(yes, 1)
        # a NO bid is a YES ask and vice versa — the side label follows the
        # token it was quoted on, the arithmetic follows YES space
        n_bids, n_bq = side(no, 0, complement=True)
        n_asks, n_aq = side(no, 1, complement=True)

        rec = {
            "ts": now.isoformat(),
            "condition_id": m.get("condition_id"),
            "question": m.get("question"),
            "slug": m.get("slug"),
            # ── the pool and its terms ──
            "rate_per_day": m.get("rate_per_day"),
            "max_spread_c": v_c,
            "min_size": msz,
            "competitiveness": m.get("competitiveness"),
            # ── the books, both tokens: the complement is half the competition ──
            "yes_token": (yes or {}).get("token_id"),
            "no_token": (no or {}).get("token_id"),
            "yes_bid_levels": y_bids, "yes_ask_levels": y_asks,
            "no_bid_levels": n_bids, "no_ask_levels": n_asks,
            # ── raw competition inside the band, unweighted ──
            "qual_yes_bid": y_bq, "qual_yes_ask": y_aq,
            "qual_no_bid": n_bq, "qual_no_ask": n_aq,
            "qual_total": round(y_bq + y_aq + n_bq + n_aq, 4),
        }
        # the scanner's own estimate, kept WITH its honesty flags so a later
        # pass can ask how wrong it was rather than re-deriving it
        for k in ("mid_c", "best_bid_c", "best_ask_c", "two_sided_required",
                  "existing_bid_score", "existing_ask_score", "capital_usd",
                  "est_daily_usd", "yield_per_dollar_per_day", "confidence",
                  "no_competition_detected", "complement_seen",
                  "mid_is_adjusted", "below_min_payout"):
            rec[k] = (score or {}).get(k)
        return rec

    # ── entry point ─────────────────────────────────────────────────────────
    def snapshot(self, markets=None):
        """Capture a slice of the reward universe. Never raises."""
        if not ENABLED:
            return 0
        try:
            from feeds.poly_rewards import fetch_reward_markets, score_market
        except Exception as e:  # noqa: BLE001
            self.last_error = f"import: {e}"
            return 0
        try:
            if markets is None:
                markets = fetch_reward_markets(tag_slug=None, min_rate=MIN_RATE,
                                               use_cache=False)
            now_s = time.time()
            due = self._due(markets, now_s)
            if not due:
                return 0

            toks = [t.get("token_id") for m in due for t in (m.get("tokens") or [])]
            books = fetch_books_batch(toks)
            if books is None:
                # A venue outage must not stamp the throttle, or every market
                # would be marked freshly captured and go quiet for 15 minutes.
                return 0

            def fetcher(token_id, timeout=6):
                bk = books.get(str(token_id))
                if bk is None:
                    raise KeyError(token_id)
                return bk

            fh = self._fh_for_today()
            now = datetime.now(timezone.utc)
            written = 0
            for m in due:
                try:
                    score = score_market(m, book_fetcher=fetcher)
                except Exception:  # noqa: BLE001
                    score = None          # a book we could not price is still
                                          # worth recording for its raw levels
                fh.write(json.dumps(self._record(m, books, score, now),
                                    default=str) + "\n")
                self._last[m.get("condition_id")] = now_s
                written += 1
            if written:
                fh.flush()
                self.n_written += written
            self._save_state()
            return written
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("reward book log failed: %s", e)
            return 0

    def state(self):
        return {"enabled": ENABLED, "written": self.n_written,
                "path": str(self.path) if self.path else str(LOG_PATH),
                "min_rate": MIN_RATE, "max_markets": MAX_MARKETS,
                "sample_sec": SAMPLE_SEC, "last_error": self.last_error}
