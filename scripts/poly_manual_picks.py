#!/usr/bin/env python3
"""
scripts/poly_manual_picks.py — read-only pick list for MANUAL LP quoting.

Runs the two-leg strategy's entry checks against live data and prints every
market that is BOTH data-decided and market-decided right now:

  - NEAR-LOCK: the station's daily extreme is already in (same plateau check
    the weather bot and the two-leg executor use), joined per-bucket via live
    Polymarket temperature events;
  - market consensus: one side's mid >= 85c (a bucket priced 50/50 is not
    "decided" no matter what the plateau check says — both real paper losses
    on gated buckets were mid-priced contested markets);

but — deliberately — WITHOUT the estimator-confidence gate the automated
ranker applies. An empty book makes our pool-share estimate unmeasurable
(confidence=low), which is why the bot refuses to rank those markets; for a
HUMAN quoting by hand an empty book is the ideal case — no competing makers
means most of whatever the pool pays goes to whoever is quoting. The
est-confidence column is printed so you know which numbers not to trust.

For each pick it prints the concrete quoting card: suggested YES+NO bids at
band_fraction 0.5, size, capital, the reward pool, and the station's METAR
print minutes — be FLAT from ~7 minutes before each print minute until ~6
after (that is the only moment a locked market gets new information, and it
is when resting quotes get picked off).

Mind the $1/market/day minimum payout: a safe bucket on a $10/day pool will
accrue cents and pay nothing — concentrate in 1-2 markets with real pools.

Run (droplet):  cd /opt/kalshi-poly && venv/bin/python scripts/poly_manual_picks.py
Env:  POLY_TWOLEG_CONSENSUS_MIN_C (default 85) — consensus threshold
      POLY_MANUAL_MIN_RATE (default 5) — skip pools under $/day
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from feeds.poly_rewards import scan, get_twoleg_plan, fetch_reward_markets  # noqa: E402
from feeds.poly_weather import fetch_temperature_events  # noqa: E402
from modules.weather import MIN_LOCAL_HOUR, MIN_LOCAL_HOUR_LOW, MIN_MAX_AGE_MIN  # noqa: E402
import modules.poly_rewards_twoleg as tl  # noqa: E402

CONSENSUS_MIN_C = float(os.getenv("POLY_TWOLEG_CONSENSUS_MIN_C", "85"))
MIN_RATE = float(os.getenv("POLY_MANUAL_MIN_RATE", "5"))
# Both legs of a pair must be fundable at the market's CURRENT min qualifying
# size — Polymarket adjusts rewards_min_size intraday (observed 20 -> 100 on
# Busan while a user was mid-placement: sub-min orders carry full fill risk
# and earn exactly zero). Set to your spendable USDC minus headroom for the
# live weather bot's own stakes.
BUDGET_USD = float(os.getenv("POLY_MANUAL_BUDGET_USD", "45"))


def main():
    results = scan(tag_slug="weather", min_rate=MIN_RATE,
                   question_filter=lambda q: "temperature in" in q.lower())
    bot = tl.PolyTwoLeg(on_log=lambda i, m: None)

    events = fetch_temperature_events()
    by_cid = {}
    for e in events:
        if e.get("source") != "metar" or not e.get("station"):
            continue
        d = e["date"].isoformat() if hasattr(e["date"], "isoformat") else str(e["date"])
        for b in e.get("buckets", []):
            if b.get("condition_id") and not b.get("closed") and not b.get("resolved"):
                by_cid[b["condition_id"]] = (e["station"], e["city"], e["kind"], d)

    stations, matched = {}, []
    for r in results:
        hit = by_cid.get(r["condition_id"])
        if not hit:
            continue
        station, city, kind, date = hit
        tz = bot.exec._climo_tz.get(station)
        if not tz:
            continue
        stations[station] = tz
        matched.append((r, station, city, kind, date))

    if not matched:
        print("no reward markets matched to live temperature events")
        return
    bot.exec.metar.set_stations(stations)
    bot.exec.metar.poll()
    snap = bot.exec.metar.snapshot()

    markets = {m["condition_id"]: m for m in fetch_reward_markets(tag_slug="weather")}
    rows = []
    for r, station, city, kind, date in matched:
        st = snap.get(station)
        if not st or st.get("local_date") != date:
            continue
        lh = st.get("local_hour")
        age = st.get("max_age_min") if kind == "high" else st.get("min_age_min")
        min_h = MIN_LOCAL_HOUR if kind == "high" else MIN_LOCAL_HOUR_LOW
        if lh is None or age is None or lh < min_h or age < MIN_MAX_AGE_MIN:
            continue
        m = markets.get(r["condition_id"])
        if not m:
            continue
        plan = get_twoleg_plan(r["condition_id"], band_fraction=0.5, market=m)
        if not plan:
            continue
        if max(plan["yes_mid_c"], plan["no_mid_c"]) < CONSENSUS_MIN_C:
            continue
        minutes = bot._print_minutes_of(station)
        obs_ext = st.get("max_c") if kind == "high" else st.get("min_c")
        rows.append((plan["rate_per_day"], plan, city, kind, station,
                     sorted(minutes) if minutes else "?", lh, age, obs_ext,
                     r.get("confidence")))

    rows.sort(key=lambda x: (-(x[1]["capital_usd"] <= BUDGET_USD), -x[0]))
    if not rows:
        print("nothing is both locked AND market-agreed right now — try again "
              "later (best windows: your evening for Asian highs, early-to-mid "
              "afternoon MYT for European lows, late night for US highs)")
        return
    print(f"{len(rows)} locked + market-agreed (>= {CONSENSUS_MIN_C:.0f}c) picks, "
          f"best pool first:\n")
    for rate, p, city, kind, station, minutes, lh, age, ext, conf in rows[:10]:
        afford = ("FITS BUDGET" if p["capital_usd"] <= BUDGET_USD else
                  f"OVER BUDGET (needs ${p['capital_usd']:.0f} > ${BUDGET_USD:.0f} — "
                  f"min qualifying size is {p['size']:.0f} sh; sub-min orders "
                  f"earn NOTHING and still carry fill risk)")
        print(f"[{afford}] {p['question']}")
        print(f"  {city} {kind} | {station} prints at minutes {minutes} — be flat "
              f"~7min before to ~6min after | local hour {lh:.1f}, extreme set "
              f"{age:.0f}min ago (obs {ext}C)")
        print(f"  pool ${rate:.0f}/day | mids YES {p['yes_mid_c']:.1f} / "
              f"NO {p['no_mid_c']:.1f} | quote: YES bid {p['yes_bid_c']:.0f}c + "
              f"NO bid {p['no_bid_c']:.0f}c ({p['size']:.0f} sh each, "
              f"${p['capital_usd']:.2f} capital) | est-confidence={conf}")
        danger = "YES" if p["yes_mid_c"] >= p["no_mid_c"] else "NO"
        print(f"  danger leg: the {danger} bid (it fills only if the lock breaks "
              f"— if the obs starts moving toward the bucket edge, pull it)")
        print()


if __name__ == "__main__":
    main()
