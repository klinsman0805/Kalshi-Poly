"""
modules/copytrader.py — COPY-TRADE scanner (Polymarket only, read-only, FLAGGED OFF).

Ranks Polymarket leaderboard wallets and scores each for copy-trade suitability.
This is a MONITOR/RESEARCH module: it surfaces candidates, it does not place any
orders. Polymarket only — Kalshi exposes no per-account public data, so it cannot
be scanned this way.

Gate: disabled unless COPYTRADE_ENABLED=true. The whole feature flips off from
.env without touching any other module.

Config (env):
  COPYTRADE_ENABLED        true|false   (default false — feature flag)
  COPYTRADE_METRIC         profit|volume (default profit)
  COPYTRADE_WINDOW         1d|7d|30d|all (default all)
  COPYTRADE_TOP_N          how many leaderboard wallets to score (default 25)
  COPYTRADE_MAX_TRADE_USD  copyability gate: max avg trade size (default 5000)
  COPYTRADE_REFRESH_SEC    poll interval for the dashboard loop (default 300)
  COPYTRADE_DEEP           true|false  reconstruct lifetime realized winrate
                                       from /activity (slower) (default true)
  COPYTRADE_MAX_EVENTS     per-wallet activity events to scan when deep (default 1500)

Run standalone:  python -m modules.copytrader
"""

import logging
import os

from feeds import poly_leaderboard

log = logging.getLogger("modules.copytrader")


def _enabled() -> bool:
    return os.getenv("COPYTRADE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


class CopyTraderEngine:
    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda i, m: None)
        self.enabled = _enabled()
        self.metric = os.getenv("COPYTRADE_METRIC", "profit").strip().lower()
        self.window = os.getenv("COPYTRADE_WINDOW", "all").strip().lower()
        self.top_n = int(os.getenv("COPYTRADE_TOP_N", "25"))
        self.max_trade_usd = float(os.getenv("COPYTRADE_MAX_TRADE_USD", "5000"))
        self.refresh_sec = int(os.getenv("COPYTRADE_REFRESH_SEC", "300"))
        self.deep = os.getenv("COPYTRADE_DEEP", "true").strip().lower() in ("1", "true", "yes", "on")
        self.max_events = int(os.getenv("COPYTRADE_MAX_EVENTS", "1500"))
        self.rows: list = []
        self.last_error: str = ""

    def refresh(self) -> list:
        """Re-scan the leaderboard. No-op (returns []) when the flag is off."""
        if not self.enabled:
            return []
        try:
            self.rows = poly_leaderboard.scan(
                metric=self.metric, window=self.window,
                top_n=self.top_n, max_copy_trade_usd=self.max_trade_usd,
                deep=self.deep, max_events=self.max_events,
            )
            self.last_error = ""
            copyable = sum(1 for r in self.rows if r["copyable"])
            self.on_log("◆", f"[copytrade] scanned {len(self.rows)} wallets "
                             f"({self.metric}/{self.window}) — {copyable} copyable")
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.on_log("✗", f"[copytrade] scan error: {e}")
        return self.rows

    def state(self) -> dict:
        return {
            "enabled": self.enabled,
            "config": {
                "metric": self.metric,
                "window": self.window,
                "top_n": self.top_n,
                "max_trade_usd": self.max_trade_usd,
                "refresh_sec": self.refresh_sec,
                "deep": self.deep,
                "max_events": self.max_events,
            },
            "rows": self.rows,
            "last_error": self.last_error,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    os.environ.setdefault("COPYTRADE_ENABLED", "true")  # force-on for the CLI
    eng = CopyTraderEngine(on_log=lambda i, m: print(i, m))
    rows = eng.refresh()
    if not rows:
        print("No rows — leaderboard empty or COPYTRADE_ENABLED off.")
    else:
        def pct(v):
            return f"{v*100:>5.0f}%" if v is not None else "   — "
        hdr = (f"{'name':<18} {'profit$':>13} {'realWin':>8} {'realROI':>8} "
               f"{'res':>4} {'avg$':>10} {'pos':>4} copy")
        print("\n" + hdr)
        print("-" * len(hdr))
        for r in rows:
            rr = f"{r['realized_roi']*100:>6.0f}% " if r['realized_roi'] is not None else "     — "
            cap = "*" if r.get("realized_capped") else " "
            print(f"{r['name'][:18]:<18} {r['profit']:>13,.0f} "
                  f"{pct(r['realized_winrate'])}{cap} {rr} "
                  f"{(r['resolved_markets'] if r['resolved_markets'] is not None else '—'):>4} "
                  f"{r['avg_trade_usd']:>10,.0f} {r['open_positions']:>4} "
                  f"{'✓' if r['copyable'] else ' '}")
        print("\n  realWin/realROI = LIFETIME realized (settled markets). "
              "* = capped at max_events (partial history).")
        print("\nProfile URLs:")
        for r in rows[:10]:
            print(f"  {r['name'][:18]:<18} {r['url']}")
