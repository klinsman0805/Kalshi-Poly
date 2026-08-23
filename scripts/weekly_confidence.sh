#!/usr/bin/env bash
# Weekly state-of-the-pipeline snapshot, appended to candidate_data/weekly.log.
#
# Runs on the droplet because that is where the data lives: candidate_data/ is
# gitignored and never leaves the box, so nothing off-host can produce this.
# Labels first (markets settle the morning after, so a fresh pass usually adds
# rows), then scores whatever is resolvable.
set -uo pipefail
cd /opt/kalshi-poly || exit 1
PY=./venv/bin/python
OUT=candidate_data/weekly.log

{
  echo "════════════════════════════════════════════════════════════════"
  echo "WEEKLY CONFIDENCE REPORT — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "════════════════════════════════════════════════════════════════"
  echo
  echo "── labelling pass ──"
  timeout 900 "$PY" scripts/label_candidates.py 2>&1 | tail -12
  echo
  echo "── acceptance test ──"
  timeout 300 "$PY" scripts/confidence_report.py 2>&1
  echo
  echo "── capture volume ──"
  echo "  candidate snapshots: $(cat candidate_data/candidates-*.jsonl 2>/dev/null | wc -l)"
  echo "  labelled markets:    $(wc -l < candidate_data/labels.jsonl 2>/dev/null || echo 0)"
  echo "  days captured:       $(ls candidate_data/candidates-* 2>/dev/null | wc -l)"
  echo
  echo "── system ──"
  echo "  units:  $(systemctl is-active kalshi-bot kalshi-paper poly-rewards esports-recorder | tr '\n' ' ')"
  echo "  gates:  poly=$(grep '^WEATHER_MAX_OPEN=' .env | cut -d= -f2) kalshi=$(grep '^KALSHI_WEATHER_MAX_OPEN=' .env | cut -d= -f2)  (0 = shut)"
  echo "  tests:  $(timeout 300 "$PY" -m pytest tests -q 2>&1 | tail -1)"
  echo "  host:   $(free -m | awk '/^Mem:/{print $7"MB avail"}'), $(df -h / | awk 'NR==2{print $4" disk free"}')"
  echo
} >> "$OUT" 2>&1
