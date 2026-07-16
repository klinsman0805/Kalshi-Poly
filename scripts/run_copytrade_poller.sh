#!/usr/bin/env bash
# run_copytrade_poller.sh — start the copy-trade forward-test poller DETACHED,
# so it survives the terminal/agent session that launched it.
#
# The poller rehydrates open/settled state from copytrade_positions.jsonl on
# start, so restarting it never loses data. This wrapper just makes it durable.
#
#   scripts/run_copytrade_poller.sh start   # launch detached (idempotent)
#   scripts/run_copytrade_poller.sh stop    # stop it
#   scripts/run_copytrade_poller.sh status  # is it alive? show tail
set -euo pipefail

cd "$(dirname "$0")/.."
PIDFILE="copytrade_poller.pid"
LOG="copytrade_exec.log"

# Forward-test config (EV filter + flat-notional-risk sizing).
export COPYTRADE_TOP_N=50
export COPYTRADE_EXEC_INTERVAL=90
export COPYTRADE_EXEC_MAX_USD=5000
export COPYTRADE_EXEC_MAX_OPEN=60
export COPYTRADE_EXEC_ENTRY_MAX_C=85
export COPYTRADE_EXEC_RISK_USD=5
export COPYTRADE_EXEC_MAX_SLIP_C=3

is_alive() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-start}" in
  start)
    if is_alive; then
      echo "already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    # macOS has no setsid. Double-fork instead: the inner `nohup … &` runs in a
    # subshell that exits immediately, so the python process is reparented to
    # init (PPID 1) and outlives this shell/session. nohup ignores SIGHUP.
    ( nohup python -u -m modules.copytrade_exec >> "$LOG" 2>&1 & echo $! > "$PIDFILE" ) &
    sleep 2
    if is_alive; then
      echo "started detached (pid $(cat "$PIDFILE")) → $LOG"
    else
      echo "FAILED to start — check $LOG"; tail -3 "$LOG"; exit 1
    fi
    ;;
  stop)
    if is_alive; then
      kill "$(cat "$PIDFILE")" && echo "stopped (pid $(cat "$PIDFILE"))"
      rm -f "$PIDFILE"
    else
      echo "not running"; rm -f "$PIDFILE"
    fi
    ;;
  status)
    if is_alive; then
      echo "ALIVE (pid $(cat "$PIDFILE"))"
      tail -2 "$LOG" 2>/dev/null || true
    else
      echo "DEAD"
    fi
    ;;
  *) echo "usage: $0 {start|stop|status}"; exit 1 ;;
esac
