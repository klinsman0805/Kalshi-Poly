#!/usr/bin/env python3
"""
scripts/monitor_poly_rewards.py — periodic snapshot logger for the Polymarket
LP-reward scanner (feeds/poly_rewards.py).

Read-only: runs scan() on a timer and appends one JSONL line per cycle. Its
only job is to answer, over days rather than one snapshot, the question the
scanner alone can't: do the highest-yield opportunities persist, or do they
get filled by other makers within minutes of appearing? That's the evidence
needed before ever resting real capital against this estimate — see
scripts/poly_rewards_report.py for the summary once this has run a while.

Places no orders, holds no keys, needs no credentials.

Run:  python scripts/monitor_poly_rewards.py
Env:  POLY_REWARDS_REFRESH_SEC (default 300 — reward samples are ~60s apart,
      but scanning every market's live book that often is needless load for
      a persistence study)
      POLY_REWARDS_MIN_RATE   (default 5 — skip markets paying under $/day)
      POLY_REWARDS_TOP_N      (default 15 — rows kept per snapshot)
      POLY_REWARDS_ALL_WEATHER (default false — temperature-only unless set)
      POLY_REWARDS_LOG   (default poly_rewards_log.jsonl)
      POLY_REWARDS_STATE (default poly_rewards_state.json)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from feeds.poly_rewards import scan  # noqa: E402
from modules.poly_rewards_autoexec import POLY_REWARDS_AUTOEXEC, PolyRewardsAutoExec  # noqa: E402
from modules.poly_rewards_exec import PolyRewardsExec  # noqa: E402
from modules.poly_rewards_papersim import PolyRewardsPaperSim  # noqa: E402
from modules.poly_rewards_twoleg import POLY_TWOLEG_LIVE, PolyTwoLeg  # noqa: E402

REFRESH_SEC = int(os.getenv("POLY_REWARDS_REFRESH_SEC", "300"))
MIN_RATE = float(os.getenv("POLY_REWARDS_MIN_RATE", "5"))
TOP_N = int(os.getenv("POLY_REWARDS_TOP_N", "15"))
ALL_WEATHER = os.getenv("POLY_REWARDS_ALL_WEATHER", "false").lower() == "true"
LOG_PATH = os.getenv("POLY_REWARDS_LOG", "poly_rewards_log.jsonl")
STATE_PATH = os.getenv("POLY_REWARDS_STATE", "poly_rewards_state.json")

QUESTION_FILTER = None if ALL_WEATHER else (lambda q: "temperature in" in q.lower())


def _log(icon, msg):
    print(f"{time.strftime('%H:%M:%S')} {icon} {msg}", flush=True)


_exec = PolyRewardsExec(on_log=_log)
_autoexec = PolyRewardsAutoExec(exec_=_exec, on_log=_log)
_papersim = PolyRewardsPaperSim(on_log=_log)   # real book/METAR, zero real capital — always on
_twoleg = PolyTwoLeg(on_log=_log)              # two-legged quoting + auto-complete (paper unless POLY_TWOLEG_LIVE)


def _row(r):
    return {
        "slug": r["slug"],
        "question": r["question"],
        "yield_per_dollar_per_day": round(r["yield_per_dollar_per_day"], 4),
        "est_daily_usd": round(r["est_daily_usd"], 2),
        "rate_per_day": round(r["rate_per_day"], 2),
        "mid_c": round(r["mid_c"], 2),
        "two_sided_required": r["two_sided_required"],
        "capital_usd": round(r["capital_usd"], 2),
    }


def run_once():
    results = scan(tag_slug="weather", min_rate=MIN_RATE, question_filter=QUESTION_FILTER)
    try:
        _exec.check(results)   # NEAR-LOCK-gated candidates, logged only — no orders placed
    except Exception as e:  # noqa: BLE001
        _log("✗", f"lp-exec check failed: {e}")
    tracked = _exec._open_tracked_orders()
    # spin up a real API client only when there's actually something to do
    # with it (an order to check/cancel) — pure paper logging needs none
    client = None
    if tracked or POLY_REWARDS_AUTOEXEC:
        try:
            import polymarket
            client = polymarket.PolyClient()
        except Exception as e:  # noqa: BLE001
            _log("✗", f"lp client init failed: {e}")
    if tracked and client:
        try:
            _exec.check_fills(client)
        except Exception as e:  # noqa: BLE001
            _log("✗", f"lp-exec fill check failed: {e}")
    try:
        _autoexec.cycle(client, results)
    except Exception as e:  # noqa: BLE001
        _log("✗", f"lp-autoexec cycle failed: {e}")
    try:
        _papersim.cycle(results)
    except Exception as e:  # noqa: BLE001
        _log("✗", f"lp-papersim cycle failed: {e}")
    try:
        _twoleg.cycle(results)
    except Exception as e:  # noqa: BLE001
        _log("✗", f"lp-twoleg cycle failed: {e}")
    top = results[:TOP_N]
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_scored": len(results),
        "total_pool_usd_day": round(sum(r["rate_per_day"] for r in results), 2),
        "top": [_row(r) for r in top],
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(snapshot) + "\n")

    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f)
    os.replace(tmp, STATE_PATH)  # atomic — any reader never sees a half-written file
    return snapshot


def main():
    _log("▶", f"poly rewards monitor starting — every {REFRESH_SEC}s, "
              f"min_rate=${MIN_RATE}/day, top={TOP_N}, "
              f"scope={'all weather' if ALL_WEATHER else 'temperature only'}")
    _log("▶", f"log={LOG_PATH}  state={STATE_PATH}")
    while True:
        cycle_start = time.time()
        try:
            snap = run_once()
            best = snap["top"][0] if snap["top"] else None
            if best:
                _log("✓", f"{snap['n_scored']} markets, pool ${snap['total_pool_usd_day']}/day — "
                          f"best {best['yield_per_dollar_per_day']*100:.0f}%/day "
                          f"{best['question'][:50]}")
            else:
                _log("✓", f"{snap['n_scored']} markets scored, none above threshold")
        except Exception as e:  # noqa: BLE001
            _log("✗", f"cycle failed: {e}")
        elapsed = time.time() - cycle_start
        time.sleep(max(1.0, REFRESH_SEC - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("■", "stopped")
