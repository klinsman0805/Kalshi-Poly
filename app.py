"""
app.py — Two-module trading dashboard: SCALPING (crypto) + SOCCER (World Cup).

Monitor + dry-run signals (no real orders in this build).
  • Scalping: Kalshi 15-min up/down vs live Coinbase spot → edge / fee gate /
    paper P&L that settles within the 15-min window.
  • Soccer:  Kalshi KXWCGAME vs Polymarket per-match → cross-venue spread +
    Draw-underpricing flags, logged as signals.

The legacy cross-venue arb dashboard is preserved in app_arb.py.

Run:  python app.py     →  http://localhost:5001
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

# This build is monitor + dry-run only — never send real orders.
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("KALSHI_DEMO", "false")  # public read endpoints are prod

try:
    import websocket  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock
    import sys
    sys.modules["websocket"] = MagicMock()

import engine
from feeds import poly_leaderboard
from feeds.spot import SpotFeed
from modules.scalping import ScalpEngine
from modules.soccer import SoccerEngine
from modules.soccer_exec import SoccerExecutor
from modules.copytrader import CopyTraderEngine
from modules.copytrade_exec import CopyTradeExecutor

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

# ── Shared state ──────────────────────────────────────────────────────────────
_bot: engine.BotEngine = None
_bot_lock = threading.Lock()
_event_queue: queue.Queue = queue.Queue(maxsize=500)

_spot = SpotFeed(assets=engine.ASSETS)
_scalp = ScalpEngine(dry_run=True)
_soccer_exec = SoccerExecutor()
_soccer = SoccerEngine(dry_run=True, executor=_soccer_exec)
_soccer_thread = None
_soccer_stop = threading.Event()

# Copy-trade scanner (Polymarket only, read-only) — off unless COPYTRADE_ENABLED=true.
_copytrade = CopyTraderEngine()
_copytrade_exec = CopyTradeExecutor()   # forward-test executor (paper by default)
_copytrade_thread = None
_copytrade_stop = threading.Event()

BOT_STATE = {
    "status": "stopped",
    "dry_run": True,
    "started_at": None,
    "log": [],
}


def _push(event_type: str, data: dict):
    try:
        _event_queue.put_nowait(json.dumps({"type": event_type, "ts": time.time(), **data}))
    except queue.Full:
        pass


def _add_log(icon: str, msg: str):
    entry = {"ts": datetime.now(timezone.utc).strftime("%H:%M:%S"), "icon": icon, "msg": msg}
    BOT_STATE["log"].append(entry)
    if len(BOT_STATE["log"]) > 200:
        BOT_STATE["log"] = BOT_STATE["log"][-200:]
    _push("log", entry)


_scalp.on_log = _add_log
_soccer.on_log = _add_log
_soccer_exec.on_log = _add_log
_copytrade.on_log = _add_log
_copytrade_exec.on_log = _add_log


# ── Engine callbacks ──────────────────────────────────────────────────────────
def _on_log(icon, msg):
    _add_log(icon, msg)


def _on_prices(markets, snapshots):
    """Kalshi crypto snapshot tick → recompute scalping signals."""
    try:
        view = _scalp.compute(snapshots, _spot.snapshot())
        _push("scalping", {"assets": view, "session": _scalp.session})
    except Exception as e:  # noqa: BLE001
        _add_log("✗", f"scalp compute error: {e}")


def _on_status(status):
    BOT_STATE["status"] = status
    _push("status", {"status": status})


# ── Soccer poll loop ──────────────────────────────────────────────────────────
def _soccer_loop():
    while not _soccer_stop.is_set():
        try:
            rows = _soccer.refresh()
            _push("soccer", {"matches": rows, "config": _soccer.state()["config"],
                             "exec": _soccer_exec.state()})
        except Exception as e:  # noqa: BLE001
            _add_log("✗", f"soccer refresh error: {e}")
        _soccer_stop.wait(12)


# ── Copy-trade poll loop (Polymarket scan) ────────────────────────────────────
def _copytrade_loop():
    while not _copytrade_stop.is_set():
        try:
            _copytrade.refresh()
            # keep the forward-test executor following the scanner's copyable set
            _copytrade_exec.follow_from_scan(_copytrade.rows)
            st = _copytrade.state()
            st["exec"] = _copytrade_exec.state()
            _push("copytrade", st)
        except Exception as e:  # noqa: BLE001
            _add_log("✗", f"copytrade refresh error: {e}")
        _copytrade_stop.wait(_copytrade.refresh_sec)


# Executor polls faster than the scanner — catch new trades / settlements promptly.
def _copytrade_exec_loop():
    interval = int(os.getenv("COPYTRADE_EXEC_INTERVAL", "60"))
    while not _copytrade_stop.is_set():
        try:
            _copytrade_exec.poll()
        except Exception as e:  # noqa: BLE001
            _add_log("✗", f"copytrade exec error: {e}")
        _copytrade_stop.wait(interval)


# ── Lifecycle ─────────────────────────────────────────────────────────────────
def _start_bot():
    global _bot, _soccer_thread
    with _bot_lock:
        if _bot and _bot.is_running():
            return False, "already running"
        engine.DRY_RUN = True
        engine.USE_DEMO = False
        _spot.start()
        _bot = engine.BotEngine(on_log=_on_log, on_prices=_on_prices, on_status=_on_status)
        BOT_STATE["started_at"] = datetime.now(timezone.utc).isoformat()
        BOT_STATE["status"] = "starting"
        threading.Thread(target=engine.pre_warm_connection, daemon=True, name="http-prewarm").start()
        threading.Thread(target=_bot.start, daemon=True, name="bot-start").start()
        _soccer_stop.clear()
        if not (_soccer_thread and _soccer_thread.is_alive()):
            _soccer_thread = threading.Thread(target=_soccer_loop, daemon=True, name="soccer-poll")
            _soccer_thread.start()
        global _copytrade_thread
        if _copytrade.enabled and not (_copytrade_thread and _copytrade_thread.is_alive()):
            _copytrade_stop.clear()
            _copytrade_thread = threading.Thread(target=_copytrade_loop, daemon=True, name="copytrade-poll")
            _copytrade_thread.start()
            threading.Thread(target=_copytrade_exec_loop, daemon=True, name="copytrade-exec").start()
            _add_log("◆", "Copy-trade scanner + forward-test executor ENABLED (paper)")
        _add_log("→", "Dashboard started — scalping + soccer feeds live (dry-run)")
        return True, "ok"


def _stop_bot():
    global _bot
    with _bot_lock:
        _soccer_stop.set()
        _copytrade_stop.set()
        _spot.stop()
        if _bot:
            _bot.stop()
            BOT_STATE["status"] = "stopped"
            _add_log("■", "Dashboard stopped")
            return True, "ok"
        return False, "not running"


# ── SSE ───────────────────────────────────────────────────────────────────────
def _sse_generator():
    yield f"data: {json.dumps({'type': 'init', 'status': BOT_STATE['status'], 'dry_run': BOT_STATE['dry_run']})}\n\n"
    for entry in BOT_STATE["log"][-50:]:
        yield f"data: {json.dumps({'type': 'log', **entry})}\n\n"
    last_hb = time.time()
    while True:
        try:
            payload = _event_queue.get(timeout=1.0)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            pass
        if time.time() - last_hb > 15:
            yield f"data: {json.dumps({'type': 'heartbeat', 'ts': time.time()})}\n\n"
            last_hb = time.time()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    return Response(_sse_generator(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/start", methods=["POST"])
def api_start():
    ok, msg = _start_bot()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = _stop_bot()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/scalping")
def api_scalping():
    return jsonify(_scalp.state())


@app.route("/api/soccer")
def api_soccer():
    st = _soccer.state()
    st["exec"] = _soccer_exec.state()
    return jsonify(st)


@app.route("/api/soccer_config", methods=["POST"])
def api_soccer_config():
    data = request.get_json() or {}
    if "mode" in data:
        _soccer_exec.set_mode(data["mode"])
    if "stake_usd" in data:
        try:
            _soccer_exec.stake_usd = max(1.0, float(data["stake_usd"]))
            _add_log("⚙", f"[exec] stake = ${_soccer_exec.stake_usd}")
        except (TypeError, ValueError):
            pass
    return jsonify(_soccer_exec.state())


@app.route("/api/copytrade")
def api_copytrade():
    """Copy-trade scan results + forward-test executor state."""
    st = _copytrade.state()
    st["exec"] = _copytrade_exec.state()
    return jsonify(st)


@app.route("/api/copytrade/scan", methods=["POST"])
def api_copytrade_scan():
    """Force an immediate re-scan (respects the flag; no-op when disabled).

    Optional JSON body {metric, window} overrides the ranking before scanning.
    """
    data = request.get_json(silent=True) or {}
    if data.get("metric") in poly_leaderboard.VALID_METRICS:
        _copytrade.metric = data["metric"]
    if data.get("window") in poly_leaderboard.VALID_WINDOWS:
        _copytrade.window = data["window"]
    rows = _copytrade.refresh()
    return jsonify({"ok": _copytrade.enabled, "count": len(rows), **_copytrade.state()})


@app.route("/api/state")
def api_state():
    return jsonify({
        "status": BOT_STATE["status"],
        "dry_run": BOT_STATE["dry_run"],
        "started_at": BOT_STATE["started_at"],
        "log": BOT_STATE["log"][-50:],
    })


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-18s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    creds_ok = bool(engine.KALSHI_KEY_ID and Path(engine.KALSHI_KEY_FILE).exists())
    print("\n" + "=" * 60)
    print("  TWO-MODULE DASHBOARD — Scalping + Soccer (monitor + dry-run)")
    print(f"  Dashboard → http://localhost:5001")
    print(f"  Kalshi WS creds: {'found' if creds_ok else 'MISSING (ticker-only data)'}")
    print("=" * 60 + "\n")

    _start_bot()
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True, use_reloader=False)
