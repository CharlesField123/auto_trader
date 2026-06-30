"""Scans a universe of Alpaca crypto pairs on 1-minute bars and ranks them to
find the *best* long candidates right now.

Crypto is the only Alpaca asset class that trades 24/7, so it's what powers the
24/5 scanner (Alpaca offers no futures product). The scanner is the crypto
counterpart to ``supabase_client.fetch_signals`` — it returns the same
:class:`~supabase_client.Signal` objects, so its output flows through the
existing risk manager and order path unchanged.

Scoring (per symbol, over the last ``SCAN_LOOKBACK_BARS`` one-minute bars):

* **Momentum** — rate of change of close across the window.
* **Volume confirmation** — recent volume vs. the window average.

  ``score = rate_of_change × volume_factor``

Because Alpaca crypto is **long-only** (no short selling) the scanner only emits
long candidates: symbols with positive, volume-backed momentum that clear
``CRYPTO_MIN_SCORE``. The top ``CRYPTO_TOP_N`` are returned, best first.

Stops and targets are percentage-based (``CRYPTO_STOP_LOSS_PCT`` /
``CRYPTO_TAKE_PROFIT_PCT``). Alpaca crypto supports neither bracket nor OCO
orders, so those levels are enforced by the scan loop closing the position when
unrealized P&L crosses them — the percentages here keep entry sizing and the
exit thresholds consistent.
"""

from __future__ import annotations

import os
from statistics import fmean

from alpaca_client import AlpacaClient, Bar
from logger import log
from supabase_client import Signal


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Comma-separated crypto pairs to scan (Alpaca "BASE/QUOTE" format).
CRYPTO_UNIVERSE = [
    s.strip().upper()
    for s in os.environ.get(
        "CRYPTO_UNIVERSE",
        "BTC/USD,ETH/USD,SOL/USD,LTC/USD,BCH/USD,AVAX/USD,LINK/USD,UNI/USD,DOGE/USD",
    ).split(",")
    if s.strip()
]

CRYPTO_TOP_N = _env_int("CRYPTO_TOP_N", 3)               # how many best to trade
SCAN_LOOKBACK_BARS = _env_int("SCAN_LOOKBACK_BARS", 20)  # 1-min bars per scan
CRYPTO_MIN_SCORE = _env_float("CRYPTO_MIN_SCORE", 0.0008)  # momentum gate
CRYPTO_STOP_LOSS_PCT = _env_float("CRYPTO_STOP_LOSS_PCT", 0.01)     # 1% stop
CRYPTO_TAKE_PROFIT_PCT = _env_float("CRYPTO_TAKE_PROFIT_PCT", 0.02)  # 2% target
MIN_BARS_REQUIRED = max(5, SCAN_LOOKBACK_BARS // 2)


def _score_symbol(bars: list[Bar]) -> tuple[float, float]:
    """Return ``(score, entry)`` for a long candidate, or ``(0.0, 0.0)``.

    Only positive (bullish) momentum scores, since Alpaca crypto is long-only.
    """
    if len(bars) < MIN_BARS_REQUIRED:
        return 0.0, 0.0

    closes = [b.close for b in bars]
    first, entry = closes[0], closes[-1]
    if first <= 0 or entry <= 0:
        return 0.0, 0.0

    roc = (entry - first) / first
    if roc <= 0:  # long-only: ignore flat/falling symbols
        return 0.0, 0.0

    volumes = [b.volume for b in bars]
    avg_vol = fmean(volumes) or 1.0
    recent_vol = fmean(volumes[-max(3, len(volumes) // 3):])
    volume_factor = min(recent_vol / avg_vol, 3.0)  # clip so one spike can't dominate

    return roc * volume_factor, entry


def _strength_for(score: float) -> str:
    if score >= CRYPTO_MIN_SCORE * 4:
        return "STRONG"
    if score >= CRYPTO_MIN_SCORE * 2:
        return "MODERATE"
    return "WEAK"


def scan(alpaca: AlpacaClient, exclude: set[str] | None = None) -> list[Signal]:
    """Scan the crypto universe and return the best long candidates as signals.

    Symbols already held (in ``exclude``, normalized) are skipped. The result is
    sorted best-first and capped at ``CRYPTO_TOP_N``.
    """
    exclude = {s.replace("/", "").upper() for s in (exclude or set())}
    ranked: list[tuple[float, Signal]] = []

    for symbol in CRYPTO_UNIVERSE:
        if symbol.replace("/", "").upper() in exclude:
            continue
        bars = alpaca.get_crypto_minute_bars(symbol, limit=SCAN_LOOKBACK_BARS)
        score, entry = _score_symbol(bars)
        if score < CRYPTO_MIN_SCORE:
            continue
        ranked.append(
            (
                score,
                Signal(
                    ticker=symbol,
                    direction="long",
                    take_profit=round(entry * (1 + CRYPTO_TAKE_PROFIT_PCT), 6),
                    stop_loss=round(entry * (1 - CRYPTO_STOP_LOSS_PCT), 6),
                    strength=_strength_for(score),
                    confidence=round(score, 6),
                    price=entry,
                    source="crypto_scanner",
                ),
            )
        )

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    best = [signal for _, signal in ranked[:CRYPTO_TOP_N]]

    log.info(
        "Scanned %d pair(s) — %d cleared score gate, trading top %d.",
        len(CRYPTO_UNIVERSE), len(ranked), len(best),
    )
    return best
