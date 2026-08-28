#!/usr/bin/env python3
"""
scripts/daily_check.py — one command for the daily look.

Leads with anomalies, because the two real bugs found so far were both
invisible to the summary statistics and obvious in the raw numbers. If nothing
is wrong it says so in one line and moves on.

Run:  ./venv/bin/python scripts/daily_check.py
"""

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(override=False)

from modules import anomaly as A                                  # noqa: E402
from modules import confidence as C                               # noqa: E402
from scripts.label_candidates import (                            # noqa: E402
    LABEL_PATH, load_candidates, _market_id)

UNITS = ("kalshi-bot", "kalshi-paper", "poly-rewards", "esports-recorder")


def _sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=60).stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


def main():
    now = datetime.now(timezone.utc)
    print("=" * 74)
    print("DAILY CHECK  %s" % now.strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 74)

    labels = {}
    for line in (LABEL_PATH.open(encoding="utf-8") if LABEL_PATH.exists() else []):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("won") is not None:
            labels[r["market_id"]] = r

    latest, newest_ts = {}, ""
    for rec in load_candidates():
        mid = _market_id(rec)
        newest_ts = max(newest_ts, rec.get("ts") or "")
        p = latest.get(mid)
        if p is None or rec.get("ts", "") > p.get("ts", ""):
            latest[mid] = rec

    pairs = [(r, labels[m]["actual_extreme"])
             for m, r in latest.items() if m in labels]
    # A market is settleable once its own day has closed, i.e. its date is
    # strictly before today. Using yesterday made every 08-23 market fail the
    # comparison on 08-24, so the count came out zero and labels_stalled — the
    # detector meant to catch exactly the resolver bug we just had — could
    # never fire.
    today = now.date().isoformat()
    settleable = [m for m, r in latest.items() if (r.get("date") or "9999") < today]

    # ── anomalies first ──────────────────────────────────────────────────────
    issues = A.scan(pairs, latest_capture_ts=newest_ts,
                    n_settleable=len(settleable),
                    n_labelled=sum(1 for m in settleable if m in labels), now=now)
    down = [u for u in UNITS if _sh("systemctl is-active %s" % u) != "active"]
    if down:
        issues.insert(0, {"level": "ERROR", "code": "unit_down",
                          "detail": "not active: %s" % ", ".join(down)})

    print("\n── ANOMALIES ──")
    if not issues:
        print("   none")
    for i in issues:
        print("   [%s] %s: %s" % (i["level"], i["code"], i["detail"]))
        for row in i.get("rows", []):
            print("        %-30s we observed %s, label says %s (off by %+.1f%s)"
                  % (row["key"], row.get("observed"), row["settled"],
                     row["miss"], row.get("unit") or ""))

    # ── capture ──────────────────────────────────────────────────────────────
    days = sorted({(r.get("ts") or "")[:10] for r in latest.values() if r.get("ts")})
    print("\n── CAPTURE ──")
    print("   markets %d over %d day(s) %s" % (len(latest), len(days),
                                               "%s..%s" % (days[0], days[-1]) if days else ""))
    print("   newest snapshot %s" % (newest_ts[:19] or "none"))
    print("   labelled %d of %d settleable" % (len(labels), len(settleable)))
    by_venue = Counter(r.get("venue") for r in latest.values())
    lab_venue = Counter(latest[m].get("venue") for m in labels if m in latest)
    for v in sorted(by_venue):
        print("     %-7s captured %-5d labelled %d" % (v, by_venue[v], lab_venue.get(v, 0)))

    # ── accuracy, per unit, never aggregated ─────────────────────────────────
    if pairs:
        print("\n── SETTLED vs OUR BUCKET (per unit — never averaged across) ──")
        for d in A.unit_summary(pairs):
            print("   %-4s n=%-4d inside %-5s median miss %-5s max %-5s  above/below %d/%d"
                  % ("°" + d["unit"], d["n"], "%.0f%%" % d["inside_pct"],
                     d["median_miss"], d["max_miss"], d["above"], d["below"]))

        cleared = [(r, a) for r, a in pairs if C.cleared_timing(r)]
        if cleared:
            cs = A.unit_summary(cleared)
            tot = sum(d["n"] for d in cs)
            ins = sum(d["inside"] for d in cs)
            print("   locked markets only: %d of %d inside the bucket (%.0f%%)"
                  % (ins, tot, 100 * ins / tot))

    # ── the funnel that decides everything ───────────────────────────────────
    rows = [(r, labels[m]["won"]) for m, r in latest.items()
            if m in labels and r.get("model_p") is not None and r.get("ask_c") is not None]
    if rows:
        recs = [r for r, _ in rows]
        outs = [o for _, o in rows]
        cr, co, _ = C.split_contested(recs, outs)
        tr, _to, _ = C.split_timing(cr, co)
        n_enter = C.signal_mix(recs).get("ENTER", 0)
        print("\n── FUNNEL ──")
        print("   labelled %d -> contested %d -> also past timing %d"
              % (len(rows), len(cr), len(tr)))
        print("   ENTER rows settled: %d %s"
              % (n_enter, "" if n_enter else "  <- the milestone still pending"))
        if len(tr) >= 30:
            print("   *** usable set has reached %d — run confidence_report.py ***" % len(tr))

    print("\n── SYSTEM ──")
    print("   units   %s" % " ".join("%s=%s" % (u.split("-")[-1], _sh("systemctl is-active %s" % u))
                                     for u in UNITS))
    env = Path("/opt/kalshi-poly/.env")
    if env.exists():
        txt = env.read_text()
        def g(k):
            for ln in txt.splitlines():
                if ln.startswith(k + "="):
                    return ln.split("=", 1)[1]
            return "?"
        print("   gates   poly=%s kalshi=%s   (0 = shut)"
              % (g("WEATHER_MAX_OPEN"), g("KALSHI_WEATHER_MAX_OPEN")))
    print("   host    %s | %s" % (_sh("free -m | awk '/^Mem:/{print $7\"MB avail\"}'"),
                                  _sh("df -h / | awk 'NR==2{print $4\" disk\"}'")))
    print("   git     %s" % _sh("cd /opt/kalshi-poly && git log -1 --format='%h %s' | cut -c1-58"))
    print()


if __name__ == "__main__":
    main()
