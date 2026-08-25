#!/usr/bin/env bash
#
# scripts/provision.sh — bring a fresh Ubuntu droplet up to the running state.
#
# Run ON the new droplet, as root, after copying kalshi-migrate.tar.gz to /root:
#
#     curl -fsSL https://raw.githubusercontent.com/klinsman0805/Kalshi-Poly/main/scripts/provision.sh | bash
#   or, having cloned already:
#     bash /opt/kalshi-poly/scripts/provision.sh
#
# Idempotent: safe to re-run. It does NOT start the trading services. Starting
# them is a deliberate act on a box that can place real orders, so it is left
# to a human who has checked the gates first.
set -euo pipefail

REPO="https://github.com/klinsman0805/Kalshi-Poly.git"
DIR="/opt/kalshi-poly"
ARCHIVE="${ARCHIVE:-/root/kalshi-migrate.tar.gz}"

say() { printf "\n\033[1m== %s\033[0m\n" "$1"; }

say "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git curl ca-certificates >/dev/null
echo "  python $(python3 -V 2>&1 | cut -d' ' -f2), git $(git --version | cut -d' ' -f3)"

# The old box ran 512MB with no swap and came within ~100MB of the OOM killer
# while the live bot held positions. Swap first, before anything memory-hungry.
say "swap"
if swapon --show | grep -q .; then
  echo "  already present: $(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
else
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "  created 1G swapfile and added it to fstab"
fi

say "repository"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin main && git -C "$DIR" reset --hard -q origin/main
  echo "  updated to $(git -C "$DIR" log -1 --format='%h %s' | cut -c1-56)"
else
  git clone -q "$REPO" "$DIR"
  echo "  cloned to $DIR at $(git -C "$DIR" log -1 --format=%h)"
fi
cd "$DIR"

say "virtualenv"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
echo "  installed $(./venv/bin/pip list 2>/dev/null | wc -l) packages"

# Everything gitignored lives only in the archive: .env and kalshi.key, the
# trading ledgers, the candidate/label capture, the esports frames, and the
# climatology table. Losing any of it is unrecoverable.
say "restore data"
if [ -f "$ARCHIVE" ]; then
  tar xzf "$ARCHIVE" -C "$DIR"
  echo "  restored $(tar tzf "$ARCHIVE" | wc -l) entries from $ARCHIVE"
  chmod 600 "$DIR/.env" 2>/dev/null || true
  chmod 600 "$DIR/kalshi.key" 2>/dev/null || true
else
  echo "  !! $ARCHIVE not found — the box has NO credentials and NO history."
  echo "     Copy it over and re-run, or the services will not work."
fi

say "executable bits"
chmod +x scripts/*.sh 2>/dev/null || true

say "systemd units"
for f in deploy/*.service; do
  u=$(basename "$f")
  install -m644 "$f" "/etc/systemd/system/$u"
  echo "  installed $u"
done
systemctl daemon-reload

say "crontab"
if [ -f /root/crontab.saved ]; then
  crontab /root/crontab.saved && echo "  restored from /root/crontab.saved"
elif [ -f deploy/crontab.txt ]; then
  crontab deploy/crontab.txt && echo "  restored from deploy/crontab.txt"
else
  echo "  no crontab to restore"
fi

say "verify"
./venv/bin/python -m compileall -q app.py modules scripts feeds && echo "  compile OK"
./venv/bin/python -m pytest tests -q 2>&1 | tail -1

say "gates"
for k in WEATHER_MAX_OPEN KALSHI_WEATHER_MAX_OPEN WEATHER_LIVE KALSHI_WEATHER_LIVE; do
  printf "  %-26s %s\n" "$k" "$(grep -m1 "^$k=" .env 2>/dev/null | cut -d= -f2 || echo '?')"
done

cat <<'DONE'

== provisioned ==

Services are installed but NOT started, and not enabled. This box can place
real orders, so starting it is a deliberate act.

Check the gates above first. MAX_OPEN=0 means that venue is shut.

  systemctl enable --now esports-recorder     # read-only, safe to start
  systemctl enable --now poly-rewards         # paper only
  systemctl enable --now kalshi-bot           # dashboard + Polymarket weather
  systemctl enable --now kalshi-paper         # REAL Kalshi orders when armed

Then:
  cd /opt/kalshi-poly && ./venv/bin/python scripts/daily_check.py
DONE
