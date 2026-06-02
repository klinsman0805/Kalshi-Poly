"""
polymarket.py — Polymarket CLOB client

PRICES:    Float 0.0–1.0 on the wire; converted to integer cents (1–99) internally
           (same scale as Kalshi — enables direct comparison for arb detection)
TOKENS:    Each binary market has two ERC1155 outcome tokens:
             index 0 = "Up"  outcome → treated as YES
             index 1 = "Down" outcome → treated as NO
           Token IDs are ~78-digit decimal strings (uint256)
AUTH:      L1: HMAC-SHA256 API-key headers (for REST endpoints that need it)
           L2: EIP-712 wallet signature embedded in order body
WS:        wss://ws-subscriptions-clob.polymarket.com/ws/market
REST:      https://clob.polymarket.com
DISCOVERY: https://gamma-api.polymarket.com/events?slug=btc-updown-15m-{ts}

SLUG PATTERN:
  {asset}-updown-15m-{window_start_unix_seconds}
  e.g. btc-updown-15m-1779292800
  Consecutive windows differ by exactly 900 seconds.
  The slug timestamp = window START time = Kalshi's MarketSnapshot.window_ts
  → no static market config file needed; look up per window.

FEE (per docs.polymarket.com — CLOB fee curve + Maker Rebates Program):
  Makers pay 0 and earn a rebate (Crypto: ~20% of taker fees, paid daily in USDC).
  Takers pay: (fee_bps / 10_000) × p × (1 − p) × size  in USDC,
              where p = price as decimal in [0, 1].
  fee_bps comes from one of three sources (in priority order):
    1. POLY_FEE_BPS_OVERRIDE env var (testing only)
    2. GET clob.polymarket.com/fee-rate?token_id=... (authoritative, live)
    3. gamma `takerBaseFee` field on the market (may be stale)
  If none of those return a value, the market is skipped. There is no
  hard-coded default — the previous 1000-bps default was an order of
  magnitude too high and caused profitable arbs to be rejected.

ORDER SIGNING (EIP-712):
  Domain: "Polymarket CTF Exchange" v1, chainId=137
  Contract: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E  (neg_risk=false markets)
  Side BUY:
    makerAmount = price_float × size × DECIMALS   (USDC in, 6 dec)
    takerAmount = size × DECIMALS                 (shares out, 6 dec)
"""

import os
import json
import time
import hmac
import hashlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List

import requests
import websocket

# Force IPv4 — Polymarket is dual-stack (Cloudflare) but this env has no working
# IPv6 route, so requests AND the websocket hang ~40-100s on IPv6 before falling
# back. This was the root cause of the dead Poly feed. We patch BOTH urllib3
# (for requests) and socket.getaddrinfo (for websocket-client, which doesn't use
# urllib3) to return only IPv4 addresses. (Idempotent with engine.py.)
import socket as _socket
try:
    import urllib3.util.connection as _u3conn
    _u3conn.allowed_gai_family = lambda: _socket.AF_INET
except Exception:
    pass
if not getattr(_socket, "_ipv4_forced", False):
    _orig_gai = _socket.getaddrinfo
    def _gai_ipv4(host, port, family=0, *a, **k):
        return _orig_gai(host, port, _socket.AF_INET, *a, **k)
    _socket.getaddrinfo = _gai_ipv4
    _socket._ipv4_forced = True

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("kalshi.polymarket")

# ── Constants ─────────────────────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"
POLY_REST  = "https://clob.polymarket.com"
POLY_WS    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Seconds between periodic REST re-seeds of each subscribed market (safety net
# for silent/reconnecting WS feeds). Kept short so re-seeded snapshots stay
# within the arb staleness gate's acceptance window.
POLY_RESEED_INTERVAL = float(os.getenv("POLY_RESEED_INTERVAL", "2.0"))

# Polygon mainnet CTF Exchange contract (neg_risk=false markets)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# ERC1155 + USDC both use 6 decimal places in Polymarket's CLOB
DECIMALS = 10 ** 6

_ASSET_SLUG = {"BTC": "btc", "ETH": "eth", "SOL": "sol"}

# Optional override: force a specific taker fee (in basis points) regardless of
# what gamma or the CLOB /fee-rate endpoint returns. Use for testing only.
# Empty string = disabled.
_POLY_FEE_BPS_OVERRIDE = os.getenv("POLY_FEE_BPS_OVERRIDE", "").strip()

def _fee_bps_override() -> Optional[int]:
    if not _POLY_FEE_BPS_OVERRIDE:
        return None
    try:
        return int(_POLY_FEE_BPS_OVERRIDE)
    except ValueError:
        log.warning("Bad POLY_FEE_BPS_OVERRIDE %r — ignoring", _POLY_FEE_BPS_OVERRIDE)
        return None

def fetch_live_fee_bps(token_id: str) -> Optional[int]:
    """
    GET clob.polymarket.com/fee-rate?token_id=... — authoritative current rate.

    Response shape isn't strictly nailed down in public docs; we try a few
    common field names and return None on any error. Caller should fall back
    to the gamma `takerBaseFee` value.
    """
    try:
        r = requests.get(
            f"{POLY_REST}/fee-rate",
            params={"token_id": token_id},
            timeout=3,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug("Poly /fee-rate fetch failed for %s: %s", token_id[-8:], e)
        return None

    for key in ("fee_rate_bps", "feeRateBps", "taker_fee_bps", "takerFeeBps",
                "fee_rate", "rate", "bps"):
        if key in data:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                continue
    log.debug("Poly /fee-rate: unrecognized response shape: %s", data)
    return None

# POLY_DRY_RUN overrides DRY_RUN for the Polymarket leg only.
# Use this to test Polymarket order flow independently (Kalshi stays simulated).
# WARNING: a real Poly fill with no Kalshi fill = naked position. Use size=1 only.
_global_dry = os.getenv("DRY_RUN", "true").lower() != "false"
DRY_RUN     = os.getenv("POLY_DRY_RUN", str(_global_dry)).lower() != "false"

# ── EIP-712 order schema ──────────────────────────────────────────────────────

_EIP712_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,
    "verifyingContract": CTF_EXCHANGE,
}

_ORDER_TYPE_FIELDS = [
    {"name": "salt",          "type": "uint256"},
    {"name": "maker",         "type": "address"},
    {"name": "signer",        "type": "address"},
    {"name": "taker",         "type": "address"},
    {"name": "tokenId",       "type": "uint256"},
    {"name": "makerAmount",   "type": "uint256"},
    {"name": "takerAmount",   "type": "uint256"},
    {"name": "expiration",    "type": "uint256"},
    {"name": "nonce",         "type": "uint256"},
    {"name": "feeRateBps",    "type": "uint256"},
    {"name": "side",          "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
]

# ── Local orderbook ───────────────────────────────────────────────────────────

class _TokenBook:
    """Thread-safe ask-side book for one Polymarket outcome token."""

    def __init__(self):
        self._asks: Dict[str, float] = {}   # price_str → size (USDC)
        self._lock = threading.Lock()

    def apply_snapshot(self, asks: List[dict]):
        if not asks:
            return  # never wipe existing data with an empty snapshot
        with self._lock:
            self._asks = {a["price"]: float(a.get("size", 0)) for a in asks}

    def apply_change(self, side: str, price: str, size: float):
        if side.upper() != "ASK":
            return
        with self._lock:
            if size <= 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = size

    def best_ask_cents(self) -> Optional[int]:
        with self._lock:
            candidates = [float(p) for p, s in self._asks.items() if s > 0]
            if not candidates:
                return None
            return round(min(candidates) * 100)

    def best_ask_depth(self) -> Optional[float]:
        """Shares resting at the best (lowest) ask price."""
        with self._lock:
            live = [(float(p), s) for p, s in self._asks.items() if s > 0]
            if not live:
                return None
            best_price = min(p for p, _ in live)
            return sum(s for p, s in live if p == best_price)


# ── Market snapshot ───────────────────────────────────────────────────────────

@dataclass
class PolySnap:
    """Best ask prices for both outcome tokens of one Polymarket market."""
    condition_id: str
    yes_token_id: str   # "Up" outcome token
    no_token_id:  str   # "Down" outcome token
    fee_bps:      int   # taker fee in basis points
    yes_ask: Optional[int]   = None   # integer cents, or None if book empty
    no_ask:  Optional[int]   = None
    yes_ask_depth: Optional[float] = None  # shares at top of YES ask
    no_ask_depth:  Optional[float] = None  # shares at top of NO ask
    ts: float = field(default_factory=time.time)


# ── Market metadata ────────────────────────────────────────────────────────────

@dataclass
class PolyMarketInfo:
    condition_id:  str
    yes_token_id:  str
    no_token_id:   str
    fee_bps:       int
    accepting_orders: bool = True
    fee_source:    str  = "unknown"   # "override" | "clob" | "gamma"


# ── Dynamic discovery ─────────────────────────────────────────────────────────

def get_market_for_window(asset: str, window_ts: int) -> Optional[PolyMarketInfo]:
    """
    Look up the Polymarket market for a given asset + 15-min window.

    Uses the gamma API with slug = {asset}-updown-15m-{window_ts}.
    window_ts is the window START timestamp in Unix seconds, which is the same
    value as MarketSnapshot.window_ts from the Kalshi engine.

    Returns None if the market doesn't exist or isn't accepting orders yet.
    """
    prefix = _ASSET_SLUG.get(asset.upper())
    if not prefix:
        log.warning("No Polymarket slug prefix for asset %s", asset)
        return None

    slug = f"{prefix}-updown-15m-{window_ts}"
    try:
        r = requests.get(
            f"{GAMMA_BASE}/events",
            params={"slug": slug},
            timeout=5,
        )
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        log.warning("Poly market lookup %s (%s): %s", slug, asset, e)
        return None

    if not events:
        log.debug("No Polymarket event for slug %s", slug)
        return None

    event   = events[0] if isinstance(events, list) else events
    markets = event.get("markets", [])
    if not markets:
        log.debug("Poly event %s has no markets", slug)
        return None

    mkt = markets[0]

    # clobTokenIds is a stringified JSON array: '["yes_token_id", "no_token_id"]'
    raw_tokens = mkt.get("clobTokenIds", "[]")
    try:
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
    except Exception:
        log.warning("Could not parse clobTokenIds for %s: %r", slug, raw_tokens)
        return None

    if len(tokens) < 2:
        log.warning("Unexpected token count (%d) for %s", len(tokens), slug)
        return None

    # index 0 = "Up" = YES, index 1 = "Down" = NO (confirmed by outcomes array)
    yes_token = str(tokens[0])
    no_token  = str(tokens[1])

    # Fee resolution: override → live CLOB → gamma → skip (fail closed).
    fee_bps = _fee_bps_override()
    fee_src = "override"
    if fee_bps is None:
        fee_bps = fetch_live_fee_bps(yes_token)
        fee_src = "clob"
    if fee_bps is None:
        raw_fee = mkt.get("takerBaseFee")
        if raw_fee is not None:
            try:
                fee_bps = int(raw_fee)
                fee_src = "gamma"
            except (TypeError, ValueError):
                fee_bps = None
    if fee_bps is None:
        log.warning(
            "Poly market %s — no fee rate from override/clob/gamma; skipping",
            slug,
        )
        return None

    accepting = bool(mkt.get("acceptingOrders", True))

    log.info(
        "Poly market found: %s  cond=%s...  fee=%dbps (%s)  accepting=%s",
        slug, mkt.get("conditionId", "")[:12], fee_bps, fee_src, accepting,
    )
    return PolyMarketInfo(
        condition_id=mkt.get("conditionId", ""),
        yes_token_id=yes_token,
        no_token_id=no_token,
        fee_bps=fee_bps,
        accepting_orders=accepting,
        fee_source=fee_src,
    )


# ── CLOB client ───────────────────────────────────────────────────────────────

class PolyClient:
    """
    Polymarket CLOB client: live WS orderbook feed + FOK order placement.

    Usage:
        client = PolyClient()
        # At window start — subscribe with market info from get_market_for_window()
        client.subscribe_market(info)
        snap = client.snap(condition_id)    # → PolySnap with yes_ask/no_ask in cents
        filled = client.place_fok(yes_token_id, price_cents=88, size=5, fee_bps=info.fee_bps)
    """

    def __init__(self, on_snap: Optional[Callable[[PolySnap], None]] = None):
        self._on_snap = on_snap
        self._snaps:  Dict[str, PolySnap]    = {}   # condition_id → PolySnap
        self._books:  Dict[str, _TokenBook]  = {}   # token_id → _TokenBook
        self._infos:  Dict[str, "PolyMarketInfo"] = {}  # condition_id → info (for re-seed)
        self._ws:     Optional[websocket.WebSocketApp] = None
        self._subscribed_tokens: List[str]   = []
        self._reconnect = True
        self._lock = threading.Lock()

        # Credentials
        self._private_key  = os.getenv("POLY_PRIVATE_KEY", "")
        self._api_key      = os.getenv("POLY_API_KEY", "")
        self._api_secret   = os.getenv("POLY_API_SECRET", "")
        self._passphrase   = os.getenv("POLY_API_PASSPHRASE", "")
        self._chain_id     = int(os.getenv("POLY_CHAIN_ID", "137"))
        # Deposit-wallet (proxy) flow: funder = Polymarket deposit address,
        # signature_type 1 = Email/Magic proxy, 2 = browser-wallet Gnosis Safe.
        self._funder         = os.getenv("POLY_FUNDER", "")
        self._signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))
        self._address: Optional[str] = None

        if self._private_key:
            self._address = self._derive_address()

        # Official CLOB v2 client — order placement only. The legacy hand-rolled
        # v1 signing was deprecated server-side (CLOB v2 migration, Apr 2026);
        # the v2 SDK resolves the order schema/version automatically.
        self._clob = self._build_clob_client()

        # Start WS loop
        self._ws_thread = threading.Thread(
            target=self._ws_loop, name="poly-ws", daemon=True
        )
        self._ws_thread.start()

        # Periodic REST re-seed: safety net so a market whose WS feed is silent
        # (reconnect gap, missed snapshot) doesn't go permanently stale and get
        # blocked by the arb staleness gate. Refreshes every subscribed market
        # from REST on a short cadence; WS updates still take precedence.
        self._reseed_thread = threading.Thread(
            target=self._reseed_loop, name="poly-reseed", daemon=True
        )
        self._reseed_thread.start()

    def _reseed_loop(self):
        while self._reconnect:
            time.sleep(POLY_RESEED_INTERVAL)
            with self._lock:
                infos = list(self._infos.values())
            for info in infos:
                try:
                    self._seed_rest(info)
                except Exception as e:
                    log.debug("Poly re-seed %s: %s", info.condition_id[-8:], e)

    # ── Address derivation ───────────────────────────────────────────────────

    def _derive_address(self) -> Optional[str]:
        try:
            from eth_account import Account
            return Account.from_key(self._private_key).address
        except Exception as e:
            log.warning("Could not derive Polymarket address: %s", e)
            return None

    def _build_clob_client(self):
        """
        Construct the official py-clob-client-v2 ClobClient for order placement.
        Returns None if creds/funder are missing (DRY_RUN can still run without it).
        """
        if not (self._private_key and self._api_key and self._funder):
            log.warning(
                "Poly v2 client not built — need POLY_PRIVATE_KEY, POLY_API_KEY, "
                "and POLY_FUNDER. Order placement will be unavailable."
            )
            return None
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds
        except ImportError:
            log.error("py-clob-client-v2 not installed: pip install py-clob-client-v2")
            return None
        try:
            creds = ApiCreds(self._api_key, self._api_secret, self._passphrase)
            client = ClobClient(
                POLY_REST,
                chain_id=self._chain_id,
                key=self._private_key,
                creds=creds,
                signature_type=self._signature_type,
                funder=self._funder,
            )
            log.info(
                "Poly v2 client ready — funder=%s sigType=%d",
                self._funder, self._signature_type,
            )
            return client
        except Exception as e:
            log.error("Failed to build Poly v2 client: %s", e)
            return None

    # ── L1 auth headers ──────────────────────────────────────────────────────

    def _l1_headers(self, method: str, path: str, body: str = "") -> dict:
        """
        HMAC-SHA256 L1 API-key authentication.
        POLY_API_SECRET is stored as base64 — decode to raw bytes before use.
        Signature output must also be base64-encoded (not hex).
        """
        import base64
        ts          = str(int(time.time()))
        msg         = ts + method.upper() + path + body
        # Polymarket issues the API secret as URL-safe base64 (contains - and _).
        # Standard b64decode throws "Incorrect padding" on those chars; the
        # signature must also be URL-safe base64 to match their L2 HMAC spec.
        secret_bytes = base64.urlsafe_b64decode(self._api_secret)
        raw_sig     = hmac.new(secret_bytes, msg.encode(), hashlib.sha256).digest()
        sig         = base64.urlsafe_b64encode(raw_sig).decode()
        # Polymarket L2 header keys use UNDERSCORES, not hyphens (per the official
        # py-clob-client headers.py). Hyphens → server sees no api key → HTTP 401.
        return {
            "POLY_ADDRESS":    self._address or "",
            "POLY_SIGNATURE":  sig,
            "POLY_TIMESTAMP":  ts,
            "POLY_API_KEY":    self._api_key,
            "POLY_PASSPHRASE": self._passphrase,
            "Content-Type":    "application/json",
        }

    # ── Order signing ────────────────────────────────────────────────────────

    def _sign_order(self, token_id: str, price_cents: int,
                    size: int, fee_bps: int) -> Optional[dict]:
        """
        Build and EIP-712-sign a FOK buy order.
        Returns the full order body for POST /order, or None on error.
        """
        if not self._private_key or not self._address:
            log.error("Polymarket private key not configured")
            return None
        try:
            from eth_account import Account
        except ImportError:
            log.error("eth-account not installed: pip install eth-account")
            return None

        try:
            account      = Account.from_key(self._private_key)
            price_float  = price_cents / 100.0
            maker_amount = int(price_float * size * DECIMALS)  # USDC spending
            taker_amount = int(size * DECIMALS)                 # shares receiving
            salt         = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF

            order_msg = {
                "salt":          salt,
                "maker":         account.address,
                "signer":        account.address,
                "taker":         "0x0000000000000000000000000000000000000000",
                "tokenId":       int(token_id),
                "makerAmount":   maker_amount,
                "takerAmount":   taker_amount,
                "expiration":    0,
                "nonce":         0,
                "feeRateBps":    fee_bps,
                "side":          0,    # BUY
                "signatureType": 0,    # EOA
            }

            signed = account.sign_typed_data(
                domain_data=_EIP712_DOMAIN,
                message_types={"Order": _ORDER_TYPE_FIELDS},
                message_data=order_msg,
            )

            return self._order_to_json(order_msg, signed.signature.hex(), "BUY")
        except Exception as e:
            log.error("Order signing failed: %s", e)
            return None

    def _order_to_json(self, order_msg: dict, signature_hex: str,
                       side_str: str, order_type: str = "FOK") -> dict:
        """
        Convert a signed EIP-712 order struct into the JSON body POST /order
        expects. The signed struct uses numeric side/salt; the JSON body uses
        string amounts, numeric salt, the "BUY"/"SELL" side string, and `owner`
        = API key UUID (matches the official py-clob-client order_to_json).
        """
        sig_hex = signature_hex if signature_hex.startswith("0x") else "0x" + signature_hex
        return {
            "order": {
                "salt":          order_msg["salt"],          # number
                "maker":         order_msg["maker"],
                "signer":        order_msg["signer"],
                "taker":         order_msg["taker"],
                "tokenId":       str(order_msg["tokenId"]),
                "makerAmount":   str(order_msg["makerAmount"]),
                "takerAmount":   str(order_msg["takerAmount"]),
                "expiration":    str(order_msg["expiration"]),
                "nonce":         str(order_msg["nonce"]),
                "feeRateBps":    str(order_msg["feeRateBps"]),
                "side":          side_str,                    # "BUY" / "SELL"
                "signatureType": order_msg["signatureType"], # number
                "signature":     sig_hex,
            },
            "owner":     self._api_key,                       # API key UUID
            "orderType": order_type,
        }

    # ── Order placement ──────────────────────────────────────────────────────

    @staticmethod
    def _filled_shares(resp: dict, fallback_size: float) -> float:
        """
        Extract filled share count from a v2 post_order response. A FOK that
        fully fills returns status=matched with takingAmount = shares received;
        a kill returns status=live/unmatched with empty amounts.
        """
        status = (resp.get("status") or "").lower()
        taking = resp.get("takingAmount", "")
        if taking not in ("", None):
            try:
                return float(taking)
            except (TypeError, ValueError):
                pass
        # No explicit amount — infer from status (matched → full, else 0).
        if status == "matched":
            return float(fallback_size)
        return 0.0

    def place_fok(self, token_id: str, price_cents: int,
                  size: int, fee_bps: int) -> float:
        """
        Place a FOK buy order for `size` shares at `price_cents` via CLOB v2.
        Returns filled share count as float (simulates full fill in DRY_RUN).
        """
        if DRY_RUN:
            log.info(
                "[DRY RUN] Poly FOK buy token=...%s price=%dc x%d",
                token_id[-6:], price_cents, size,
            )
            return float(size)

        if self._clob is None:
            log.error("Poly v2 client unavailable — cannot place FOK buy")
            return 0.0

        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY
        except ImportError:
            log.error("py-clob-client-v2 not installed")
            return 0.0

        try:
            args   = OrderArgs(price=price_cents / 100.0, size=size,
                               side=BUY, token_id=token_id)
            signed = self._clob.create_order(args)
            resp   = self._clob.post_order(signed, OrderType.FOK)
            filled = self._filled_shares(resp, size)
            if filled == 0:
                log.warning(
                    "Poly FOK not filled token=...%s price=%dc x%d — resp: %s",
                    token_id[-6:], price_cents, size, str(resp)[:300],
                )
            else:
                log.info(
                    "Poly FOK token=...%s price=%dc x%d → filled=%s",
                    token_id[-6:], price_cents, size, filled,
                )
            return filled
        except Exception as e:
            log.error("Poly FOK buy error token=...%s price=%dc: %s",
                      token_id[-6:], price_cents, e)
            return 0.0

    def place_sell_fok(self, token_id: str, size: float, fee_bps: int) -> float:
        """
        Emergency unwind sell of `size` shares at any available bid via CLOB v2.

        CRITICAL: uses FAK (Fill-And-Kill), NOT FOK. A naked-leg unwind must take
        whatever liquidity exists right now — FOK's all-or-nothing means a thin
        book sells ZERO and leaves the position fully naked (this cost a real
        $2.60 loss on 2026-05-29). The order is a SELL limit at 1¢ so it crosses
        any bid; FAK fills what it can immediately and kills the remainder.

        Returns sold share count (may be partial). Caller must check < size.
        """
        if DRY_RUN:
            log.info("[DRY RUN] Poly FAK sell token=...%s x%s", token_id[-6:], size)
            return float(size)

        if self._clob is None:
            log.error("Poly v2 client unavailable — cannot unwind (naked leg!)")
            return 0.0

        try:
            from py_clob_client_v2.clob_types import (
                OrderArgs, OrderType, BalanceAllowanceParams, AssetType,
            )
            from py_clob_client_v2.order_builder.constants import SELL
        except ImportError:
            log.error("py-clob-client-v2 not installed")
            return 0.0

        # Polymarket order size is whole shares; round DOWN so we never try to
        # sell more than we hold (a 5.55 fill means 5 sellable whole shares).
        sell_size = int(size)
        if sell_size <= 0:
            log.warning("Poly unwind: size %.4f rounds to 0 shares — nothing to sell", size)
            return 0.0

        def _submit_sell():
            args   = OrderArgs(price=0.01, size=sell_size, side=SELL, token_id=token_id)
            signed = self._clob.create_order(args)
            resp   = self._clob.post_order(signed, OrderType.FAK)
            return self._filled_shares(resp, sell_size)

        try:
            # SELL limit @1¢ + FAK: crosses any bid, takes all available now,
            # kills the rest. Never all-or-nothing.
            sold = _submit_sell()
        except Exception as e:
            # The CLOB's balance ledger can lag a successful FOK buy by 1–3 s
            # (matching engine confirms fill instantly; ERC-1155 transfer + indexer
            # take a few seconds). The unwind path always fires inside this gap.
            # Distinguish race from real failure by polling the balance directly —
            # only retry if/when the ledger confirms the shares are actually there.
            # Real config/allowance/settlement failures keep balance at 0 across
            # all probes and fall through to the halt below.
            need_units = sell_size * 1_000_000  # shares → 6-dec units
            sold = 0.0
            for wait in (0.5, 1.0, 2.0):  # total ≤3.5 s before giving up
                time.sleep(wait)
                try:
                    bal = self._clob.get_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL, token_id=token_id,
                        )
                    )
                    if int(bal.get("balance", 0)) < need_units:
                        continue
                    sold = _submit_sell()
                    log.warning(
                        "Poly unwind retry succeeded after %.1fs ledger lag — "
                        "token=...%s sold=%s", wait, token_id[-6:], sold,
                    )
                    break
                except Exception as e2:
                    log.debug("Unwind retry probe failed: %s", e2)
            if sold == 0.0:
                log.error(
                    "Poly unwind sell error token=...%s x%s: %s — POSITION STILL NAKED",
                    token_id[-6:], sell_size, e,
                )
                return 0.0

        if sold < sell_size:
            log.error(
                "Poly UNWIND PARTIAL token=...%s sold=%s/%s — STILL NAKED on remainder!",
                token_id[-6:], sold, sell_size,
            )
        else:
            log.info("Poly unwind sell token=...%s x%s → sold=%s",
                     token_id[-6:], sell_size, sold)
        return sold

    # ── Market data ──────────────────────────────────────────────────────────

    def subscribe_market(self, info: PolyMarketInfo):
        """
        Register a market for live WS tracking.
        Seeds from REST first, then live updates flow through the WS.
        Can be called mid-session to add new windows.
        """
        with self._lock:
            if info.yes_token_id not in self._books:
                self._books[info.yes_token_id] = _TokenBook()
            if info.no_token_id not in self._books:
                self._books[info.no_token_id]  = _TokenBook()
            if info.condition_id not in self._snaps:
                self._snaps[info.condition_id] = PolySnap(
                    condition_id=info.condition_id,
                    yes_token_id=info.yes_token_id,
                    no_token_id=info.no_token_id,
                    fee_bps=info.fee_bps,
                )
            for tok in (info.yes_token_id, info.no_token_id):
                if tok not in self._subscribed_tokens:
                    self._subscribed_tokens.append(tok)
            self._infos[info.condition_id] = info

        # Seed from REST before WS catches up
        self._seed_rest(info)

        # Re-subscribe WS with the new tokens
        if self._ws:
            self._ws_subscribe(self._ws)

    def _seed_rest(self, info: PolyMarketInfo):
        """Fetch initial orderbook state via REST for both tokens."""
        for token_id, ask_attr, depth_attr in [
            (info.yes_token_id, "yes_ask", "yes_ask_depth"),
            (info.no_token_id,  "no_ask",  "no_ask_depth"),
        ]:
            try:
                r = requests.get(
                    f"{POLY_REST}/book",
                    params={"token_id": token_id},
                    timeout=5,
                )
                r.raise_for_status()
                data = r.json()
                book = self._books.get(token_id)
                if book:
                    book.apply_snapshot(data.get("asks", []))
                snap = self._snaps.get(info.condition_id)
                if snap and book:
                    setattr(snap, ask_attr, book.best_ask_cents())
                    setattr(snap, depth_attr, book.best_ask_depth())
                    snap.ts = time.time()
            except Exception as e:
                log.debug("Poly REST seed %s: %s", token_id[-8:], e)

    def snap(self, condition_id: str) -> Optional[PolySnap]:
        return self._snaps.get(condition_id)

    # ── WebSocket ────────────────────────────────────────────────────────────

    def stop(self):
        self._reconnect = False
        if self._ws:
            self._ws.close()

    def _ws_loop(self):
        while self._reconnect:
            try:
                self._connect_ws()
            except Exception as e:
                log.warning("Poly WS loop: %s — retry in 5s", e)
            if self._reconnect:
                time.sleep(5)

    def _connect_ws(self):
        # Per-connection flag so the keepalive thread stops when this socket dies.
        ka_stop = threading.Event()

        def on_open(ws):
            self._ws_subscribe(ws)
            # Polymarket's Market channel requires an APPLICATION-LEVEL "PING"
            # text frame every ≤10s (protocol ping frames are NOT enough — the
            # server drops idle connections on a ~80s timer, which is what we
            # were seeing). Send "PING" every 5s on a dedicated thread.
            def _keepalive():
                while not ka_stop.wait(5.0):
                    try:
                        ws.send("PING")
                    except Exception:
                        break
            threading.Thread(target=_keepalive, name="poly-ws-ping",
                             daemon=True).start()

        def on_message(ws, raw):
            # Server may reply "PONG" to our PING — ignore non-JSON frames.
            if raw == "PONG" or not raw:
                return
            try:
                data = json.loads(raw)
                msgs = data if isinstance(data, list) else [data]
                for m in msgs:
                    self._handle_msg(m)
            except Exception as e:
                log.debug("Poly WS parse: %s", e)

        def on_error(ws, err):
            log.warning("Poly WS error: %s", err)

        def on_close(ws, *args):
            ka_stop.set()
            log.info("Poly WS closed")

        self._ws = websocket.WebSocketApp(
            POLY_WS,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        # Keep protocol-level ping too as a secondary heartbeat.
        self._ws.run_forever(ping_interval=20, ping_timeout=10)
        ka_stop.set()

    def _ws_subscribe(self, ws):
        with self._lock:
            tokens = list(self._subscribed_tokens)
        if not tokens:
            return
        # Market channel subscribe (current spec): type MUST be lowercase
        # "market" — the old "Market" (capital M) was silently ignored by the
        # server, so it accepted the socket but streamed no book data.
        msg = {"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}
        try:
            ws.send(json.dumps(msg))
            log.info("Poly WS subscribed to %d tokens", len(tokens))
        except Exception as e:
            log.debug("Poly WS subscribe: %s", e)

    def _handle_msg(self, msg: dict):
        event    = msg.get("event_type") or msg.get("type", "")
        token_id = msg.get("asset_id", "")

        book = self._books.get(token_id)
        if book is None:
            return

        snap = next(
            (s for s in self._snaps.values()
             if token_id in (s.yes_token_id, s.no_token_id)),
            None,
        )
        if snap is None:
            return

        is_yes = (token_id == snap.yes_token_id)

        ask_affected = False
        if event == "book":
            book.apply_snapshot(msg.get("asks", []))
            ask_affected = True
        elif event == "price_change":
            for ch in msg.get("changes", []):
                if ch.get("side", "").upper() == "ASK":
                    book.apply_change(
                        ch.get("side", ""),
                        ch.get("price", ""),
                        float(ch.get("size", 0)),
                    )
                    ask_affected = True
        else:
            return

        snap.ts = time.time()
        snap._ws_confirmed = True

        if not ask_affected:
            return  # bid-only event — ask price unchanged, skip overwrite

        new_ask   = book.best_ask_cents()
        new_depth = book.best_ask_depth()
        if new_ask is not None:            # never overwrite a valid price with None
            if is_yes:
                snap.yes_ask = new_ask
                snap.yes_ask_depth = new_depth
            else:
                snap.no_ask = new_ask
                snap.no_ask_depth = new_depth

        if self._on_snap:
            self._on_snap(snap)


# ── CLI discovery helper ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    asset = (sys.argv[1] if len(sys.argv) > 1 else "BTC").upper()
    ts    = int(sys.argv[2]) if len(sys.argv) > 2 else None

    if ts:
        # Explicit timestamp supplied
        info = get_market_for_window(asset, ts)
        if info:
            print(f"\nFound {asset} market for window ts={ts}:")
            print(f"  condition_id : {info.condition_id}")
            print(f"  yes_token_id : {info.yes_token_id}")
            print(f"  no_token_id  : {info.no_token_id}")
            print(f"  fee_bps      : {info.fee_bps}")
            print(f"  accepting    : {info.accepting_orders}")
        else:
            print(f"\nNo market found for {asset} ts={ts}")
    else:
        # Walk backwards from the current 15-min window until we find 3 markets.
        # Each Polymarket window starts at a multiple of 900 seconds.
        now_ts      = int(time.time())
        base_window = (now_ts // 900) * 900   # floor to nearest 15-min boundary

        print(f"\nSearching recent {asset} 15-min markets (current window_ts={base_window})...\n")

        found = 0
        for offset in range(8):              # check current + up to 7 prior windows
            w_ts = base_window - offset * 900
            info = get_market_for_window(asset, w_ts)
            if not info:
                continue
            label = "← current" if offset == 0 else f"← {offset * 15}m ago"
            print(f"  window_ts    : {w_ts}  {label}")
            print(f"  condition_id : {info.condition_id}")
            print(f"  yes_token_id : {info.yes_token_id}")
            print(f"  no_token_id  : {info.no_token_id}")
            print(f"  fee_bps      : {info.fee_bps}")
            print(f"  accepting    : {info.accepting_orders}")
            print()
            found += 1
            if found >= 3:
                break

        if found == 0:
            print(f"No recent {asset} 15-min markets found.")
            print(f"Check that '{_ASSET_SLUG.get(asset, '?')}-updown-15m-{{ts}}' exists on Polymarket.")
            print(f"Tried window timestamps: {base_window} down to {base_window - 7*900}")
