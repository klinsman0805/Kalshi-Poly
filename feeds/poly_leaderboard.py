"""
feeds/poly_leaderboard.py — Polymarket trader-scanning feed (read-only).

For copy-trade research: rank wallets off Polymarket's public leaderboard, then
score each one from its on-chain positions. Everything here is read-only public
data (no keys), pulled from two hosts:

  • lb-api.polymarket.com/{profit,volume}?window=&limit=  → ranked wallets
  • data-api.polymarket.com/positions?user=               → per-position PnL
  • data-api.polymarket.com/activity?user=&type=TRADE     → raw trade history

Caveats (important — read before trusting the numbers):
  • /positions returns only CURRENTLY-HELD positions. Once a wallet redeems a
    resolved market the position drops off, so "win rate" computed here is a
    SNAPSHOT of open bets, not a lifetime record. Treat leaderboard `profit`
    and ROI as the primary signals; win rate is secondary/indicative.
  • High win rate ≠ profitable (favourite-buyers win often, earn little). We
    surface profit + ROI first on purpose.
  • `avg_trade_usd` is the copyability gate: big-size traders move thin books,
    so you can't actually mirror their fills — smaller avg size is better.
"""

import logging

import requests

log = logging.getLogger("feeds.poly_leaderboard")

LB_BASE = "https://lb-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

VALID_METRICS = ("profit", "volume")
VALID_WINDOWS = ("1d", "7d", "30d", "all")


def fetch_leaderboard(metric: str = "profit", window: str = "all", limit: int = 50) -> list:
    """Top wallets by `metric` (profit|volume) over `window`. Read-only, no keys.

    Returns [{wallet, name, amount}], richest first. Empty list on any error.
    """
    if metric not in VALID_METRICS:
        metric = "profit"
    if window not in VALID_WINDOWS:
        window = "all"
    try:
        r = requests.get(
            f"{LB_BASE}/{metric}",
            params={"window": window, "limit": limit},
            timeout=12,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("poly leaderboard fetch failed (%s/%s): %s", metric, window, e)
        return []
    out = []
    for row in rows or []:
        w = row.get("proxyWallet")
        if not w:
            continue
        out.append({
            "wallet": w,
            "name": row.get("name") or row.get("pseudonym") or w[:10],
            "amount": float(row.get("amount") or 0),
        })
    return out


def fetch_positions(wallet: str, limit: int = 500) -> list:
    """All currently-held positions for `wallet`. Empty list on error."""
    try:
        r = requests.get(
            f"{DATA_BASE}/positions",
            params={"user": wallet, "limit": limit, "sortBy": "CURRENT", "sortDirection": "DESC"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:  # noqa: BLE001
        log.warning("poly positions fetch failed (%s): %s", wallet[:10], e)
        return []


def fetch_activity(wallet: str, limit: int = 100) -> list:
    """Recent TRADE activity for `wallet` (most recent first). Empty on error."""
    try:
        r = requests.get(
            f"{DATA_BASE}/activity",
            params={"user": wallet, "limit": limit, "type": "TRADE"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:  # noqa: BLE001
        log.warning("poly activity fetch failed (%s): %s", wallet[:10], e)
        return []


def fetch_all_activity(wallet: str, max_events: int = 1500, page: int = 500) -> list:
    """Paginate /activity (all types) up to `max_events`. Empty list on error."""
    out: list = []
    offset = 0
    while len(out) < max_events:
        try:
            r = requests.get(
                f"{DATA_BASE}/activity",
                params={"user": wallet, "limit": page, "offset": offset},
                timeout=20,
            )
            r.raise_for_status()
            batch = r.json() or []
        except Exception as e:  # noqa: BLE001
            log.warning("poly activity page fetch failed (%s @%d): %s", wallet[:10], offset, e)
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out[:max_events]


def realized_stats(wallet: str, max_events: int = 1500) -> dict:
    """Reconstruct LIFETIME realized win rate / PnL from on-chain cash flows.

    Groups every event by market (conditionId) and nets cash in vs cash out:
      cash OUT  ← TRADE BUY, SPLIT   (USDC you spent)
      cash IN   ← TRADE SELL, REDEEM, MERGE   (USDC you received)
    A market counts as RESOLVED only if it has a REDEEM/MERGE (i.e. carried to
    settlement) — that's the copy-trade-relevant population. Win = net cash > 0.

    `capped` is True if we hit `max_events` (older trades excluded → numbers are
    a recent-history estimate, not the full lifetime).
    """
    acts = fetch_all_activity(wallet, max_events=max_events)
    by_market: dict = {}
    for a in acts:
        cid = a.get("conditionId")
        if not cid:
            continue
        m = by_market.setdefault(cid, {"cin": 0.0, "cout": 0.0, "settled": False})
        t = a.get("type")
        usd = float(a.get("usdcSize") or 0)
        if t == "TRADE":
            if a.get("side") == "BUY":
                m["cout"] += usd
            elif a.get("side") == "SELL":
                m["cin"] += usd
        elif t in ("REDEEM", "MERGE"):
            m["cin"] += usd
            m["settled"] = True
        elif t == "SPLIT":
            m["cout"] += usd
        # CONVERSION / REWARD / other: ignored for PnL

    resolved = [m for m in by_market.values() if m["settled"]]
    wins = sum(1 for m in resolved if (m["cin"] - m["cout"]) > 0)
    pnl = sum(m["cin"] - m["cout"] for m in resolved)
    cost = sum(m["cout"] for m in resolved)
    n = len(resolved)
    return {
        "resolved_markets": n,
        "realized_winrate": round(wins / n, 4) if n else None,
        "realized_pnl": round(pnl, 2),
        "realized_roi": round(pnl / cost, 4) if cost else None,
        "events_scanned": len(acts),
        "capped": len(acts) >= max_events,
    }


def score_trader(wallet: str, name: str, profit: float,
                 max_copy_trade_usd: float = 5000.0,
                 deep: bool = False, max_events: int = 1500) -> dict:
    """Score one wallet for copy-trade suitability.

    `profit` is the leaderboard realized-PnL headline (lifetime/window). Open-*
    fields come from the current open-position snapshot. When `deep=True` we also
    reconstruct LIFETIME realized win rate / ROI from /activity (slower — several
    paginated calls per wallet). See module caveats.
    """
    positions = fetch_positions(wallet)
    n = len(positions)
    wins = sum(1 for p in positions if (p.get("cashPnl") or 0) > 0)
    open_pnl = sum(float(p.get("cashPnl") or 0) for p in positions)
    invested = sum(float(p.get("initialValue") or 0) for p in positions)
    exposure = sum(float(p.get("currentValue") or 0) for p in positions)
    avg_trade = (invested / n) if n else 0.0
    roi = (open_pnl / invested) if invested else 0.0
    win_rate = (wins / n) if n else 0.0

    row = {
        "wallet": wallet,
        "name": name,
        "url": f"https://polymarket.com/profile/{wallet}",
        "profit": round(profit, 2),            # leaderboard realized PnL (primary)
        "open_positions": n,
        "win_rate": round(win_rate, 4),        # snapshot proxy — secondary
        "open_pnl": round(open_pnl, 2),
        "roi": round(roi, 4),                  # open-snapshot ROI
        "avg_trade_usd": round(avg_trade, 2),  # copyability gate
        "open_exposure": round(exposure, 2),
        "copyable": bool(0 < avg_trade <= max_copy_trade_usd),
        # lifetime realized fields default to None until a deep scan fills them
        "resolved_markets": None,
        "realized_winrate": None,
        "realized_pnl": None,
        "realized_roi": None,
        "realized_capped": None,
    }
    if deep:
        rs = realized_stats(wallet, max_events=max_events)
        row.update({
            "resolved_markets": rs["resolved_markets"],
            "realized_winrate": rs["realized_winrate"],
            "realized_pnl": rs["realized_pnl"],
            "realized_roi": rs["realized_roi"],
            "realized_capped": rs["capped"],
        })
    return row


def token_price(token_id: str, side: str = "buy"):
    """Current CLOB price for a token in cents (buy=ask you'd pay, sell=bid).

    Returns an int in cents, or None if the token has no live orderbook
    (e.g. the market has resolved/delisted).
    """
    try:
        r = requests.get(
            f"{CLOB_BASE}/price",
            params={"token_id": token_id, "side": side},
            timeout=10,
        )
        r.raise_for_status()
        p = (r.json() or {}).get("price")
        return round(float(p) * 100) if p not in (None, "") else None
    except Exception as e:  # noqa: BLE001
        log.debug("clob price failed (%s): %s", str(token_id)[:12], e)
        return None


def _gamma_market(token_id: str, closed: bool):
    """One gamma market row for `token_id`, filtered by closed state. [] if none."""
    r = requests.get(
        f"{GAMMA_BASE}/markets",
        params={"clob_token_ids": token_id, "closed": str(bool(closed)).lower()},
        timeout=12,
    )
    r.raise_for_status()
    return (r.json() or [])


def market_resolution(token_id: str):
    """Resolution state for the market that owns `token_id`.

    Returns {closed, price} where `price` is this token's outcome price in [0,1]:
    ~1.0 means this side WON, ~0.0 means it LOST. Returns None ONLY on a genuine
    lookup failure — callers must not treat None as "still open".

    NOTE: gamma's /markets excludes closed markets by default, so a resolved
    market looks like a 404 unless you ask for closed=true explicitly. We probe
    closed first (that's the state we care about), then fall back to open.
    """
    import json as _json
    try:
        rows = _gamma_market(token_id, closed=True)
        if not rows:
            rows = _gamma_market(token_id, closed=False)
        if not rows:
            return None
        m = rows[0]
        ids = m.get("clobTokenIds")
        prices = m.get("outcomePrices")
        if isinstance(ids, str):
            ids = _json.loads(ids)
        if isinstance(prices, str):
            prices = _json.loads(prices)
        idx = ids.index(token_id) if (ids and token_id in ids) else 0
        price = float(prices[idx]) if (prices and len(prices) > idx) else None
        return {"closed": bool(m.get("closed")), "price": price}
    except Exception as e:  # noqa: BLE001
        log.debug("gamma resolution failed (%s): %s", str(token_id)[:12], e)
        return None


def scan(metric: str = "profit", window: str = "all", top_n: int = 25,
         max_copy_trade_usd: float = 5000.0,
         deep: bool = False, max_events: int = 1500) -> list:
    """Fetch the leaderboard and score the top `top_n` wallets.

    Returned rows are sorted by leaderboard profit (already the leaderboard order).
    With `deep=True` each wallet also gets lifetime realized win rate / ROI (slower).
    Read-only; safe to call repeatedly. Returns [] if the leaderboard is empty.
    """
    board = fetch_leaderboard(metric=metric, window=window, limit=top_n)
    out = []
    for row in board:
        try:
            out.append(score_trader(row["wallet"], row["name"], row["amount"],
                                    max_copy_trade_usd=max_copy_trade_usd,
                                    deep=deep, max_events=max_events))
        except Exception as e:  # noqa: BLE001
            log.debug("score error %s: %s", row.get("wallet", "?")[:10], e)
    return out
