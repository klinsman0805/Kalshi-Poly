"""
feeds/riot_livestats.py — LoL esports in-game state feed (lolesports.com).

Polymarket's LoL markets expose `live`, `period` and `score`, but the score is
SERIES state ("0-0|Bo3", which game of the BO3) — there is no gold, no kills,
no objectives. Every in-play signal has to come from here instead.

Two hosts, both public and unauthenticated in practice:

  esports-api.lolesports.com  — schedule / live / event details. Needs the
      long-published web x-api-key below (it is the key the lolesports site
      itself ships in its JS bundle; it is not a secret and not tied to us).
  feed.lolesports.com         — the livestats frames: 10-second granularity
      gold/kills/towers/inhibitors/barons/dragons per side.

Things learned the hard way probing this API, all of which the code depends on:

  • Schedule events have NO top-level `id`. The id that getEventDetails wants
    is `event["match"]["id"]`. Using `event["id"]` returns KeyError on schedule
    rows and silently empty `games` if you paper over it.
  • window/<gameId> with no startingTime returns the FIRST 10 frames of the
    game — every counter zero. That looks like "the feed is broken" and is not;
    you have to ask for a time.
  • startingTime must be snapped DOWN to a 10-second boundary and formatted
    exactly %Y-%m-%dT%H:%M:%SZ. Anything else 404s.
  • A request for a time before the game's first frame returns 204 with an
    empty body, not an error. Scheduled start time is NOT game start — draft
    runs several minutes — so early polls legitimately return 204 for a while.
  • Coverage is not universal. Some leagues carry no livestats at all; a match
    can be live on Polymarket with no frames here, forever. Callers must treat
    "no frames" as a normal steady state, not a failure to retry around.

Everything here is read-only. No credentials, no orders.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("feeds.riot_livestats")

# Public web key shipped in the lolesports.com JS bundle. Not a secret, not
# ours, and not rate-limited per-account — but be polite anyway.
API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
ESPORTS_API = "https://esports-api.lolesports.com/persisted/gw"
FEED = "https://feed.lolesports.com/livestats/v1"
TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0"}

# How far back to search for the newest available live frame. The feed runs
# behind real time by an amount we are explicitly trying to MEASURE, so this
# has to be a search, not a constant. 300s is generous; if nothing is found
# inside it the game almost certainly has no livestats coverage.
MAX_LAG_SEARCH_SEC = 300
LAG_STEP_SEC = 10

# Backoff after a cold search finds nothing at all: 30s, 60s, 120s ... capped.
MISS_BACKOFF_BASE_SEC = 30.0
MISS_BACKOFF_MAX_SEC = 600.0


def _snap10(t):
    """Snap a datetime down to a 10s boundary in the exact format the feed wants."""
    t = t.replace(microsecond=0)
    return t.replace(second=(t.second // 10) * 10).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class RiotLivestatsFeed:
    """Read-only view of live LoL esports matches and their in-game frames."""

    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self._s = requests.Session()
        self._s.headers.update(UA)
        self.last_error = None
        # gameId -> the lag (seconds) at which we last found a frame. Starting
        # the next search from the last known lag instead of from zero turns a
        # ~15-request walk into ~1-2 requests per poll.
        self._lag_hint = {}
        # gameId -> (consecutive_misses, earliest_next_attempt_monotonic).
        # A game with no coverage costs a full 30-request walk EVERY poll
        # otherwise — and "no coverage" is a steady state that can last the
        # whole match, so that is 30 requests every 10s, forever, per game.
        # Back off instead: draft phases resolve in minutes, dead leagues never.
        self._miss = {}

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _get(self, url, key=False, params=None):
        """Returns (status_code, parsed_json_or_None). 204/404 are normal here."""
        headers = {"x-api-key": API_KEY} if key else None
        try:
            r = self._s.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("GET %s failed: %s", url, e)
            return None, None
        if r.status_code != 200 or not r.content:
            return r.status_code, None
        try:
            return 200, r.json()
        except ValueError:
            return r.status_code, None

    # ── schedule / live ──────────────────────────────────────────────────────
    def live_matches(self):
        """Matches lolesports currently reports as in progress.

        Returns [{match_id, league, teams:[a,b], state, block}].
        """
        _, d = self._get(f"{ESPORTS_API}/getLive", key=True, params={"hl": "en-US"})
        events = (((d or {}).get("data") or {}).get("schedule") or {}).get("events") or []
        out = []
        for e in events:
            m = e.get("match") or {}
            if not m.get("id"):
                continue
            out.append({
                "match_id": str(m["id"]),
                "league": ((e.get("league") or {}).get("name") or "").strip(),
                "teams": [(t.get("name") or "").strip() for t in (m.get("teams") or [])],
                "state": e.get("state"),
                "block": e.get("blockName"),
                "start_time": e.get("startTime"),
            })
        return out

    def schedule(self):
        """Recent + upcoming matches. Same shape as live_matches()."""
        _, d = self._get(f"{ESPORTS_API}/getSchedule", key=True, params={"hl": "en-US"})
        events = (((d or {}).get("data") or {}).get("schedule") or {}).get("events") or []
        out = []
        for e in events:
            m = e.get("match") or {}
            if not m.get("id"):
                continue
            out.append({
                "match_id": str(m["id"]),
                "league": ((e.get("league") or {}).get("name") or "").strip(),
                "teams": [(t.get("name") or "").strip() for t in (m.get("teams") or [])],
                "state": e.get("state"),
                "block": e.get("blockName"),
                "start_time": e.get("startTime"),
            })
        return out

    def games_for_match(self, match_id):
        """Games in a match: [{game_id, number, state}]. state is
        unstarted | inProgress | completed | unneeded."""
        _, d = self._get(f"{ESPORTS_API}/getEventDetails", key=True,
                         params={"hl": "en-US", "id": match_id})
        games = ((((d or {}).get("data") or {}).get("event") or {})
                 .get("match") or {}).get("games") or []
        return [{"game_id": str(g.get("id")), "number": g.get("number"),
                 "state": g.get("state")} for g in games if g.get("id")]

    # ── frames ───────────────────────────────────────────────────────────────
    def window(self, game_id, at=None):
        """One livestats window (up to 10 frames) ending at `at` (UTC datetime).

        Returns (frames, status). status is one of:
          "ok"        frames present
          "no_data"   204 — asked for a time outside the game's frame range
          "not_found" 404 — bad game id, or time far outside the game
          "error"     transport failure
        """
        params = {"startingTime": _snap10(at)} if at else None
        code, d = self._get(f"{FEED}/window/{game_id}", params=params)
        if code == 200 and d:
            return (d.get("frames") or []), "ok"
        if code == 204:
            return [], "no_data"
        if code == 404:
            return [], "not_found"
        return [], "error"

    def latest_frame(self, game_id):
        """Newest frame currently published for a live game, with its measured lag.

        Returns None when the game has no livestats coverage or has not started
        producing frames yet — both normal and both indistinguishable from here.

        The returned `lag_sec` is the whole point of this feed for now: how far
        behind real time the data we could actually trade on is.
        """
        hint = self._lag_hint.get(game_id)
        if hint is None:
            # No hint means any attempt is a full walk. Honour the backoff.
            misses, next_try = self._miss.get(game_id, (0, 0.0))
            if misses and time.monotonic() < next_try:
                return None

        now = datetime.now(timezone.utc)
        # Search near last known lag first, then widen. Lag drifts slowly, so
        # the hint is usually right and this costs one request.
        starts = []
        if hint is not None:
            starts = [hint, hint + LAG_STEP_SEC, max(LAG_STEP_SEC, hint - LAG_STEP_SEC)]
        starts += list(range(LAG_STEP_SEC, MAX_LAG_SEARCH_SEC + 1, LAG_STEP_SEC))

        seen = set()
        for back in starts:
            if back in seen or back <= 0:
                continue
            seen.add(back)
            frames, status = self.window(game_id, now - timedelta(seconds=back))
            if status == "error":
                return None
            if status != "ok" or not frames:
                continue
            f = frames[-1]
            fts = _parse_ts(f.get("rfc460Timestamp"))
            if not fts:
                continue
            self._lag_hint[game_id] = back
            self._miss.pop(game_id, None)
            return {"frame": f,
                    "frame_ts": fts.isoformat(),
                    "lag_sec": round((datetime.now(timezone.utc) - fts).total_seconds(), 1),
                    "requested_back_sec": back}

        # Nothing anywhere in the window. Drop any stale hint so the next
        # attempt is honestly treated as a cold search, and back off: 30s,
        # 60s, 120s ... capped at 10 minutes.
        self._lag_hint.pop(game_id, None)
        misses = self._miss.get(game_id, (0, 0.0))[0] + 1
        delay = min(MISS_BACKOFF_BASE_SEC * (2 ** (misses - 1)), MISS_BACKOFF_MAX_SEC)
        self._miss[game_id] = (misses, time.monotonic() + delay)
        return None

    @staticmethod
    def summarize(frame):
        """Flatten a livestats frame to the fields a win-probability model needs."""
        if not frame:
            return None
        b, r = frame.get("blueTeam") or {}, frame.get("redTeam") or {}

        def side(t):
            return {
                "gold": t.get("totalGold"),
                "kills": t.get("totalKills"),
                "towers": t.get("towers"),
                "inhibitors": t.get("inhibitors"),
                "barons": t.get("barons"),
                "dragons": len(t.get("dragons") or []),
            }

        blue, red = side(b), side(r)
        gold_diff = None
        if blue["gold"] is not None and red["gold"] is not None:
            gold_diff = blue["gold"] - red["gold"]
        return {
            "game_state": frame.get("gameState"),
            "frame_ts": frame.get("rfc460Timestamp"),
            "blue": blue,
            "red": red,
            "gold_diff_blue": gold_diff,
        }
