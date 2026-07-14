"""
app.py — Trading dashboard: SCALPING (Kalshi paper reference) + COPY-TRADE +
WEATHER NEAR-LOCK (Polymarket).

Monitor + dry-run signals (no real orders in this build).
  • Scalping: Kalshi 15-min up/down vs live Coinbase spot → edge / fee gate /
    paper P&L that settles within the 15-min window. Kept as the reference
    implementation of the Kalshi paper-trade pattern (no live edge at n=135).
  • Copy-trade: Polymarket leaderboard scanner + forward-test executor.
  • Weather: Polymarket daily-high-temperature markets vs live METAR at the
    settlement station — NEAR-LOCK convergence signals + paper forward test.

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
from feeds.metar import MetarFeed
from modules.scalping import ScalpEngine
from modules.copytrader import CopyTraderEngine
from modules.copytrade_exec import CopyTradeExecutor
from modules.weather import WeatherEngine
from modules.weather_exec import WeatherExecutor

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

# ── Shared state ──────────────────────────────────────────────────────────────
_bot: engine.BotEngine = None
_bot_lock = threading.Lock()
_event_queue: queue.Queue = queue.Queue(maxsize=500)

_spot = SpotFeed(assets=engine.ASSETS)
_scalp = ScalpEngine(dry_run=True)

# Copy-trade scanner (Polymarket only, read-only) — off unless COPYTRADE_ENABLED=true.
_copytrade = CopyTraderEngine()
_copytrade_exec = CopyTradeExecutor()   # forward-test executor (paper by default)
_copytrade_thread = None
_copytrade_stop = threading.Event()

# Weather NEAR-LOCK (Polymarket daily temperature markets) — on unless WEATHER_ENABLED=false.
WEATHER_ENABLED = os.getenv("WEATHER_ENABLED", "true").strip().lower() == "true"
_metar = MetarFeed()
_weather_exec = WeatherExecutor()
_weather = WeatherEngine(_metar, executor=_weather_exec)
_weather_thread = None
_weather_stop = threading.Event()
WEATHER_REFRESH_SEC = int(os.getenv("WEATHER_REFRESH_SEC", "60"))

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
_copytrade.on_log = _add_log
_copytrade_exec.on_log = _add_log
_metar.on_log = _add_log
_weather.on_log = _add_log
_weather_exec.on_log = _add_log


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


# ── Weather poll loop (Polymarket temp markets + METAR) ───────────────────────
def _weather_loop():
    settle_every, last_settle = 300, 0.0
    while not _weather_stop.is_set():
        try:
            rows = _weather.refresh()
            if time.time() - last_settle > settle_every:
                _weather_exec.poll()
                last_settle = time.time()
            st = _weather.state()
            st["exec"] = _weather_exec.state()
            _push("weather", st)
        except Exception as e:  # noqa: BLE001
            _add_log("✗", f"weather refresh error: {e}")
        _weather_stop.wait(WEATHER_REFRESH_SEC)


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
    global _bot
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
        global _copytrade_thread
        if _copytrade.enabled and not (_copytrade_thread and _copytrade_thread.is_alive()):
            _copytrade_stop.clear()
            _copytrade_thread = threading.Thread(target=_copytrade_loop, daemon=True, name="copytrade-poll")
            _copytrade_thread.start()
            threading.Thread(target=_copytrade_exec_loop, daemon=True, name="copytrade-exec").start()
            _add_log("◆", "Copy-trade scanner + forward-test executor ENABLED (paper)")
        global _weather_thread
        if WEATHER_ENABLED and not (_weather_thread and _weather_thread.is_alive()):
            _weather_stop.clear()
            _weather_thread = threading.Thread(target=_weather_loop, daemon=True, name="weather-poll")
            _weather_thread.start()
            _add_log("◆", "Weather NEAR-LOCK engine ENABLED (paper forward-test)")
        _add_log("→", "Dashboard started — scalping + copy-trade + weather feeds live (dry-run)")
        return True, "ok"


def _stop_bot():
    global _bot
    with _bot_lock:
        _copytrade_stop.set()
        _weather_stop.set()
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


@app.route("/api/weather")
def api_weather():
    """Weather NEAR-LOCK signals + forward-test executor state."""
    st = _weather.state()
    st["enabled"] = WEATHER_ENABLED
    st["exec"] = _weather_exec.state()
    st["metar"] = {"last_poll": _metar.last_poll_ts, "error": _metar.last_error}
    return jsonify(st)


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
    print("  DASHBOARD — Scalping (paper) + Copy-trade (monitor + dry-run)")
    print(f"  Dashboard → http://localhost:5001")
    print(f"  Kalshi WS creds: {'found' if creds_ok else 'MISSING (ticker-only data)'}")
    print("=" * 60 + "\n")

    _start_bot()
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True, use_reloader=False)
