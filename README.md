# Kalshi × Polymarket Trading Bot

Active experiments: **weather NEAR-LOCK** and **copy-trade scanner** (both Polymarket).
Retired strategies (cross-venue arb, single-venue ladder arb, scalping, soccer) were removed —
see git history and `archive/` for their logs.

## Files
- `app.py`             — Flask dashboard (SSE): weather + copy-trade + scalping panels
- `engine.py`          — Kalshi market discovery, WebSocket feed, orderbook
- `trader.py`          — Kalshi order execution, quote manager, position tracking
- `polymarket.py`      — Polymarket CLOB client: WS books, FOK buy/sell, fees, auth
- `modules/`           — Strategy engines (weather + exec, copytrader + exec, scalping ref)
- `feeds/`             — Data feeds (METAR stations, Polymarket weather markets +
                         leaderboard, Coinbase spot)
- `scripts/build_weather_climo.py` — remaining-rise climatology (run before weather trading)
- `test_kalshi_bot.py` — Kalshi engine/trader test suite
- `.env.example`       — Config template
- `kalshi-bot.service` — systemd unit

## Weather NEAR-LOCK strategy (paper forward-test)

Polymarket lists daily "Highest/Lowest temperature in <city>" bucket markets that settle to a
specific airport station's observations (Wunderground/NOAA pages mirroring METAR). In the
last hours of the local day the extreme is largely locked in, but bucket prices can lag the
already-printed observation. The engine:

1. discovers markets + settlement stations (`feeds/poly_weather.py`),
2. tracks each station's running daily max/min via METAR (`feeds/metar.py`),
3. converts observed extreme + local-hour into bucket probabilities using a per-station
   empirical remaining-rise/fall table (`scripts/build_weather_climo.py` → `data/`),
4. papers an entry when p ≥ 0.92, ask ≤ 82¢, edge ≥ 8¢ (`modules/weather_exec.py`),
5. settles from the market's own UMA resolution — so the forward test also verifies
   our observation feed matches the real settlement source.

Supports both °C (international) and °F (US) cities. Go-live gate: ≥100 paper settlements
with win-rate within a few points of average model p (calibration, not just P&L). Hong Kong
settles to the HKO downtown station (not the airport METAR) and is monitor-only.

## Install
```bash
pip install requests websocket-client cryptography python-dotenv
```

## Setup
1. Go to https://kalshi.com/account/profile → API Keys → Create New API Key
2. Save the downloaded `.key` file as `kalshi.key` in this folder
3. Copy `.env.example` to `.env` and fill in your credentials
4. Build the weather climatology once: `python scripts/build_weather_climo.py`

## Run
```bash
# Dashboard (weather + copy-trade + scalping panels) on http://localhost:5001
python app.py
```
Or install as a service: copy `kalshi-bot.service` to `/etc/systemd/system/` and
`systemctl enable --now kalshi-bot`.

Everything is paper/monitor by default. Live order paths are double-gated (an env flag
*and* a runtime toggle) — see `WEATHER_LIVE` / `COPYTRADE_LIVE` in `.env.example`.

## Run tests
```bash
python -m pytest test_kalshi_bot.py -v
# or without pytest:
python -m unittest test_kalshi_bot -v
```

## Fee reference (Kalshi)
- Taker: `0.07 × P × (1-P)` per contract — max **1.75¢** at P=0.50
- Maker: `0.0175 × P × (1-P)` per contract — max **0.4375¢** at P=0.50
- Total rounded UP to nearest cent on the full order
- No fee to cancel a resting order
