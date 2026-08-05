"""
feeds/kalshi_stations.py — Kalshi daily-temperature city → station map.

Every Kalshi daily high/low temperature series settles to an NWS Climatological
Report (CLI) station. That station's airport ICAO is the CLI `issuedby=` code
prefixed with "K", and ALL 20 of them return live METAR from aviationweather.gov
(verified 2026-07-24, including Central Park = KNYC). So every Kalshi temperature
city is observable with the same feed the Polymarket engine already uses.

`icao` is what the METAR feed and the climatology (data/weather_climo.json) key
on. `poly_icao` records the station Polymarket settles the SAME city to — equal
for Tier-A cities (climatology transfers), different or None otherwise.

Settlement caveat: Kalshi settles to the CLI DAILY report, not METAR directly.
Same-station Polymarket settles via UMA/Wunderground. They usually converge but
can differ on rounding/timing — treat as a small independent settlement risk.
"""

# city label → mapping. `series_high`/`series_low` are the Kalshi series tickers
# (the KX-prefixed canonical ones; legacy duplicates point at the same markets).
# unit is always "F" for Kalshi US markets.
KALSHI_STATIONS = {
    # ── Tier A: same station as Polymarket → climatology transfers directly ──
    "Atlanta":       {"icao": "KATL", "poly_icao": "KATL", "series_high": "KXHIGHTATL", "series_low": "KXLOWTATL"},
    "Austin":        {"icao": "KAUS", "poly_icao": "KAUS", "series_high": "KXHIGHAUS",  "series_low": "KXLOWTAUS"},
    "Houston":       {"icao": "KHOU", "poly_icao": "KHOU", "series_high": "KXHIGHTHOU", "series_low": "KXLOWTHOU"},
    "Los Angeles":   {"icao": "KLAX", "poly_icao": "KLAX", "series_high": "KXHIGHLAX",  "series_low": "KXLOWTLAX"},
    "Miami":         {"icao": "KMIA", "poly_icao": "KMIA", "series_high": "KXHIGHMIA",  "series_low": "KXLOWTMIA"},
    "San Francisco": {"icao": "KSFO", "poly_icao": "KSFO", "series_high": "KXHIGHTSFO", "series_low": "KXLOWTSFO"},
    "Seattle":       {"icao": "KSEA", "poly_icao": "KSEA", "series_high": "KXHIGHTSEA", "series_low": "KXLOWTSEA"},

    # ── Tier B: Kalshi-only, or a DIFFERENT station than Polymarket's ──
    # (climatology must be built for these ICAOs; poly_icao noted where the city
    #  also trades on Polymarket but resolves to another station — do NOT reuse it)
    "Chicago":       {"icao": "KMDW", "poly_icao": "KORD", "series_high": "KXHIGHCHI",  "series_low": "KXLOWTCHI"},
    "Dallas":        {"icao": "KDFW", "poly_icao": "KDAL", "series_high": "KXHIGHTDAL", "series_low": "KXLOWTDAL"},
    "Denver":        {"icao": "KDEN", "poly_icao": "KBKF", "series_high": "KXHIGHDEN",  "series_low": "KXLOWTDEN"},
    "NYC":           {"icao": "KNYC", "poly_icao": "KLGA", "series_high": "KXHIGHNY",   "series_low": "KXLOWTNYC"},
    "Boston":        {"icao": "KBOS", "poly_icao": None,   "series_high": "KXHIGHTBOS", "series_low": "KXLOWTBOS"},
    "Las Vegas":     {"icao": "KLAS", "poly_icao": None,   "series_high": "KXHIGHTLV",  "series_low": "KXLOWTLV"},
    "Minneapolis":   {"icao": "KMSP", "poly_icao": None,   "series_high": "KXHIGHTMIN", "series_low": "KXLOWTMIN"},
    "New Orleans":   {"icao": "KMSY", "poly_icao": None,   "series_high": "KXHIGHTNOLA","series_low": "KXLOWTNOLA"},
    "Oklahoma City": {"icao": "KOKC", "poly_icao": None,   "series_high": "KXHIGHTOKC", "series_low": "KXLOWTOKC"},
    "Philadelphia":  {"icao": "KPHL", "poly_icao": None,   "series_high": "KXHIGHPHIL", "series_low": "KXLOWTPHIL"},
    "Phoenix":       {"icao": "KPHX", "poly_icao": None,   "series_high": "KXHIGHTPHX", "series_low": "KXLOWTPHX"},
    "San Antonio":   {"icao": "KSAT", "poly_icao": None,   "series_high": "KXHIGHTSATX","series_low": "KXLOWTSATX"},
    "Washington DC": {"icao": "KDCA", "poly_icao": None,   "series_high": "KXHIGHTDC",  "series_low": "KXLOWTDC"},
}

UNIT = "F"  # all Kalshi US daily-temperature markets

# Cities whose station matches Polymarket's, so data/weather_climo.json already
# has usable °F climatology — the Tier-A "port for free" set.
TIER_A = [c for c, m in KALSHI_STATIONS.items() if m["icao"] == m["poly_icao"]]

# ICAOs that need climatology built before the model can price them.
def missing_climo_icaos(climo: dict) -> list:
    """ICAOs in the Kalshi map that lack a °F pmf in the given climatology dict."""
    need = []
    for c, m in KALSHI_STATIONS.items():
        st = climo.get(m["icao"])
        if not st or "pmf" not in st or st.get("unit", "C") != "F":
            need.append((c, m["icao"]))
    return need


def series_for(city: str, kind: str):
    m = KALSHI_STATIONS.get(city)
    if not m:
        return None
    return m["series_high"] if kind == "high" else m["series_low"]


def city_for_series(series_ticker: str):
    """Reverse lookup: Kalshi series ticker → (city, kind). None if unknown."""
    for city, m in KALSHI_STATIONS.items():
        if m["series_high"] == series_ticker:
            return city, "high"
        if m["series_low"] == series_ticker:
            return city, "low"
    return None, None
