#!/usr/bin/env python3
"""
scripts/conditions_report.py — read out the weather-conditions shadow experiment.

Joins the `conditions` block recorded on each entry (see feeds/metar_conditions.py)
against how that position actually resolved, and reports whether any signal
separates winners from losers. Reads the ledgers only — runs anywhere, changes
nothing.

The question this has to answer honestly is NOT "do losers look bad" — it is
"do losers look bad in a way winners DON'T". A first pass over 28 backfilled
entries said no for the composite score (winners mean 0.79 vs losers 0.68, and
the biggest loss scored a perfect 1.00) while the low-ceiling flag alone was
0 winners / 2 losers. Both numbers are too small to act on; this script exists
to re-run that as live data accumulates.

Usage:  python3 scripts/conditions_report.py [--days N]
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

LEDGERS = [
    ("poly", os.getenv("POLY_LEDGER", "weather_live.jsonl")),
    ("kalshi", os.getenv("KALSHI_LEDGER", "kalshi_weather_paper.jsonl")),
]

FLAGS = [
    ("precip_now", lambda c: bool(c.get("precip_now"))),
    ("precip<=90m", lambda c: (c.get("precip_recent_min") is not None
                               and c["precip_recent_min"] <= 90)),
    ("ceiling<5000ft", lambda c: (c.get("ceiling_ft") is not None
                                  and c["ceiling_ft"] < 5000)),
    ("ceiling<3000ft", lambda c: (c.get("ceiling_ft") is not None
                                  and c["ceiling_ft"] < 3000)),
    ("convective", lambda c: bool(c.get("convective"))),
    ("BECMG/TEMPO", lambda c: c.get("trend") in ("BECMG", "TEMPO")),
    ("gust/variable", lambda c: bool(c.get("gust_kt") or c.get("wind_variable"))),
    ("dry>=15C", lambda c: (c.get("dewpoint_spread_c") is not None
                            and c["dewpoint_spread_c"] >= 15)),
]


def _agree(o):
    """Fraction of ensemble members biased AGAINST the position: for a high,
    members reading cold (the station has more room to climb than they think);
    for a low, members reading warm. None when no ensemble was recorded.

    This is the whole hypothesis in one number — see feeds/gfs_ensemble. The
    raw ensemble spread was measured NOT to narrow through the day, so the
    distribution itself says little at bucket width; the level bias might."""
    e = o.get("ensemble") or {}
    n = e.get("n_members")
    if not n:
        return None
    against = e["members_cold"] if o.get("kind", "high") == "high" else e["members_warm"]
    return against / n


def _tail_breaks(o):
    """Does the UPPER TAIL of the anchored remaining-extreme distribution fall
    outside the bucket? (p90 of the max for a high; p10 of the min for a low.)
    Rounded, because settlement is a whole degree. None if not computable.

    SECOND hypothesis, added 2026-07-27 AFTER London (bucket 26°C) settled at
    27°C: the members-against metric said 32% — reassuring — and lost, while
    p90 was 26.82 → 27, outside. Istanbul's reconstruction agrees. That is one
    clean case plus one reconstruction, and the metric was chosen with the
    answer already known, so it proves nothing yet. It is recorded HERE, beside
    the first metric, so both are judged on data that arrives from now on."""
    e = o.get("ensemble") or {}
    high = o.get("kind", "high") == "high"
    edge = o.get("hi") if high else o.get("lo")
    if edge is None:
        return None                     # catch-all bucket has no edge to break
    key = "anch_max_p90_" if high else "anch_min_p10_"
    # The open record doesn't carry the market's unit, and a threshold on the
    # edge value can't recover it — a 40°F winter bucket looks exactly like 40°C.
    # Pick whichever unit puts the forecast nearest the edge; °C and °F readings
    # of the same temperature are far apart, so this is unambiguous.
    cands = [(abs(e[key + u] - edge), e[key + u]) for u in ("c", "f") if e.get(key + u) is not None]
    if not cands:
        return None
    v = min(cands)[1]
    return round(v) > edge if high else round(v) < edge


def _gfs_txt(o):
    a = _agree(o)
    if a is None:
        return "-"
    e = o["ensemble"]
    t = _tail_breaks(o)
    mark = "" if t is None else ("!" if t else "-")
    return f"{a*100:.0f}%/{e['bias_med_c']:+.1f}C{mark}"


def load(path, venue, cutoff):
    opens, settles = {}, {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "open":
                    r["venue"] = venue
                    opens[r["key"]] = r
                elif r.get("type") == "settle":
                    settles[r["key"]] = r
    except FileNotFoundError:
        return []
    out = []
    for key, o in opens.items():
        if not o.get("conditions") and not o.get("ensemble"):
            continue                      # pre-experiment entry
        try:
            if datetime.fromisoformat(o["opened"]) < cutoff:
                continue
        except (KeyError, ValueError):
            pass
        s = settles.get(key)
        out.append((o, s.get("pnl_usd") if s else None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    trades = []
    for venue, path in LEDGERS:
        trades.extend(load(path, venue, cutoff))
    trades.sort(key=lambda t: t[0]["opened"])

    settled = [(o, p) for o, p in trades if p is not None]
    pending = [(o, p) for o, p in trades if p is None]

    print(f"conditions experiment — {len(trades)} entries carry conditions "
          f"({len(settled)} settled, {len(pending)} still open)\n")
    if not trades:
        print("No entries recorded with conditions yet. The shadow logging went")
        print("live 2026-07-26; entries fire a few times a day, so give it time.")
        return

    print(f"{'when':<12}{'venue':<8}{'city':<15}{'kind':<6}{'score':>6}"
          f"{'gfs':>10}{'pnl':>9}  flags")
    for o, pnl in trades:
        c = o.get("conditions") or {}
        fl = [name for name, fn in FLAGS if fn(c)]
        when = o["opened"][5:16].replace("T", " ")
        pnl_txt = f"{pnl:+.2f}" if pnl is not None else "open"
        sc = c.get("score")
        sc_txt = f"{sc:6.2f}" if isinstance(sc, (int, float)) else "     -"
        print(f"{when:<12}{o['venue']:<8}{o['city']:<15}{o.get('kind','high'):<6}"
              f"{sc_txt}{_gfs_txt(o):>10}{pnl_txt:>9}  {', '.join(fl) or 'clean'}")

    if not settled:
        print("\nNothing settled yet — no win/loss split to report.")
        return

    wins = [(o, p) for o, p in settled if p >= 0]
    losses = [(o, p) for o, p in settled if p < 0]
    ws = [(o.get("conditions") or {}).get("score") for o, _ in wins
          if (o.get("conditions") or {}).get("score") is not None]
    ls = [(o.get("conditions") or {}).get("score") for o, _ in losses
          if (o.get("conditions") or {}).get("score") is not None]

    print("\n── composite score ──")
    if ws:
        print(f"  winners n={len(ws):<3} mean={sum(ws)/len(ws):.3f}  min={min(ws):.2f}")
    if ls:
        print(f"  losers  n={len(ls):<3} mean={sum(ls)/len(ls):.3f}  max={max(ls):.2f}")
    if ws and ls:
        sep = (sum(ws)/len(ws)) - (sum(ls)/len(ls))
        print(f"  separation (want clearly >0): {sep:+.3f}")

    print("\n── per-flag: does it appear more in losers than winners? ──")
    print(f"  {'flag':<16}{'winners':>10}{'losers':>10}   read")
    for name, fn in FLAGS:
        w = sum(1 for o, _ in wins if fn(o.get("conditions") or {}))
        l = sum(1 for o, _ in losses if fn(o.get("conditions") or {}))
        wr = f"{w}/{len(wins)}"
        lr = f"{l}/{len(losses)}"
        note = ""
        if len(wins) and len(losses):
            wpct, lpct = w / len(wins), l / len(losses)
            if l and w == 0:
                note = "← losers only"
            elif lpct > wpct * 2 and l >= 2:
                note = "← skews loser"
            elif w and l == 0:
                note = "(winners only)"
        print(f"  {name:<16}{wr:>10}{lr:>10}   {note}")

    print("\n── what a score gate would have cost ──")
    for thr in (0.4, 0.5, 0.6, 0.7):
        bw = [(o, p) for o, p in wins if (o["conditions"].get("score") or 1) < thr]
        bl = [(o, p) for o, p in losses if (o["conditions"].get("score") or 1) < thr]
        print(f"  score>={thr}: blocks {len(bw)}/{len(wins)} winners "
              f"(${sum(p for _, p in bw):+.2f}) and {len(bl)}/{len(losses)} losers "
              f"(${sum(p for _, p in bl):+.2f})  net ${-sum(p for _, p in bw) - sum(p for _, p in bl):+.2f}")
    print("\n  (net > 0 means the gate would have helped. Sample size matters more")
    print("   than sign here — the solar-elevation idea looked fine at n=1 too.)")

    _ensemble_section(wins, losses)


def _ensemble_section(wins, losses):
    """Anchored-ensemble readout: does members-biased-against-the-position at
    entry separate winners from losers?"""
    wa = [(o, p, _agree(o)) for o, p in wins if _agree(o) is not None]
    la = [(o, p, _agree(o)) for o, p in losses if _agree(o) is not None]
    print("\n── anchored GFS ensemble ──")
    if not wa and not la:
        print("  No settled entry carries ensemble data yet (logging went live")
        print("  2026-07-27). Nothing to say.")
        return
    print("  'against' = members biased the way that would HURT the position")
    print("  (cold members on a high, warm members on a low).\n")
    for name, rows in (("winners", wa), ("losers", la)):
        if not rows:
            continue
        vals = sorted(a for _, _, a in rows)
        med = vals[len(vals) // 2]
        print(f"  {name:<9} n={len(rows):<3} median against={med*100:.0f}%  "
              f"range {vals[0]*100:.0f}–{vals[-1]*100:.0f}%")
    if wa and la:
        mw = sorted(a for _, _, a in wa)[len(wa) // 2]
        ml = sorted(a for _, _, a in la)[len(la) // 2]
        print(f"  separation (want losers clearly higher): {(ml - mw)*100:+.0f} pts")

    print("\n  what a unanimity gate would have cost:")
    for thr in (0.80, 0.90, 1.00):
        bw = [(o, p) for o, p, a in wa if a >= thr]
        bl = [(o, p) for o, p, a in la if a >= thr]
        print(f"    block if against>={thr*100:.0f}%: "
              f"{len(bw)}/{len(wa)} winners (${sum(p for _, p in bw):+.2f}), "
              f"{len(bl)}/{len(la)} losers (${sum(p for _, p in bl):+.2f})  "
              f"net ${-sum(p for _, p in bw) - sum(p for _, p in bl):+.2f}")
    # ── second metric: does the anchored upper tail leave the bucket? ──
    wt = [(o, p, _tail_breaks(o)) for o, p in wins]
    lt = [(o, p, _tail_breaks(o)) for o, p in losses]
    wt = [x for x in wt if x[2] is not None]
    lt = [x for x in lt if x[2] is not None]
    if wt or lt:
        bw = [(o, p) for o, p, t in wt if t]
        bl = [(o, p) for o, p, t in lt if t]
        print("\n  tail metric — p90 of the anchored extreme falls OUTSIDE the bucket:")
        print(f"    flags {len(bw)}/{len(wt)} winners (${sum(p for _, p in bw):+.2f}), "
              f"{len(bl)}/{len(lt)} losers (${sum(p for _, p in bl):+.2f})  "
              f"net if used as a gate: ${-sum(p for _, p in bw) - sum(p for _, p in bl):+.2f}")

    print("\n  Do NOT act on either metric until losers clearly separate AND n is")
    print("  well past the ~10 that made solar elevation look convincing. The")
    print("  first metric ALREADY failed its first real case (London 2026-07-27:")
    print("  32% against, read as safe, lost). The tail metric was picked after")
    print("  seeing that result, so it owes us out-of-sample evidence.")


if __name__ == "__main__":
    main()
