"""
modules/soccer.py — World Cup cross-venue soccer signals (monitor + dry-run).

Two layers:
  (b) RESOLUTION AUDIT — before comparing prices, check the two venues are
      betting on the same thing. Kalshi KXWCGAME settles on the REGULATION-time
      result (a Tie is payable). Polymarket is only comparable when it also
      offers a Draw; if it has no Draw (or its rules invoke extra-time/penalties
      /advancement) it's a full-match bet → BASIS-RISK, prices not comparable.
  (a) FAIR VALUE — only on SAFE matches: de-vig each venue's three asks into a
      true probability, blend them (liquidity-weighted) into a "fair" line, and
      score each venue's price as edge = fair - ask. Positive edge = underpriced
      = the side to buy.

(a) is gated by (b): we never score edge on a BASIS-RISK match, because a gap
there is an artifact of different rules, not a mispricing — the same basis-risk
trap that sank the crypto arb.

Dry-run = SIGNAL LOGGING only (matches settle days later): detected edges are
appended to a log for later review against actual results.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from feeds import kalshi_soccer, poly_soccer
from feeds.livescore import LiveScoreFeed
from feeds.poly_soccer import normalize_team as _norm

log = logging.getLogger("modules.soccer")

SIGNAL_LOG = Path(os.getenv("SOCCER_SIGNAL_LOG", "soccer_signals.jsonl"))
VALUE_EDGE_CENTS = float(os.getenv("SOCCER_VALUE_EDGE_CENTS", "3"))      # |fair_blend - ask|
DRAW_ABS_EDGE_CENTS = float(os.getenv("SOCCER_DRAW_ABS_EDGE_CENTS", "3"))  # model_draw - ask
MODEL_TOTAL_GOALS = float(os.getenv("SOCCER_MODEL_TOTAL_GOALS", "2.6"))   # WC avg goals/match
# opt 1/2/3 config
BROADEN = os.getenv("SOCCER_BROADEN", "true").strip().lower() == "true"   # opt 3: Poly-only leagues
RELEVANT_HOURS = float(os.getenv("SOCCER_RELEVANT_HOURS", "8"))           # broaden window
GOAL_WINDOW_SECS = float(os.getenv("SOCCER_GOAL_WINDOW_SECS", "180"))     # news-latency reaction window
NEWS_LAG_EDGE_CENTS = float(os.getenv("SOCCER_NEWS_LAG_EDGE_CENTS", "3")) # venue gap post-goal
# Fixed-total Poisson is unreliable for draws in blowouts; only trust the
# absolute draw signal when neither side is a runaway favourite.
MODEL_MAX_FAV = float(os.getenv("SOCCER_MODEL_MAX_FAV", "0.70"))
# Words in Poly's resolution text that mean "decided beyond regulation" → basis risk.
KNOCKOUT_KEYS = ("advance", "advances", "progress", "penalty shoot", "penalties", "extra time")


def _team_eq(a, b):
    """Same team if the smaller token set is contained in the larger
    ('Congo DR'/'DR Congo', 'Bosnia'/'Bosnia and Herzegovina')."""
    ta, tb = set((a or "").split()), set((b or "").split())
    if not ta or not tb:
        return False
    shared = ta & tb
    return shared == ta or shared == tb


def _teams_match(k, p):
    direct = _team_eq(k["home_n"], p["home_n"]) and _team_eq(k["away_n"], p["away_n"])
    swapped = _team_eq(k["home_n"], p["away_n"]) and _team_eq(k["away_n"], p["home_n"])
    return direct or swapped


def _resolution_audit(pm):
    """(b) Returns (safe, reason). safe=None when there's no Poly to compare."""
    if pm is None:
        return None, "no Poly link — single venue only"
    if not pm.get("has_draw"):
        return False, "Poly has no Draw outcome → resolves on full match"
    txt = (pm.get("rules") or "").lower()
    hit = next((k for k in KNOCKOUT_KEYS if k in txt), None)
    if hit:
        return False, f"Poly rules mention '{hit}' → beyond-regulation resolution"
    return True, "both resolve on the regulation result (Draw payable)"


def _devig(prices):
    """Normalise the three asks into a probability distribution (sums to 1).
    Requires all three present, else None (a partial de-vig is misleading)."""
    vals = {o: prices.get(o) for o in ("home", "draw", "away")}
    if any(v is None for v in vals.values()):
        return None
    s = sum(vals.values())
    if s <= 0:
        return None
    return {o: v / s for o, v in vals.items()}


def _fair(k, pm, safe):
    """(a) Liquidity-weighted blend of the two de-vigged lines. Returns (fair, wp)."""
    if not safe:
        return None, None
    kdev = _devig(k["prices"])
    pdev = _devig((pm or {}).get("prices") or {})
    if kdev and pdev:
        kl = k.get("liq") or 0.0
        pl = (pm or {}).get("liquidity") or 0.0
        wp = pl / (pl + kl) if (pl + kl) > 0 else 0.5
        wp = min(0.8, max(0.2, wp))  # clamp so one venue can't fully dominate
        return {o: (1 - wp) * kdev[o] + wp * pdev[o] for o in kdev}, round(wp, 2)
    if pdev:
        return pdev, 1.0
    if kdev:
        return kdev, 0.0
    return None, None


# ── (a-2) Absolute base-rate model ───────────────────────────────────────────
# Independent draw reference: a draw is mostly a function of how evenly matched
# the teams are. We take ONLY the home/away asks (ignoring the draw price), read
# off the two-way strength, then fit a Poisson goals model (fixed total goals)
# whose home-win share matches it — and read the model's DRAW probability back
# out. That draw number never saw the market's draw quote, so if the quote is
# mispriced (retail underpricing the draw), the model exposes it.

def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _match_probs(lam_h, lam_a, maxg=8):
    ph = pd = pa = 0.0
    hv = [_pois(i, lam_h) for i in range(maxg + 1)]
    av = [_pois(j, lam_a) for j in range(maxg + 1)]
    for i in range(maxg + 1):
        for j in range(maxg + 1):
            p = hv[i] * av[j]
            if i > j:
                ph += p
            elif i == j:
                pd += p
            else:
                pa += p
    return ph, pd, pa


def _model_line(home_ask, away_ask, total_goals=MODEL_TOTAL_GOALS):
    """Poisson H/D/A line from the two-way (draw-excluded) home/away strength."""
    if home_ask is None or away_ask is None or (home_ask + away_ask) <= 0:
        return None
    ph2 = home_ask / (home_ask + away_ask)  # market two-way home prob
    lo, hi = 0.05, 0.95
    for _ in range(34):  # bisection on the goal split
        rho = (lo + hi) / 2
        mh, _md, ma = _match_probs(total_goals * rho, total_goals * (1 - rho))
        m2 = mh / (mh + ma) if (mh + ma) > 0 else 0.5
        if m2 < ph2:
            lo = rho
        else:
            hi = rho
    rho = (lo + hi) / 2
    mh, md, ma = _match_probs(total_goals * rho, total_goals * (1 - rho))
    return {"home": mh, "draw": md, "away": ma}


def _blend_ask(kc, pc, wp):
    if kc is None and pc is None:
        return None
    if kc is None:
        return pc
    if pc is None:
        return kc
    w = 0.5 if wp is None else wp
    return (1 - w) * kc + w * pc


class SoccerEngine:
    def __init__(self, dry_run=True, on_log=None, executor=None):
        self.dry_run = dry_run
        self.on_log = on_log or (lambda i, m: None)
        self.executor = executor   # SoccerExecutor; None → signal-logging only
        self.livescore = LiveScoreFeed()
        self._matches = []
        self._seen_signals = set()
        self._recent_goals = {}    # fixture key -> latest goal event

    def refresh(self) -> list:
        kalshi = kalshi_soccer.fetch_matches()
        poly_wc = poly_soccer.fetch_matches(tag_slug="fifa-world-cup")
        live_matches, goals = self.livescore.refresh()
        self._register_goals(goals)

        rows = []
        used = set()
        for k in kalshi:                                   # WC cross-venue rows
            pm = next((p for p in poly_wc if _teams_match(k, p)), None)
            if pm:
                used.add(pm.get("slug"))
            rows.append(self._build_row(k, pm))

        if BROADEN:                                        # opt 3: Poly-only leagues
            try:
                poly_more = poly_soccer.fetch_matches(tag_slug="soccer", limit=120)
                for pm in poly_more:
                    if pm.get("slug") in used or not self._relevant(pm):
                        continue
                    rows.append(self._build_poly_row(pm))
            except Exception as e:  # noqa: BLE001
                log.debug("broaden fetch: %s", e)

        for r in rows:                                     # opt 1 + 2: live + news
            self._attach_live(r)
            self._apply_news(r)

        rows.sort(key=lambda r: (0 if (r.get("live") or {}).get("state") == "in" else 1,
                                 r.get("start") or ""))
        self._matches = rows
        for r in rows:
            self._log_signals(r)
            if self.executor is not None:
                self.executor.consider(r)
        return rows

    # ── opt 1/2/3 helpers ────────────────────────────────────────────────────
    def _relevant(self, pm):
        """Broaden filter: include a Poly-only match if it's live or starts soon."""
        ls = self.livescore.find(pm.get("home_n"), pm.get("away_n"))
        if ls and ls.get("state") == "in":
            return True
        start = pm.get("start")
        if not start:
            return False
        try:
            t = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except Exception:
            return False
        dt_h = (t - datetime.now(timezone.utc)).total_seconds() / 3600.0
        return -3 <= dt_h <= RELEVANT_HOURS

    def _register_goals(self, goals):
        for g in goals:
            self._recent_goals[g["key"]] = g
            self.on_log("⚽", f"[soccer] GOAL {g['home']} {g['score']} {g['away']} "
                             f"({g['scorer_side']}, {g['clock']}) — watch for venue lag")

    def _attach_live(self, row):
        ls = self.livescore.find(row.get("home_n_k") or "", row.get("away_n_k") or "")
        if ls is None:
            ls = self.livescore.find(_norm(row["home"]), _norm(row["away"]))
        if ls:
            row["live"] = {"state": ls["state"], "detail": ls["detail"], "clock": ls["clock"],
                           "score": f"{ls['score_home']}-{ls['score_away']}"
                                    if ls["score_home"] is not None else None}
        else:
            row["live"] = None

    def _apply_news(self, row):
        """opt 2: if a goal just happened, flag the cheaper venue on the scoring side."""
        key = LiveScoreFeed.key(_norm(row["home"]), _norm(row["away"]))
        g = self._recent_goals.get(key)
        if not g or (time.time() - g["ts"]) > GOAL_WINDOW_SECS:
            return
        slot = g["scorer_side"]                 # home/away team that just scored
        o = row["outcomes"].get(slot, {})
        asks = [(v, a) for v, a in (("kalshi", o.get("kalshi")), ("poly", o.get("poly")))
                if a is not None]
        if len(asks) < 2:
            return
        cheap_v, cheap_a = min(asks, key=lambda x: x[1])
        dear_v, dear_a = max(asks, key=lambda x: x[1])
        if (dear_a - cheap_a) >= NEWS_LAG_EDGE_CENTS:   # one venue hasn't caught up
            row["chip"] = "NEWS-LAG"
            row["chip_basis"] = f"goal {g['score']} {g['clock']}"
            row["chip_edge"] = dear_a - cheap_a
            row["chip_buy"] = {"venue": cheap_v, "slot": slot, "ask": cheap_a,
                               "ticker": (row.get("tickers") or {}).get(slot),
                               "token": (row.get("tokens") or {}).get(slot),
                               "min_size": (row.get("min_size") or {}).get(slot, 5)}

    def _build_row(self, k, pm):
        safe, reason = _resolution_audit(pm)
        fair, wp = _fair(k, pm, bool(safe))
        kp, pp = k["prices"], (pm or {}).get("prices") or {}

        # (a-2) absolute Poisson line from blended home/away strength (draw-free)
        h_blend = _blend_ask(kp.get("home"), pp.get("home"), wp)
        a_blend = _blend_ask(kp.get("away"), pp.get("away"), wp)
        model = _model_line(h_blend, a_blend)
        model_c = {s: round(model[s] * 100) for s in model} if model else None

        outcomes = {}
        best = None  # cross-venue: (edge_c, venue, slot, ask) vs blended fair
        for slot in ("home", "draw", "away"):
            kc, pc = kp.get(slot), pp.get(slot)
            fair_c = round(fair[slot] * 100) if fair else None
            ek = (fair_c - kc) if (fair_c is not None and kc is not None) else None
            ep = (fair_c - pc) if (fair_c is not None and pc is not None) else None
            for venue, ask, edge in (("kalshi", kc, ek), ("poly", pc, ep)):
                if edge is not None and edge > 0 and (best is None or edge > best[0]):
                    best = (edge, venue, slot, ask)
            outcomes[slot] = {"kalshi": kc, "poly": pc, "fair": fair_c,
                              "model": (model_c[slot] if model_c else None),
                              "edge_k": ek, "edge_p": ep}

        # absolute draw-value: cheaper venue's draw ask below the MODEL draw,
        # but only in balanced matches where the Poisson draw is trustworthy.
        draw_abs = None
        balanced = model is not None and max(model["home"], model["away"]) <= MODEL_MAX_FAV
        if model_c is not None and safe is not False and balanced:
            asks = [(v, a) for v, a in (("kalshi", kp.get("draw")), ("poly", pp.get("draw")))
                    if a is not None]
            if asks:
                venue, ask = min(asks, key=lambda x: x[1])
                e = model_c["draw"] - ask
                if e >= DRAW_ABS_EDGE_CENTS:
                    draw_abs = (e, venue, ask)

        chip, chip_edge, chip_buy, chip_basis = "NONE", None, None, None
        if safe is False:
            chip = "BASIS-RISK"
        elif draw_abs:
            chip, chip_basis = "DRAW-VALUE", "model"
            chip_edge, chip_buy = draw_abs[0], {"venue": draw_abs[1], "slot": "draw", "ask": draw_abs[2]}
        elif best and best[0] >= VALUE_EDGE_CENTS:
            edge_c, venue, slot, ask = best
            chip, chip_basis = "VALUE", "cross-venue"
            chip_edge, chip_buy = edge_c, {"venue": venue, "slot": slot, "ask": ask}

        # attach the concrete order target so the executor can act on the signal
        if chip_buy:
            v, s = chip_buy["venue"], chip_buy["slot"]
            if v == "kalshi":
                chip_buy["ticker"] = (k.get("tickers") or {}).get(s)
            else:
                chip_buy["token"] = ((pm or {}).get("tokens") or {}).get(s)
                chip_buy["min_size"] = ((pm or {}).get("min_size") or {}).get(s, 5)

        return {
            "home": k["home"], "away": k["away"], "start": k["start"],
            "linked": pm is not None, "venues": "both",
            "safe": safe, "reason": reason, "blend_wp": wp,
            "model": model_c, "model_balanced": balanced,
            "outcomes": outcomes,
            "chip": chip, "chip_edge": chip_edge, "chip_buy": chip_buy, "chip_basis": chip_basis,
            "tickers": k.get("tickers"),
            "tokens": (pm or {}).get("tokens"), "min_size": (pm or {}).get("min_size"),
            "poly_vol": (pm or {}).get("volume") if pm else None,
            "poly_liq": (pm or {}).get("liquidity") if pm else None,
            "kalshi_liq": k.get("liq"),
        }

    def _build_poly_row(self, pm):
        """opt 3: a Polymarket-only league match (no Kalshi counterpart). Signals
        come from the absolute Poisson model only (no cross-venue fair)."""
        pp = pm["prices"]
        h, a = pp.get("home"), pp.get("away")
        model = _model_line(h, a)
        model_c = {s: round(model[s] * 100) for s in model} if model else None
        balanced = model is not None and max(model["home"], model["away"]) <= MODEL_MAX_FAV
        outcomes = {}
        for slot in ("home", "draw", "away"):
            outcomes[slot] = {"kalshi": None, "poly": pp.get(slot),
                              "fair": None, "model": (model_c[slot] if model_c else None),
                              "edge_k": None, "edge_p": None}
        chip, chip_edge, chip_buy, chip_basis = "NONE", None, None, None
        if model_c and balanced and pp.get("draw") is not None:
            e = model_c["draw"] - pp["draw"]
            if e >= DRAW_ABS_EDGE_CENTS:
                chip, chip_basis, chip_edge = "DRAW-VALUE", "model", e
                chip_buy = {"venue": "poly", "slot": "draw", "ask": pp["draw"],
                            "token": (pm.get("tokens") or {}).get("draw"),
                            "min_size": (pm.get("min_size") or {}).get("draw", 5)}
        return {
            "home": pm["home"], "away": pm["away"], "start": pm.get("start"),
            "linked": False, "venues": "poly",
            "safe": None, "reason": "Poly-only league match (model signal)", "blend_wp": None,
            "model": model_c, "model_balanced": balanced,
            "outcomes": outcomes,
            "chip": chip, "chip_edge": chip_edge, "chip_buy": chip_buy, "chip_basis": chip_basis,
            "tickers": None, "tokens": pm.get("tokens"), "min_size": pm.get("min_size"),
            "poly_vol": pm.get("volume"), "poly_liq": pm.get("liquidity"), "kalshi_liq": None,
        }

    def _log_signals(self, row):
        if row["chip"] not in ("DRAW-VALUE", "VALUE", "NEWS-LAG"):
            return
        key = f"{row['home']}|{row['away']}|{row['chip']}|{row['chip_edge']}"
        if key in self._seen_signals:
            return
        self._seen_signals.add(key)
        buy = row["chip_buy"] or {}
        rec = {
            "type": "soccer_signal", "ts": datetime.now(timezone.utc).isoformat(),
            "match": f"{row['home']} vs {row['away']}", "start": row["start"],
            "chip": row["chip"], "basis": row["chip_basis"], "edge_c": row["chip_edge"], "buy": buy,
            "model": row["model"], "outcomes": row["outcomes"],
        }
        try:
            with open(SIGNAL_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("signal log write: %s", e)
        self.on_log("◆", f"[soccer] {row['chip']} ({row['chip_basis']}) +{row['chip_edge']}¢ — "
                         f"buy {buy.get('venue')} {buy.get('slot')} @ {buy.get('ask')}¢ — "
                         f"{row['home']} vs {row['away']}")

    def state(self):
        return {
            "matches": self._matches,
            "config": {"value_edge_c": VALUE_EDGE_CENTS,
                       "draw_abs_edge_c": DRAW_ABS_EDGE_CENTS,
                       "model_total_goals": MODEL_TOTAL_GOALS},
        }
