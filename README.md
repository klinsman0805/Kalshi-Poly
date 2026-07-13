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

## Weather NEAR-LOCK strategy (paper forward-test)

Polymarket lists daily "Highest temperature in <city>" bucket markets that settle to a
specific airport station's observations (Wunderground/NOAA pages mirroring METAR). In the
last hours of the local day the max is largely locked in, but bucket prices can lag the
already-printed observation. The engine:

1. discovers markets + settlement stations (`feeds/poly_weather.py`),
2. tracks each station's running daily max via METAR (`feeds/metar.py`),
3. converts observed-max + local-hour into bucket probabilities using a per-station
   empirical remaining-rise table (`scripts/build_weather_climo.py` → `data/`),
4. papers an entry when p ≥ 0.92, ask ≤ 82¢, edge ≥ 8¢ (`modules/weather_exec.py`),
5. settles from the market's own UMA resolution — so the forward test also verifies
   our observation feed matches the real settlement source.

Go-live gate: ≥100 paper settlements with win-rate within a few points of average
model p (calibration, not just P&L). Hong Kong settles to HKO (not the airport) and
US cities use °F buckets — both monitor-only in v1.

## Install
```bash
pip install requests websocket-client cryptography python-dotenv
```

## Setup
1. Go to https://kalshi.com/account/profile → API Keys → Create New API Key
2. Save the downloaded `.key` file as `kalshi.key` in this folder
3. Copy `.env.example` to `.env` and fill in your `KALSHI_KEY_ID`
4. Start on DEMO with `KALSHI_DEMO=true` and `DRY_RUN=true`

## Run tests
```bash
python -m pytest test_kalshi_bot.py -v
# or without pytest:
python -m unittest test_kalshi_bot -v
```

## Usage (integrate into your app)

```python
from engine import BotEngine
from trader import QuoteManager, execute_arb

quote_managers = {a: QuoteManager(a) for a in ["BTC", "ETH", "SOL"]}

def on_prices(markets, snapshots):
    for asset, snap_dict in snapshots.items():
        if snap_dict:
            mkt = markets.get(asset)
            snap = ...  # your snapshot object
            quote_managers[asset].update(snap, mkt)

def on_arb(snap):
    # Fires when taker-profitable gap detected
    mkt = bot.markets[snap.asset]
    execute_arb(snap, mkt, bot)

def on_log(icon, msg):
    print(f"{icon} {msg}")

bot = BotEngine(
    on_log=on_log,
    on_prices=on_prices,
    on_arb=on_arb,
    on_status=lambda s: print(f"Status: {s}"),
)
bot.start()
```

## Key design decisions vs Polymarket bot

| | Polymarket | Kalshi |
|---|---|---|
| Price format | Float 0–1 | Integer cents 1–99 |
| Orderbook | YES asks + NO asks | YES bids + NO bids only |
| Implied ask | Direct from book | `100 - best_opposite_bid` |
| Auth | Wallet private key | RSA-PSS signed headers |
| WS keepalive | Manual text "PING" | Standard WS ping frames |
| Fee formula | `0.25 × p × (1-p)` | `0.07 × p × (1-p)` taker |
| Maker fee | Same as taker | `0.0175 × p × (1-p)` (4× cheaper) |
| Primary strategy | FOK arb taker orders | Resting maker quotes |
| Window length | 5 minutes | 15 minutes |

## Fee reference
- Taker: `0.07 × P × (1-P)` per contract — max **1.75¢** at P=0.50
- Maker: `0.0175 × P × (1-P)` per contract — max **0.4375¢** at P=0.50
- Total rounded UP to nearest cent on the full order
- No fee to cancel a resting order
