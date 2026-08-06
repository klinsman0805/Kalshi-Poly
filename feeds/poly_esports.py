"""
feeds/poly_esports.py — Polymarket LoL match market discovery + order books.

Deliberately standalone from feeds/poly_weather.py. The two sectors share a
venue but not a fee schedule, not a market shape, and not a discovery path,
and coupling them would mean a weather change can break esports at 3am.

What probing the live API established, and what the code here relies on:

  • `startDate` on an event is when the EVENT was created, not when the match
    is played — it is always in the past, for every event, and sorting by it
    tells you nothing. The real kickoff is `market["gameStartTime"]`, an
    ISO-ish string with a "+00" offset (not "Z", not "+00:00").
  • `closed=false` is not enough to find live matches: months-old resolved
    events still come back with closed=false. Filter on gameStartTime.
  • `clearBookOnStart: true` — the resting book is WIPED when the match starts.
    In-play liquidity is rebuilt from scratch, so pre-match depth tells you
    nothing about what you can trade against once the game is running.
  • `secondsDelay: 1` — every order sits one second before it can match. Any
    strategy premised on reacting faster than the market pays this twice.
  • Fees are `sports_fees_v2`: {rate 0.05, exponent 1, takerOnly true,
    rebateRate 0.15}. Takers pay; makers pay nothing and earn a rebate. That
    asymmetry points hard away from the take-the-panic trade.
  • Liquidity is bimodal. A tier-1 LCK/LPL market showed liquidityNum ~$10k on
    a 3c spread while regional matches showed $190-390 on 42-86c spreads. Most
    matches are not tradeable at any size; the recorder logs all of them anyway
    so we can measure what fraction ever is.

Read-only: discovery and book snapshots. No order placement lives here.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger("feeds.poly_esports")

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"
TIMEOUT = 20
PAGE = 100
MAX_PAGES = 4
UA = {"User-Agent": "Mozilla/5.0"}

# Confirmed live from market["feeSchedule"]. Same p(1-p) shape as the weather
# taker fee, charged on the taker side only.
TAKER_FEE_RATE = float(os.getenv("ESPORTS_TAKER_FEE_RATE", "0.05"))

TAG = os.getenv("ESPORTS_TAG_SLUG", "league-of-legends")

# "LoL: Team A vs Team B (BO3) - LPL Group Ascend"
_TITLE = re.compile(r"^LoL:\s*(.+?)\s+vs\.?\s+(.+?)\s*(?:\((BO\d)\))?\s*(?:-\s*(.+))?$", re.I)
# "LoL: Team A vs Team B - Game 1 Winner"
_GAME_N = re.compile(r"-\s*Game\s+(\d+)\s+Winner\s*$", re.I)

_session = requests.Session()
_session.headers.update(UA)


def taker_fee_c(price_c):
    """Taker fee in CENTS per share for a fill at `price_c` cents."""
    if not price_c:
        return 0.0
    p = price_c / 100.0
    return TAKER_FEE_RATE * p * (1.0 - p) * 100.0


def _jload(s, default=None):
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except (ValueError, TypeError):
        return default if default is not None else []


def _parse_game_start(s):
    """gameStartTime arrives as '2026-08-06 18:00:00+00' — not ISO, not RFC3339."""
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    # normalise a bare '+00' / '+0000' offset to '+00:00'
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    s = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_lol_events(hours_back=6.0, hours_ahead=12.0):
    """Open LoL match events whose kickoff falls in the given window.

    Returns [{event_id, title, slug, teams, live, period, score, game_start,
              markets: [...]}] with one entry per event, markets pre-filtered
    to those still accepting orders.
    """
    now = datetime.now(timezone.utc)
    seen = {}
    for page in range(MAX_PAGES):
        try:
            r = _session.get(GAMMA, timeout=TIMEOUT, params={
                "closed": "false", "limit": PAGE, "offset": page * PAGE,
                "tag_slug": TAG, "order": "startDate", "ascending": "false"})
            r.raise_for_status()
            batch = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("gamma page %d failed: %s", page, e)
            break
        if not batch:
            break
        for e in batch:
            seen[e.get("id")] = e

    out = []
    for e in seen.values():
        title = e.get("title") or ""
        if not title.lower().startswith("lol:"):
            continue          # season/futures markets, not per-match
        markets = []
        game_start = None
        for m in (e.get("markets") or []):
            gs = _parse_game_start(m.get("gameStartTime"))
            if gs and (game_start is None or gs < game_start):
                game_start = gs
            if not m.get("acceptingOrders"):
                continue
            toks = _jload(m.get("clobTokenIds"), [])
            outs = _jload(m.get("outcomes"), [])
            if len(toks) < 2 or len(outs) < 2:
                continue
            gm = _GAME_N.search(m.get("question") or "")
            markets.append({
                "market_id": m.get("id"),
                "condition_id": m.get("conditionId"),
                "question": m.get("question"),
                "game_number": int(gm.group(1)) if gm else None,
                "outcomes": outs,
                "token_ids": [str(t) for t in toks],
                "best_bid": m.get("bestBid"),
                "best_ask": m.get("bestAsk"),
                "spread": m.get("spread"),
                "liquidity_num": m.get("liquidityNum"),
                "volume_24hr": m.get("volume24hr"),
                "order_min_size": m.get("orderMinSize"),
                "tick_size": m.get("orderPriceMinTickSize"),
                "seconds_delay": m.get("secondsDelay"),
                "clear_book_on_start": m.get("clearBookOnStart"),
                "game_start": _parse_game_start(m.get("gameStartTime")),
            })
        if not markets or not game_start:
            continue
        age_h = (now - game_start).total_seconds() / 3600.0
        if not (-hours_ahead <= age_h <= hours_back):
            continue
        mt = _TITLE.match(title)
        out.append({
            "event_id": e.get("id"),
            "title": title,
            "slug": e.get("slug"),
            "teams": [(t.get("name") or "").strip() for t in (e.get("teams") or [])],
            "title_teams": [mt.group(1).strip(), mt.group(2).strip()] if mt else [],
            "league_hint": (mt.group(4) or "").strip() if mt else "",
            "live": e.get("live"),
            "period": e.get("period"),
            "score": e.get("score"),
            "game_start": game_start,
            "hours_since_start": round(age_h, 3),
            "markets": markets,
        })
    out.sort(key=lambda x: x["game_start"])
    return out


def fetch_book(token_id, timeout=8):
    """Full book snapshot for a token, or None.

    Depth is reported in DOLLARS at each side (size x price summed), because
    "is there $200 behind the touch" is the question that decides whether a
    signal is tradeable — raw share counts hide that a 3c contract with 5000
    shares is $150.
    """
    try:
        r = _session.get(CLOB_BOOK, params={"token_id": token_id}, timeout=timeout)
        r.raise_for_status()
        d = r.json()
    except Exception as e:  # noqa: BLE001
        log.debug("book %s failed: %s", token_id, e)
        return None

    def level(rows):
        out = []
        for x in rows or []:
            try:
                out.append((float(x["price"]), float(x["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    bids = sorted(level(d.get("bids")), key=lambda p: -p[0])
    asks = sorted(level(d.get("asks")), key=lambda p: p[0])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    return {
        "best_bid_c": round(best_bid * 100, 2) if best_bid is not None else None,
        "best_ask_c": round(best_ask * 100, 2) if best_ask is not None else None,
        "spread_c": (round((best_ask - best_bid) * 100, 2)
                     if best_bid is not None and best_ask is not None else None),
        "bid_depth_usd": round(sum(p * s for p, s in bids), 2),
        "ask_depth_usd": round(sum(p * s for p, s in asks), 2),
        "n_bids": len(bids),
        "n_asks": len(asks),
        # Top 5 levels only — the tail is noise and this file gets written
        # every few seconds for every live market.
        "bids": [[round(p * 100, 2), s] for p, s in bids[:5]],
        "asks": [[round(p * 100, 2), s] for p, s in asks[:5]],
    }
