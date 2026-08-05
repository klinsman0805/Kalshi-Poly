#!/usr/bin/env python3
"""
scripts/climo_backtest.py — counterfactual calibration backtest.

The live bot only tells us about the ~80 city-days we actually entered — and
those are a biased sample (we only entered when the model already agreed with
itself). This replays REAL METAR history — the same live feed the bot trades
on, not the ERA5 reanalysis the climatology was BUILT from, so this is a
genuine out-of-sample check, not circular — through the exact NEAR-LOCK gate
logic (age-since-trough plateau check, bucket_prob) for EVERY city-day the
station has data for, whether we traded it or not. Output: a calibration
table of claimed model probability vs what actually happened, at a sample
size well beyond the live ledger.

CAVEAT — read before trusting the sample size: aviationweather.gov's free
METAR history endpoint caps at ~400 observations per station regardless of
the requested window. For a ~2/hr station that's ~8 days of real history,
not months. This is a genuine, honest starting archive — real data, not a
deep backtest. Run it periodically (e.g. daily via cron) with --append to
grow results.jsonl over time instead of re-deriving a fresh 8-day sample
each run.

Deliberately does NOT attempt a full strategy backtest (that needs historical
Polymarket/Kalshi order-book prices, which no free API provides going back
further than "now") — this only tests the MODEL's own calibration: does
bucket_prob's claimed P(final == running extreme) match the real outcome
rate, at the exact moment the NEAR-LOCK plateau check would have fired.

Run: python scripts/climo_backtest.py [--out results.jsonl] [--append]
     [--min-local-hour-high 13] [--min-local-hour-low 10] [--min-age-min 120]
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

REPO = Path(__file__).resolve().parent.parent
CLIMO_PATH = REPO / "data" / "weather_climo.json"
API = "https://aviationweather.gov/api/data/metar"
TIMEOUT = 20
CHUNK = 10
HOURS = 500          # the endpoint caps around ~400 obs regardless; ask generously


def _f(c):
    return None if c is None else c * 9.0 / 5.0 + 32.0


def _obs_time(o):
    t = o.get("reportTime")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_all(icaos):
    """Chunked METAR pull, mirrors feeds/metar.py's batching (per-station cap
    on the shared endpoint, not a per-request one)."""
    by_station = defaultdict(list)
    for i in range(0, len(icaos), CHUNK):
        chunk = icaos[i:i + CHUNK]
        try:
            r = requests.get(API, params={"ids": ",".join(chunk), "format": "json",
                                          "hours": HOURS}, timeout=TIMEOUT)
            r.raise_for_status()
            for o in r.json():
                by_station[o.get("icaoId")].append(o)
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed for {chunk}: {e}", file=sys.stderr)
        time.sleep(0.3)
    return by_station


def bucket_prob(climo_entry, kind, month, hour, run_ext, lo, hi):
    """Exact mirror of WeatherEngine.bucket_prob (modules/weather.py) — kept
    standalone here so this script never has to boot the live engine (which
    would try to fetch live Polymarket events) just to reuse one formula."""
    table = climo_entry.get("pmf") if kind == "high" else climo_entry.get("pmf_low")
    pmf = ((table or {}).get(str(month)) or {}).get(str(int(hour)))
    if not pmf:
        return None
    r = round(run_ext)
    p = 0.0
    for k_str, pk in pmf.items():
        final = r + int(k_str) if kind == "high" else r - int(k_str)
        if (lo is None or final >= lo) and (hi is None or final <= hi):
            p += pk
    return p


def replay_station(icao, climo_entry, raw_obs, min_hour_high, min_hour_low, min_age_min):
    """Causal replay: at each observation, only obs up to and including it are
    used to compute the running extreme and its age — never peeks forward.
    Mirrors feeds/metar.py's _reduce age-since-trough logic (a max only counts
    as "held" from the first touch AT/AFTER the day's low, so a day that opens
    near its peak and cools first isn't mistaken for an already-locked one).
    """
    unit = climo_entry.get("unit", "C")
    tz = ZoneInfo(climo_entry["tz"])

    obs = sorted(
        ((_obs_time(o), o) for o in raw_obs if _obs_time(o) is not None and o.get("temp") is not None),
        key=lambda x: x[0])
    if not obs:
        return []

    by_day = defaultdict(list)
    for ts, o in obs:
        local_date = ts.astimezone(tz).date()
        temp_c = float(o["temp"])
        by_day[local_date].append((ts, temp_c, o.get("maxT"), o.get("minT")))

    rows = []
    days_sorted = sorted(by_day)
    for day in days_sorted[:-1]:               # drop the last, still-incomplete day
        day_obs = sorted(by_day[day])
        if len(day_obs) < 6:                    # too sparse to trust a "final" value
            continue
        month = day.month

        # ground truth uses the WHOLE day (computed once, after the fact)
        final_max_c = max(t for _, t, _, _ in day_obs)
        final_min_c = min(t for _, t, _, _ in day_obs)
        for ts, _t, mx, mn in day_obs:
            if ts.astimezone(tz).hour >= 6:
                if mx is not None and float(mx) > final_max_c:
                    final_max_c = float(mx)
                if mn is not None and float(mn) < final_min_c:
                    final_min_c = float(mn)

        fired = {"high": False, "low": False}
        for i in range(len(day_obs)):
            causal = day_obs[:i + 1]           # only obs up to "now" — no peeking
            ts_now = causal[-1][0]
            local_hour = ts_now.astimezone(tz).hour + ts_now.astimezone(tz).minute / 60.0

            run_max = max(t for _, t, _, _ in causal)
            run_min = min(t for _, t, _, _ in causal)
            for ts, _t, mx, mn in causal:
                if ts.astimezone(tz).hour >= 6:
                    if mx is not None and float(mx) > run_max:
                        run_max = float(mx)
                    if mn is not None and float(mn) < run_min:
                        run_min = float(mn)

            def first_touch(value, after_ts, want_max):
                for ts, t, _mx, _mn in causal:
                    if after_ts is not None and ts < after_ts:
                        continue
                    if (t >= value - 1e-9) if want_max else (t <= value + 1e-9):
                        return ts
                return None

            min_ts_raw = first_touch(run_min, None, False) or causal[0][0]
            max_ts_raw = first_touch(run_max, None, True) or causal[0][0]
            max_ts = first_touch(run_max, min_ts_raw, True) or max_ts_raw
            min_ts = first_touch(run_min, max_ts_raw, False) or min_ts_raw
            max_age = (ts_now - max_ts).total_seconds() / 60.0
            min_age = (ts_now - min_ts).total_seconds() / 60.0

            run_max_u = run_max if unit == "C" else _f(run_max)
            run_min_u = run_min if unit == "C" else _f(run_min)
            final_max_u = final_max_c if unit == "C" else _f(final_max_c)
            final_min_u = final_min_c if unit == "C" else _f(final_min_c)

            if not fired["high"] and local_hour >= min_hour_high and max_age >= min_age_min:
                fired["high"] = True
                atm = round(run_max_u)
                p = bucket_prob(climo_entry, "high", month, int(local_hour), run_max_u, atm, atm)
                if p is not None:
                    rows.append({
                        "icao": icao, "date": day.isoformat(), "kind": "high",
                        "month": month, "local_hour": round(local_hour, 1),
                        "run_ext": round(run_max_u, 1), "final": round(final_max_u, 1),
                        "claimed_p": round(p, 4), "hit": round(final_max_u) == atm,
                    })
            if not fired["low"] and local_hour >= min_hour_low and min_age >= min_age_min:
                fired["low"] = True
                atm = round(run_min_u)
                p = bucket_prob(climo_entry, "low", month, int(local_hour), run_min_u, atm, atm)
                if p is not None:
                    rows.append({
                        "icao": icao, "date": day.isoformat(), "kind": "low",
                        "month": month, "local_hour": round(local_hour, 1),
                        "run_ext": round(run_min_u, 1), "final": round(final_min_u, 1),
                        "claimed_p": round(p, 4), "hit": round(final_min_u) == atm,
                    })
            if fired["high"] and fired["low"]:
                break
    return rows


def summarize(rows):
    print(f"\n{len(rows)} model-would-have-fired moments across "
          f"{len({(r['icao'], r['date'], r['kind']) for r in rows})} city-day-kinds\n")
    bins = [(0.98, 1.01), (0.95, 0.98), (0.92, 0.95), (0.85, 0.92), (0.0, 0.85)]
    print(f"{'claimed p':>12}  {'n':>5}  {'real hit rate':>14}  {'avg claimed':>12}")
    for lo, hi in bins:
        sub = [r for r in rows if lo <= r["claimed_p"] < hi]
        if not sub:
            continue
        hit_rate = sum(r["hit"] for r in sub) / len(sub)
        avg_p = sum(r["claimed_p"] for r in sub) / len(sub)
        flag = "  <-- overconfident" if avg_p - hit_rate > 0.08 else ""
        print(f"[{lo:.2f},{hi:.2f})  {len(sub):>5}  {hit_rate:>13.1%}  {avg_p:>11.1%}{flag}")

    p_min_rows = [r for r in rows if r["claimed_p"] >= 0.92]
    if p_min_rows:
        real = sum(r["hit"] for r in p_min_rows) / len(p_min_rows)
        print(f"\nAt the live P_MIN=0.92 threshold: n={len(p_min_rows)}, "
              f"real hit rate={real:.1%} (claims avg "
              f"{sum(r['claimed_p'] for r in p_min_rows)/len(p_min_rows):.1%})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "climo_backtest_results.jsonl"))
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--min-local-hour-high", type=float, default=13.0)
    ap.add_argument("--min-local-hour-low", type=float, default=10.0)
    ap.add_argument("--min-age-min", type=float, default=120.0)
    args = ap.parse_args()

    climo = json.loads(CLIMO_PATH.read_text())
    icaos = sorted(climo)
    print(f"replaying {len(icaos)} stations, up to ~{HOURS}h history each "
          f"(endpoint caps ~400 obs/station in practice)...")
    by_station = fetch_all(icaos)

    all_rows = []
    for icao in icaos:
        raw = by_station.get(icao)
        if not raw:
            continue
        rows = replay_station(icao, climo[icao], raw,
                              args.min_local_hour_high, args.min_local_hour_low,
                              args.min_age_min)
        all_rows.extend(rows)

    mode = "a" if args.append else "w"
    with open(args.out, mode) as f:
        for r in all_rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(all_rows)} rows to {args.out} (mode={mode})")

    if args.append and Path(args.out).exists():
        all_saved = [json.loads(l) for l in open(args.out) if l.strip()]
        print("\n=== cumulative results (all runs so far) ===")
        summarize(all_saved)
    else:
        summarize(all_rows)


if __name__ == "__main__":
    main()
