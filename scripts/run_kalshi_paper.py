#!/usr/bin/env python3
"""
scripts/run_kalshi_paper.py — Kalshi weather NEAR-LOCK forward-test runner.

Runs the Kalshi engine (same strategy as Polymarket) in an isolated process,
writing its own ledger, so it never shares memory or state with the live
Polymarket bot. Paper by default; live-CAPABLE but gated off — see
KalshiWeatherExecutor (modules/kalshi_weather_exec.py), which reads its own
KALSHI_WEATHER_LIVE / KALSHI_WEATHER_START_LIVE flags, entirely independent of
Polymarket's WEATHER_LIVE. Flipping one has no effect on the other.

  • WEATHER_EXEC_LOG    → separate Kalshi ledger (default kalshi_weather_paper.jsonl)
  • WEATHER_STAKE_USD   → forced to the Kalshi-specific stake (client wants its own)
  • KALSHI_WEATHER_LIVE / KALSHI_WEATHER_START_LIVE → the actual live gate (default false)

Run:  python scripts/run_kalshi_paper.py
Env:  KALSHI_WEATHER_STAKE_USD (default 8), KALSHI_WEATHER_CITIES (csv, optional;
      default = all mapped cities with climatology), WEATHER_REFRESH_SEC.
"""

import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env explicitly, here, before any modules.*/feeds.* import — otherwise
# modules.weather_exec computes its ENV_ARMED/START_LIVE module constants from
# an empty environment (dotenv only fires later, as a side effect of engine.py
# being pulled in transitively through feeds.kalshi_order). Those two Poly-
# specific constants aren't what gates real trading here (KalshiWeatherExecutor
# uses its own KALSHI_ENV_ARMED for that, computed correctly), but the shared
# state() method surfaces them as "env_armed"/"start_live" on the dashboard —
# without this, Kalshi's tab showed both as False while genuinely live and
# trading real money (caught 2026-07-26).
from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(override=False)

# Kalshi's ledger/stake are independent of Polymarket's, regardless of which
# executor class ends up running — set AFTER dotenv, and UNCONDITIONALLY (not
# setdefault): .env defines WEATHER_EXEC_LOG=weather_live.jsonl for Poly's own
# use, and load_dotenv() above now runs before these lines, so setdefault
# would silently no-op (the key already exists) and this process would recover
# from / write into Poly's live ledger instead of its own. Caught live
# 2026-07-26 — stopped within seconds, no actual write happened, but it was
# one refresh cycle away from doing so.
os.environ["WEATHER_EXEC_LOG"] = "kalshi_weather_paper.jsonl"
os.environ["WEATHER_MISS_LOG"] = "kalshi_weather_misses.jsonl"
os.environ["WEATHER_STAKE_USD"] = os.getenv("KALSHI_WEATHER_STAKE_USD", "8")
os.environ.pop("WEATHER_LIVE_BASELINE_USD", None)   # no Polymarket live baseline here

from feeds.metar import MetarFeed                                # noqa: E402
from modules.kalshi_weather_exec import KalshiWeatherExecutor    # noqa: E402
from modules.kalshi_weather import KalshiWeatherEngine           # noqa: E402
from modules import weather_exec as weather_exec_mod             # noqa: E402

REFRESH_SEC = int(os.getenv("WEATHER_REFRESH_SEC", "60"))
SHUTDOWN_DRAIN_SEC = float(os.getenv("SHUTDOWN_DRAIN_SEC", "30"))
SETTLE_EVERY = 300
# Snapshot the engine+executor state here each cycle so the (separate) dashboard
# process can render a Kalshi tab without running its own engine. Atomic write.
STATE_FILE = os.getenv("KALSHI_WEATHER_STATE", "kalshi_weather_state.json")


def _log(icon, msg):
    print(f"{time.strftime('%H:%M:%S')} {icon} {msg}", flush=True)


def _write_state(engine, execu):
    import json
    try:
        st = engine.state()
        st["exec"] = execu.state()
        st["venue"] = "kalshi"
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)   # atomic — dashboard never reads a half file
    except Exception as e:  # noqa: BLE001
        _log("✗", f"state write failed: {e}")


def main():
    cities = None
    if os.getenv("KALSHI_WEATHER_CITIES"):
        cities = [c.strip() for c in os.environ["KALSHI_WEATHER_CITIES"].split(",") if c.strip()]

    metar = MetarFeed()
    execu = KalshiWeatherExecutor(on_log=_log)
    engine = KalshiWeatherEngine(metar, executor=execu, on_log=_log, cities=cities)

    mode_txt = "LIVE — REAL MONEY" if execu.is_live else "paper"
    _log("◆" if not execu.is_live else "🔴",
         f"Kalshi forward-test up — mode={mode_txt} · stake ${execu.stake_usd} · "
         f"cities={cities or 'all-with-climo'} · ledger={os.environ['WEATHER_EXEC_LOG']}")

    # This process places REAL Kalshi orders whenever KALSHI_WEATHER_LIVE is set,
    # regardless of the "paper" in its name and its unit file. So it needs the
    # same SIGTERM drain as the Polymarket bot: without it a restart can kill it
    # between a fill and its ledger write, orphaning the position.
    def _on_sigterm(signum, frame):
        _log("◆", "SIGTERM — draining in-flight orders")
        if weather_exec_mod.begin_shutdown(timeout=SHUTDOWN_DRAIN_SEC):
            _log("→", "drained cleanly, exiting")
        else:
            _log("✗", f"SHUTDOWN TIMEOUT after {SHUTDOWN_DRAIN_SEC}s with an order STILL "
                      f"IN FLIGHT — a fill may be unrecorded; reconcile before resuming")
        _write_state(engine, execu)
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    last_settle = 0.0
    while True:
        try:
            rows = engine.refresh()
            enters = [r for r in rows if r["signal"] == "ENTER"]
            if time.time() - last_settle > SETTLE_EVERY:
                execu.poll()
                last_settle = time.time()
            _write_state(engine, execu)
            s = execu.state()["session"]
            _log("→", f"{len(rows)} mkts · {len(enters)} ENTER · "
                      f"open {len(execu.open)} · settled {s['settled']} · "
                      f"paper P&L ${s['realized_pnl']:+.2f}")
        except Exception as e:  # noqa: BLE001
            _log("✗", f"loop error: {e}")
        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    main()
