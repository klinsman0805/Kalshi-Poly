"""
modules/poly_rewards_live.py — parameterized GTC placement via Polymarket's
NEW SDK (`polymarket-client`, class AsyncSecureClient), gated behind
POLY_REWARDS_NEW_SDK (default OFF).

Why this exists: the OLD SDK (py_clob_client_v2, what polymarket.py's
place_gtc wraps) cannot sign GTC orders for this proxy-funded account —
"invalid POLY_PROXY signature", reproduced live, root-caused this session
to a missing order-type branch in create_order's proxy-signing path (taker
orders, FOK/FAK, are unaffected — the weather bot keeps using the old SDK
for those). The NEW SDK auto-resolves signing and fixes this — confirmed
live (Buenos Aires and Miami GTC orders both placed successfully).

The new SDK's package is literally named `polymarket`, colliding with this
project's own polymarket.py. Every order placed by hand this session worked
around that by running an isolated script from /tmp, outside the repo, with
no sys.path insertion. This module instead loads the new SDK under an
ALIASED name via importlib pointed straight at its installed file — no cwd
sensitivity, coexists with `import polymarket` (our own file) anywhere else
in the same process, so this becomes a normal importable module instead of
a script that needs re-editing and re-copying to the droplet every time.

place_at_band() is the parameterized entry point: given a fresh
feeds.poly_rewards.get_band() result, it fires directly off that market's
current numbers — no hand-edited PRICE/TOKEN_ID constants, the pattern
used ~6 times this session as Miami's reference price drifted
31->42.5->44.5->37->53.5c across ~35 minutes.

POLY_REWARDS_NEW_SDK=false (default): place_at_band() logs what it would
send and returns None — same shadow-mode discipline as everything else
this session started in. Flip to true only once this path has been
paper-checked end to end.
"""

import asyncio
import importlib.util
import logging
import os
import sys

from modules.poly_rewards_exec import record_order_placed

log = logging.getLogger("modules.poly_rewards_live")

POLY_REWARDS_NEW_SDK = os.getenv("POLY_REWARDS_NEW_SDK", "false").strip().lower() == "true"

_NEW_SDK_ALIAS = "polymarket_client_sdk"
_NEW_SDK_INSTALL_DIR = os.getenv(
    "POLY_NEW_SDK_PATH", "/opt/kalshi-poly/venv/lib/python3.12/site-packages/polymarket",
)
_ENV_PATH = os.getenv("POLY_ENV_PATH", "/opt/kalshi-poly/.env")


def _load_new_sdk():
    """Load the installed `polymarket-client` package under an alias so it
    never collides with this project's own polymarket.py, regardless of cwd
    or import order elsewhere in the process."""
    if _NEW_SDK_ALIAS in sys.modules:
        return sys.modules[_NEW_SDK_ALIAS]
    init_path = os.path.join(_NEW_SDK_INSTALL_DIR, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        _NEW_SDK_ALIAS, init_path, submodule_search_locations=[_NEW_SDK_INSTALL_DIR],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_NEW_SDK_ALIAS] = mod
    spec.loader.exec_module(mod)
    return mod


def _read_env_var(name, env_path=_ENV_PATH):
    with open(env_path) as f:
        for line in f:
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return None


async def _place_limit_order_async(token_id, price, size, side, post_only=True):
    sdk = _load_new_sdk()
    private_key = _read_env_var("POLY_PRIVATE_KEY")
    wallet = _read_env_var("POLY_FUNDER")
    client = await sdk.clients.AsyncSecureClient.create(private_key=private_key, wallet=wallet)
    return await client.place_limit_order(
        token_id=token_id, price=price, size=size, side=side, post_only=post_only,
    )


def _order_id_of(result):
    r = result or {}
    return r.get("orderID") or r.get("orderId") or r.get("order_id")


def place_limit(token_id, price_c, size, side="BUY", post_only=True, live=None):
    """General parameterized limit order via the new SDK. Returns order_id, or
    None on failure / when not armed.

    post_only=True is the resting-maker case (rejected rather than filled if
    it would cross). post_only=False is what the auto-complete path needs: a
    deliberately marketable order that must trade NOW, because leaving a
    one-sided fill open is the whole risk being managed.

    `live` overrides the module gate so a caller with its own flag (the
    two-leg executor has POLY_TWOLEG_LIVE) decides for itself.
    """
    armed = POLY_REWARDS_NEW_SDK if live is None else bool(live)
    price_c = round(float(price_c))
    price = price_c / 100.0
    if not armed:
        log.info("[lp-live] PAPER — would place %s %s %.0f sh @ %dc (post_only=%s)",
                 side, token_id[:12], size, price_c, post_only)
        return None
    try:
        result = asyncio.run(_place_limit_order_async(token_id, price, size, side, post_only))
    except Exception as e:  # noqa: BLE001
        log.warning("[lp-live] %s order failed (%s @ %dc): %s", side, token_id[:12], price_c, e)
        return None
    order_id = _order_id_of(result)
    if not order_id:
        log.warning("[lp-live] placement returned no recognizable order id: %s", result)
    return order_id


def place_at_band(band, side="BUY", size=None, offset_c=0.0, condition_id=None,
                  city=None, kind=None, date=None, station=None):
    """Fires a GTC order at band['mid_c'] + offset_c, clamped into the
    reward-eligible range and rounded to the 1c tick. Sized at the band's
    own min_size unless overridden.

    `band` must be a FRESH feeds.poly_rewards.get_band(condition_id) result
    — always re-fetch right before calling this, never reuse one from even
    a few minutes ago (this session watched one market's reference price
    move 5 times in ~35 minutes).

    On success, records the same order_placed ledger entry place_and_track
    writes (via record_order_placed), so the existing check_fills() fill-
    monitor picks it up regardless of which SDK placed it.

    Returns the order_id on success, None on failure or when
    POLY_REWARDS_NEW_SDK is off (paper-logs what it would have sent).
    """
    price_c = band["mid_c"] + offset_c
    price_c = min(max(price_c, band["band_lo_c"]), band["band_hi_c"])
    price_c = round(price_c)  # new SDK requires 0.01 tick, whole cents only
    price = price_c / 100.0
    shares = size if size is not None else band["min_size"]
    token_id = band["token_id"]

    if not POLY_REWARDS_NEW_SDK:
        log.info("[lp-live] PAPER — would place %s %.1f sh @ %dc on %s (band %.1f-%.1fc), "
                 "POLY_REWARDS_NEW_SDK is off", side, shares, price_c, band.get("question"),
                 band["band_lo_c"], band["band_hi_c"])
        return None

    try:
        result = asyncio.run(_place_limit_order_async(token_id, price, shares, side))
    except Exception as e:  # noqa: BLE001
        log.warning("[lp-live] order placement failed: %s", e)
        return None

    order_id = _order_id_of(result)
    if not order_id:
        log.warning("[lp-live] placement returned no recognizable order id: %s", result)
        return None

    record_order_placed(order_id, token_id, price_c, shares, side,
                        condition_id or band.get("condition_id"), city, kind, date, station)
    log.info("[lp-live] LIVE placed %s %.1f sh @ %dc on %s -> order_id=%s",
             side, shares, price_c, band.get("question"), order_id)
    return order_id
