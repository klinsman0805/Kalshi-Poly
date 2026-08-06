#!/usr/bin/env python3
"""
scripts/record_esports.py — Phase 0 passive recorder for the LoL esports sector.

PLACES NO ORDERS. Reads two public APIs and writes what it sees to a JSONL.
Nothing downstream of this file trades; it exists to answer three questions
that decide whether the sector is worth building at all, none of which can be
settled by reasoning:

  1. Are we the slow money?  Every snapshot carries `lag_sec` — how far behind
     real time the Riot frame we could have traded on actually was. If the
     book moves before our frames do, the whole in-play thesis is dead and no
     model can save it. This is the measurement that matters most.
  2. Does the overreaction exist?  Joined gold/kill state against the book at
     the same wall-clock instant lets us ask what price did at T+30/60/120s
     after a swing, instead of assuming.
  3. Is there anything to trade against?  `clearBookOnStart` wipes resting
     liquidity at kickoff, so in-play depth has to be observed, not inferred
     from the pre-match book.

Design notes:
  • Append-only JSONL, one record per line, flushed every write. A crash or a
    restart loses at most the current line, and analysis can run on a file the
    recorder still holds open.
  • "No livestats frames" is a NORMAL state, not an error — coverage is patchy
    and some leagues never publish. Unmatched Polymarket matches are recorded
    too, under kind="coverage", so we can measure what fraction of the sector
    is reachable at all rather than silently sampling only the easy ones.
  • Team-name matching between the two venues is fuzzy by necessity: Riot says
    "Xi'an Team WE" where Polymarket says "Team WE". Every join records the
    names it matched on so a bad pairing is auditable after the fact and never
    has to be taken on trust.

Run:  python scripts/record_esports.py
Env:  ESPORTS_RECORD_LOG (default esports_frames.jsonl)
      ESPORTS_SNAP_SEC (default 10 — livestats frame granularity)
      ESPORTS_DISCOVER_SEC (default 120)
      ESPORTS_MAX_GAMES (default 6)
      ESPORTS_MAX_MARKETS (default 3)
"""

import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv                                       # noqa: E402
load_dotenv(override=False)

from feeds.poly_esports import fetch_lol_events, fetch_book          # noqa: E402
from feeds.riot_livestats import RiotLivestatsFeed                   # noqa: E402

LOG_PATH = Path(os.getenv("ESPORTS_RECORD_LOG", "esports_frames.jsonl"))
STATE_PATH = Path(os.getenv("ESPORTS_RECORD_STATE", "esports_recorder_state.json"))
SNAP_SEC = float(os.getenv("ESPORTS_SNAP_SEC", "10"))
DISCOVER_SEC = float(os.getenv("ESPORTS_DISCOVER_SEC", "120"))
# Caps, because this shares a 512MB box with live trading bots. Six concurrent
# games x three markets x two tokens is ~36 book requests per 10s cycle, which
# is already the top of what is polite.
MAX_GAMES = int(os.getenv("ESPORTS_MAX_GAMES", "6"))
MAX_MARKETS = int(os.getenv("ESPORTS_MAX_MARKETS", "3"))

_stop = False

# Tokens that carry no identifying information across the two venues.
_NOISE = {"esports", "esport", "e-sports", "gaming", "club", "the", "gg"}


def _log(icon, msg):
    print(f"{time.strftime('%H:%M:%S')} {icon} {msg}", flush=True)


def _norm(name):
    """Normalised token set for a team name."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return {t for t in s.split() if t and t not in _NOISE}


def _team_match(a, b):
    """Do these two team names plausibly denote the same team?

    Subset and overlap matching are only trusted when the SMALLER name still
    carries two distinct tokens. Otherwise "G2 Esports" — which normalises to
    the single token {g2} once the noise words go — swallows "G2 NORD", a
    different team in a different league. A wrong join here is worse than a
    missed one: it silently pairs one match's book with another match's game
    state, and nothing downstream can detect it.
    """
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    if min(len(ta), len(tb)) < 2:
        return False
    if ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.5


def _pair_match(poly_teams, riot_teams):
    """True if the two team pairs are the same fixture, in either order."""
    if len(poly_teams) < 2 or len(riot_teams) < 2:
        return False
    p1, p2 = poly_teams[0], poly_teams[1]
    r1, r2 = riot_teams[0], riot_teams[1]
    return ((_team_match(p1, r1) and _team_match(p2, r2)) or
            (_team_match(p1, r2) and _team_match(p2, r1)))


class Recorder:
    def __init__(self):
        self.riot = RiotLivestatsFeed(on_log=_log)
        self.fh = LOG_PATH.open("a", encoding="utf-8")
        self.pairs = []            # active poly<->riot joins
        self.last_discover = 0.0
        self.n_records = 0
        self.n_frames = 0          # snapshots that carried a real Riot frame
        self.started = datetime.now(timezone.utc).isoformat()

    # ── output ───────────────────────────────────────────────────────────────
    def emit(self, rec):
        rec["ts"] = datetime.now(timezone.utc).isoformat()
        self.fh.write(json.dumps(rec, default=str) + "\n")
        self.fh.flush()
        self.n_records += 1

    def write_state(self):
        try:
            st = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "started": self.started,
                "records": self.n_records,
                "frames_captured": self.n_frames,
                "active_pairs": [
                    {"title": p["poly"]["title"], "league": p["riot"]["league"],
                     "game_id": p.get("game_id"), "game_number": p.get("game_number"),
                     "last_lag_sec": p.get("last_lag_sec")}
                    for p in self.pairs],
                "log": str(LOG_PATH),
                "log_bytes": LOG_PATH.stat().st_size if LOG_PATH.exists() else 0,
            }
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except Exception as e:  # noqa: BLE001
            _log("✗", f"state write failed: {e}")

    # ── discovery ────────────────────────────────────────────────────────────
    def discover(self):
        """Rebuild the poly<->riot pairing. Cheap enough to redo wholesale."""
        try:
            poly = fetch_lol_events()
        except Exception as e:  # noqa: BLE001
            _log("✗", f"poly discovery failed: {e}")
            return
        try:
            riot_live = self.riot.live_matches()
        except Exception as e:  # noqa: BLE001
            _log("✗", f"riot discovery failed: {e}")
            riot_live = []

        pairs, unmatched = [], []
        for pe in poly:
            names = pe["teams"] or pe["title_teams"]
            hit = next((rm for rm in riot_live if _pair_match(names, rm["teams"])), None)
            if not hit:
                unmatched.append({"title": pe["title"], "teams": names,
                                  "live": pe["live"], "period": pe["period"],
                                  "hours_since_start": pe["hours_since_start"]})
                continue
            games = []
            try:
                games = self.riot.games_for_match(hit["match_id"])
            except Exception as e:  # noqa: BLE001
                _log("✗", f"games lookup failed for {hit['match_id']}: {e}")
            g = next((x for x in games if x["state"] == "inProgress"), None)
            pairs.append({
                "poly": pe, "riot": hit,
                "game_id": g["game_id"] if g else None,
                "game_number": g["number"] if g else None,
                "matched_on": {"poly": names, "riot": hit["teams"]},
                "last_lag_sec": None,
            })

        self.pairs = pairs[:MAX_GAMES]
        self.emit({"kind": "coverage",
                   "poly_events_in_window": len(poly),
                   "riot_live_matches": len(riot_live),
                   "matched": len(pairs),
                   "unmatched_poly": unmatched,
                   "riot_live": [{"league": r["league"], "teams": r["teams"]}
                                 for r in riot_live]})
        _log("→", f"discovery — poly={len(poly)} riot_live={len(riot_live)} "
                  f"matched={len(pairs)} recording={len(self.pairs)}")

    # ── snapshot ─────────────────────────────────────────────────────────────
    def snapshot(self):
        for p in self.pairs:
            pe = p["poly"]
            frame_info = None
            if p.get("game_id"):
                try:
                    frame_info = self.riot.latest_frame(p["game_id"])
                except Exception as e:  # noqa: BLE001
                    _log("✗", f"frame fetch failed: {e}")
            if frame_info:
                p["last_lag_sec"] = frame_info["lag_sec"]
                self.n_frames += 1

            books = []
            for m in pe["markets"][:MAX_MARKETS]:
                for tok, outcome in zip(m["token_ids"], m["outcomes"]):
                    b = fetch_book(tok)
                    if not b:
                        continue
                    books.append({
                        "question": m["question"],
                        "game_number": m["game_number"],
                        "outcome": outcome,
                        "token_id": tok,
                        "tick_size": m["tick_size"],
                        "seconds_delay": m["seconds_delay"],
                        **b,
                    })

            self.emit({
                "kind": "snap",
                "poly_event_id": pe["event_id"],
                "title": pe["title"],
                "teams": pe["teams"],
                "live": pe["live"],
                "period": pe["period"],
                "score": pe["score"],
                "game_start": pe["game_start"],
                "riot_match_id": p["riot"]["match_id"],
                "riot_league": p["riot"]["league"],
                "riot_game_id": p.get("game_id"),
                "riot_game_number": p.get("game_number"),
                "matched_on": p["matched_on"],
                # None here means the game has no livestats coverage yet or at
                # all — deliberately distinct from a frame with zeroed counters,
                # which means the game genuinely just started.
                "riot_lag_sec": frame_info["lag_sec"] if frame_info else None,
                "riot_requested_back_sec": (frame_info["requested_back_sec"]
                                            if frame_info else None),
                "riot_state": (self.riot.summarize(frame_info["frame"])
                               if frame_info else None),
                "books": books,
            })


def main():
    global _stop

    def _on_sig(signum, frame):
        global _stop
        _stop = True
        _log("◆", "signal received — finishing current cycle and exiting")

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    rec = Recorder()
    _log("▶", f"esports recorder up — log={LOG_PATH} snap={SNAP_SEC}s "
              f"discover={DISCOVER_SEC}s · PLACES NO ORDERS")

    while not _stop:
        cycle_start = time.time()
        try:
            if cycle_start - rec.last_discover > DISCOVER_SEC:
                rec.discover()
                rec.last_discover = cycle_start
            if rec.pairs:
                rec.snapshot()
                lags = [p["last_lag_sec"] for p in rec.pairs if p["last_lag_sec"] is not None]
                lag_txt = (f"lag {min(lags):.0f}-{max(lags):.0f}s" if lags
                           else "no livestats frames")
                _log("·", f"{len(rec.pairs)} game(s) · {lag_txt} · {rec.n_records} recs")
            rec.write_state()
        except Exception as e:  # noqa: BLE001
            _log("✗", f"cycle error: {e}")
        time.sleep(max(1.0, SNAP_SEC - (time.time() - cycle_start)))

    rec.write_state()
    rec.fh.close()
    _log("■", f"stopped — {rec.n_records} records written to {LOG_PATH}")


if __name__ == "__main__":
    main()
