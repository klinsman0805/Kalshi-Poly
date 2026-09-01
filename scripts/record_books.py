#!/usr/bin/env python3
"""
scripts/record_books.py — capture full order book ladders. Measurement only.

Runs as its own short-lived process, deliberately NOT inside the trading bot.
The bot holds live positions on a 458MB box that already peaks at 434MB of
swap; adding per-cycle HTTP fan-out to it would trade a real risk for a
measurement convenience. Nothing here imports the executor, and no code on the
trading path imports this.

What it is for. Three questions the existing capture cannot answer:

  1. Is the bid real?  Selling gate-refused rows at their recorded bid
     backtests at +9.4c/share, +22.3c after fees. But `book_depth` was recorded
     on 0 of 1,608 EARLY rows, because it is only populated for rows that reach
     _book_confirm — which happens on ENTER alone. A one-share bid produces the
     identical backtest and is worth nothing.

  2. Do the ladders sum?  Buckets are mutually exclusive and exhaustive, so
     asks should sum a little over 100c. Testing that needs a whole ladder at
     one instant, which the candidate log never stored.

  3. Is the edge 3-5 days out, as the published work claims?  Our capture is
     almost empty there because the engine drops events more than a day ahead.

Usage:
    ./venv/bin/python scripts/record_books.py            # one slice, both venues
    ./venv/bin/python scripts/record_books.py --venue poly
    ./venv/bin/python scripts/record_books.py --once --verbose

Intended to run from cron every 15 minutes. State (per-event throttle and the
round-robin cursor) persists in candidate_data/book_state-<venue>.json so
successive runs advance through the feed instead of re-fetching its head.
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")

from modules.book_log import BookLogger, LOG_PATH                 # noqa: E402

VENUES = ("poly", "kalshi")


def _events(venue):
    """The venue's live event list, in the shape BookLogger expects."""
    if venue == "poly":
        from feeds.poly_weather import fetch_temperature_events
    else:
        from feeds.kalshi_weather import fetch_temperature_events
    return fetch_temperature_events()


def run(venue, verbose=False):
    state = LOG_PATH.with_name(f"book_state-{venue}.json")
    logger = BookLogger(venue, state_path=state)
    try:
        events = _events(venue)
    except Exception as e:  # noqa: BLE001
        print(f"{venue:7s} FEED FAILED: {e}")
        return 0
    n = logger.snapshot(events)
    logger.close()
    st = logger.state()
    print(f"{venue:7s} events={len(events):4d}  snapshots={st['snapshots']:3d}  "
          f"rows={n:4d}  -> {st['path']}")
    if st["last_error"]:
        print(f"        last_error: {st['last_error']}")
    if verbose and n:
        _summarise(st["path"], venue)
    return n


def _summarise(path, venue):
    """Print what the slice actually caught. The point of this recorder is
    depth, so lead with whether there was any."""
    import json
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("venue") == venue:
                rows.append(r)
    if not rows:
        return
    rows = rows[-400:]
    two_sided = sum(1 for r in rows if r.get("bid_c") is not None
                    and r.get("ask_c") is not None)
    no_bid = sum(1 for r in rows if not r.get("n_bid_levels"))
    sizes = sorted(r["bid_size"] for r in rows if r.get("bid_size"))
    print(f"        {len(rows)} buckets: {two_sided} two-sided, "
          f"{no_bid} with NO BID at all")
    if sizes:
        print(f"        bid size at the touch: min {sizes[0]:.0f}  "
              f"median {sizes[len(sizes)//2]:.0f}  max {sizes[-1]:.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", choices=VENUES, default=None,
                    help="default: both")
    ap.add_argument("--verbose", action="store_true",
                    help="summarise depth in the slice just captured")
    ap.add_argument("--quiet", action="store_true", help="suppress log noise")
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR if args.quiet else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    total = 0
    for v in ([args.venue] if args.venue else VENUES):
        total += run(v, verbose=args.verbose)
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
