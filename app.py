"""
app.py — Kalshi Momentum Bot Dashboard
Run:  python app.py
Open: http://localhost:5001
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

os.environ.setdefault("DRY_RUN",     "true")
os.environ.setdefault("KALSHI_DEMO", "false")

try:
    import websocket  # noqa
except ImportError:
    from unittest.mock import MagicMock
    import sys
    sys.modules["websocket"] = MagicMock()

import engine
import trader

# Strategy selector: "arb" (cross-venue Kalshi×Polymarket) or "momentum".
STRATEGY = os.getenv("STRATEGY", "momentum").strip().lower()

if STRATEGY == "arb":
    import polymarket
    import arb_trader

from flask import Flask, Response, jsonify, render_template_string, request

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

# ── Shared state ──────────────────────────────────────────────────────────────
_bot: engine.BotEngine = None
_momentum_traders: dict = {}
_arb_traders: dict = {}
_poly_client = None
_arb_windows: dict = {}          # asset → last seen window_ts (for per-window reset)
_event_queue: queue.Queue = queue.Queue(maxsize=500)
_bot_lock = threading.Lock()

BOT_STATE = {
    "status":             "stopped",
    "dry_run":            os.getenv("DRY_RUN",     "true").lower()  != "false",
    "demo":               os.getenv("KALSHI_DEMO", "true").lower()  != "false",
    "update_count":       0,
    "started_at":         None,
    "markets":            {},
    "snapshots":          {},
    "log":                [],
    "enabled_assets":     list(engine.ASSETS),
    "session_pnl":        0.0,
    "session_trades":     0,
    "session_wins":       0,
    "momentum_positions": {a: None for a in engine.ASSETS},
}

def _push(event_type: str, data: dict):
    payload = json.dumps({"type": event_type, "ts": time.time(), **data})
    try:
        _event_queue.put_nowait(payload)
    except queue.Full:
        pass

def _add_log(icon: str, msg: str):
    entry = {
        "ts":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "icon": icon,
        "msg":  msg,
    }
    BOT_STATE["log"].append(entry)
    if len(BOT_STATE["log"]) > 200:
        BOT_STATE["log"] = BOT_STATE["log"][-200:]
    _push("log", entry)

# ── Bot callbacks ─────────────────────────────────────────────────────────────
def _on_log(icon: str, msg: str):
    _add_log(icon, msg)

def _on_momentum_update(asset: str, position):
    BOT_STATE["momentum_positions"][asset] = position
    _push("momentum_update", {"asset": asset, "position": position})

if STRATEGY == "arb":
    _poly_client = polymarket.PolyClient()
    _arb_traders = arb_trader.build_arb_traders(
        list(engine.ASSETS),
        _poly_client,
        on_log=lambda ic, msg: _add_log(ic, msg),
    )
else:
    for _a in engine.ASSETS:
        _momentum_traders[_a] = trader.MomentumTrader(
            _a,
            on_log=lambda ic, msg: _add_log(ic, msg),
            on_update=_on_momentum_update,
        )

def _on_prices(markets: dict, snapshots: dict):
    BOT_STATE["markets"]   = markets
    BOT_STATE["snapshots"] = snapshots
    if _bot:
        BOT_STATE["update_count"] = _bot.update_count
    _push("prices", {"markets": markets, "snapshots": snapshots})

    if _bot:
        for asset in BOT_STATE["enabled_assets"]:
            snap = _bot.get_snapshot(asset)
            mkt  = markets.get(asset)
            if not (snap and mkt):
                continue
            if STRATEGY == "arb":
                arb = _arb_traders.get(asset)
                if arb is None:
                    continue
                # Arb engine has no internal window detection — reset on new window.
                if _arb_windows.get(asset) != snap.window_ts:
                    _arb_windows[asset] = snap.window_ts
                    arb.reset()
                arb.update(snap)
            else:
                _momentum_traders[asset].update(snap, mkt)

def _on_status(status: str):
    BOT_STATE["status"] = status
    _push("status", {"status": status})

# ── Bot control ───────────────────────────────────────────────────────────────
def _start_bot():
    global _bot
    with _bot_lock:
        if _bot and _bot.is_running():
            return False, "already running"
        engine.DRY_RUN  = BOT_STATE["dry_run"]
        trader.DRY_RUN  = BOT_STATE["dry_run"]
        engine.USE_DEMO = BOT_STATE["demo"]
        if STRATEGY == "arb":
            # arb_trader imported DRY_RUN by value at module load — re-bind it
            # (and polymarket's) so the live/dry toggle actually reaches them.
            arb_trader.DRY_RUN = BOT_STATE["dry_run"]
            if hasattr(polymarket, "DRY_RUN"):
                polymarket.DRY_RUN = BOT_STATE["dry_run"]
        _bot = engine.BotEngine(
            on_log    = _on_log,
            on_prices = _on_prices,
            on_status = _on_status,
        )
        BOT_STATE["update_count"]   = 0
        BOT_STATE["started_at"]     = datetime.now(timezone.utc).isoformat()
        BOT_STATE["session_pnl"]    = 0.0
        BOT_STATE["session_trades"] = 0
        BOT_STATE["session_wins"]   = 0
        BOT_STATE["status"]         = "starting"
        threading.Thread(target=engine.pre_warm_connection, daemon=True, name="http-prewarm").start()
        threading.Thread(target=_bot.start, daemon=True, name="bot-start").start()
        _add_log("→", f"Bot starting  demo={BOT_STATE['demo']}  dry_run={BOT_STATE['dry_run']}")
        return True, "ok"

def _stop_bot():
    global _bot
    with _bot_lock:
        if _bot:
            _bot.stop()
            BOT_STATE["status"] = "stopped"
            _add_log("■", "Bot stopped")
            return True, "ok"
        return False, "not running"

# ── SSE stream ────────────────────────────────────────────────────────────────
def _sse_generator():
    init_data = {k: v for k, v in BOT_STATE.items() if k != "log"}
    yield f"data: {json.dumps({'type': 'init', **init_data})}\n\n"
    for entry in BOT_STATE["log"][-50:]:
        yield f"data: {json.dumps({'type': 'log', **entry})}\n\n"
    last_heartbeat = time.time()
    while True:
        try:
            payload = _event_queue.get(timeout=1.0)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            pass
        if time.time() - last_heartbeat > 15:
            yield f"data: {json.dumps({'type': 'heartbeat', 'ts': time.time()})}\n\n"
            last_heartbeat = time.time()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

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

@app.route("/api/state")
def api_state():
    state = dict(BOT_STATE)
    state["log"] = state["log"][-50:]
    if _bot:
        state["update_count"] = _bot.update_count
    return jsonify(state)

@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json() or {}
    if "dry_run" in data:
        BOT_STATE["dry_run"] = bool(data["dry_run"])
        engine.DRY_RUN = BOT_STATE["dry_run"]
        trader.DRY_RUN = BOT_STATE["dry_run"]
    if "demo" in data:
        BOT_STATE["demo"] = bool(data["demo"])
    _add_log("⚙", f"Config updated: dry_run={BOT_STATE['dry_run']}  demo={BOT_STATE['demo']}")
    _push("config", {"dry_run": BOT_STATE["dry_run"], "demo": BOT_STATE["demo"]})
    return jsonify({"ok": True, "dry_run": BOT_STATE["dry_run"], "demo": BOT_STATE["demo"]})

@app.route("/api/assets", methods=["POST"])
def api_assets():
    data  = request.get_json() or {}
    asset = data.get("asset")
    enabled = data.get("enabled")
    if asset in engine.ASSETS and isinstance(enabled, bool):
        ea = BOT_STATE["enabled_assets"]
        if enabled and asset not in ea:
            ea.append(asset)
        elif not enabled and asset in ea:
            ea.remove(asset)
        _add_log("⚙", f"{asset} {'enabled' if enabled else 'disabled'}")
        _push("config", {"enabled_assets": BOT_STATE["enabled_assets"]})
    return jsonify({"ok": True, "enabled_assets": BOT_STATE["enabled_assets"]})

@app.route("/api/positions")
def api_positions():
    with trader.POSITIONS._lock:
        return jsonify(dict(trader.POSITIONS._data))

# Decision-logging rows that flood trades.jsonl but aren't real trades.
_NOISY_TRADE_TYPES = {"arb_attempt", "arb_ev_abort", "arb_poly_miss"}

@app.route("/api/trades")
def api_trades():
    """Return recent trade-log rows. By default hides the repetitive decision
    rows (arb_attempt/ev_abort/poly_miss) so the panel shows only real trades
    (entries, unwinds). Pass ?all=1 to include everything."""
    show_all = request.args.get("all") == "1"
    try:
        tf = trader.TRADES_FILE
        if Path(tf).exists():
            lines = Path(tf).read_text().strip().splitlines()
            rows  = []
            # scan from the end so we keep the most recent meaningful rows
            for l in reversed(lines):
                if not l.strip():
                    continue
                try:
                    r = json.loads(l)
                except Exception:
                    continue
                if not show_all and r.get("type") in _NOISY_TRADE_TYPES:
                    continue
                rows.append(r)
                if len(rows) >= 100:
                    break
            return jsonify({"trades": rows})
    except Exception:
        pass
    return jsonify({"trades": []})

@app.route("/api/arb")
def api_arb():
    """Full arb observability snapshot: per-asset live prices/spreads/stats/
    positions, the global kill-switch state, and the config thresholds the UI
    needs to render gates (combined threshold, min profit, poly min order, etc.)."""
    if STRATEGY != "arb":
        return jsonify({"strategy": STRATEGY, "arb_enabled": False})
    assets = {a: t.get_state() for a, t in _arb_traders.items()}
    return jsonify({
        "strategy":      "arb",
        "arb_enabled":   True,
        "halted":        arb_trader.TRADING_HALTED,
        "halt_reason":   arb_trader.HALT_REASON,
        "assets":        assets,
        "config": {
            "threshold":      arb_trader.ARB_THRESHOLD,
            "tolerance":      arb_trader.ARB_TOLERANCE,
            "trade_size":     arb_trader.ARB_TRADE_SIZE,
            "min_profit":     arb_trader.ARB_MIN_PROFIT,
            "poly_min_usd":   arb_trader.ARB_POLY_MIN_ORDER_USD,
            "kalshi_slippage":arb_trader.ARB_KALSHI_SLIPPAGE,
            "poly_price_floor": round(arb_trader.ARB_POLY_MIN_ORDER_USD
                                      / max(arb_trader.ARB_TRADE_SIZE, 1) * 100),
            "strike_buffer_pct": arb_trader.ARB_STRIKE_BUFFER_PCT,
            "exit_buffer_pct":   arb_trader.ARB_EXIT_BUFFER_PCT,
            "assets":            arb_trader.ARB_ASSETS,
        },
    })

@app.route("/api/momentum_positions")
def api_momentum_positions():
    if STRATEGY == "arb":
        positions = {}
        for a in engine.ASSETS:
            t = _arb_traders.get(a)
            pos = t.position if t else None
            positions[a] = (pos.__dict__ if pos else None)
        return jsonify({"strategy": "arb", "momentum_positions": positions})
    positions = {a: _momentum_traders[a].get_position() for a in engine.ASSETS}
    return jsonify({"strategy": "momentum", "momentum_positions": positions})

# ── Dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi × Polymarket Arb Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a0d11;--surf:#0f1419;--surf2:#141c24;--border:#1e2a38;--border2:#28394e;
  --text:#cdd9e5;--muted:#4d6478;--dim:#283848;--accent:#00d4aa;--up:#4da6ff;
  --down:#ff6b6b;--warn:#f5a623;--ok:#22c55e;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'IBM Plex Mono',monospace;font-size:13px;overflow:hidden}
.shell{display:flex;flex-direction:column;height:100vh}
.spacer{flex:1}

/* ── Topbar ── */
.topbar{display:flex;align-items:center;gap:12px;padding:0 18px;height:46px;
  border-bottom:1px solid var(--border);background:var(--surf);flex-shrink:0}
.logo{font-size:15px;font-weight:600;letter-spacing:.2em;color:var(--accent);text-transform:uppercase}
.logo em{color:var(--dim);font-style:normal}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex-shrink:0;transition:all .3s}
.sdot.run{background:var(--accent);box-shadow:0 0 7px var(--accent);animation:pulse 2s infinite}
.sdot.disc{background:var(--warn)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#slabel{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.data-dot-wrap{display:flex;align-items:center;gap:6px;font-size:9px;color:var(--muted)}
.data-dot{width:7px;height:7px;border-radius:50%;background:var(--down);flex-shrink:0;transition:background .5s}
.data-dot.live{background:var(--ok);box-shadow:0 0 5px var(--ok)}
.mode-toggle{display:flex;align-items:center;background:var(--surf2);border:1px solid var(--border2);
  border-radius:20px;padding:3px;gap:2px}
.mode-btn{padding:3px 12px;border-radius:17px;border:none;background:transparent;
  font-family:inherit;font-size:9px;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;
  color:var(--muted);transition:all .2s}
.mode-btn.active-monitor{background:rgba(77,166,255,.15);color:var(--up)}
.mode-btn.active-trade{background:rgba(255,107,107,.15);color:var(--down)}
.mode-btn:hover:not(.active-monitor):not(.active-trade){color:var(--text)}
.btn{padding:5px 16px;border-radius:4px;border:1px solid var(--border2);background:transparent;
  color:var(--text);font-family:inherit;font-size:10px;letter-spacing:.1em;cursor:pointer;
  text-transform:uppercase;transition:all .15s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn-stop{border-color:var(--down)!important;color:var(--down)!important}
.btn-stop:hover{background:rgba(255,107,107,.1)!important}

/* ── Stats bar ── */
.stats-bar{display:flex;align-items:stretch;height:52px;border-bottom:1px solid var(--border);
  background:var(--surf);flex-shrink:0}
.stat-cell{flex:1;display:flex;flex-direction:column;justify-content:center;padding:0 20px;
  border-right:1px solid var(--border)}
.stat-cell:last-child{border-right:none}
.stat-lbl{font-size:8px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.stat-val{font-size:18px;font-weight:500;line-height:1}
.stat-val.pos{color:var(--ok)}
.stat-val.neg{color:var(--down)}
.stat-val.neutral{color:var(--text)}

/* ── Markets row ── */
.markets-row{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--border);flex-shrink:0}
.mcard{background:var(--surf);padding:10px 14px;display:flex;flex-direction:column;gap:7px}
.mcard+.mcard{border-left:1px solid var(--border)}
.mcard-top{display:flex;align-items:center;gap:8px}
.masset{font-size:16px;font-weight:600;color:var(--accent)}
.mticker{font-size:9px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mtimer{font-size:10px;padding:1px 6px;border-radius:3px;background:var(--surf2);
  border:1px solid var(--border);color:var(--muted);white-space:nowrap;flex-shrink:0}
.mtimer.warn{color:var(--warn);border-color:rgba(245,166,35,.35)}
.mtimer.crit{color:var(--down);border-color:rgba(255,107,107,.35);animation:pulse 1s infinite}
.mcard-prices{display:grid;grid-template-columns:1fr 1fr 1fr}
.mprice-col{padding:0 10px 0 0}
.mprice-col+.mprice-col{padding-left:10px;border-left:1px solid var(--border)}
.mprice-lbl{font-size:8px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
.mprice-ask{font-size:20px;font-weight:500;line-height:1}
.mprice-ask.up{color:var(--up)}
.mprice-ask.dn{color:var(--down)}
.mprice-ask.hot{color:var(--warn)}
.mprice-sub{font-size:9px;color:var(--muted);margin-top:1px}
.mprice-sub b{color:var(--text)}
.sw{display:inline-flex;align-items:center;cursor:pointer}
.sw-track{width:26px;height:14px;border-radius:7px;background:var(--border2);
  position:relative;transition:background .2s;flex-shrink:0}
.sw-track::after{content:'';position:absolute;top:2px;left:2px;width:10px;height:10px;
  border-radius:50%;background:var(--muted);transition:transform .2s,background .2s}
.sw input{display:none}
.sw input:checked + .sw-track{background:rgba(0,212,170,.35)}
.sw input:checked + .sw-track::after{transform:translateX(12px);background:var(--accent)}

/* ── Halt banner ── */
#halt-banner{background:rgba(255,107,107,.12);border-bottom:2px solid var(--down);
  color:var(--down);padding:8px 18px;font-size:11px;font-weight:600;letter-spacing:.06em;
  flex-shrink:0;display:flex;align-items:center;gap:10px}
#halt-banner::before{content:'\1F6D1';font-size:14px}

/* ── Arb config strip ── */
#arb-cfg-strip{display:flex;align-items:center;gap:14px;padding:5px 18px;background:var(--surf2);
  border-bottom:1px solid var(--border);flex-shrink:0;font-size:9px;color:var(--muted);flex-wrap:wrap}
#arb-cfg-strip .cfg-label{letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
#arb-cfg-strip b{color:var(--text);font-weight:500}
#arb-cfg-items{display:flex;gap:14px;flex-wrap:wrap}

/* ── Arb market cards ── */
.acard{background:var(--surf);padding:10px 14px;display:flex;flex-direction:column;gap:8px}
.acard+.acard{border-left:1px solid var(--border)}
.acard-top{display:flex;align-items:center;gap:8px}
.acard-asset{font-size:16px;font-weight:600;color:var(--accent)}
.acard-link{font-size:8px;padding:1px 6px;border-radius:3px;letter-spacing:.08em;text-transform:uppercase}
.acard-link.on{color:var(--ok);border:1px solid rgba(34,197,94,.4);background:rgba(34,197,94,.06)}
.acard-link.off{color:var(--muted);border:1px solid var(--border2)}
.acard-timer{font-size:10px;color:var(--muted);margin-left:auto}
/* venue price grid */
.acard-venues{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.venue{border:1px solid var(--border);border-radius:4px;padding:5px 8px}
.venue-name{font-size:8px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.venue-row{display:flex;justify-content:space-between;font-size:11px}
.venue-row b{font-weight:500}
.yes-c{color:var(--up)}
.no-c{color:var(--down)}
/* spread readout */
.acard-spreads{display:flex;gap:8px}
.spread{flex:1;border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:9px}
.spread-lbl{color:var(--muted);font-size:8px;letter-spacing:.06em}
.spread-val{font-size:14px;font-weight:600;margin-top:1px}
.spread.hot{border-color:rgba(0,212,170,.5);background:rgba(0,212,170,.07)}
/* strike-distance / basis-risk readout */
.strike-row{font-size:9px;padding:3px 6px;border-radius:3px;margin-top:1px}
.strike-row.safe{color:var(--ok);background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.25)}
.strike-row.danger{color:var(--down);background:rgba(255,107,107,.06);border:1px solid rgba(255,107,107,.3)}
.strike-row.dim{color:var(--muted)}
.strike-row b{font-weight:600}
.spread.hot .spread-val{color:var(--accent)}
.spread .spread-val{color:var(--text)}

/* ── Arb decisions (skip breakdown) ── */
.dec-grid{display:flex;flex-direction:column;gap:5px}
.dec-row{display:flex;align-items:center;gap:10px;padding:5px 9px;border-radius:4px;
  background:var(--surf);border:1px solid var(--border)}
.dec-row.good{border-color:rgba(34,197,94,.35)}
.dec-row.bad{border-color:rgba(255,107,107,.35)}
.dec-name{font-size:10px;flex:1}
.dec-name small{color:var(--muted);font-size:9px}
.dec-count{font-size:15px;font-weight:600;min-width:42px;text-align:right}
.dec-count.good{color:var(--ok)}
.dec-count.bad{color:var(--down)}
.dec-count.zero{color:var(--dim)}
.dec-count.neutral{color:var(--text)}
.dec-section-lbl{font-size:8px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
  margin:8px 0 2px}
/* open arb position chip */
.apos{border:1px solid rgba(0,212,170,.4);background:rgba(0,212,170,.05);border-radius:4px;
  padding:7px 10px;font-size:10px;margin-bottom:6px}
.apos b{color:var(--accent)}

/* ── Momentum positions row ── */
#momentum-row{border-bottom:1px solid var(--border);background:var(--surf);
  padding:7px 14px;flex-shrink:0}
.mom-row-inner{display:flex;align-items:center;gap:14px}
.mom-label{font-size:8px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--muted);flex-shrink:0;min-width:80px}
.mom-cards{display:flex;gap:8px;flex:1;flex-wrap:wrap}
.mom-card{display:flex;align-items:center;gap:5px;padding:3px 8px;border-radius:3px;
  font-size:9px;border:1px solid var(--border2);background:var(--surf2)}
.mom-card.holding{border-color:rgba(245,166,35,.4);background:rgba(245,166,35,.06)}
.mom-card.hedged{border-color:rgba(77,166,255,.4);background:rgba(77,166,255,.06)}
.mom-card.closed{border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.06)}
.mom-cfg{font-size:9px;color:var(--muted);flex-shrink:0;margin-left:auto;white-space:nowrap}

/* ── Main area ── */
.main-area{display:flex;flex-direction:column;flex:1;overflow:hidden;min-height:0}
.content-cols{display:grid;grid-template-columns:1fr 1fr;flex:1;gap:1px;
  background:var(--border);overflow:hidden;min-height:0}
.panel{background:var(--bg);display:flex;flex-direction:column;overflow:hidden;min-height:0}
.ph{display:flex;align-items:center;gap:10px;padding:8px 14px;
  border-bottom:1px solid var(--border);background:var(--surf);flex-shrink:0}
.pt{font-size:9px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.pb{flex:1;overflow-y:auto;padding:10px 14px;min-height:0}
.pb::-webkit-scrollbar{width:3px}
.pb::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.no-data{color:var(--muted);font-size:11px;padding:28px 16px;text-align:center;line-height:1.9}

/* ── Momentum status cards (left panel) ── */
.ms-card{border:1px solid var(--border);border-radius:5px;background:var(--surf);
  margin-bottom:8px;overflow:hidden}
.ms-card.watching{border-color:rgba(245,166,35,.35);background:rgba(245,166,35,.03)}
.ms-card.holding{border-color:rgba(245,166,35,.5);background:rgba(245,166,35,.05)}
.ms-card.hedged{border-color:rgba(77,166,255,.5);background:rgba(77,166,255,.05)}
.ms-card.closed{border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.04)}
.ms-head{display:flex;align-items:center;gap:8px;padding:7px 11px;border-bottom:1px solid var(--border)}
.ms-asset{font-size:13px;font-weight:600;color:var(--accent)}
.ms-ticker{font-size:9px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-timer{font-size:10px;color:var(--muted);flex-shrink:0}
.ms-timer.warn{color:var(--warn)}
.ms-timer.crit{color:var(--down)}
.ms-phase{font-size:8px;padding:2px 6px;border-radius:2px;font-weight:600;letter-spacing:.1em;flex-shrink:0}
.ms-phase.dim{background:var(--surf2);color:var(--muted);border:1px solid var(--border)}
.ms-phase.warn{background:rgba(245,166,35,.15);color:var(--warn);border:1px solid rgba(245,166,35,.3)}
.ms-phase.up{background:rgba(77,166,255,.15);color:var(--up);border:1px solid rgba(77,166,255,.3)}
.ms-phase.ok{background:rgba(34,197,94,.15);color:var(--ok);border:1px solid rgba(34,197,94,.3)}
.ms-prices{display:grid;grid-template-columns:1fr 1fr 1fr;padding:6px 0 2px}
.ms-pcol{padding:4px 11px}
.ms-pcol+.ms-pcol{border-left:1px solid var(--border)}
.ms-plbl{font-size:8px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
.ms-pval{font-size:17px;font-weight:500;line-height:1}
.ms-pval.up{color:var(--up)}
.ms-pval.dn{color:var(--down)}
.ms-pval.hot{color:var(--warn)}
.ms-pval.dim{color:var(--muted)}
.ms-psub{font-size:9px;color:var(--muted);margin-top:1px}
.ms-psub b{color:var(--text)}
.ms-pos{display:flex;align-items:center;gap:8px;padding:5px 11px;
  border-top:1px solid var(--border);font-size:10px;flex-wrap:wrap}
.ms-pos-side.up{color:var(--up)}
.ms-pos-side.dn{color:var(--down)}
.ms-pos-bid{color:var(--muted)}
.ms-pos-pnl.pos{color:var(--ok)}
.ms-pos-pnl.neg{color:var(--down)}
.ms-pos-tp{color:var(--muted)}
.ms-pos-tp.ok{color:var(--ok);font-weight:600}
.ms-pos-hedge{color:var(--up)}

/* ── Trade history ── */
.trade-row{display:flex;align-items:center;gap:10px;padding:7px 14px;
  border-bottom:1px solid var(--border);font-size:10px}
.tbadge{font-size:8px;padding:2px 6px;border-radius:2px;font-weight:600;letter-spacing:.08em;flex-shrink:0}
.tbadge.dry{background:rgba(77,166,255,.1);color:var(--up);border:1px solid rgba(77,166,255,.2)}
.tbadge.live{background:rgba(34,197,94,.1);color:var(--ok);border:1px solid rgba(34,197,94,.2)}
.tbadge.err{background:rgba(255,107,107,.1);color:var(--down);border:1px solid rgba(255,107,107,.2)}
.trade-detail{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trade-pnl{color:var(--ok);font-size:10px;flex-shrink:0}
.trade-ts{color:var(--muted);font-size:9px;flex-shrink:0}

/* ── Log panel ── */
#log-panel{border-top:2px solid var(--border2);background:var(--surf);flex-shrink:0;
  display:flex;flex-direction:column;height:46px;min-height:46px}
#log-panel.expanded{height:var(--log-h,220px)}
#log-drag{height:5px;cursor:ns-resize;flex-shrink:0;border-top:2px solid transparent;
  transition:border-color .15s;margin-top:-2px}
#log-drag:hover{border-top-color:var(--accent)}
.log-ph{display:flex;align-items:center;gap:10px;padding:6px 14px;flex-shrink:0}
.log-pt{font-size:9px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
#log-body{flex:1;overflow-y:auto;padding:4px 14px 6px;min-height:0;display:none}
#log-panel.expanded #log-body{display:block}
#log-panel.expanded .log-ph{border-bottom:1px solid var(--border)}
.log-toggle{font-size:9px;cursor:pointer;color:var(--muted);padding:2px 8px;border-radius:3px;
  border:1px solid var(--border);background:transparent;font-family:inherit;letter-spacing:.08em;
  text-transform:uppercase;transition:all .15s}
.log-toggle:hover{border-color:var(--border2);color:var(--text)}
.log-entry{display:flex;gap:8px;padding:2px 0;border-bottom:1px solid rgba(30,42,56,.5);align-items:baseline}
.le-ts{color:var(--muted);font-size:9px;flex-shrink:0;min-width:54px}
.le-icon{width:16px;text-align:center;flex-shrink:0}
.le-msg{color:var(--text);font-size:10px;word-break:break-all;line-height:1.5}
.le-msg.err{color:var(--down)}
.le-msg.ok{color:var(--ok)}
.le-msg.dim{color:var(--muted)}
.le-msg.warn{color:var(--warn)}
.le-msg.cross{color:var(--muted);opacity:.7}
</style>
</head>
<body>
<div class="shell">

<!-- TOPBAR -->
<div class="topbar">
  <div class="logo">KALSHI<em>/</em>BOT</div>
  <div class="sdot" id="sdot"></div>
  <span id="slabel">STOPPED</span>
  <div class="spacer"></div>
  <div class="data-dot-wrap">
    <div class="data-dot" id="data-dot"></div>
    <span id="data-label">No data</span>
  </div>
  <div class="mode-toggle">
    <button class="mode-btn" id="mode-monitor" onclick="setMode(true)">Monitor</button>
    <button class="mode-btn" id="mode-trade"   onclick="setMode(false)">Trade</button>
  </div>
  <button class="btn"          id="btn-start" onclick="startBot()">&#9654; Start</button>
  <button class="btn btn-stop" id="btn-stop"  onclick="stopBot()" style="display:none">&#9632; Stop</button>
</div>

<!-- STATS BAR -->
<div class="stats-bar">
  <div class="stat-cell">
    <div class="stat-lbl">Session P&amp;L</div>
    <div class="stat-val neutral" id="s-pnl">$0.0000</div>
  </div>
  <div class="stat-cell">
    <div class="stat-lbl">Trades</div>
    <div class="stat-val neutral" id="s-trades">0</div>
  </div>
  <div class="stat-cell">
    <div class="stat-lbl">Win Rate</div>
    <div class="stat-val neutral" id="s-winrate">&#8212;</div>
  </div>
  <div class="stat-cell">
    <div class="stat-lbl">Uptime</div>
    <div class="stat-val neutral" id="s-uptime">&#8212;</div>
  </div>
</div>

<!-- HALT BANNER (hidden unless kill-switch tripped) -->
<div id="halt-banner" style="display:none"></div>

<!-- ARB CONFIG STRIP -->
<div id="arb-cfg-strip">
  <span class="cfg-label">ARB CONFIG</span>
  <span id="arb-cfg-items"></span>
</div>

<!-- ARB MARKET CARDS (per-asset, both venues + spreads) -->
<div class="markets-row" id="arb-cards"></div>

<!-- MAIN AREA -->
<div class="main-area">
  <div class="content-cols">

    <!-- ARB DECISIONS (per-window skip-reason breakdown) -->
    <div class="panel">
      <div class="ph">
        <div class="pt">Arb Decisions <span style="color:var(--muted)">· this window</span></div>
        <div class="spacer"></div>
        <span id="arb-window-age" style="font-size:9px;color:var(--muted)"></span>
      </div>
      <div class="pb" id="arb-decisions-body">
        <div class="no-data">Waiting for arb evaluations…</div>
      </div>
    </div>

    <!-- TRADE HISTORY -->
    <div class="panel">
      <div class="ph">
        <div class="pt">Trade History</div>
        <button class="log-toggle" id="trades-toggle-btn" onclick="toggleTradesAll()"
          title="Show/hide repetitive skip & decision rows">Show skips</button>
        <div class="spacer"></div>
        <div style="display:flex;gap:16px;font-size:9px;color:var(--muted)">
          <span>P&amp;L:&nbsp;<b id="h-pnl" style="color:var(--ok)">+$0.0000</b></span>
          <span>Fills:&nbsp;<b id="h-fills" style="color:var(--text)">0</b></span>
        </div>
      </div>
      <div class="pb" id="trades-body" style="padding:0">
        <div class="no-data">No trades yet</div>
      </div>
    </div>

  </div>

  <!-- LOG PANEL -->
  <div id="log-panel">
    <div id="log-drag" onmousedown="startDrag(event)"></div>
    <div class="log-ph">
      <div class="log-pt">Event Log</div>
      <div class="spacer"></div>
      <div class="data-dot-wrap" style="margin-right:8px">
        <div class="data-dot" id="conn-dot"></div>
        <span id="conn-label" style="font-size:9px;color:var(--muted)">Disconnected</span>
      </div>
      <button class="log-toggle" id="log-clear-btn" onclick="clearLog()" title="Clear the on-screen log (server keeps full history)">Clear</button>
      <button class="log-toggle" id="log-toggle-btn" onclick="toggleLog()">Expand</button>
    </div>
    <div id="log-body"></div>
  </div>
</div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  botRunning:false, dryRun:true, demo:false,
  enabledAssets:['BTC','ETH','SOL'],
  snapshots:{},
  arb:{}, arbCfg:null, arbHalted:false, arbHaltReason:'',
  tradesShowAll:false,
  sessionPnl:0, sessionTrades:0, sessionWins:0,
  startedAt:null, lastPriceTs:0, assetTimers:{},
  logExpanded:false, logHeight:220,
};

// ── SSE ───────────────────────────────────────────────────────────────────────
let es = null, _sseEnabled = true;
function connectSSE() {
  _sseEnabled = true;
  if (es) try { es.close(); } catch(e) {}
  es = new EventSource('/stream');
  es.onopen = () => {
    document.getElementById('conn-dot').classList.add('live');
    document.getElementById('conn-label').textContent = 'Connected';
  };
  es.onerror = () => {
    document.getElementById('conn-dot').classList.remove('live');
    document.getElementById('conn-label').textContent = 'Reconnecting…';
    if (_sseEnabled) setTimeout(connectSSE, 3000);
  };
  es.onmessage = e => handleMsg(JSON.parse(e.data));
}

function handleMsg(msg) {
  const t = msg.type;
  if (t === 'init') {
    S.dryRun        = msg.dry_run;
    S.demo          = msg.demo;
    S.enabledAssets = msg.enabled_assets || ['BTC','ETH','SOL'];
    S.sessionPnl    = msg.session_pnl    || 0;
    S.sessionTrades = msg.session_trades || 0;
    S.sessionWins   = msg.session_wins   || 0;
    S.startedAt     = msg.started_at     || null;
    updateMode(); updateStatus(msg.status);
    S.snapshots = msg.snapshots || {};
    updateStats(); refreshArb();
    (msg.log || []).forEach(addLog);
  } else if (t === 'prices') {
    S.snapshots   = msg.snapshots || {};
    S.lastPriceTs = Date.now();
  } else if (t === 'momentum_update') {
    // arb mode: momentum updates ignored
  } else if (t === 'log') {
    addLog(msg);
  } else if (t === 'status') {
    updateStatus(msg.status);
  } else if (t === 'session_stats') {
    S.sessionPnl    = msg.session_pnl;
    S.sessionTrades = msg.session_trades;
    S.sessionWins   = msg.session_wins;
    updateStats();
  } else if (t === 'config') {
    if (msg.enabled_assets) { S.enabledAssets = msg.enabled_assets; }
    if (msg.dry_run !== undefined) { S.dryRun = !!msg.dry_run; updateMode(); }
  } else if (t === 'order_result') {
    refreshTrades();
  } else if (t === 'positions') {
    refreshTrades();
  }
}

// ── Status ────────────────────────────────────────────────────────────────────
function updateStatus(st) {
  const dot = document.getElementById('sdot');
  dot.className = 'sdot';
  S.botRunning = ['monitoring','connected'].includes(st);
  if (S.botRunning) dot.classList.add('run');
  else if (['discovering','connecting','starting','reconnecting','waiting'].includes(st))
    dot.classList.add('disc');
  document.getElementById('slabel').textContent = st.toUpperCase();
  document.getElementById('btn-start').style.display = S.botRunning ? 'none' : '';
  document.getElementById('btn-stop').style.display  = S.botRunning ? ''     : 'none';
}

function updateMode() {
  document.getElementById('mode-monitor').className =
    'mode-btn' + ( S.dryRun ? ' active-monitor' : '');
  document.getElementById('mode-trade').className =
    'mode-btn' + (!S.dryRun ? ' active-trade'   : '');
}
async function setMode(monitorMode) {
  S.dryRun = monitorMode; updateMode();
  await fetch('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({dry_run: monitorMode})
  });
}

// ── Arb config strip ───────────────────────────────────────────────────────
function renderArbConfig() {
  const c = S.arbCfg; if (!c) return;
  document.getElementById('arb-cfg-items').innerHTML = [
    `Combined&nbsp;<b>&lt;${c.threshold}¢</b>`,
    `Min&nbsp;profit&nbsp;<b>${c.min_profit}¢</b>`,
    `Size&nbsp;<b>${c.trade_size}</b>`,
    `Poly&nbsp;floor&nbsp;<b>≥${c.poly_price_floor}¢</b>`,
    `K&nbsp;slippage&nbsp;<b>+${c.kalshi_slippage}¢</b>`,
    `Tolerance&nbsp;<b>+${c.tolerance}¢</b>`,
  ].join('');
}

// ── Halt banner ──────────────────────────────────────────────────────────────
function renderHalt() {
  const b = document.getElementById('halt-banner');
  if (S.arbHalted) { b.style.display = 'flex'; b.textContent = 'TRADING HALTED — ' + (S.arbHaltReason || 'inspect naked position, restart to clear'); }
  else b.style.display = 'none';
}

// ── Arb market cards (per-asset, both venues + spreads) ──────────────────────
function renderArbCards() {
  document.getElementById('arb-cards').innerHTML =
    ['BTC','ETH','SOL'].map(renderAcard).join('');
}

function renderAcard(asset) {
  const st = S.arb[asset];
  if (!st) return `<div class="acard"><div class="acard-top">
    <span class="acard-asset">${asset}</span>
    <span class="acard-link off">no data</span></div>
    <div style="font-size:10px;color:var(--muted)">Waiting…</div></div>`;
  const L = st.live || {};
  const linked = L.poly_linked;
  const thr = S.arbCfg ? S.arbCfg.threshold : 99;
  const floor = S.arbCfg ? S.arbCfg.poly_price_floor : 20;
  // timer
  let timer = '—';
  if (L.secs_left != null) {
    const s = L.secs_left;
    timer = s >= 60 ? Math.floor(s/60)+'m '+(s%60)+'s' : s+'s';
  }
  const cell = (v) => v == null ? '—' : v + '¢';
  // spread A = K_yes + P_no ; spread B = K_no + P_yes
  const sA = L.spread_a, sB = L.spread_b;
  const hotA = sA != null && sA < thr && (L.p_no   == null || L.p_no   >= floor);
  const hotB = sB != null && sB < thr && (L.p_yes  == null || L.p_yes  >= floor);
  return `<div class="acard">
    <div class="acard-top">
      <span class="acard-asset">${asset}</span>
      <span class="acard-link ${linked?'on':'off'}">${linked?'linked':'no link'}</span>
      <span class="acard-timer">${timer} ${L.ws_confirmed?'·WS':(linked?'·seed':'')}</span>
    </div>
    <div class="acard-venues">
      <div class="venue"><div class="venue-name">Kalshi ask</div>
        <div class="venue-row"><span class="yes-c">YES</span><b>${cell(L.k_yes)}</b></div>
        <div class="venue-row"><span class="no-c">NO</span><b>${cell(L.k_no)}</b></div>
      </div>
      <div class="venue"><div class="venue-name">Polymarket ask</div>
        <div class="venue-row"><span class="yes-c">YES</span><b>${cell(L.p_yes)}</b></div>
        <div class="venue-row"><span class="no-c">NO</span><b>${cell(L.p_no)}</b></div>
      </div>
    </div>
    <div class="acard-spreads">
      <div class="spread ${hotA?'hot':''}"><div class="spread-lbl">K-YES + P-NO</div>
        <div class="spread-val">${sA!=null?sA+'¢':'—'}</div></div>
      <div class="spread ${hotB?'hot':''}"><div class="spread-lbl">K-NO + P-YES</div>
        <div class="spread-val">${sB!=null?sB+'¢':'—'}</div></div>
    </div>
    ${strikeRow(L)}
  </div>`;
}

// Strike-distance / basis-risk readout. Green when safely away from strike,
// red when inside the danger zone (gate would skip).
function strikeRow(L) {
  if (L.dist_pct == null) return '<div class="strike-row dim">strike: waiting…</div>';
  const buf = (S.arbCfg && S.arbCfg.strike_buffer_pct) || 0.15;
  const safe = L.dist_pct >= buf;
  return `<div class="strike-row ${safe?'safe':'danger'}">`
    + `spot ${L.spot} · strike ${L.strike} · `
    + `<b>${L.dist_pct}%</b> from strike `
    + `(${safe?'OK ✓':'DANGER ✗ — gated'}, need ≥${buf}%)</div>`;
}

// ── Arb decisions (per-window skip-reason breakdown) ─────────────────────────
const DEC_ORDER = [
  ['entries',             'Entered',            'both legs filled',           'good'],
  ['qualified',           'Qualified',          'passed combined gate',       'neutral'],
  ['poly_miss',           'Poly miss',          'Poly FOK didn’t fill',  'neutral'],
  ['skip_ev_recheck',     'EV abort',           'edge gone at exec price',    'neutral'],
  ['kalshi_miss_naked',   'Kalshi miss',        'naked → unwound',       'bad'],
  ['kalshi_partial_naked','Partial hedge',      'excess unwound',             'bad'],
];
const SKIP_ORDER = [
  ['skip_near_strike',     'Near strike',     'basis-risk danger zone'],
  ['skip_unwind_risk',     'Unwind risk',     'unwind cost &gt; 3× profit'],
  ['skip_thin_kalshi_depth','Thin Kalshi',    'not enough depth'],
  ['skip_thin_poly_depth', 'Thin Poly',       'not enough depth'],
  ['skip_below_poly_min',  'Below Poly min',  'leg &lt; $1 order'],
  ['skip_below_min_profit','Below min profit','net &lt; 1¢ after fees'],
];

function renderArbDecisions() {
  // aggregate stats across all assets for the current window
  const agg = {};
  let anyData = false, maxAge = 0;
  ['BTC','ETH','SOL'].forEach(a => {
    const st = S.arb[a]; if (!st) return;
    if (st.window_age_s != null) maxAge = Math.max(maxAge, st.window_age_s);
    const s = st.stats || {};
    Object.keys(s).forEach(k => { agg[k] = (agg[k]||0) + s[k]; anyData = true; });
  });
  document.getElementById('arb-window-age').textContent =
    anyData ? ('window age ' + maxAge + 's') : '';
  const body = document.getElementById('arb-decisions-body');

  // open positions first
  let posHtml = '';
  ['BTC','ETH','SOL'].forEach(a => {
    const p = S.arb[a] && S.arb[a].position;
    if (p) posHtml += `<div class="apos">${a} OPEN — <b>K ${p.kalshi_side?.toUpperCase()}@${p.kalshi_price}¢</b> + <b>P ${p.poly_side?.toUpperCase()}@${p.poly_price}¢</b> × ${(p.count||0).toFixed?p.count.toFixed(2):p.count} · exp ~${(p.expected_profit||0).toFixed(2)}¢/ct · ${p.phase}</div>`;
  });

  if (!anyData && !posHtml) { body.innerHTML = '<div class="no-data">Waiting for arb evaluations…</div>'; return; }

  const row = (key, name, sub, cls) => {
    const v = agg[key] || 0;
    const cc = v === 0 ? 'zero' : cls;
    return `<div class="dec-row ${cls}"><div class="dec-name">${name} <small>${sub}</small></div>
      <div class="dec-count ${cc}">${v}</div></div>`;
  };
  let html = posHtml;
  html += '<div class="dec-section-lbl">Outcomes</div><div class="dec-grid">';
  DEC_ORDER.forEach(([k,n,s,c]) => html += row(k,n,s,c));
  html += '</div><div class="dec-section-lbl">Skips (why no trade)</div><div class="dec-grid">';
  SKIP_ORDER.forEach(([k,n,s]) => html += row(k,n,s,'neutral'));
  html += '</div>';
  body.innerHTML = html;
}

// poll arb state (arb data isn't on the SSE stream)
async function refreshArb() {
  try {
    const r = await fetch('/api/arb');
    const d = await r.json();
    if (!d.arb_enabled) return;
    S.arb = d.assets || {};
    S.arbCfg = d.config;
    S.arbHalted = d.halted;
    S.arbHaltReason = d.halt_reason;
    S.lastPriceTs = Date.now();
    renderArbConfig(); renderHalt(); renderArbCards(); renderArbDecisions();
  } catch(e) {}
}

async function toggleAsset(asset, enabled) {
  if (enabled && !S.enabledAssets.includes(asset)) S.enabledAssets.push(asset);
  else if (!enabled) S.enabledAssets = S.enabledAssets.filter(a => a !== asset);
  await fetch('/api/assets', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({asset, enabled})
  });
}


// ── Stats bar ─────────────────────────────────────────────────────────────────
function updateStats() {
  const pnl = S.sessionPnl || 0;
  const el  = document.getElementById('s-pnl');
  el.textContent = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(4);
  el.className   = 'stat-val ' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neutral');
  document.getElementById('s-trades').textContent = S.sessionTrades || 0;
  document.getElementById('s-winrate').textContent = S.sessionTrades > 0
    ? Math.round(S.sessionWins / S.sessionTrades * 100) + '%' : '—';
}

setInterval(() => {
  if (!S.startedAt) { document.getElementById('s-uptime').textContent = '—'; return; }
  const secs = Math.floor((Date.now() - new Date(S.startedAt).getTime()) / 1000);
  const h = Math.floor(secs/3600), m = Math.floor(secs%3600/60), s2 = secs%60;
  document.getElementById('s-uptime').textContent =
    h > 0 ? h+'h '+m+'m' : m > 0 ? m+'m '+s2+'s' : s2+'s';
}, 1000);

setInterval(() => {
  const fresh = S.lastPriceTs && (Date.now() - S.lastPriceTs) < 6000;
  document.getElementById('data-dot').className  = 'data-dot' + (fresh ? ' live' : '');
  document.getElementById('data-label').textContent = fresh ? 'Live' : 'No data';
}, 1000);

// ── Trade history ─────────────────────────────────────────────────────────────
async function refreshTrades() {
  const url = '/api/trades' + (S.tradesShowAll ? '?all=1' : '');
  const d   = await fetch(url).then(r => r.json()).catch(() => ({trades:[]}));
  const pos = await fetch('/api/positions').then(r => r.json()).catch(() => ({}));
  renderTrades(d.trades || []);
  const pnl = pos.realised_pnl || 0;
  document.getElementById('h-pnl').textContent =
    (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(4);
  document.getElementById('h-fills').textContent = pos.total_fills || 0;
}

function toggleTradesAll() {
  S.tradesShowAll = !S.tradesShowAll;
  document.getElementById('trades-toggle-btn').textContent =
    S.tradesShowAll ? 'Hide skips' : 'Show skips';
  refreshTrades();
}

function renderTrades(trades) {
  const body = document.getElementById('trades-body');
  if (!trades.length) {
    body.innerHTML = '<div class="no-data">' +
      (S.tradesShowAll ? 'No trade-log rows yet' : 'No trades yet (skips hidden — toggle ALL to see decisions)') +
      '</div>';
    return;
  }
  body.innerHTML = trades.slice(0, 80).map(t => {
    const ty = t.type || '';
    let bc = 'live', lbl = 'TRADE', det = '';
    if (ty === 'arb_entry') {
      bc = 'ok'; lbl = 'ENTER';
      det = `${t.asset} K-${(t.kalshi_side||'').toUpperCase()}@${t.kalshi_price}¢ + P-${(t.poly_side||'').toUpperCase()}@${t.poly_price}¢ ×${t.count} · comb ${t.combined_cost}¢`;
    } else if (ty === 'arb_unwind') {
      bc = t.fully_unwound ? 'warn' : 'err'; lbl = 'UNWIND';
      det = `${t.asset} ${t.fully_unwound?'flat':'PARTIAL'} · P sold ${t.poly_sold}/${t.poly_filled}` + (t.kalshi_filled ? ` · K sold ${t.kalshi_sold}/${t.kalshi_filled}` : '');
    } else if (ty === 'arb_poly_miss') {
      bc = 'dry'; lbl = 'P-MISS';
      det = `${t.asset} P-${(t.p_side||'').toUpperCase()}@${t.poly_order_price}¢ no fill · ${t.poly_latency_ms}ms`;
    } else if (ty === 'arb_ev_abort') {
      bc = 'dry'; lbl = 'EV-ABORT';
      det = `${t.asset} edge gone · det ${t.detection_net}¢ → exec ${t.execution_net}¢`;
    } else if (ty === 'arb_attempt') {
      bc = 'dry'; lbl = 'SKIP';
      det = `${t.asset} ${(t.reason||'').replace(/_/g,' ')} · K-${(t.k_side||'').toUpperCase()}@${t.k_price}¢ + P@${t.p_price}¢`;
    } else {
      det = `${t.asset||''} ${ty}`;
    }
    const pnlStr = t.pnl != null
      ? `<span class="trade-pnl">${t.pnl>=0?'+':''}$${Math.abs(t.pnl).toFixed(4)}</span>` : '';
    return `<div class="trade-row">
      <span class="tbadge ${bc}">${lbl}</span>
      <span class="trade-detail">${esc(det)}</span>
      ${pnlStr}
      <span class="trade-ts">${(t.ts||'').slice(11,19)}</span>
    </div>`;
  }).join('');
}

// ── Log panel ─────────────────────────────────────────────────────────────────
function addLog(e) {
  const cls = e.icon==='✗'?'err':e.icon==='✅'?'ok':e.icon==='→'?'dim':e.icon==='!'?'warn':e.icon==='~'?'cross':'';
  const body = document.getElementById('log-body');
  const d = document.createElement('div');
  d.className = 'log-entry';
  d.innerHTML = `<span class="le-ts">${e.ts||''}</span>`
    + `<span class="le-icon">${e.icon||'·'}</span>`
    + `<span class="le-msg ${cls}">${esc(e.msg||'')}</span>`;
  body.insertBefore(d, body.firstChild);
  while (body.children.length > 150) body.removeChild(body.lastChild);
}

function clearLog() {
  // Wipe the on-screen event log only. The server keeps the full history in
  // bot_run.log / trades.jsonl — this just declutters the live view.
  const body = document.getElementById('log-body');
  if (body) body.innerHTML = '';
}

function toggleLog() {
  const panel = document.getElementById('log-panel');
  S.logExpanded = !S.logExpanded;
  if (S.logExpanded) {
    panel.classList.add('expanded');
    panel.style.setProperty('--log-h', S.logHeight + 'px');
    document.getElementById('log-toggle-btn').textContent = 'Collapse';
  } else {
    panel.classList.remove('expanded');
    document.getElementById('log-toggle-btn').textContent = 'Expand';
  }
}

let _dragging = false, _dragY0 = 0, _h0 = 0;
function startDrag(e) {
  if (!S.logExpanded) { toggleLog(); return; }
  _dragging = true; _dragY0 = e.clientY; _h0 = S.logHeight;
  document.addEventListener('mousemove', onDrag);
  document.addEventListener('mouseup',   stopDrag);
  e.preventDefault();
}
function onDrag(e) {
  if (!_dragging) return;
  S.logHeight = Math.max(80, Math.min(500, _h0 + (_dragY0 - e.clientY)));
  document.getElementById('log-panel').style.setProperty('--log-h', S.logHeight + 'px');
}
function stopDrag() {
  _dragging = false;
  document.removeEventListener('mousemove', onDrag);
  document.removeEventListener('mouseup',   stopDrag);
}

// ── Bot controls ──────────────────────────────────────────────────────────────
async function startBot() {
  S.startedAt = new Date().toISOString();
  document.getElementById('btn-start').style.display = 'none';
  document.getElementById('btn-stop').style.display  = '';
  document.getElementById('slabel').textContent = 'STARTING';
  document.getElementById('sdot').className = 'sdot disc';
  await fetch('/api/start', {method:'POST'});
  connectSSE();
}
async function stopBot() {
  S.startedAt   = null;
  S.botRunning  = false;
  S.lastPriceTs = 0;
  document.getElementById('btn-stop').style.display  = 'none';
  document.getElementById('btn-start').style.display = '';
  document.getElementById('slabel').textContent = 'STOPPED';
  document.getElementById('sdot').className = 'sdot';
  _sseEnabled = false;
  if (es) { try { es.close(); } catch(e) {} es = null; }
  document.getElementById('conn-dot').classList.remove('live');
  document.getElementById('conn-label').textContent = 'Disconnected';
  await fetch('/api/stop', {method:'POST'});
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
updateMode();
refreshArb();
connectSSE();
setInterval(refreshTrades, 5000);
setInterval(refreshArb, 1500);   // arb state isn't on the SSE stream — poll it
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-16s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    creds_ok = bool(engine.KALSHI_KEY_ID and Path(engine.KALSHI_KEY_FILE).exists())
    if not creds_ok:
        print("\n" + "="*60)
        print("  KALSHI MOMENTUM BOT — Starting in DRY RUN / DEMO mode")
        print("  No credentials found. Set in .env:")
        print("    KALSHI_KEY_ID=your-key-id")
        print("    KALSHI_KEY_FILE=kalshi.key")
        print("="*60 + "\n")
        os.environ["DRY_RUN"]     = "true"
        os.environ["KALSHI_DEMO"] = "true"
        BOT_STATE["dry_run"] = True
        BOT_STATE["demo"]    = True

    print(f"  Dashboard → http://localhost:5001")
    print(f"  Strategy: {STRATEGY.upper()}")
    print(f"  Mode: {'DEMO' if BOT_STATE['demo'] else 'LIVE'}  |  {'DRY RUN' if BOT_STATE['dry_run'] else 'REAL ORDERS'}\n")

    _start_bot()

    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True, use_reloader=False)
