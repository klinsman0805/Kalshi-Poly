"""
feeds/poly_soccer.py — Polymarket soccer match feed (gamma API, read-only).

Pulls per-match 3-way moneyline events ("Home vs. Away" with Home / Draw / Away
Yes/No markets) and normalises them into a simple PolyMatch shape the Soccer
module can line up against Kalshi. We use gamma's quoted bestAsk/bestBid (and
fall back to outcomePrices) — good enough for a monitoring view, no CLOB needed.
"""

import logging
import json
import re
import unicodedata

import requests

log = logging.getLogger("feeds.poly_soccer")

GAMMA_BASE = "https://gamma-api.polymarket.com"


def normalize_team(name: str) -> str:
    """Lowercase, strip accents + common club suffixes/punct for fuzzy matching."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"\b(fc|sc|cf|afc|cd|ec|club|de|the)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _price(market: dict, idx: int = 0):
    """Best ask (what you'd pay) for the Yes side, in cents. Fall back to last price."""
    for key in ("bestAsk",):
        v = market.get(key)
        if v not in (None, "", 0, "0"):
            try:
                return round(float(v) * 100)
            except (TypeError, ValueError):
                pass
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try:
            import json
            op = json.loads(op)
        except Exception:
            op = None
    if isinstance(op, list) and len(op) > idx:
        try:
            return round(float(op[idx]) * 100)
        except (TypeError, ValueError):
            return None
    return None


def _yes_token(mk: dict):
    """The CLOB token id for the Yes side of a binary outcome market."""
    ids = mk.get("clobTokenIds")
    outs = mk.get("outcomes")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except Exception:
            return None
    if isinstance(outs, str):
        try:
            outs = json.loads(outs)
        except Exception:
            outs = None
    if isinstance(ids, list) and ids:
        if isinstance(outs, list) and "Yes" in outs:
            return ids[outs.index("Yes")]
        return ids[0]
    return None


def parse_event(ev: dict):
    """Return a normalised match dict, or None if not a clean 3-way moneyline."""
    title = (ev.get("title") or "").strip()
    if "More Markets" in title or " vs" not in title.lower():
        return None
    # "Home vs. Away" -> teams
    m = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(m) != 2:
        return None
    home, away = m[0].strip(), m[1].strip()
    markets = ev.get("markets") or []
    prices = {"home": None, "draw": None, "away": None}
    tokens = {"home": None, "draw": None, "away": None}   # Yes-side CLOB token per outcome
    min_size = {"home": 5, "draw": 5, "away": 5}
    hn, an = normalize_team(home), normalize_team(away)
    for mk in markets:
        gi = (mk.get("groupItemTitle") or "").strip()
        gin = normalize_team(gi)
        slot = None
        if gi.lower().startswith("draw") or "draw" in gi.lower():
            slot = "draw"
        elif gin and (gin in hn or hn in gin):
            slot = "home"
        elif gin and (gin in an or an in gin):
            slot = "away"
        if slot:
            prices[slot] = _price(mk)
            tokens[slot] = _yes_token(mk)
            min_size[slot] = int(mk.get("orderMinSize") or 5)
    if prices["home"] is None and prices["away"] is None:
        return None
    return {
        "venue": "poly",
        "slug": ev.get("slug"),
        "home": home,
        "away": away,
        "home_n": hn,
        "away_n": an,
        "start": ev.get("startDate"),
        "prices": prices,
        "tokens": tokens,
        "min_size": min_size,
        "has_draw": prices["draw"] is not None,
        "rules": ev.get("description"),
        "volume": float(ev.get("volume") or 0),
        "liquidity": float(ev.get("liquidity") or 0),
    }


def fetch_matches(limit: int = 100, tag_slug: str = "fifa-world-cup") -> list:
    """Fetch upcoming/open soccer match events as normalised PolyMatch dicts."""
    try:
        r = requests.get(
            f"{GAMMA_BASE}/events",
            params={
                "closed": "false",
                "limit": limit,
                "tag_slug": tag_slug,
                "order": "startDate",
                "ascending": "true",
            },
            timeout=10,
        )
        r.raise_for_status()
        events = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("poly soccer fetch failed: %s", e)
        return []
    out = []
    for ev in events or []:
        try:
            mt = parse_event(ev)
            if mt:
                out.append(mt)
        except Exception as e:  # noqa: BLE001
            log.debug("poly parse error: %s", e)
    return out
