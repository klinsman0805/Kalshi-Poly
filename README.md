# Kalshi 15-min Crypto Market-Making Bot

## Files

### Live runtime (loaded by `app.py`)
- `app.py`             — Flask entry point + bot lifecycle (run via systemd; see `kalshi-bot.service`)
- `engine.py`          — Kalshi market discovery, WebSocket feed, orderbook, auth
- `trader.py`          — Momentum strategy + maker quote manager
- `arb_trader.py`      — Cross-venue (Kalshi×Polymarket) arb taker strategy
- `polymarket.py`      — Polymarket CLOB client (orders, fills, WS feed)

### Tools (standalone scripts)
- `preflight.py`       — Pre-launch readiness check for a new device
- `pnl_report.py`      — Realized P&L per arb, reconciled from Kalshi + Polymarket APIs
- `arb_report.py`      — Activity summary from `trades.jsonl`
- `simulate.py`        — Momentum strategy scenario simulator (tuning aid)
- `test_kalshi_bot.py` — Pytest suite for `engine` + `trader`

### Config
- `.env.example`       — Config template
- `kalshi-bot.service` — systemd unit

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

## Run

```bash
# 1. Verify the environment is ready (config, keys, venue connectivity)
python preflight.py

# 2. Start the bot (Flask UI on :5000 + bot threads)
python app.py
```

Or install as a service: copy `kalshi-bot.service` to `/etc/systemd/system/` and
`systemctl enable --now kalshi-bot`.

## Reporting

```bash
python arb_report.py            # activity summary from trades.jsonl
python pnl_report.py            # realized P&L reconciled from venue APIs
python simulate.py --help       # momentum strategy scenario sweeps
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
