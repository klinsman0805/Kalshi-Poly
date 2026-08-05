#!/usr/bin/env bash
# scripts/daily_lp_check.sh — daily cron wrapper for the two LP health checks:
# real-payment calibration (is the estimator still honest?) and the two-leg
# report (both-fill rate, real auto-complete cost). Run once daily shortly
# after 00:00 UTC, when Polymarket's daily reward distribution has settled.
#
# Calibrates YESTERDAY (UTC), not today — get_earnings_for_user_for_day needs
# the day to have fully closed, and running this at 00:15 UTC for "today"
# would query a day that's 15 minutes old with nothing in it yet.
#
# Installed via crontab -e:
#   15 0 * * * /opt/kalshi-poly/scripts/daily_lp_check.sh >> /opt/kalshi-poly/daily_lp_check.log 2>&1
set -euo pipefail
cd /opt/kalshi-poly

YESTERDAY=$(date -u -d 'yesterday' +%Y-%m-%d 2>/dev/null || date -u -v-1d +%Y-%m-%d)

echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) daily LP check (calibrating $YESTERDAY) ====="
venv/bin/python scripts/poly_rewards_calibrate.py "$YESTERDAY" || echo "calibrate.py FAILED (exit $?)"
echo
venv/bin/python scripts/poly_twoleg_report.py || echo "twoleg_report.py FAILED (exit $?)"
echo
# Oversight last: the breaker state and any belief/reality gap are what decide
# whether the strategy may keep running at all, so they should be the final
# thing in the log rather than buried above the PnL tables.
venv/bin/python scripts/poly_lp_supervisor_report.py || echo "supervisor_report.py FAILED (exit $?)"
echo
