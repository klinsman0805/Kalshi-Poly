#!/usr/bin/env python3
"""
scripts/build_weather_climo.py — build the remaining-rise climatology table.

For every settlement station currently referenced by a Polymarket temperature
market, pull ~5 years of hourly temperatures (Open-Meteo ERA5 archive, local
time) and estimate, for each (month, local hour), TWO PMFs:

    pmf     P( round(final daily max) − round(running max through hour) = k )
    pmf_low P( round(running min through hour) − round(final daily min) = k )

These are the NEAR-LOCK engine's only model: given today's observed running
extreme at hour h, how much further the day can still go. Empirical,
station-specific, and deliberately smoothed (Laplace α) so no bucket is ever
assigned probability 1.0 — the scalping lesson is that overconfident models
donate fees.

Months are pooled ±1 (e.g. July uses Jun+Jul+Aug days) for sample size:
~450 day-samples per (month, hour) from 5 years.

Output: data/weather_climo.json
    {icao: {tz, lat, lon, n_days, pmf: {month: {hour: {k: p}}}, pmf_low: {...}}}

Run:  python scripts/build_weather_climo.py [--force] [ICAO ...]
      (no ICAOs = every metar-source station in current Polymarket events;
       --force rebuilds stations already built today)
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
# cap on remaining-rise degrees tracked, per unit (12°C ≈ 22°F span)
K_MAX = {"C": 12, "F": 22}
ALPHA = 0.5         # Laplace smoothing pseudo-count per k bin
METAR_API = "https://aviationweather.gov/api/data/metar"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def station_units():
    """Map station ICAO → settlement unit ('C'|'F') from current Poly events."""
    units = {}
    for e in fetch_temperature_events():
        if e["source"] != "metar" or not e["station"] or not e["buckets"]:
            continue
        units[e["station"]] = e["buckets"][0]["unit"]
    return units


def station_coords(icaos):
    r = requests.get(METAR_API, params={"ids": ",".join(icaos), "format": "json"},
                     timeout=20)
    r.raise_for_status()
    out = {}
    for o in r.json():
        if o.get("lat") is not None:
            out[o["icaoId"]] = (float(o["lat"]), float(o["lon"]))
    return out


def fetch_hourly(lat, lon, unit):
    end = date.today().replace(day=1)             # full months only
    start = end.replace(year=end.year - YEARS)
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": "temperature_2m", "timezone": "auto",
    }
    if unit == "F":
        params["temperature_unit"] = "fahrenheit"
    r = requests.get(ARCHIVE, params=params, timeout=60)
    r.raise_for_status()
    d = r.json()
    return d["timezone"], d["hourly"]["time"], d["hourly"]["temperature_2m"]


def _smooth(counts, k_max):
    """pool ±1 month, Laplace-smooth, normalise → {month: {hour: {k: p}}}."""
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
            denom = total + ALPHA * (k_max + 1)
            pmf[str(m)][str(h)] = {
                str(k): round((c[k] + ALPHA) / denom, 6) for k in range(k_max + 1)
            }
    return pmf


def build_pmf(times, temps, k_max):
    """Remaining-rise (daily max) and remaining-fall (daily min) PMFs."""
    days = defaultdict(list)                      # 'YYYY-MM-DD' -> [(hour, temp)]
    for t, v in zip(times, temps):
        if v is None:
            continue
        days[t[:10]].append((int(t[11:13]), v))
    hi = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    lo = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    n_days = 0
    for day, rows in days.items():
        if len(rows) < 24:
            continue
        n_days += 1
        month = int(day[5:7])
        rows.sort()
        final_max = round(max(v for _, v in rows))
        final_min = round(min(v for _, v in rows))
        run_max = run_min = None
        for hour, v in rows:
            run_max = v if run_max is None else max(run_max, v)
            run_min = v if run_min is None else min(run_min, v)
            k_hi = min(max(final_max - round(run_max), 0), k_max)
            k_lo = min(max(round(run_min) - final_min, 0), k_max)
            hi[month][hour][k_hi] += 1
            lo[month][hour][k_lo] += 1
    return _smooth(hi, k_max), _smooth(lo, k_max), n_days


def main():
    force = "--force" in sys.argv[1:]
    icaos = [a.upper() for a in sys.argv[1:] if not a.startswith("-")]
    units = station_units()
    if not icaos:
        icaos = sorted(units)
        print(f"stations from current Polymarket events: {icaos}")
    coords = station_coords(icaos)
    missing = [i for i in icaos if i not in coords]
    if missing:
        print(f"WARN no coords (skipped): {missing}")

    existing = {}
    if OUT.exists():
        existing = json.loads(OUT.read_text())
    out = dict(existing)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    todo = [c for c in icaos if c in coords]
    for i, icao in enumerate(todo):
        unit = units.get(icao, "C")
        ent = out.get(icao)
        fresh = ent and "pmf_low" in ent and ent.get("built") == date.today().isoformat()
        if not force and fresh and ent.get("unit") == unit:
            print(f"[{i+1}] {icao} already built today ({unit}) — skip")
            continue
        # Backfill: existing entries predate unit tagging and were built in °C.
        # A °C station just needs the tag — no refetch.
        if not force and fresh and unit == "C" and ent.get("unit") is None:
            ent["unit"] = "C"
            OUT.write_text(json.dumps(out))
            print(f"[{i+1}] {icao} tagged °C (no refetch)")
            continue
        lat, lon = coords[icao]
        print(f"[{i+1}] {icao} ({lat:.3f},{lon:.3f}) [{unit}] …", end=" ", flush=True)
        try:
            tz, times, temps = fetch_hourly(lat, lon, unit)
            pmf_hi, pmf_lo, n_days = build_pmf(times, temps, K_MAX[unit])
        except Exception as e:  # noqa: BLE001
            print(f"FAILED: {e}")
            continue
        out[icao] = {"tz": tz, "lat": lat, "lon": lon, "n_days": n_days, "unit": unit,
                     "built": date.today().isoformat(),
                     "pmf": pmf_hi, "pmf_low": pmf_lo}
        # write after every station: interrupts lose nothing, and the running
        # dashboard hot-reloads the table as it grows
        OUT.write_text(json.dumps(out))
        print(f"ok tz={tz} days={n_days}")
        time.sleep(1.0)                           # be polite to the free API
    print(f"\nwrote {OUT} ({len(out)} stations, {OUT.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
