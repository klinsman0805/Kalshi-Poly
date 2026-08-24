"""
modules/settlement_truth.py — resolve what a weather market ACTUALLY settled to.

This is the label side of the confidence rebuild. It answers one question per
market: did the bucket win, according to the venue's own settlement source?

Why not just compare against our observed extreme. Because that is the mistake
this whole effort is correcting. The METAR read and the settlement value
diverge — through the settlement clock (Kalshi's CLI runs on local standard
time), through sensor rounding, through the CLI's own quality control. A model
trained on our observation would learn to predict our observation, which is not
what the contract pays on. Both resolvers below therefore go to the venue's
source and nothing else.

  Kalshi     NWS Climatological Report via api.weather.gov. Published once, the
             following morning, covering the full prior day. This mirrors the
             proven parser in KalshiWeatherExecutor._cli_crosscheck.
  Polymarket the market's own resolution via Gamma — umaResolutionStatus
             "resolved" plus outcomePrices. That IS the settlement, so there is
             nothing to infer.

"Not resolvable yet" is a first-class answer and is returned as None, never as
a loss. A CLI that has not published, or a market UMA has not settled, must
never be silently labelled — a false label is far more damaging than a missing
one, because nothing downstream can detect it.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("modules.settlement_truth")

NWS_CLI = "https://api.weather.gov/products/types/CLI/locations/{station}"
NWS_PRODUCT = "https://api.weather.gov/products/{pid}"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
TIMEOUT = 20
UA = {"User-Agent": "Mozilla/5.0"}

# NWS issues SEVERAL CLI products per station-day: preliminaries through the
# day, then a final after the climate day closes, plus occasional corrections.
# 40 covers roughly a week at that rate.
CLI_LOOKBACK_PRODUCTS = 40

_session = requests.Session()
_session.headers.update(UA)


class SettlementTruth:
    """Resolves settled extremes, with an in-process cache.

    The cache is what makes a batch pass affordable: one CLI fetch serves every
    candidate recorded for that station-day, and there may be dozens.
    """

    def __init__(self):
        self._cli = {}        # (station, date) -> {"high": int, "low": int} | None
        self._gamma = {}      # slug -> resolved bool | None
        self.stats = {"cli_hit": 0, "cli_miss": 0, "gamma_hit": 0, "gamma_miss": 0,
                      "errors": 0}

    # ── Kalshi: the NWS climate report ───────────────────────────────────────
    def cli_extremes(self, icao, date_iso, tz_name=None):
        """{"high": F, "low": F} for a station-day, or None if not yet FINAL.

        The subtlety that matters: a CLI issued *during* its own day reports the
        extreme so far, not the day's extreme. Austin 2026-08-23 published
        MAXIMUM 84 at 07:35 local and MAXIMUM 105 after midnight — taking the
        first product that matched the date labelled a 105F day as 84F, a 21
        degree error that then trained the model.

        So a report only counts once it was issued after the end of the climate
        day it describes, in local STANDARD time — the same boundary Kalshi
        settles on. Without a timezone we cannot establish that, and the honest
        answer is "not yet knowable".
        """
        key = (icao, date_iso)
        if key in self._cli:
            return self._cli[key]
        if not tz_name:
            return None

        try:
            tz = ZoneInfo(tz_name)
            day = datetime.fromisoformat(date_iso).date()
        except Exception:  # noqa: BLE001
            return None
        # end of the climate day = next midnight in local standard time
        probe = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
        local = probe.astimezone(tz)
        std_off = local.utcoffset() - (local.dst() or timedelta(0))
        day_end_utc = (datetime.combine(day, datetime.min.time())
                       + timedelta(days=1)).replace(tzinfo=timezone.utc) - std_off

        station = icao[1:] if icao and icao.startswith("K") else icao
        try:
            r = _session.get(NWS_CLI.format(station=station), timeout=TIMEOUT)
            r.raise_for_status()
            products = r.json().get("@graph", [])
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            log.debug("CLI list failed for %s: %s", station, e)
            return None       # transport failure is not evidence of absence

        # Only products issued after the day closed can be final. Newest first,
        # so a later correction supersedes an earlier final.
        eligible = []
        for prod in products[:CLI_LOOKBACK_PRODUCTS]:
            ts = prod.get("issuanceTime")
            if not ts:
                continue
            try:
                issued = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
            if issued >= day_end_utc:
                eligible.append((issued, prod))
        eligible.sort(key=lambda x: -x[0].timestamp())

        text = None
        for _, prod in eligible:
            try:
                rr = _session.get(NWS_PRODUCT.format(pid=prod["id"]), timeout=TIMEOUT)
                body = rr.json().get("productText", "")
            except Exception:  # noqa: BLE001
                continue
            m = re.search(r"CLIMATE SUMMARY FOR (\w+ \d+ \d+)", body)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%B %d %Y").date().isoformat()
            except ValueError:
                continue
            if d == date_iso:
                text = body
                break

        if text is None:
            self.stats["cli_miss"] += 1
            return None       # no final report yet — retry on a later pass

        m_hi = re.search(r"MAXIMUM\s+(-?\d+)", text)
        m_lo = re.search(r"MINIMUM\s+(-?\d+)", text)
        if not (m_hi and m_lo):
            self.stats["cli_miss"] += 1
            self._cli[key] = None
            return None

        out = {"high": int(m_hi.group(1)), "low": int(m_lo.group(1))}
        self._cli[key] = out
        self.stats["cli_hit"] += 1
        return out

    # ── Polymarket: the market's own resolution ──────────────────────────────
    def gamma_resolved(self, slug):
        """True/False if the market has settled, else None."""
        if slug in self._gamma:
            return self._gamma[slug]
        try:
            r = _session.get(GAMMA_MARKETS, params={"slug": slug}, timeout=TIMEOUT)
            r.raise_for_status()
            markets = r.json()
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            log.debug("gamma lookup failed for %s: %s", slug, e)
            return None
        if not markets:
            self.stats["gamma_miss"] += 1
            return None
        m = markets[0] if isinstance(markets, list) else markets
        if m.get("umaResolutionStatus") != "resolved":
            self.stats["gamma_miss"] += 1
            return None       # not settled yet; do not cache
        try:
            prices = m.get("outcomePrices")
            prices = json.loads(prices) if isinstance(prices, str) else prices
            won = float(prices[0]) >= 0.5
        except (TypeError, ValueError, IndexError):
            self.stats["gamma_miss"] += 1
            return None
        self._gamma[slug] = won
        self.stats["gamma_hit"] += 1
        return won

    # ── the one entry point the labeller uses ────────────────────────────────
    @staticmethod
    def bucket_contains(extreme, lo, hi):
        """Does the settled extreme fall in this bucket?

        A catch-all bucket carries only ONE bound by design (">=60F", "<=98F").
        Requiring both to be present made every catch-all evaluate False, which
        once mislabelled a real San Francisco win as a mismatch.
        """
        if extreme is None:
            return None
        return (lo is None or extreme >= lo) and (hi is None or extreme <= hi)

    def label(self, rec):
        """Resolve one candidate record.

        Returns {"won", "actual_extreme", "source", "reason"} where `won` is
        None when the outcome is not yet knowable.
        """
        venue = rec.get("venue")
        kind = rec.get("kind", "high")

        if venue == "kalshi":
            icao = rec.get("station")
            date = rec.get("settlement_date_market") or rec.get("date")
            if not icao or not date:
                return {"won": None, "actual_extreme": None,
                        "source": "cli", "reason": "no station or date"}
            if not rec.get("station_tz"):
                return {"won": None, "actual_extreme": None, "source": "cli",
                        "reason": "no station timezone; cannot tell final from preliminary"}
            ext = self.cli_extremes(icao, date, rec.get("station_tz"))
            if ext is None:
                return {"won": None, "actual_extreme": None,
                        "source": "cli", "reason": "no FINAL CLI yet"}
            actual = ext["high"] if kind == "high" else ext["low"]
            return {"won": self.bucket_contains(actual, rec.get("lo"), rec.get("hi")),
                    "actual_extreme": actual, "source": "cli", "reason": "ok"}

        if venue == "poly":
            slug = rec.get("slug")
            if not slug:
                return {"won": None, "actual_extreme": None,
                        "source": "gamma", "reason": "no slug"}
            won = self.gamma_resolved(slug)
            if won is None:
                return {"won": None, "actual_extreme": None,
                        "source": "gamma", "reason": "not resolved yet"}
            # Gamma resolves the WHOLE event; the per-bucket outcome comes from
            # the bucket's own market, which is what `slug` addresses here.
            return {"won": won, "actual_extreme": None,
                    "source": "gamma", "reason": "ok"}

        return {"won": None, "actual_extreme": None,
                "source": "unknown", "reason": f"unknown venue {venue!r}"}
