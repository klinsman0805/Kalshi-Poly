"""
feeds/livescore.py — ESPN soccer live scores (free, no key).

Enables the in-play (opt 1) and news-latency (opt 2) features: tells us which
fixtures are LIVE, the score/clock, and — by diffing successive polls — when a
GOAL just happened. A goal is the "news" event whose repricing lag across venues
is the edge.

ESPN endpoint per league:
  https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard
"""

import logging
import time

import requests

from feeds.poly_soccer import normalize_team

log = logging.getLogger("feeds.livescore")

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/{lg}/scoreboard"
# Leagues to watch (WC + top divisions + UCL/MLS). Kept small to bound API calls.
LEAGUES = [
    "fifa.world", "uefa.champions", "eng.1", "esp.1", "ita.1", "ger.1",
    "fra.1", "usa.1", "mex.1", "bra.1",
]


def _parse_event(ev, league):
    comp = (ev.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    home = away = None
    sh = sa = None
    for c in comps:
        name = (c.get("team") or {}).get("displayName")
        try:
            score = int(c.get("score")) if c.get("score") not in (None, "") else None
        except (TypeError, ValueError):
            score = None
        if c.get("homeAway") == "home":
            home, sh = name, score
        else:
            away, sa = name, score
    stype = (ev.get("status") or {}).get("type") or {}
    return {
        "league": league,
        "home": home, "away": away,
        "home_n": normalize_team(home), "away_n": normalize_team(away),
        "state": stype.get("state"),           # pre | in | post
        "detail": stype.get("detail"),         # "23'", "HT", "FT", kickoff time
        "clock": (ev.get("status") or {}).get("displayClock"),
        "score_home": sh, "score_away": sa,
    }


def _fetch_league(lg, sess):
    try:
        r = sess.get(ESPN.format(lg=lg), timeout=8)
        r.raise_for_status()
        return [_parse_event(e, lg) for e in r.json().get("events", [])]
    except Exception as e:  # noqa: BLE001
        log.debug("espn %s: %s", lg, e)
        return []


class LiveScoreFeed:
    """Polls ESPN; remembers last score per fixture to surface GOAL events."""

    def __init__(self, leagues=None):
        self.leagues = leagues or LEAGUES
        self._scores = {}   # key -> (sh, sa)
        self._matches = []

    @staticmethod
    def key(home_n, away_n):
        return tuple(sorted([home_n or "", away_n or ""]))

    def refresh(self):
        """Returns (matches, goal_events). goal_events fire when a score changes."""
        sess = requests.Session()
        matches, goals = [], []
        for lg in self.leagues:
            matches.extend(_fetch_league(lg, sess))
        for m in matches:
            if m["state"] != "in":
                continue
            k = self.key(m["home_n"], m["away_n"])
            cur = (m["score_home"], m["score_away"])
            prev = self._scores.get(k)
            if prev is not None and cur != prev and None not in cur:
                side = "home" if cur[0] > prev[0] else "away"
                goals.append({
                    "key": k, "home": m["home"], "away": m["away"],
                    "scorer_side": side, "score": f"{cur[0]}-{cur[1]}",
                    "prev": f"{prev[0]}-{prev[1]}", "clock": m["clock"],
                    "ts": time.time(),
                })
            if None not in cur:
                self._scores[k] = cur
        self._matches = matches
        return matches, goals

    def find(self, home_n, away_n):
        """Live/score state for a fixture, matched by normalized team names."""
        def eq(a, b):
            ta, tb = set((a or "").split()), set((b or "").split())
            if not ta or not tb:
                return False
            shared = ta & tb
            return shared == ta or shared == tb
        for m in self._matches:
            if (eq(m["home_n"], home_n) and eq(m["away_n"], away_n)) or \
               (eq(m["home_n"], away_n) and eq(m["away_n"], home_n)):
                return m
        return None
