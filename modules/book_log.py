"""
modules/book_log.py — full order book recorder for the weather sector.

PLACES NO ORDERS AND CHANGES NO DECISION. Like candidate_log it observes and
writes; unlike candidate_log it captures the WHOLE ladder, both sides, with
sizes, for events we are not trading and days we are not yet watching.

Why a second recorder rather than more fields on the first. candidate_log
records the bucket the model chose, at the moment it chose it. That is the
right shape for asking "was the model right", and the wrong shape for the three
questions now outstanding:

  Is the bid real?      Selling gate-refused rows at their recorded bid
                        backtests at +22.3c a contract after fees. But
                        `book_depth` is populated only inside _book_confirm,
                        which runs on ENTER alone, so across 1,608 EARLY rows
                        it was recorded exactly 0 times. A 30c bid one share
                        deep produces that same backtest and is worth nothing.
                        An early probe found Polymarket ladders with `bids: []`
                        outright, which is the shape that would kill it.

  Does the ladder sum?  Buckets are mutually exclusive and collectively
                        exhaustive, so asks should sum just over 100c and bids
                        just under. Testing that needs every bucket of one
                        ladder at one instant. candidate_log stores a median of
                        1 bucket per event per hour, so the test could not be
                        run at all — grouping by timestamp reassembled nothing.
                        Hence `snap_id`: a ladder is regrouped by identity, not
                        by binning clock times and hoping.

  Where is the edge?    The published work puts it 3-5 days out, where our
                        capture is nearly empty because the engine drops events
                        more than a day ahead. This recorder takes the events
                        the feed already returned, whatever their date, and
                        stamps `days_out` so the window can be sliced later.

Cost control. One HTTP call per event per venue — Polymarket's POST /books
takes the whole ladder at once, and Kalshi's /markets?event_ticker= returns
every strike with its top-of-book size. Events are visited round-robin under a
per-cycle cap so a 300-event feed spreads over many cycles instead of stalling
one. The box has 458MB of RAM and swaps under load; this stays small on
purpose.

Env:
  WEATHER_BOOK_LOG           default candidate_data/books.jsonl
  WEATHER_BOOK_LOG_ENABLED   default true
  WEATHER_BOOK_SAMPLE_SEC    default 1800 — per-event throttle
  WEATHER_BOOK_MAX_EVENTS    default 12 — events fetched per cycle
  WEATHER_BOOK_MAX_DAYS_OUT  default 7 — ignore events further ahead than this
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("modules.book_log")

ENABLED = os.getenv("WEATHER_BOOK_LOG_ENABLED", "true").strip().lower() == "true"
LOG_PATH = Path(os.getenv("WEATHER_BOOK_LOG", "candidate_data/books.jsonl"))
SAMPLE_SEC = float(os.getenv("WEATHER_BOOK_SAMPLE_SEC", "1800"))
MAX_EVENTS = int(os.getenv("WEATHER_BOOK_MAX_EVENTS", "12"))
MAX_DAYS_OUT = int(os.getenv("WEATHER_BOOK_MAX_DAYS_OUT", "7"))

# Depth within this many cents of the touch. One share at the touch and a wall
# behind it are different books, and the difference is the whole question.
DEPTH_BAND_C = 5.0


def _path_for(day):
    return LOG_PATH.with_name(f"{LOG_PATH.stem}-{day}{LOG_PATH.suffix}")


def _depth_within(levels, band_c):
    """Total size available within `band_c` of the best price."""
    if not levels:
        return 0.0
    best = levels[0][0]
    return round(sum(s for p, s in levels if abs(p - best) <= band_c), 4)


def _age_s(book_ts, now=None):
    """Seconds since the venue stamped this book. Polymarket sends epoch millis
    as a string; Kalshi sends an ISO timestamp."""
    if book_ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        s = str(book_ts)
        if s.isdigit():
            secs = float(s) / (1000.0 if len(s) > 11 else 1.0)
            return round(now.timestamp() - secs, 2)
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return round((now - t).total_seconds(), 2)
    except (ValueError, OverflowError, OSError):
        return None


class BookLogger:
    """Round-robin full-ladder recorder. One instance per executor."""

    def __init__(self, venue, on_log=None, state_path=None):
        self.venue = venue
        self.on_log = on_log or (lambda i, m: None)
        self.day = None
        self.fh = None
        self.path = None
        self._last = {}          # event key -> unix seconds of last snapshot
        self._cursor = 0         # round-robin position across cycles
        self.n_written = 0
        self.n_snapshots = 0
        self.last_error = None
        # Run from cron the process is short-lived, so the throttle and the
        # round-robin cursor have to outlive it or every run would re-fetch the
        # same head of the list and no city past the first dozen would ever be
        # captured.
        self.state_path = Path(state_path) if state_path else None
        self._load_state()

    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._last = dict(d.get("last") or {})
            self._cursor = int(d.get("cursor") or 0)
        except (ValueError, OSError) as e:
            log.warning("book log state unreadable (%s); starting fresh", e)

    def _save_state(self):
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last": self._last,
                                       "cursor": self._cursor}), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            log.warning("book log state not saved: %s", e)

    # ── output ───────────────────────────────────────────────────────────────
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

    # ── selection ────────────────────────────────────────────────────────────
    @staticmethod
    def _days_out(event, today=None):
        d = event.get("date")
        if d is None:
            return None
        try:
            d = d if hasattr(d, "toordinal") else datetime.fromisoformat(str(d)).date()
        except ValueError:
            return None
        return (d - (today or datetime.now(timezone.utc).date())).days

    def _due(self, events, now):
        """Events worth fetching this cycle, resuming where we left off.

        Round-robin rather than "first N": a stable sort would refetch the same
        head of the list forever and never reach the tail, which on a 300-event
        feed means most cities are never captured at all.
        """
        pool = []
        for e in events or []:
            d = self._days_out(e)
            if d is None or d < 0 or d > MAX_DAYS_OUT:
                continue
            k = self._event_key(e)
            prev = self._last.get(k)
            if prev is not None and now - prev < SAMPLE_SEC:
                continue
            pool.append(e)
        if not pool:
            return []
        if self._cursor >= len(pool):
            self._cursor = 0
        picked = pool[self._cursor:self._cursor + MAX_EVENTS]
        self._cursor += len(picked)
        return picked

    @staticmethod
    def _event_key(e):
        return f"{e.get('city')}|{e.get('date')}|{e.get('kind')}"

    # ── record ───────────────────────────────────────────────────────────────
    def _rows(self, event, books, snap_id, now):
        key = self._event_key(event)
        buckets = event.get("buckets") or []
        out = []
        for b in buckets:
            tok = b.get("token_yes") or b.get("ticker")
            bk = (books or {}).get(str(tok)) if tok else None
            if bk is None:
                continue
            bids, asks = bk.get("bid_levels") or [], bk.get("ask_levels") or []
            bid_c = bids[0][0] if bids else None
            ask_c = asks[0][0] if asks else None
            out.append({
                "ts": now.isoformat(),
                # every bucket of one ladder shares this, so a snapshot can be
                # reassembled by identity instead of by binning timestamps
                "snap_id": snap_id,
                "venue": self.venue,
                "key": key,
                "city": event.get("city"),
                "date": str(event.get("date")) if event.get("date") else None,
                "kind": event.get("kind"),
                "unit": b.get("unit") or event.get("unit"),
                "slug": event.get("slug"),
                "days_out": self._days_out(event),
                "ladder_n": len(buckets),
                "label": b.get("label"),
                "lo": b.get("lo"),
                "hi": b.get("hi"),
                "token": str(tok),
                # ── the numbers this recorder exists for ──
                "bid_c": bid_c,
                "ask_c": ask_c,
                "spread_c": (round(ask_c - bid_c, 4)
                             if bid_c is not None and ask_c is not None else None),
                "bid_size": bids[0][1] if bids else 0.0,
                "ask_size": asks[0][1] if asks else 0.0,
                "bid_depth": _depth_within(bids, DEPTH_BAND_C),
                "ask_depth": _depth_within(asks, DEPTH_BAND_C),
                "bid_levels": bids,
                "ask_levels": asks,
                "n_bid_levels": len(bids),
                "n_ask_levels": len(asks),
                # ── venue metadata ──
                # Polymarket stamps each book; Kalshi does not, so book_age_s
                # is None there rather than a plausible-looking wrong number.
                "book_ts": bk.get("book_ts"),
                "book_age_s": _age_s(bk.get("book_ts"), now),
                "market_updated": bk.get("market_updated"),
                "tick_size": bk.get("tick_size"),
                "min_order_size": bk.get("min_order_size"),
                "last_trade_c": bk.get("last_trade_c"),
                "volume": bk.get("volume"),
                "open_interest": bk.get("open_interest"),
            })
        return out

    # ── entry point ──────────────────────────────────────────────────────────
    def snapshot(self, events):
        """Fetch and record full ladders for a slice of `events`. Never raises."""
        if not ENABLED:
            return 0
        try:
            from feeds.order_books import fetch_kalshi_ladder, fetch_poly_books
        except Exception as e:  # noqa: BLE001
            self.last_error = f"import: {e}"
            return 0
        try:
            now_m = time.time()
            due = self._due(events, now_m)
            if not due:
                return 0
            fh = self._fh_for_today()
            now = datetime.now(timezone.utc)
            written = 0
            for e in due:
                if self.venue == "kalshi":
                    ev_ticker = e.get("event_ticker") or e.get("slug")
                    books = fetch_kalshi_ladder(ev_ticker)
                else:
                    toks = [b.get("token_yes") for b in (e.get("buckets") or [])]
                    books = fetch_poly_books(toks)
                # None means the call failed. Do NOT stamp the throttle, or a
                # broken venue would quietly mark every event as freshly done.
                if books is None:
                    continue
                self._last[self._event_key(e)] = now_m
                rows = self._rows(e, books, uuid.uuid4().hex[:12], now)
                for r in rows:
                    fh.write(json.dumps(r, default=str) + "\n")
                written += len(rows)
                if rows:
                    self.n_snapshots += 1
            if written:
                fh.flush()
                self.n_written += written
            self._save_state()
            return written
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("book log failed: %s", e)
            return 0

    def state(self):
        return {"enabled": ENABLED, "written": self.n_written,
                "snapshots": self.n_snapshots,
                "path": str(self.path) if self.path else str(LOG_PATH),
                "sample_sec": SAMPLE_SEC, "max_events": MAX_EVENTS,
                "max_days_out": MAX_DAYS_OUT, "last_error": self.last_error}
