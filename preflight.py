#!/usr/bin/env python3
"""
preflight.py — Pre-launch readiness check for the Kalshi×Polymarket arb bot.

Run this on a NEW device BEFORE launching app.py. It verifies everything that
does NOT travel with git (secrets, deps, network, auth, funder/proxy setup) and
everything that has bitten us before. It places NO orders and is safe to run any
time. Read-only.

    python preflight.py

Exit code 0 = all critical checks passed; 1 = at least one critical failure.
"""
import os
import sys
import time
import importlib

# ── pretty output ───────────────────────────────────────────────────────────
OK, WARN, FAIL = "\033[32m✓\033[0m", "\033[33m⚠\033[0m", "\033[31m✗\033[0m"
_fails = 0
_warns = 0

def ok(msg):   print(f"  {OK} {msg}")
def warn(msg):
    global _warns; _warns += 1; print(f"  {WARN} {msg}")
def fail(msg):
    global _fails; _fails += 1; print(f"  {FAIL} {msg}")
def section(t): print(f"\n\033[1m{t}\033[0m")


# ── 1. Python + dependencies ──────────────────────────────────────────────────
def check_deps():
    section("1. Python & dependencies")
    print(f"  python: {sys.version.split()[0]}  ({sys.executable})")
    deps = {
        "requests": "requests", "websocket": "websocket-client",
        "dotenv": "python-dotenv", "eth_account": "eth-account",
        "cryptography": "cryptography", "flask": "flask",
        "py_clob_client_v2": "py-clob-client-v2",
    }
    for mod, pkg in deps.items():
        try:
            importlib.import_module(mod)
            ok(f"{pkg}")
        except ImportError:
            fail(f"{pkg} MISSING  →  pip install {pkg}")


# ── 2. Secrets / .env / key file ──────────────────────────────────────────────
def check_env():
    section("2. Secrets & config (.env)")
    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=True)
    except Exception:
        pass
    if not os.path.exists(".env"):
        fail(".env file MISSING — copy it from the other device (NOT in git)")
        return
    ok(".env present")

    required = ["KALSHI_KEY_ID", "POLY_PRIVATE_KEY", "POLY_API_KEY",
                "POLY_API_SECRET", "POLY_API_PASSPHRASE", "POLY_FUNDER"]
    for k in required:
        v = os.getenv(k, "")
        if v and "your-" not in v and "here" not in v:
            ok(f"{k} set")
        else:
            fail(f"{k} missing/placeholder — bot won't function without it")

    # the funder/sig-type gotcha that broke us repeatedly
    if not os.getenv("POLY_FUNDER"):
        fail("POLY_FUNDER empty → Poly v2 client won't build ('client unavailable')")
    if os.getenv("POLY_SIGNATURE_TYPE", "1") not in ("1", "2"):
        warn(f"POLY_SIGNATURE_TYPE={os.getenv('POLY_SIGNATURE_TYPE')} (expected 1=Magic/email, 2=browser wallet)")

    # Kalshi key file
    kf = os.getenv("KALSHI_KEY_FILE", "kalshi.key")
    demo = os.getenv("KALSHI_DEMO", "false").lower() == "true"
    if demo:
        kf = os.getenv("KALSHI_DEMO_KEY_FILE", "kalshi_demo.key")
    if os.path.exists(kf):
        ok(f"Kalshi key file '{kf}' present")
    else:
        fail(f"Kalshi key file '{kf}' MISSING (not in git) — copy it over")


# ── 3. Mode flags (deliberate, not accidental) ────────────────────────────────
def check_mode():
    section("3. Trading mode")
    dry = os.getenv("DRY_RUN", "true").lower() != "false"
    demo = os.getenv("KALSHI_DEMO", "false").lower() == "true"
    strat = os.getenv("STRATEGY", "momentum").lower()
    print(f"  STRATEGY={strat}  KALSHI_DEMO={demo}  DRY_RUN={dry}")
    if strat != "arb":
        warn("STRATEGY is not 'arb' — the momentum strategy will run instead")
    if dry:
        ok("DRY_RUN=true — no real orders (safe to start for observation)")
    else:
        warn("DRY_RUN=false — LIVE. Real orders will be placed. Confirm this is intended.")
    print(f"  ARB_ASSETS={os.getenv('ARB_ASSETS','ETH,SOL')}  "
          f"SIZE={os.getenv('ARB_TRADE_SIZE','5')}  "
          f"STRIKE_BUFFER={os.getenv('ARB_STRIKE_BUFFER_PCT','0.15')}%  "
          f"EXIT_BUFFER={os.getenv('ARB_EXIT_BUFFER_PCT','0')}%")


# ── 4. Network: IPv6 hang + latency to both venues ────────────────────────────
def check_network():
    section("4. Network (IPv4 force + venue latency)")
    # IPv4 force is applied on importing polymarket/engine; import to apply it
    try:
        import polymarket  # noqa  (applies the AF_INET patch)
    except Exception as e:
        fail(f"could not import polymarket (IPv4 patch not applied): {e}")
    import requests
    for name, url in [("Polymarket CLOB", "https://clob.polymarket.com/"),
                      ("Polymarket gamma", "https://gamma-api.polymarket.com/events?slug=x"),
                      ("Kalshi", "https://api.elections.kalshi.com/trade-api/v2/exchange/status")]:
        t0 = time.time()
        try:
            requests.get(url, timeout=12)
            ms = (time.time() - t0) * 1000
            if ms < 2000:
                ok(f"{name}: {ms:.0f}ms")
            elif ms < 8000:
                warn(f"{name}: {ms:.0f}ms (slow — watch for throttling)")
            else:
                fail(f"{name}: {ms:.0f}ms — likely IPv6 hang or routing issue (arb infeasible this slow)")
        except Exception as e:
            fail(f"{name}: error {e}")


# ── 5. Auth on both venues (read-only calls) ──────────────────────────────────
def check_auth():
    section("5. Authentication (read-only)")
    # Kalshi: signed GET balance
    try:
        from unittest.mock import MagicMock
        sys.modules.setdefault("websocket", MagicMock())
        import engine, requests
        h = engine._auth_headers("GET", "/trade-api/v2/portfolio/balance")
        r = requests.get(engine.API_BASE + "/portfolio/balance", headers=h, timeout=10)
        if r.ok:
            bal = r.json().get("balance", 0) / 100.0
            ok(f"Kalshi auth OK — balance ${bal:.2f}")
        else:
            fail(f"Kalshi auth failed: HTTP {r.status_code} {r.text[:80]}")
    except Exception as e:
        fail(f"Kalshi auth error: {e}")

    # Polymarket: build v2 client + L2 read (the funder/proxy gotcha)
    try:
        import polymarket
        c = polymarket.PolyClient()
        if c._clob is None:
            fail("Poly v2 client did NOT build — check POLY_FUNDER / creds (would block all orders)")
        else:
            ok("Poly v2 client built (funder + creds accepted)")
            try:
                orders = c._clob.get_open_orders()
                ok(f"Poly L2 auth OK — {len(orders)} open orders")
            except Exception as e:
                warn(f"Poly L2 read failed (auth may still be ok for placement): {str(e)[:80]}")
    except Exception as e:
        fail(f"Poly client error: {e}")


# ── 6. Market discovery (can the bot find live markets?) ──────────────────────
def check_markets():
    section("6. Market discovery (both venues)")
    try:
        from unittest.mock import MagicMock
        sys.modules.setdefault("websocket", MagicMock())
        import engine, polymarket
        for a in [x.strip().upper() for x in os.getenv("ARB_ASSETS", "ETH,SOL").split(",")]:
            km = engine.discover_market(a, lambda i, m: None)
            ktick = km.get("ticker") if km else None
            kstrike = km.get("floor_strike") if km else None
            # poly
            import time as _t
            now = int(_t.time()); ws = now - (now % 900)
            pinfo = None
            for w in [ws, ws + 900, ws - 900]:
                pinfo = polymarket.get_market_for_window(a, w)
                if pinfo:
                    break
            kok = "✓" if ktick else "✗"
            pok = "✓" if pinfo else "✗"
            note = "" if kstrike is not None else " (strike null — initialized; backfills when active)"
            if ktick and pinfo:
                ok(f"{a}: Kalshi {kok} {ktick}  Poly {pok}{note}")
            else:
                warn(f"{a}: Kalshi {kok}  Poly {pok} — may just be between windows")
    except Exception as e:
        fail(f"market discovery error: {e}")


def main():
    print("=" * 60)
    print("  ARB BOT PREFLIGHT CHECK")
    print("=" * 60)
    check_deps()
    check_env()
    check_mode()
    check_network()
    check_auth()
    check_markets()
    print("\n" + "=" * 60)
    if _fails:
        print(f"  RESULT: {FAIL} {_fails} CRITICAL issue(s), {_warns} warning(s) — DO NOT launch until fixed")
        sys.exit(1)
    elif _warns:
        print(f"  RESULT: {OK} all critical checks passed, {_warns} warning(s) — review before launch")
        sys.exit(0)
    else:
        print(f"  RESULT: {OK} all checks passed — ready to launch")
        sys.exit(0)


if __name__ == "__main__":
    main()
