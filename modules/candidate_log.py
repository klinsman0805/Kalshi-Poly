"""
modules/candidate_log.py — shadow feature recorder for the weather sector.

PLACES NO ORDERS AND CHANGES NO DECISION. It observes the rows the engine has
already scored and writes a feature vector for each one to a JSONL, so that a
confidence model can later be fitted and — more importantly — *calibrated*
against outcomes.

Why this exists at all. The live model_p averaged 0.956 across 45 settled
entries that won 62.2% of the time; its Brier score (0.3516) is worse than a
coin flip and worse than always predicting the base rate (0.2351). Replacing it
needs labelled data, and 45 trades cannot support a fitted model — that is the
sample size at which this project has twice found a pattern that inverted on
re-check.

The fix is to record every market that reaches the scoring stage rather than
only the handful that become trades. Blocked and skipped candidates are the
counterfactuals: without them the sample is conditioned on the bot's own gates
and cannot say whether those gates are helping.

Two things this deliberately does NOT do:

  • It writes no label. The outcome that matters is the SETTLEMENT value, not
    our observed extreme, and the two diverge (settlement clock, sensor
    rounding, CLI quality control). Every record therefore carries the
    identifiers needed to resolve the true outcome later — slug, ticker,
    station, settlement date, bucket bounds — and labelling is a separate pass
    against the venue's own resolution source.

  • It never raises into the caller. A recorder fault must never disturb
    trading, so observe() swallows everything.

Env:
  WEATHER_CANDIDATE_LOG          default candidate_data/candidates.jsonl
  WEATHER_CANDIDATE_SAMPLE_SEC   default 900 — per-market throttle
  WEATHER_CANDIDATE_LOG_ENABLED  default true
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("modules.candidate_log")

ENABLED = os.getenv("WEATHER_CANDIDATE_LOG_ENABLED", "true").strip().lower() == "true"
LOG_PATH = Path(os.getenv("WEATHER_CANDIDATE_LOG", "candidate_data/candidates.jsonl"))
SAMPLE_SEC = float(os.getenv("WEATHER_CANDIDATE_SAMPLE_SEC", "900"))


def _path_for(day):
    """One file per UTC day, so finished days can be compressed by cron without
    touching the file this process still holds open."""
    return LOG_PATH.with_name(f"{LOG_PATH.stem}-{day}{LOG_PATH.suffix}")


class CandidateLogger:
    """Throttled feature recorder. One instance per executor."""

    def __init__(self, venue, on_log=None):
        self.venue = venue
        self.on_log = on_log or (lambda i, m: None)
        self.day = None
        self.fh = None
        self.path = None
        self._last = {}          # key -> (monotonic, signal) of the last write
        self.n_written = 0
        self.last_error = None

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

    # ── feature extraction ───────────────────────────────────────────────────
    @staticmethod
    def _chosen_bucket(row):
        """The bucket the engine actually scored, with its live quote."""
        label = row.get("best_label")
        for b in row.get("buckets") or []:
            if b.get("label") == label:
                return b
        return {}

    @staticmethod
    def _hours_left(executor, row):
        """Hours until the SETTLEMENT source's day closes.

        Not the same as hours left on the wall clock: Kalshi's CLI day runs on
        local standard time, so in summer it closes an hour after the local
        calendar date rolls over. That extra hour is where both of August's
        large Kalshi losses were made, which makes this one of the more
        promising features in the set.
        """
        try:
            today = executor._settlement_today(row)
            mkt = row.get("date")
            if not today or not mkt:
                return None
            d_today = datetime.fromisoformat(today).date()
            d_mkt = datetime.fromisoformat(mkt).date()
            if d_today > d_mkt:
                return 0.0                    # window already closed
            tzname = row.get("station_tz")
            if not tzname:
                return None
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tzname)
            now = datetime.now(timezone.utc)
            local = now.astimezone(tz)
            off = local.utcoffset()
            if executor.SETTLEMENT_STANDARD_TIME:
                off = off - (local.dst() or timedelta(0))
            # midnight ending the market's day, expressed back in UTC
            end_local_naive = datetime.combine(d_mkt, datetime.min.time()) + timedelta(days=1)
            end_utc = end_local_naive.replace(tzinfo=timezone.utc) - off
            return round((end_utc - now).total_seconds() / 3600.0, 3)
        except Exception:  # noqa: BLE001
            return None

    def _record(self, executor, row):
        b = self._chosen_bucket(row)
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "venue": self.venue,
            # ── identity, enough to resolve the true outcome later ──
            "key": f"{row.get('city')}|{row.get('date')}|{row.get('kind')}",
            "city": row.get("city"),
            "date": row.get("date"),
            "kind": row.get("kind"),
            "unit": row.get("unit"),
            "station": row.get("station"),
            "station_tz": row.get("station_tz"),
            "station_local_date": row.get("station_local_date"),
            "settlement_date": executor._settlement_today(row),
            "slug": row.get("slug"),
            "label": row.get("best_label"),
            "lo": b.get("lo"),
            "hi": b.get("hi"),
            "token_yes": b.get("token_yes") or b.get("ticker"),
            # ── the engine's own verdict, so gates can be studied ──
            "signal": row.get("signal"),
            "why": row.get("why"),
            "group": row.get("group"),
            "tradeable": row.get("tradeable"),
            # ── model ──
            "model_p": row.get("best_p"),
            "edge_c": b.get("edge_c"),
            "fee_c": b.get("fee_c"),
            # ── market ──
            "ask_c": b.get("ask_c"),
            "bid_c": b.get("bid_c"),
            "spread_c": (round(b["ask_c"] - b["bid_c"], 2)
                         if b.get("ask_c") is not None and b.get("bid_c") is not None
                         else None),
            "shares_planned": b.get("shares_planned"),
            "book_depth": b.get("book_depth"),
            # ── observation ──
            "ext_c": row.get("ext_c"),
            "temp_c": row.get("temp_c"),
            "ext_age_min": row.get("ext_age_min"),
            "obs_today": row.get("obs_today"),
            "local_hour": row.get("local_hour"),
            "decline": executor._decline_deg(row),
            "hours_left_settlement": self._hours_left(executor, row),
            # ── the two blocks already collected and still unused by any gate ──
            "conditions": row.get("conditions"),
            "ensemble": row.get("ensemble"),
        }

    # ── entry point ──────────────────────────────────────────────────────────
    def observe(self, rows, executor):
        """Record scoreable rows. Never raises."""
        if not ENABLED:
            return 0
        try:
            now = time.monotonic()
            written = 0
            fh = self._fh_for_today()
            for row in rows or []:
                # Only rows the engine actually scored: a model probability and
                # a real quote. Everything else has no features to learn from.
                if row.get("best_p") is None:
                    continue
                b = self._chosen_bucket(row)
                if b.get("ask_c") is None:
                    continue
                key = f"{row.get('city')}|{row.get('date')}|{row.get('kind')}"
                prev = self._last.get(key)
                # Throttle per market, but never skip a change of verdict — the
                # moment a market crosses into or out of ENTER is exactly the
                # decision boundary the model has to reproduce.
                if prev and row.get("signal") == prev[1] and now - prev[0] < SAMPLE_SEC:
                    continue
                fh.write(json.dumps(self._record(executor, row), default=str) + "\n")
                self._last[key] = (now, row.get("signal"))
                written += 1
            if written:
                fh.flush()
                self.n_written += written
            return written
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("candidate log failed: %s", e)
            return 0

    def state(self):
        return {"enabled": ENABLED, "written": self.n_written,
                "path": str(self.path) if self.path else str(LOG_PATH),
                "sample_sec": SAMPLE_SEC, "last_error": self.last_error}
