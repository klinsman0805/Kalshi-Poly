"""
feeds/kalshi_soccer.py — Kalshi World Cup match feed (public REST, read-only).

Series KXWCGAME = per-match "Regulation Time Moneyline" with three markets:
Home / Away / Tie. We read quoted yes_bid/yes_ask from the nested markets and,
for the soonest matches that still show no quote, fall back to /orderbook (the
markets endpoint can null quotes even when the book has depth).
"""

import logging

import requests

from feeds.poly_soccer import normalize_team

log = logging.getLogger("feeds.kalshi_soccer")

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXWCGAME"
ORDERBOOK_LOOKAHEAD = 6  # only hit /orderbook for the N soonest unquoted matches


def _cents(v):
    if v in (None, "", 0, "0"):
        return None
    try:
        return round(float(v) * 100)
    except (TypeError, ValueError):
        return None


def _yes_ask_cents(mk: dict):
    """Implied YES ask in cents from the *_dollars quote fields."""
    ya = _cents(mk.get("yes_ask_dollars"))
    if ya is not None:
        return ya
    nb = _cents(mk.get("no_bid_dollars"))
    if nb is not None:
        return 100 - nb
    return None


def _orderbook_yes_ask(ticker: str, sess: requests.Session):
    """YES ask from the public orderbook: 100 - best NO bid."""
    try:
        r = sess.get(f"{API_BASE}/markets/{ticker}/orderbook", timeout=8)
        r.raise_for_status()
        ob = r.json().get("orderbook") or {}
        no_levels = ob.get("no") or []
        if no_levels:
            best_no = max(int(p) for p, _ in no_levels)
            return 100 - best_no
        yes_levels = ob.get("yes") or []
        if yes_levels:
            # only YES bids present -> NO ask = 100 - best yes bid; no YES ask
            return None
    except Exception as e:  # noqa: BLE001
        log.debug("orderbook %s: %s", ticker, e)
    return None


def parse_event(ev: dict) -> dict:
    title = (ev.get("title") or "").split(":")[0].strip()  # "Home vs Away"
    parts = title.split(" vs ")
    home = parts[0].strip() if parts else ""
    away = parts[1].strip() if len(parts) > 1 else ""
    prices = {"home": None, "draw": None, "away": None}
    tickers = {"home": None, "draw": None, "away": None}
    hn, an = normalize_team(home), normalize_team(away)
    kickoff = None
    rules = None
    liq = 0.0
    for mk in ev.get("markets") or []:
        kickoff = kickoff or mk.get("occurrence_datetime")
        rules = rules or mk.get("rules_primary")
        liq += _cents(mk.get("liquidity_dollars")) or 0
        sub = (mk.get("yes_sub_title") or "").replace("Reg Time:", "").strip()
        subn = normalize_team(sub)
        slot = None
        if "tie" in sub.lower() or "draw" in sub.lower():
            slot = "draw"
        elif subn and (subn in hn or hn in subn):
            slot = "home"
        elif subn and (subn in an or an in subn):
            slot = "away"
        if slot:
            prices[slot] = _yes_ask_cents(mk)
            tickers[slot] = mk.get("ticker")
    return {
        "venue": "kalshi",
        "event_ticker": ev.get("event_ticker"),
        "home": home,
        "away": away,
        "home_n": hn,
        "away_n": an,
        "start": kickoff,
        "prices": prices,
        "tickers": tickers,
        "rules": rules,
        "liq": liq,
    }


def fetch_matches(limit: int = 50) -> list:
    sess = requests.Session()
    try:
        r = sess.get(
            f"{API_BASE}/events",
            params={
                "series_ticker": SERIES,
                "status": "open",
                "with_nested_markets": "true",
                "limit": limit,
            },
            timeout=10,
        )
        r.raise_for_status()
        events = r.json().get("events", [])
    except Exception as e:  # noqa: BLE001
        log.warning("kalshi soccer fetch failed: %s", e)
        return []
    matches = []
    for ev in events:
        try:
            matches.append(parse_event(ev))
        except Exception as e:  # noqa: BLE001
            log.debug("kalshi parse error: %s", e)
    # sort soonest first, backfill quotes for the nearest few via /orderbook
    matches.sort(key=lambda m: m.get("start") or "")
    backfilled = 0
    for mt in matches:
        if backfilled >= ORDERBOOK_LOOKAHEAD:
            break
        if any(v is None for v in mt["prices"].values()):
            hit = False
            for slot, tk in mt["tickers"].items():
                if mt["prices"][slot] is None and tk:
                    ya = _orderbook_yes_ask(tk, sess)
                    if ya is not None:
                        mt["prices"][slot] = ya
                        hit = True
            if hit:
                backfilled += 1
    return matches
