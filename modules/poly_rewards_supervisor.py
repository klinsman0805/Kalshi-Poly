"""
modules/poly_rewards_supervisor.py — oversight for the LP executor.

The executor's own guards answer "is this position healthy?". The supervisor
answers the two questions no in-position check can:

  1. RECONCILIATION — does what we believe match what the exchange actually
     holds? Every automated-trading loss in this project's history came from a
     belief/reality gap: orders stacked because re-entry was uncapped, legs
     orphaned when one side failed, a phantom `"order_id": null` retried
     forever, a filled position stuck 36 hours because its market fell off a
     feed. The ledger cannot detect any of those — only the exchange can. So:
       ORPHAN   — resting on the CLOB, unknown to the ledger. Real exposure
                  nothing is managing. The dangerous one.
       GHOST    — the ledger thinks it rests, the CLOB has never heard of it.
                  Usually filled/cancelled unnoticed.
       STRANDED — weather shares held that no open holding explains.

  2. CIRCUIT BREAKER — a bounded daily loss. Every guard elsewhere protects one
     position; nothing stops a systematically-wrong day from repeating the same
     loss twenty times. Realized PnL for the UTC day is summed from the ledger
     and, past the limit, new entries stop. Existing positions are still
     managed to completion: stopping mid-flight would strand exposure, which is
     the failure this is meant to prevent.

Read-only with respect to the market: it reports and gates, and never places,
cancels, or sells. Fixing what it finds is a decision for the operator or for
the executor's own (tested) unwind paths.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("modules.poly_rewards_supervisor")

TWOLEG_LOG = Path("poly_twoleg.jsonl")
# Realized loss for one UTC day past which new entries stop. Sized against the
# measured cost of a bad fill (paper auto-completes average about -$1.70) — a
# handful of them is a bad day, twenty is a broken strategy that must stop
# before it compounds.
MAX_DAILY_LOSS_USD = float(os.getenv("POLY_TWOLEG_MAX_DAILY_LOSS_USD", "10"))


def _read_ledger(path=TWOLEG_LOG):
    if not path.exists():
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue      # a torn final line must never crash oversight
    return out


def realized_pnl_today(records=None, day=None):
    """Realized PnL for a UTC day: completions plus closed holdings.

    Only realized events count. Resting quotes and open holdings are unrealized
    by definition and must not trip a breaker that exists to stop REPEATING a
    loss that has already happened.
    """
    records = _read_ledger() if records is None else records
    day = day or datetime.now(timezone.utc).date().isoformat()
    total = 0.0
    for r in records:
        if r.get("type") not in ("twoleg_completed", "twoleg_holding_closed"):
            continue
        ts = r.get("ts") or ""
        if not ts.startswith(day):
            continue
        total += float(r.get("pnl_usd") or 0.0)
    return total


def check_breaker(records=None):
    """(ok, reason). ok=False means: open no NEW positions. Positions already
    open are still managed to completion."""
    pnl = realized_pnl_today(records)
    if pnl <= -abs(MAX_DAILY_LOSS_USD):
        return False, (f"daily realized loss ${pnl:+.2f} has reached the "
                       f"${-abs(MAX_DAILY_LOSS_USD):.2f} limit — no new entries "
                       f"today; open positions are still being managed")
    return True, f"daily realized PnL ${pnl:+.2f}"


def _ledger_known_order_ids(records):
    """Order ids the ledger has ever referenced, and the subset it believes
    are still resting."""
    known, resting = set(), set()
    for r in records:
        t = r.get("type")
        if t in ("twoleg_placed", "twoleg_requoted"):
            for k in ("yes_order_id", "no_order_id"):
                oid = r.get(k)
                if oid:
                    known.add(oid)
                    resting.add(oid)
        elif t == "twoleg_orphan":
            for k in ("order_id", "yes_order_id", "no_order_id"):
                if r.get(k):
                    known.add(r[k])
        elif t in ("twoleg_pulled", "twoleg_closed", "twoleg_completed",
                   "twoleg_fill", "twoleg_holding"):
            # any of these means the pair's previous ids are no longer believed
            # to be resting; the next placed/requoted record re-establishes them
            resting.clear()
    return known, resting


def reconcile(open_orders, positions=None, records=None):
    """Compare exchange truth against the ledger.

    open_orders: [{"id", "asset_id"/"token_id", ...}] straight from the CLOB.
    positions:   [{"conditionId"/"title", "size", ...}] from the data API.

    Returns {"orphans", "ghosts", "stranded", "ok"}. Deliberately takes the
    exchange state as arguments rather than fetching it, so this stays pure
    and testable and callers control API cost.
    """
    records = _read_ledger() if records is None else records
    known, resting = _ledger_known_order_ids(records)

    live_ids = set()
    orphans = []
    for o in open_orders or []:
        oid = o.get("id") or o.get("order_id")
        if not oid:
            continue
        live_ids.add(oid)
        if oid not in known:
            orphans.append(o)
    ghosts = sorted(resting - live_ids)

    holdings = {}
    for r in records:
        if r.get("type") == "twoleg_holding":
            holdings[r["hold_id"]] = r
        elif r.get("type") == "twoleg_holding_closed":
            holdings.pop(r.get("hold_id"), None)
    held_cids = {h.get("condition_id") for h in holdings.values()}
    stranded = []
    for p in positions or []:
        title = (p.get("title") or "")
        if "temperature" not in title.lower():
            continue          # only weather is ours to explain
        if p.get("conditionId") not in held_cids:
            stranded.append(p)

    return {"orphans": orphans, "ghosts": ghosts, "stranded": stranded,
            "ok": not orphans and not ghosts and not stranded}


def describe(result):
    """One-line-per-finding summary for logs."""
    lines = []
    for o in result["orphans"]:
        lines.append(f"ORPHAN order {str(o.get('id'))[:18]} "
                     f"{o.get('outcome')} {o.get('original_size')} @ "
                     f"{float(o.get('price') or 0) * 100:.0f}c — resting on the CLOB, "
                     f"NOT in the ledger; nothing is managing it")
    for g in result["ghosts"]:
        lines.append(f"GHOST order {str(g)[:18]} — ledger believes it rests, "
                     f"CLOB does not list it (filled or cancelled unnoticed)")
    for s in result["stranded"]:
        lines.append(f"STRANDED {s.get('size')} sh of "
                     f"{(s.get('title') or '')[:44]} — held, no open holding explains it")
    return lines
