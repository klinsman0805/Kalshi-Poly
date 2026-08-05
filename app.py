"""
app.py — Trading dashboard: WEATHER NEAR-LOCK (Polymarket).

Weather: Polymarket daily-high-temperature markets vs live METAR at the
settlement station — NEAR-LOCK convergence signals + paper forward test.

Run:  python app.py     →  http://localhost:5001
"""

import json
import os
import queue
import signal
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
from feeds.metar import MetarFeed
from modules.weather import WeatherEngine
from modules import weather_exec as weather_exec_mod
from modules.weather_exec import WeatherExecutor
from modules import client_chat

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

# ── Shared state ──────────────────────────────────────────────────────────────
_bot: engine.BotEngine = None
_bot_lock = threading.Lock()
_event_queue: queue.Queue = queue.Queue(maxsize=500)

# Weather NEAR-LOCK (Polymarket daily temperature markets) — on unless WEATHER_ENABLED=false.
WEATHER_ENABLED = os.getenv("WEATHER_ENABLED", "true").strip().lower() == "true"
# The Kalshi 15-min crypto scalper. Retired (no edge at n=135) and hard-wired to
# DRY_RUN, but it still discovers markets and holds a websocket open — real CPU
# and log noise for a decision that can never fire. Off = weather-only host.
CRYPTO_ENGINE_ENABLED = os.getenv("CRYPTO_ENGINE_ENABLED", "true").strip().lower() == "true"
# Read-only dashboard for sharing with a client: the UI hides every control and
# the server REFUSES the mutating routes (hiding a button is not security — the
# POST endpoints are reachable directly). Data/SSE routes stay open.
DASHBOARD_READONLY = os.getenv("DASHBOARD_READONLY", "false").strip().lower() == "true"
# Hide the raw wallet balance and dollar P&L from a shared view; percentages,
# win-rate and calibration still show. Independent of READONLY.
DASHBOARD_HIDE_BALANCE = os.getenv("DASHBOARD_HIDE_BALANCE", "false").strip().lower() == "true"
# Serve a SECOND, read-only copy of this SAME dashboard on another port, from the
# same process — so it mirrors the live executor's real state exactly (positions,
# P&L, signals), just with the controls removed. A separate process can't do this:
# it has its own executor memory and would show a divergent paper track. Requests
# arriving on this port are read-only regardless of the global flag above.
DASHBOARD_READONLY_PORT = os.getenv("DASHBOARD_READONLY_PORT", "").strip()
# Even on the mirror, optionally blank the raw wallet $ (it's a public link).
DASHBOARD_READONLY_HIDE_BALANCE = os.getenv(
    "DASHBOARD_READONLY_HIDE_BALANCE", "false").strip().lower() == "true"
_metar = MetarFeed()
_weather_exec = WeatherExecutor()
_weather = WeatherEngine(_metar, executor=_weather_exec)
_weather_thread = None
_weather_stop = threading.Event()
WEATHER_REFRESH_SEC = int(os.getenv("WEATHER_REFRESH_SEC", "60"))
# How long SIGTERM waits for an in-flight order to reach the ledger before
# giving up and exiting anyway. Must stay below the unit's TimeoutStopSec, or
# systemd SIGKILLs us mid-drain and the wait bought nothing.
SHUTDOWN_DRAIN_SEC = float(os.getenv("SHUTDOWN_DRAIN_SEC", "30"))

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


_metar.on_log = _add_log
_weather.on_log = _add_log
_weather_exec.on_log = _add_log


# ── Engine callbacks ──────────────────────────────────────────────────────────
def _on_log(icon, msg):
    _add_log(icon, msg)


def _on_prices(markets, snapshots):
    """Kalshi snapshot tick. No consumer since scalping was retired — kept as a
    no-op so BotEngine's status/keepalive still drives the dashboard state dot."""
    return


def _on_status(status):
    BOT_STATE["status"] = status
    _push("status", {"status": status})


# ── Weather poll loop (Polymarket temp markets + METAR) ───────────────────────
def _weather_loop():
    # Runs here, not in _start_bot, so a slow or hung exchange call cannot stall
    # startup — and so it happens after the ledger has been rehydrated.
    try:
        _weather_exec.reconcile_on_start()
    except Exception as e:  # noqa: BLE001
        _add_log("✗", f"startup reconcile error: {e}")
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


# ── Lifecycle ─────────────────────────────────────────────────────────────────
def _start_bot():
    global _bot
    with _bot_lock:
        if _bot and _bot.is_running():
            return False, "already running"
        BOT_STATE["started_at"] = datetime.now(timezone.utc).isoformat()
        if CRYPTO_ENGINE_ENABLED:
            engine.DRY_RUN = True
            engine.USE_DEMO = False
            _bot = engine.BotEngine(on_log=_on_log, on_prices=_on_prices, on_status=_on_status)
            BOT_STATE["status"] = "starting"
            threading.Thread(target=engine.pre_warm_connection, daemon=True, name="http-prewarm").start()
            threading.Thread(target=_bot.start, daemon=True, name="bot-start").start()
        else:
            # Scalping was retired (no edge at n=135), but BotEngine still drove
            # the dashboard state dot — so with it off we own the status directly.
            # Skipping it also drops the BTC/ETH/SOL discovery + WS reconnect loop.
            _bot = None
            _on_status("running")
            _add_log("◆", "Kalshi crypto engine DISABLED (CRYPTO_ENGINE_ENABLED=false)")
        global _weather_thread
        if WEATHER_ENABLED and not (_weather_thread and _weather_thread.is_alive()):
            _weather_stop.clear()
            _weather_thread = threading.Thread(target=_weather_loop, daemon=True, name="weather-poll")
            _weather_thread.start()
            _mode = "LIVE — real money" if _weather_exec.is_live else "paper forward-test"
            _add_log("◆", f"Weather NEAR-LOCK engine ENABLED ({_mode})")
        _add_log("→", "Dashboard started — weather feed live (dry-run)")
        return True, "ok"


def _stop_bot():
    global _bot
    with _bot_lock:
        _weather_stop.set()
        if BOT_STATE["status"] == "stopped":
            return False, "not running"
        if _bot:
            _bot.stop()
        BOT_STATE["status"] = "stopped"
        _push("status", {"status": "stopped"})
        _add_log("■", "Dashboard stopped")
        return True, "ok"


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


def _req_readonly():
    """Is THIS request read-only? True if the global flag is set, or the request
    arrived on the dedicated read-only mirror port (same process, second server)."""
    if DASHBOARD_READONLY:
        return True
    return bool(DASHBOARD_READONLY_PORT) and \
        request.environ.get("SERVER_PORT") == DASHBOARD_READONLY_PORT


def _req_hide_balance():
    """Blank the wallet $ for this request? Global setting, plus the mirror port's
    own setting so the public link can hide balance while localhost:5001 shows it."""
    if DASHBOARD_HIDE_BALANCE:
        return True
    return _req_readonly() and DASHBOARD_READONLY_HIDE_BALANCE


def _readonly_block():
    """403 for a mutating route when the dashboard is shared read-only."""
    return jsonify({"ok": False, "msg": "dashboard is read-only"}), 403


@app.route("/api/start", methods=["POST"])
def api_start():
    if _req_readonly():
        return _readonly_block()
    ok, msg = _start_bot()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if _req_readonly():
        return _readonly_block()
    ok, msg = _stop_bot()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/weather")
def api_weather():
    """Weather NEAR-LOCK signals + forward-test executor state."""
    st = _weather.state()
    st["enabled"] = WEATHER_ENABLED
    st["exec"] = _weather_exec.state()
    st["metar"] = {"last_poll": _metar.last_poll_ts, "error": _metar.last_error}
    st["readonly"] = _req_readonly()
    if _req_hide_balance() and st["exec"].get("account"):
        # keep the ratio (real vs modeled) but drop raw dollars and wallet size
        acct = st["exec"]["account"]
        for k in ("usdc", "baseline", "equity", "open_cost", "open_value",
                  "real_pnl", "unrealized"):
            acct.pop(k, None)
    return jsonify(st)


KALSHI_STATE_FILE = os.getenv("KALSHI_WEATHER_STATE", "kalshi_weather_state.json")


@app.route("/api/kalshi")
def api_kalshi():
    """Kalshi weather paper-test snapshot. The Kalshi engine runs in a SEPARATE
    process (kalshi-paper.service) which writes its state to KALSHI_STATE_FILE
    each cycle; the dashboard just reads it — no engine in this process. Read-only
    by nature (paper), so it's served identically on the read-only mirror."""
    try:
        with open(KALSHI_STATE_FILE) as f:
            st = json.load(f)
    except (FileNotFoundError, ValueError):
        return jsonify({"rows": [], "exec": None, "unavailable": True,
                        "note": "Kalshi paper service not running or no snapshot yet"})
    st["readonly"] = True                 # nothing to control here; always view-only
    st["stale_sec"] = (time.time() - st.get("last_refresh")) if st.get("last_refresh") else None
    return jsonify(st)


@app.route("/api/weather_config", methods=["POST"])
def api_weather_config():
    """Set the weather executor mode (paper|live). Live also requires
    WEATHER_LIVE=true in the environment (double gate) — set_mode enforces it."""
    if _req_readonly():
        return _readonly_block()
    data = request.get_json(silent=True) or {}
    if "mode" in data:
        _weather_exec.set_mode(data["mode"])
    return jsonify(_weather_exec.state())


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Read-only client chat — only reachable on the read-only mirror. Answers
    are generated from a curated text snapshot of current state; there is no
    write path from a question to any live action."""
    if not _req_readonly():
        return jsonify({"ok": False, "msg": "chat is only available on the shared dashboard"}), 404
    if not client_chat.GEMINI_API_KEY:
        return jsonify({"ok": False, "msg": "chat isn't configured yet — ask the operator"}), 503

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "msg": "ask a question first"}), 400
    if len(question) > client_chat.MAX_QUESTION_CHARS:
        return jsonify({"ok": False, "msg": f"keep it under {client_chat.MAX_QUESTION_CHARS} characters"}), 400
    if not client_chat.rate_limiter.allow():
        return jsonify({"ok": False, "msg": "the chat is busy right now — try again in a bit"}), 429

    w_state = _weather_exec.state()
    weather_rows = _weather.state().get("rows") or []
    try:
        with open(KALSHI_STATE_FILE) as f:
            k_full = json.load(f)
            k_state = k_full.get("exec") or {}
            kalshi_rows = k_full.get("rows") or []
    except (FileNotFoundError, ValueError):
        k_state, kalshi_rows = {}, []

    context = client_chat.build_context(
        w_state, k_state, BOT_STATE["log"],
        weather_rows=weather_rows, kalshi_rows=kalshi_rows,
        hide_balance=_req_hide_balance())
    try:
        answer = client_chat.ask(question, context)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"couldn't get an answer just now ({e})"}), 502
    return jsonify({"ok": True, "answer": answer})


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
    # The dashboard has NO auth and exposes the live/paper toggle, so on any
    # public host bind loopback and reach it through a tunnel or ssh -L.
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "5001"))
    print("\n" + "=" * 60)
    print("  DASHBOARD — Weather (monitor + dry-run)")
    print(f"  Dashboard → http://{'localhost' if host == '0.0.0.0' else host}:{port}")
    print(f"  Kalshi WS creds: {'found' if creds_ok else 'MISSING (ticker-only data)'}")
    print("=" * 60 + "\n")

    # systemd sends SIGTERM on restart/stop. Without this the process dies
    # wherever it stands — including with a live order in flight, which orphans
    # the fill (see the graceful-shutdown block in modules/weather_exec.py).
    def _on_sigterm(signum, frame):
        _add_log("◆", "SIGTERM — draining in-flight orders")
        drained = weather_exec_mod.begin_shutdown(timeout=SHUTDOWN_DRAIN_SEC)
        if drained:
            _add_log("→", "drained cleanly, exiting")
        else:
            # Deliberately loud: this is the case that can leave a filled
            # position with no ledger record. Reconcile before trusting state.
            log_msg = (f"SHUTDOWN TIMEOUT after {SHUTDOWN_DRAIN_SEC}s with an order "
                       f"STILL IN FLIGHT — a fill may be unrecorded. Run the startup "
                       f"reconcile and check the exchange before resuming.")
            _add_log("✗", log_msg)
            logging.getLogger("app").error(log_msg)
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    _start_bot()

    # Optional read-only mirror on a second port, SAME process → identical live
    # state, controls removed. Requests on this port are read-only via _req_readonly.
    if DASHBOARD_READONLY_PORT and DASHBOARD_READONLY_PORT != str(port):
        from werkzeug.serving import make_server
        ro_host = os.getenv("DASHBOARD_READONLY_HOST", host)
        ro_srv = make_server(ro_host, int(DASHBOARD_READONLY_PORT), app, threaded=True)
        threading.Thread(target=ro_srv.serve_forever, daemon=True,
                         name="ro-mirror").start()
        print(f"  Read-only mirror → http://{ro_host}:{DASHBOARD_READONLY_PORT}"
              f"  (hide-balance={DASHBOARD_READONLY_HIDE_BALANCE})\n")

    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
