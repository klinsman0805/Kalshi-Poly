#!/usr/bin/env python3
"""
scripts/build_kalshi_climo.py — build °F climatology for Kalshi-only stations.

The Tier-A Kalshi cities share Polymarket's station, so their climatology is
already in data/weather_climo.json. This fills the 13 Tier-B ICAOs (Chicago
KMDW, NYC KNYC, etc.) that Polymarket doesn't cover. Reuses the exact PMF
builder from build_weather_climo.py — only the station list and the forced °F
unit differ, so the model math is identical to the Polymarket stations'.

Additive + idempotent: merges into the existing climo file, keyed by ICAO, so
Polymarket's 49 stations are untouched. Skips ICAOs already built as °F unless
--force.

Run:  python scripts/build_kalshi_climo.py [--force]
"""

import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.build_weather_climo import (fetch_hourly, build_pmf, station_coords,  # noqa: E402
                                         K_MAX, OUT)
from feeds.kalshi_stations import KALSHI_STATIONS  # noqa: E402

UNIT = "F"


def main():
    force = "--force" in sys.argv[1:]
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    out = dict(existing)

    # Every distinct Kalshi ICAO; skip those already present as °F (Tier-A shares
    # Polymarket's, which are °F already — nothing to do there).
    icaos = sorted({m["icao"] for m in KALSHI_STATIONS.values()})
    todo = []
    for ic in icaos:
        ent = out.get(ic)
        if not force and ent and "pmf_low" in ent and ent.get("unit") == UNIT:
            print(f"{ic} already built °F — skip")
            continue
        todo.append(ic)
    if not todo:
        print("nothing to build")
        return

    coords = station_coords(todo)
    missing = [i for i in todo if i not in coords]
    if missing:
        print(f"WARN no METAR coords (skipped): {missing}")

    for i, ic in enumerate(t for t in todo if t in coords):
        lat, lon = coords[ic]
        print(f"[{i+1}] {ic} ({lat:.3f},{lon:.3f}) [°F] …", end=" ", flush=True)
        try:
            tz, times, temps = fetch_hourly(lat, lon, UNIT)
            pmf_hi, pmf_lo, n_days = build_pmf(times, temps, K_MAX[UNIT])
        except Exception as e:  # noqa: BLE001
            print(f"FAILED: {e}")
            continue
        out[ic] = {"tz": tz, "lat": lat, "lon": lon, "n_days": n_days, "unit": UNIT,
                   "built": date.today().isoformat(),
                   "pmf": pmf_hi, "pmf_low": pmf_lo}
        OUT.write_text(json.dumps(out))   # write-after-each: safe to interrupt
        print(f"ok tz={tz} days={n_days}")
        time.sleep(1.0)
    print(f"\nwrote {OUT} ({len(out)} stations, {OUT.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
