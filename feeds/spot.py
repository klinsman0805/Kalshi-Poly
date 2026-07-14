"""
feeds/spot.py — live crypto spot reference for the Scalping module.

Polls Coinbase's public spot endpoint (no auth) for BTC/ETH/SOL and keeps a
short rolling history so the dashboard can show price + short-term velocity.
This is the "where is the asset right now" leg that the scalper compares the
Kalshi up/down contract price against.
"""

import logging
import threading
import time

import requests

log = logging.getLogger("feeds.spot")

PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
SPOT_URL = "https://api.coinbase.com/v2/prices/{pair}/spot"
HISTORY_SECS = 120


class SpotFeed:
    """Background poller. Thread-safe reads via get()/velocity()/snapshot()."""

    def __init__(self, assets=("BTC", "ETH", "SOL"), interval=2.0):
        self.assets = list(assets)
        self.interval = interval
        self._prices = {a: None for a in self.assets}
        self._hist = {a: [] for a in self.assets}  # list[(ts, price)]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thr = None

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._loop, daemon=True, name="spot-feed")
        self._thr.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        sess = requests.Session()
        while not self._stop.is_set():
            for a in self.assets:
                try:
                    r = sess.get(SPOT_URL.format(pair=PAIRS[a]), timeout=5)
                    r.raise_for_status()
                    p = float(r.json()["data"]["amount"])
                    now = time.time()
                    with self._lock:
                        self._prices[a] = p
                        h = self._hist[a]
                        h.append((now, p))
                        cutoff = now - HISTORY_SECS
                        self._hist[a] = [x for x in h if x[0] >= cutoff]
                except Exception as e:  # noqa: BLE001 — feed must never crash the loop
                    log.debug("spot %s: %s", a, e)
            self._stop.wait(self.interval)

    def get(self, asset):
        with self._lock:
            return self._prices.get(asset)

    def velocity(self, asset, window=30):
        """Dollar change over the last `window` seconds (None if too little data)."""
        with self._lock:
            h = list(self._hist.get(asset) or [])
        if len(h) < 2:
            return None
        now = time.time()
        ref = None
        for ts, p in h:
            if ts >= now - window:
                ref = p
                break
        if ref is None:
            return None
        return round(h[-1][1] - ref, 2)

    def snapshot(self):
        out = {}
        for a in self.assets:
            out[a] = {
                "spot": self.get(a),
                "vel30": self.velocity(a, 30),
                "vel60": self.velocity(a, 60),
            }
        return out
