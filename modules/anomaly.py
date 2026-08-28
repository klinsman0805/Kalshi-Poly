"""
modules/anomaly.py — detectors for the ways this pipeline has actually broken.

Every check here exists because the corresponding failure happened, was
invisible to the summary statistics, and was caught only by someone reading
concrete numbers. Brier scores cannot distinguish a confidently wrong model
from a confidently wrong label; a reliability table looks identical either way.
So these look at the raw quantities instead.

  implausible_miss    A settled value far from OUR OWN OBSERVATION is a bad
                      label. Austin 2026-08-23 was recorded as 84F while our
                      METAR read said 98.96F, because the climate report we
                      took was a mid-day preliminary rather than the final.

                      The comparison must be against the observation, not
                      against the bucket. An 11-wide ladder is scored every
                      cycle, so at 8am the engine legitimately holds a bucket
                      eight degrees from where the day ends — priced at 0.1c,
                      because the market knows it is dead. Measuring against
                      the bucket flagged three such rows as label bugs when
                      every ladder had in fact resolved correctly.

  mixed_units         Polymarket trades whole degrees Celsius abroad and
                      Fahrenheit in the US. Aggregating a "median miss" across
                      both produces a number that means nothing, which I did
                      before splitting the table by unit.

  capture_stalled     The recorder writing nothing is indistinguishable from a
                      quiet market until you check the clock.

  labels_stalled      Markets aging past settlement while the label count sits
                      still is what a broken resolver looks like from outside.
                      Every non-Kalshi market was unlabelled for two days
                      because an event slug was being queried against the
                      markets endpoint.
"""

from collections import Counter
from datetime import datetime, timezone

# Daily temperature ranges are bounded by physics. A settled extreme this far
# outside our bucket means the label is wrong, not the forecast.
IMPLAUSIBLE_MISS = {"F": 12.0, "C": 7.0}

# The recorder samples each market at most every 15 minutes but sweeps
# continuously, so an hour of silence is already abnormal.
CAPTURE_STALE_HOURS = 2.0


def _age_hours(iso, now=None):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - t).total_seconds() / 3600.0


def bucket_miss(rec, actual):
    """Degrees the settled value fell outside our bucket. 0 means inside."""
    if actual is None:
        return None
    lo, hi = rec.get("lo"), rec.get("hi")
    if lo is not None and actual < lo:
        return actual - lo
    if hi is not None and actual > hi:
        return actual - hi
    return 0.0


def observation_divergence(rec, actual):
    """Degrees between the settled value and what WE observed. None if unknown."""
    obs = rec.get("ext_c")
    if obs is None or actual is None:
        return None
    return actual - obs


def implausible_miss(rec, actual):
    """Does the settled value contradict our own observation?

    Only meaningful once the extreme has locked. Before that our reading is
    legitimately behind the day, and a gap is information rather than a fault.
    """
    from modules.confidence import cleared_timing
    if not cleared_timing(rec):
        return False
    div = observation_divergence(rec, actual)
    if div is None:
        return False
    limit = IMPLAUSIBLE_MISS.get(rec.get("unit"), IMPLAUSIBLE_MISS["F"])
    return abs(div) > limit


def find_implausible(pairs):
    """[(record, actual)] -> the ones whose miss is not physically credible."""
    return [(r, a) for r, a in pairs if implausible_miss(r, a)]


def mixed_units(records):
    """More than one temperature unit in a set that is about to be averaged."""
    return len({r.get("unit") for r in records if r.get("unit")}) > 1


def capture_stalled(latest_ts, now=None, hours=CAPTURE_STALE_HOURS):
    """Has the recorder gone quiet?"""
    age = _age_hours(latest_ts, now)
    return (True, age) if age is not None and age > hours else (False, age)


def labels_stalled(n_settleable, n_labelled):
    """Markets whose day has closed but which carry no label.

    Some lag is normal — a climate report publishes the following morning. A
    large standing backlog is what a broken resolver looks like from outside.
    """
    if not n_settleable:
        return False, 0.0
    unlabelled = n_settleable - n_labelled
    return (unlabelled / n_settleable > 0.5 and unlabelled > 20,
            round(unlabelled / n_settleable, 3))


def scan(pairs, latest_capture_ts=None, n_settleable=0, n_labelled=0, now=None):
    """Run every detector. Returns a list of {level, code, detail}."""
    out = []
    bad = find_implausible(pairs)
    if bad:
        out.append({
            "level": "ERROR", "code": "implausible_miss",
            "detail": "%d locked market(s) whose settled value contradicts our "
                      "own observation — suspect the LABEL, not the model" % len(bad),
            "rows": [{"key": r.get("key"), "unit": r.get("unit"),
                      "bucket": [r.get("lo"), r.get("hi")], "settled": a,
                      "observed": r.get("ext_c"),
                      "miss": observation_divergence(r, a)} for r, a in bad[:8]],
        })
    stalled, age = capture_stalled(latest_capture_ts, now)
    if stalled:
        out.append({"level": "ERROR", "code": "capture_stalled",
                    "detail": "no candidate written for %.1f hours" % age})
    lst, frac = labels_stalled(n_settleable, n_labelled)
    if lst:
        out.append({"level": "WARN", "code": "labels_stalled",
                    "detail": "%.0f%% of settleable markets carry no label" % (100 * frac)})
    return out


def unit_summary(pairs):
    """Per-unit accuracy. Never aggregate across units — see mixed_units."""
    by = {}
    for r, a in pairs:
        miss = bucket_miss(r, a)
        if miss is None:
            continue
        u = r.get("unit") or "?"
        d = by.setdefault(u, {"unit": u, "n": 0, "inside": 0,
                              "misses": [], "above": 0, "below": 0})
        d["n"] += 1
        if miss == 0:
            d["inside"] += 1
        else:
            d["misses"].append(abs(miss))
            if miss > 0:
                d["above"] += 1
            else:
                d["below"] += 1
    for d in by.values():
        m = sorted(d["misses"])
        d["inside_pct"] = round(100 * d["inside"] / d["n"], 1) if d["n"] else 0.0
        d["median_miss"] = m[len(m) // 2] if m else None
        d["max_miss"] = m[-1] if m else None
    return sorted(by.values(), key=lambda d: -d["n"])


def signal_counts(records):
    return dict(Counter(r.get("signal") for r in records).most_common())
