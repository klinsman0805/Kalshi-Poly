"""
client_chat.py — Read-only Q&A bot for the shared client dashboard.

Answers free-form questions about the CURRENT dashboard state (open positions,
recent settles, recent log events) using Gemini's free tier. It has no access
to place, modify, or cancel anything — it only ever sees a curated text summary
built from the same data already rendered on the read-only dashboard, and
returns a text answer. There is no tool-use / function-calling wired in, so
there is no path from a question to a live action.
"""

import os
import time
import threading
from collections import deque

import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

MAX_QUESTION_CHARS = int(os.getenv("CLIENT_CHAT_MAX_QLEN", "300"))
MAX_PER_HOUR = int(os.getenv("CLIENT_CHAT_MAX_PER_HOUR", "20"))

SYSTEM_PROMPT = """You are a read-only assistant embedded in a live client dashboard \
for a weather-market trading operation (temperature markets on Polymarket and Kalshi).

The DASHBOARD DATA includes live METAR weather-station readings for each city's \
market (today's extreme so far, current reading, wind/precip/sky conditions) — use \
this to explain WHY a position won, lost, or is still pending (e.g. "Wuhan's high \
already hit 34C, one degree past the 33C bucket, so that position is a loss once \
it settles"), not just the trade's entry/exit prices.

Rules:
- Answer ONLY using the DASHBOARD DATA given below. Never invent a number, city, \
or outcome that isn't in it.
- You have no ability to place, close, or change any trade — you are a read-only \
viewer of the same data already shown on the dashboard. If asked to do something \
(place a trade, change a setting), say you can't and that's the operator's call.
- Do not give financial or investment advice, and do not predict future market \
outcomes beyond what's already in the data.
- If the data doesn't answer the question, say so plainly instead of guessing.
- Keep answers short — a few sentences, plain language, no jargon dump.
"""


class _RateLimiter:
    """Simple sliding-window cap, shared across all clients hitting this link."""

    def __init__(self, max_per_hour: int):
        self.max_per_hour = max_per_hour
        self._hits = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self._lock:
            while self._hits and now - self._hits[0] > 3600:
                self._hits.popleft()
            if len(self._hits) >= self.max_per_hour:
                return False
            self._hits.append(now)
            return True


rate_limiter = _RateLimiter(MAX_PER_HOUR)


def _fmt_positions(rows, label):
    if not rows:
        return f"{label}: none"
    lines = [f"{label}:"]
    for p in rows:
        lines.append(
            f"  - {p.get('city')} {p.get('date')} {p.get('kind')} {p.get('label')} — "
            f"entry {p.get('entry_c')}c, {p.get('shares')} sh, cost ${p.get('cost_usd')}, "
            f"mode={p.get('mode')}"
        )
    return "\n".join(lines)


def _fmt_history(rows, label, n=15):
    if not rows:
        return f"{label}: none yet"
    lines = [f"{label} (most recent {min(n, len(rows))}):"]
    for c in rows[:n]:
        outcome = "WIN" if c.get("won") else "loss"
        lines.append(
            f"  - {c.get('city')} {c.get('date')} {c.get('kind')} {c.get('label')} — "
            f"{outcome}, pnl=${c.get('pnl_usd')}, exit={c.get('exit') or 'settled'}, "
            f"mode={c.get('mode')}"
        )
    return "\n".join(lines)


def _fmt_wx(c):
    """Plain-language gist of a METAR conditions dict — no raw jargon dump."""
    if not c:
        return "no conditions data"
    bits = []
    if c.get("wx"):
        bits.append(c["wx"])
    elif c.get("precip_now"):
        bits.append("precipitation now")
    elif c.get("precip_recent_min") is not None and c.get("precip_recent_min", 9999) <= 90:
        bits.append(f"rain in the last {c['precip_recent_min']:.0f} min")
    if c.get("convective"):
        bits.append("thunderstorm activity")
    if c.get("gust_kt"):
        bits.append(f"gusting {c['gust_kt']:.0f}kt")
    elif c.get("wind_kt"):
        bits.append(f"wind {c['wind_kt']:.0f}kt")
    if c.get("ceiling_ft") and c["ceiling_ft"] < 3000:
        bits.append(f"low ceiling {c['ceiling_ft']:.0f}ft")
    age = c.get("obs_age_min")
    bits.append(f"obs {age:.0f}min old" if age is not None else "obs age unknown")
    return ", ".join(bits) if bits else "clear/calm"


def _fmt_weather_rows(rows, n=40):
    """Live METAR-derived snapshot per city/kind, today's markets only — this
    is what lets the bot answer "what happened with <city>?" with the real
    station reading instead of just the ledger's entry/exit prices."""
    today = [r for r in (rows or []) if r.get("is_today")]
    if not today:
        return "Live conditions: no data yet"
    lines = ["Live METAR conditions (today's markets):"]
    for r in today[:n]:
        unit = r.get("unit", "C")
        lines.append(
            f"  - {r.get('city')} ({r.get('station')}) {r.get('kind')}: "
            f"day's extreme so far {r.get('ext_c')}{unit} (now reading {r.get('temp_c')}{unit}), "
            f"local hour {r.get('local_hour')}, signal={r.get('signal')} ({r.get('why')}) — "
            f"{_fmt_wx(r.get('conditions'))}"
        )
    return "\n".join(lines)


def build_context(weather_exec_state, kalshi_exec_state, log_tail,
                   weather_rows=None, kalshi_rows=None, hide_balance=False):
    """Curated, secret-free text summary of current state for both venues."""
    parts = []

    w = weather_exec_state or {}
    parts.append(f"=== POLYMARKET weather bot (mode={w.get('mode')}) ===")
    parts.append(_fmt_positions(w.get("open"), "Open positions"))
    parts.append(_fmt_history(w.get("history"), "Recent settles"))
    sess = w.get("session") or {}
    if sess:
        parts.append(
            f"Session so far — settled {sess.get('settled')}, wins {sess.get('wins')}, "
            f"win rate {sess.get('win_rate')}"
        )
    if not hide_balance and w.get("account"):
        acct = w["account"]
        parts.append(f"Account: {acct}")
    parts.append(_fmt_weather_rows(weather_rows))

    parts.append("")
    k = kalshi_exec_state or {}
    parts.append(f"=== KALSHI weather bot (paper, mode={k.get('mode')}) ===")
    parts.append(_fmt_positions(k.get("open"), "Open positions"))
    parts.append(_fmt_history(k.get("history"), "Recent settles"))
    parts.append(_fmt_weather_rows(kalshi_rows))

    parts.append("")
    parts.append("=== Recent event log ===")
    for entry in (log_tail or [])[-20:]:
        parts.append(f"  [{entry.get('ts')}] {entry.get('icon')} {entry.get('msg')}")

    return "\n".join(parts)


def ask(question: str, context: str) -> str:
    """Send one question + context to Gemini, return its text answer.

    Raises on any HTTP/parsing failure — caller decides how to surface that.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured")

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": f"DASHBOARD DATA:\n{context}\n\nCLIENT QUESTION: {question}"}],
        }],
        # Some flash-tier models spend hidden "thinking" tokens out of this same
        # budget before the visible answer starts, which truncated short answers
        # mid-sentence at 400. thinkingConfig would disable that more cleanly,
        # but the field isn't accepted by whatever model gemini-flash-latest
        # currently resolves to (400 INVALID_ARGUMENT) — so just give more room.
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.2},
    }
    resp = requests.post(
        GEMINI_URL, params={"key": GEMINI_API_KEY}, json=body, timeout=20
    )
    if not resp.ok:
        # requests' own HTTPError only carries the status + URL — the useful
        # part (Google's own error message) is in the body, which otherwise
        # gets thrown away right when it's most needed to diagnose a bad key
        # or a not-yet-enabled API.
        detail = resp.text[:300]
        raise RuntimeError(f"Gemini {resp.status_code}: {detail}")
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = data.get("promptFeedback", {}).get("blockReason")
        raise RuntimeError(f"no answer returned ({block_reason or 'empty response'})")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("empty answer")
    return text
