"""
modules/poly_rewards_live.py — parameterized limit-order placement and
cancellation via Polymarket's NEW SDK (`polymarket-client`, AsyncSecureClient),
gated behind POLY_REWARDS_NEW_SDK (default OFF).

Why the new SDK: the OLD SDK (py_clob_client_v2, what polymarket.py's place_gtc
wraps) cannot sign GTC orders for this proxy-funded account — "invalid
POLY_PROXY signature", root-caused to a missing order-type branch in
create_order's proxy-signing path (taker orders, FOK/FAK, are unaffected — the
weather bot keeps using the old SDK for those). The new SDK auto-resolves
signing.

Why a subprocess, not an in-process import: the new SDK's package is literally
named `polymarket`, colliding with this repo's own polymarket.py, and its
__init__ does absolute self-imports (`from polymarket.auth import ...`) that
CANNOT be satisfied under an alias. The previous in-process `_load_new_sdk()`
alias loader here never actually worked — verified 2026-08-04, it raised
`ModuleNotFoundError: No module named 'polymarket.auth'` on every call, so this
whole live path was a silent dead no-op and the only real orders ever placed
went through a hand-run script in /tmp. Every call now shells out to
scripts/poly_sdk_runner.py, which runs in its own interpreter where
`import polymarket` resolves to the pip SDK (see that file's docstring). At this
strategy's cadence (one position at a time, a handful of orders per lifecycle)
the ~1s subprocess spawn is irrelevant.

place_limit()  — general resting/marketable limit order; returns an order_id.
cancel_limit() — cancel a resting order by id; the load-bearing primitive the
                 two-leg executor needs so a half-placed pair or an expired
                 quote never leaks a live order onto the book.
place_at_band()— convenience entry off a fresh feeds.poly_rewards.get_band()
                 result, with ledger recording for the check_fills() monitor.

POLY_REWARDS_NEW_SDK=false (default) / the per-call `live` override: every
function logs what it would do and places/cancels nothing. Real orders require
BOTH a live=True (or the module flag) AND that a single real 1-share round-trip
has been validated on the box — the subprocess auth + cancel paths are proven
read-only, but live PLACEMENT has not been re-validated since this rewrite.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from modules.poly_rewards_exec import record_order_placed

log = logging.getLogger("modules.poly_rewards_live")

POLY_REWARDS_NEW_SDK = os.getenv("POLY_REWARDS_NEW_SDK", "false").strip().lower() == "true"

# The isolated runner (scripts/poly_sdk_runner.py) and the interpreter to run it
# with. sys.executable is already the venv python when the monitor runs under
# systemd, which is where the SDK is installed; POLY_SDK_PYTHON overrides it.
_RUNNER = str(Path(__file__).resolve().parent.parent / "scripts" / "poly_sdk_runner.py")
_SDK_PYTHON = os.getenv("POLY_SDK_PYTHON", sys.executable)
_SDK_TIMEOUT = int(os.getenv("POLY_SDK_TIMEOUT", "40"))


def _run_sdk(command):
    """Invoke the isolated SDK runner in a subprocess and return its parsed
    JSON result (always a dict; {'ok': False, 'error': ...} on any failure).

    The runner is invoked BY PATH so its sys.path[0] is scripts/ and its
    `import polymarket` binds to the pip SDK, never this repo's polymarket.py —
    that isolation is the whole reason this is a subprocess (see both docstrings).
    """
    try:
        proc = subprocess.run(
            [_SDK_PYTHON, _RUNNER],
            input=json.dumps(command),
            capture_output=True, text=True, timeout=_SDK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"sdk runner timed out after {_SDK_TIMEOUT}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"sdk runner spawn failed: {e}"}
    if proc.returncode != 0:
        return {"ok": False, "error": f"runner exit {proc.returncode}: {(proc.stderr or '').strip()[:300]}"}
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "error": "runner produced no output"}
    try:
        return json.loads(lines[-1])  # last non-empty line is the result object
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"unparseable runner output ({e}): {proc.stdout[:200]}"}


def place_limit(token_id, price_c, size, side="BUY", post_only=True, live=None):
    """General parameterized limit order. Returns the order_id, or None on
    failure / when not armed.

    post_only=True is the resting-maker case (rejected rather than filled if it
    would cross). post_only=False is what the auto-complete path needs: a
    deliberately marketable order that must trade NOW, because leaving a
    one-sided fill open is the whole risk being managed.

    `live` overrides the module gate so a caller with its own flag (the two-leg
    executor has POLY_TWOLEG_LIVE) decides for itself.
    """
    armed = POLY_REWARDS_NEW_SDK if live is None else bool(live)
    price_c = round(float(price_c))
    price = price_c / 100.0
    if not armed:
        log.info("[lp-live] PAPER — would place %s %s %.0f sh @ %dc (post_only=%s)",
                 side, token_id[:12], size, price_c, post_only)
        return None
    res = _run_sdk({"action": "place", "token_id": token_id, "price": price,
                    "size": float(size), "side": side, "post_only": bool(post_only)})
    order_id = res.get("order_id")
    if not res.get("ok") or not order_id:
        log.warning("[lp-live] %s order failed (%s @ %dc): %s", side, token_id[:12],
                    price_c, res.get("message") or res.get("error") or res)
        return None
    return order_id


def cancel_limit(order_id, live=None):
    """Cancel a resting order by id. Returns True iff the order is confirmed
    cancelled OR already gone (nothing left to leak either way); False if a
    live cancel was attempted and could not be confirmed.

    Paper (not armed): a no-op that returns True — nothing was ever really
    placed, so there is nothing to cancel. A None/empty order_id is likewise a
    successful no-op (that is the paper leg's order_id).
    """
    if not order_id:
        return True
    armed = POLY_REWARDS_NEW_SDK if live is None else bool(live)
    if not armed:
        log.info("[lp-live] PAPER — would cancel order %s", str(order_id)[:16])
        return True
    res = _run_sdk({"action": "cancel", "order_id": order_id})
    if not res.get("ok"):
        log.warning("[lp-live] cancel FAILED for %s: %s", str(order_id)[:16],
                    res.get("not_canceled") or res.get("error") or res)
        return False
    return True


def order_status(order_id, live=None):
    """Real fill state of one order, from the CLOB itself: dict with
    status / size_matched / original_size, or None when unknown.

    None means UNKNOWN, not "no fill" — the caller must treat it as "do not
    act on this leg this cycle", never as evidence the order is gone. Paper
    mode (not armed) and missing order_id also return None: there is no real
    order to ask about, so the caller's paper-mode inference applies.
    """
    if not order_id:
        return None
    armed = POLY_REWARDS_NEW_SDK if live is None else bool(live)
    if not armed:
        return None
    res = _run_sdk({"action": "status", "order_id": order_id})
    if not res.get("ok"):
        log.warning("[lp-live] status lookup failed for %s: %s",
                    str(order_id)[:16], res.get("error") or res)
        return None
    return {"status": res.get("status"),
            "size_matched": float(res.get("size_matched") or 0),
            "original_size": float(res.get("original_size") or 0)}


def cancel_token_orders(token_id, live=None):
    """Cancel EVERY one of our orders on a token — the defensive sweep for
    when a place call timed out and may or may not have been accepted (no
    order_id to cancel individually). True iff nothing of ours can still be
    resting on that token afterward. Paper: no-op True."""
    if not token_id:
        return True
    armed = POLY_REWARDS_NEW_SDK if live is None else bool(live)
    if not armed:
        log.info("[lp-live] PAPER — would sweep-cancel all orders on token %s", token_id[:12])
        return True
    res = _run_sdk({"action": "sweep", "token_id": token_id})
    if not res.get("ok"):
        log.warning("[lp-live] token sweep FAILED for %s: %s", token_id[:12],
                    res.get("not_canceled") or res.get("error") or res)
        return False
    if res.get("canceled"):
        log.info("[lp-live] token sweep cancelled %d order(s) on %s",
                 len(res["canceled"]), token_id[:12])
    return True


def place_at_band(band, side="BUY", size=None, offset_c=0.0, condition_id=None,
                  city=None, kind=None, date=None, station=None):
    """Fires a resting order at band['mid_c'] + offset_c, clamped into the
    reward-eligible range and rounded to the 1c tick. Sized at the band's own
    min_size unless overridden.

    `band` must be a FRESH feeds.poly_rewards.get_band(condition_id) result —
    always re-fetch right before calling this, never reuse one from even a few
    minutes ago (this session watched one market's reference price move 5 times
    in ~35 minutes).

    On success, records the same order_placed ledger entry place_and_track
    writes (via record_order_placed), so the existing check_fills() fill-monitor
    picks it up. Returns the order_id, or None on failure / when not armed.
    """
    price_c = band["mid_c"] + offset_c
    price_c = min(max(price_c, band["band_lo_c"]), band["band_hi_c"])
    price_c = round(price_c)  # SDK requires 0.01 tick, whole cents only
    shares = size if size is not None else band["min_size"]
    token_id = band["token_id"]

    if not POLY_REWARDS_NEW_SDK:
        log.info("[lp-live] PAPER — would place %s %.1f sh @ %dc on %s (band %.1f-%.1fc), "
                 "POLY_REWARDS_NEW_SDK is off", side, shares, price_c, band.get("question"),
                 band["band_lo_c"], band["band_hi_c"])
        return None

    order_id = place_limit(token_id, price_c, shares, side=side, post_only=True, live=True)
    if not order_id:
        return None

    record_order_placed(order_id, token_id, price_c, shares, side,
                        condition_id or band.get("condition_id"), city, kind, date, station)
    log.info("[lp-live] LIVE placed %s %.1f sh @ %dc on %s -> order_id=%s",
             side, shares, price_c, band.get("question"), order_id)
    return order_id
