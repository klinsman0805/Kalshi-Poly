#!/usr/bin/env python3
"""
scripts/poly_sdk_runner.py — subprocess-isolated bridge to the NEW Polymarket
SDK (`polymarket-client`, class AsyncSecureClient).

WHY A SUBPROCESS, AND WHY THIS FILE LIVES HERE. The installed SDK's top-level
package is literally named `polymarket`, colliding with this repo's own
polymarket.py. Worse, the SDK's __init__ does ABSOLUTE self-imports
(`from polymarket.auth import ...`), so it cannot be loaded under an alias the
way modules/poly_rewards_live.py used to try — verified 2026-08-04, that path
raised `ModuleNotFoundError: No module named 'polymarket.auth'` every time and
the live placement path had never actually worked in-process.

The only arrangement that satisfies BOTH constraints (SDK importable as
`polymarket`, repo's own polymarket.py still importable in the parent process)
is process isolation. When Python runs a script by path, sys.path[0] is the
SCRIPT'S directory, not the caller's cwd. This file sits in scripts/ — which
contains no polymarket.py — so `import polymarket` below resolves to the pip
SDK, regardless of where the parent process was launched from. Confirmed on
the droplet: run from repo root or from /tmp, both resolve to the venv SDK.

  CRITICAL: this file must NEVER do `sys.path.insert(0, <repo root>)`. Every
  other script in scripts/ does exactly that to import feeds/modules; this one
  must not, or `import polymarket` would bind to the repo file and every order
  call would fail. This script needs nothing from the repo except the raw
  credential values, which it reads straight from the .env file.

PROTOCOL. Reads one JSON command object from stdin, writes exactly one JSON
result object as the last line of stdout (SDK/library chatter may appear on
earlier lines and is ignored by the caller).

  {"action": "place",  "token_id", "price", "size", "side", "post_only"}
      -> {"ok", "order_id", "status", "code", "message"}
  {"action": "cancel", "order_id"}
      -> {"ok", "canceled": [...], "not_canceled": {...}}
  {"action": "status", "order_id"}
      -> {"ok", "status", "size_matched", "original_size"}
  {"action": "sweep",  "token_id"}          # cancel ALL our orders on a token
      -> {"ok", "canceled": [...], "not_canceled": {...}}

Credentials come from POLY_ENV_PATH (default /opt/kalshi-poly/.env), read raw
— no python-dotenv, no repo imports.

Invoked by modules/poly_rewards_live.py; not meant to be run by hand except
for debugging:  echo '{"action":"cancel","order_id":"0x.."}' | \
                  /opt/kalshi-poly/venv/bin/python scripts/poly_sdk_runner.py
"""

import asyncio
import json
import os
import sys
from decimal import Decimal

# Intentionally NO sys.path manipulation — see the module docstring. `import
# polymarket` MUST resolve to the installed SDK here, not the repo file.
from polymarket.clients import AsyncSecureClient

ENV_PATH = os.getenv("POLY_ENV_PATH", "/opt/kalshi-poly/.env")


def _env(name, env_path=ENV_PATH):
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _jsonable(obj):
    """Best-effort convert an SDK response object into something JSON can
    serialize, without depending on the SDK's exact model classes."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:  # noqa: BLE001
                break
    return str(obj)


async def _handle(cmd):
    pk, wallet = _env("POLY_PRIVATE_KEY"), _env("POLY_FUNDER")
    if not pk or not wallet:
        return {"ok": False, "error": "missing POLY_PRIVATE_KEY/POLY_FUNDER in env file"}

    client = await AsyncSecureClient.create(private_key=pk, wallet=wallet)
    action = cmd.get("action")

    if action == "place":
        res = await client.place_limit_order(
            token_id=cmd["token_id"],
            price=Decimal(str(cmd["price"])),
            size=Decimal(str(cmd["size"])),
            side=cmd["side"],
            post_only=bool(cmd.get("post_only", True)),
        )
        # AcceptedOrder has .ok/.order_id/.status; RejectedOrder has .ok(False)
        # /.code/.message. Never assume a dict — these are pydantic models.
        order_id = getattr(res, "order_id", None)
        return {
            "ok": bool(getattr(res, "ok", order_id is not None)) and order_id is not None,
            "order_id": order_id,
            "status": getattr(res, "status", None),
            "code": getattr(res, "code", None),
            "message": getattr(res, "message", None),
        }

    if action == "cancel":
        res = await client.cancel_order(order_id=cmd["order_id"])
        return _cancel_result(res, target=cmd["order_id"])

    if action == "sweep":
        # Cancel EVERY order of ours on one token — the defensive path for
        # when a `place` call timed out and we can't know whether the CLOB
        # accepted it (we hold no order_id to cancel individually).
        res = await client.cancel_market_orders(token_id=cmd["token_id"])
        return _cancel_result(res, target=None)

    if action == "status":
        # get_order returns an OpenOrder model (id/status/size_matched/
        # original_size/...). A lookup failure is reported as ok=False and the
        # caller must treat the order's state as UNKNOWN — never assume gone.
        o = await client.get_order(order_id=cmd["order_id"])
        return {
            "ok": True,
            "status": getattr(o, "status", None),
            "size_matched": float(getattr(o, "size_matched", 0) or 0),
            "original_size": float(getattr(o, "original_size", 0) or 0),
        }

    return {"ok": False, "error": f"unknown action: {action!r}"}


def _cancel_result(res, target=None):
    """Shared shaping for cancel_order / cancel_market_orders responses.

    The callers' purpose is "ensure nothing is left resting on the book", so
    "ok" is true when the target is confirmed cancelled, when nothing was
    reported un-cancelled, OR when every reported reason says the order is
    already gone (filled/cancelled/not found) — in all of those cases there is
    nothing left to leak. Markers are deliberately narrow: a reason must state
    the order no longer exists. A genuine failure (auth, network, rate limit)
    won't match and yields ok=False.
    """
    canceled = list(getattr(res, "canceled", None) or [])
    not_canceled = _jsonable(getattr(res, "not_canceled", None))
    gone_markers = ("can't be found", "cannot be found", "not found",
                    "already canceled", "already cancelled", "already matched",
                    "canceled or matched", "cancelled or matched")
    reasons = list(not_canceled.values()) if isinstance(not_canceled, dict) else []
    all_gone = bool(reasons) and all(
        any(mk in str(r).lower() for mk in gone_markers) for r in reasons)
    ok = (target is not None and target in canceled) or (not not_canceled) or all_gone
    return {"ok": ok, "canceled": canceled, "not_canceled": not_canceled}


def main():
    try:
        cmd = json.loads(sys.stdin.read() or "{}")
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"bad command json: {e}"}))
        return
    try:
        result = asyncio.run(_handle(cmd))
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
