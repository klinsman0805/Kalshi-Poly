#!/usr/bin/env python3
"""
scripts/build_weather_climo.py — build the remaining-rise climatology table.

For every settlement station currently referenced by a Polymarket temperature
market, pull ~5 years of hourly temperatures (Open-Meteo ERA5 archive, local
time) and estimate, for each (month, local hour):

    P( round(final daily max) − round(running max through this hour) = k )

That PMF is the NEAR-LOCK engine's only model: given today's observed running
max at hour h, it says how much higher the day can still go. It is empirical,
station-specific, and deliberately smoothed (Laplace α) so no bucket is ever
assigned probability 1.0 — the scalping lesson is that overconfident models
donate fees.

Months are pooled ±1 (e.g. July uses Jun+Jul+Aug days) for sample size:
~450 day-samples per (month, hour) from 5 years.

Output: data/weather_climo.json
    {icao: {tz, lat, lon, n_days, pmf: {month: {hour: {k: p}}}}}

Run:  python scripts/build_weather_climo.py [ICAO ...]
      (no args = every metar-source station in current Polymarket events)
"""

import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from feeds.poly_weather import fetch_temperature_events  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "weather_climo.json"
YEARS = 5
K_MAX = 12          # cap on remaining-rise degrees tracked
ALPHA = 0.5         # Laplace smoothing pseudo-count per k bin
METAR_API = "https://aviationweather.gov/api/data/metar"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def station_coords(icaos):
    r = requests.get(METAR_API, params={"ids": ",".join(icaos), "format": "json"},
                     timeout=20)
    r.raise_for_status()
    out = {}
    for o in r.json():
        if o.get("lat") is not None:
            out[o["icaoId"]] = (float(o["lat"]), float(o["lon"]))
    return out


def fetch_hourly(lat, lon):
    end = date.today().replace(day=1)             # full months only
    start = end.replace(year=end.year - YEARS)
    r = requests.get(ARCHIVE, params={
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": "temperature_2m", "timezone": "auto",
    }, timeout=60)
    r.raise_for_status()
    d = r.json()
    return d["timezone"], d["hourly"]["time"], d["hourly"]["temperature_2m"]


def build_pmf(times, temps):
    """counts[month][hour][k] over days; running/final max in whole °C."""
    days = defaultdict(list)                      # 'YYYY-MM-DD' -> [(hour, temp)]
    for t, v in zip(times, temps):
        if v is None:
            continue
        days[t[:10]].append((int(t[11:13]), v))
    counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    n_days = 0
    for day, rows in days.items():
        if len(rows) < 24:
            continue
        n_days += 1
        month = int(day[5:7])
        rows.sort()
        final = round(max(v for _, v in rows))
        run = None
        for hour, v in rows:
            run = v if run is None else max(run, v)
            k = min(max(final - round(run), 0), K_MAX)
            counts[month][hour][k] += 1
    # pool ±1 month, smooth, normalise
    pmf = {}
    for m in range(1, 13):
        pool = [m, m - 1 or 12, m + 1 if m < 12 else 1]
        pmf[str(m)] = {}
        for h in range(24):
            c = defaultdict(int)
            for pm in pool:
                for k, n in counts[pm][h].items():
                    c[k] += n
            total = sum(c.values())
            if total == 0:
                continue
            denom = total + ALPHA * (K_MAX + 1)
            pmf[str(m)][str(h)] = {
                str(k): round((c[k] + ALPHA) / denom, 6) for k in range(K_MAX + 1)
            }
    return pmf, n_days


def main():
    icaos = [a.upper() for a in sys.argv[1:]]
    if not icaos:
        events = fetch_temperature_events()
        icaos = sorted({e["station"] for e in events
                        if e["source"] == "metar" and e["station"]})
        print(f"stations from current Polymarket events: {icaos}")
    coords = station_coords(icaos)
    missing = [i for i in icaos if i not in coords]
    if missing:
        print(f"WARN no coords (skipped): {missing}")

    existing = {}
    if OUT.exists():
        existing = json.loads(OUT.read_text())
    out = dict(existing)
    for i, icao in enumerate(c for c in icaos if c in coords):
        lat, lon = coords[icao]
        print(f"[{i+1}] {icao} ({lat:.3f},{lon:.3f}) …", end=" ", flush=True)
        try:
            tz, times, temps = fetch_hourly(lat, lon)
            pmf, n_days = build_pmf(times, temps)
        except Exception as e:  # noqa: BLE001
            print(f"FAILED: {e}")
            continue
        out[icao] = {"tz": tz, "lat": lat, "lon": lon, "n_days": n_days,
                     "built": date.today().isoformat(), "pmf": pmf}
        print(f"ok tz={tz} days={n_days}")
        time.sleep(1.0)                           # be polite to the free API
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out))
    print(f"\nwrote {OUT} ({len(out)} stations, {OUT.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
