"""Scans a universe of futures contracts on 1-minute bars and ranks them to
find the *best* trade candidates right now.

The scanner is the futures counterpart to ``supabase_client.fetch_signals`` —
it returns the very same :class:`~supabase_client.Signal` objects, so its output
flows through the existing risk manager, Alpaca bracket-order path, and Discord
notifier with no changes to that pipeline.

Scoring (per symbol, over the last ``SCAN_LOOKBACK_BARS`` one-minute bars):

* **Momentum** — rate of change of close across the window. Its sign picks the
  trade direction (positive → long, negative → short).
* **Volume confirmation** — recent volume vs. the window average. Moves backed
  by rising volume score higher than thin drifts.

  ``score = |rate_of_change| × volume_factor``

The top ``FUTURES_TOP_N`` symbols whose score clears ``FUTURES_MIN_SCORE`` are
returned, best first. Each candidate carries a volatility-based stop (a multiple
of the average 1-minute range) and a 2:1 take-profit, which the risk manager
then uses for position sizing.

Alpaca's standard data feed is equities/crypto; many deployments therefore point
``FUTURES_UNIVERSE`` at the liquid ETF proxies that track the same underlyings
(ES→SPY, NQ→QQQ, RTY→IWM, GC→GLD, CL→USO, ...), which are tradable on Alpaca
paper today. Point it at native futures roots (MES, MNQ, ...) when running
against a feed/broker that serves them.
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


# Comma-separated list of symbols to scan. Defaults to liquid futures-tracking
# ETF proxies so the bot runs end-to-end on Alpaca paper out of the box; swap in
# native futures roots (e.g. "MES,MNQ,MYM,M2K,MGC,MCL") on a futures feed.
FUTURES_UNIVERSE = [
    s.strip().upper()
    for s in os.environ.get(
        "FUTURES_UNIVERSE", "SPY,QQQ,IWM,DIA,GLD,SLV,USO,UNG,TLT"
    ).split(",")
    if s.strip()
]

FUTURES_TOP_N = _env_int("FUTURES_TOP_N", 3)            # how many best to trade
SCAN_LOOKBACK_BARS = _env_int("SCAN_LOOKBACK_BARS", 20)  # 1-min bars per scan
FUTURES_MIN_SCORE = _env_float("FUTURES_MIN_SCORE", 0.0008)  # momentum gate
STOP_ATR_MULT = _env_float("STOP_ATR_MULT", 1.5)        # stop = mult × avg range
MIN_BARS_REQUIRED = max(5, SCAN_LOOKBACK_BARS // 2)     # need enough history


def _score_symbol(bars: list[Bar]) -> tuple[float, str, float, float, float]:
    """Return ``(score, direction, entry, stop_loss, take_profit)`` for ``bars``.

    ``score`` is 0.0 when there is too little data to judge the symbol.
    """
    if len(bars) < MIN_BARS_REQUIRED:
        return 0.0, "long", 0.0, 0.0, 0.0

    closes = [b.close for b in bars]
    first, entry = closes[0], closes[-1]
    if first <= 0 or entry <= 0:
        return 0.0, "long", 0.0, 0.0, 0.0

    # Momentum: rate of change across the window. Sign drives direction.
    roc = (entry - first) / first
    direction = "long" if roc >= 0 else "short"

    # Volume confirmation: recent third vs. the full-window average.
    volumes = [b.volume for b in bars]
    avg_vol = fmean(volumes) or 1.0
    recent_vol = fmean(volumes[-max(3, len(volumes) // 3):])
    volume_factor = min(recent_vol / avg_vol, 3.0)  # clip so one spike can't dominate

    score = abs(roc) * volume_factor

    # Volatility-based bracket: stop a multiple of the average 1-min range away.
    avg_range = fmean(b.high - b.low for b in bars)
    risk_per_unit = max(avg_range * STOP_ATR_MULT, entry * 0.0005)
    if direction == "long":
        stop_loss = round(entry - risk_per_unit, 2)
        take_profit = round(entry + 2 * risk_per_unit, 2)
    else:
        stop_loss = round(entry + risk_per_unit, 2)
        take_profit = round(entry - 2 * risk_per_unit, 2)

    return score, direction, round(entry, 2), stop_loss, take_profit


def _strength_for(score: float) -> str:
    if score >= FUTURES_MIN_SCORE * 4:
        return "STRONG"
    if score >= FUTURES_MIN_SCORE * 2:
        return "MODERATE"
    return "WEAK"


def scan(alpaca: AlpacaClient, exclude: set[str] | None = None) -> list[Signal]:
    """Scan the futures universe and return the best candidates as signals.

    Symbols in ``exclude`` (e.g. already-open positions) are skipped. The result
    is sorted best-first and capped at ``FUTURES_TOP_N``.
    """
    exclude = {s.upper() for s in (exclude or set())}
    ranked: list[tuple[float, Signal]] = []

    for symbol in FUTURES_UNIVERSE:
        if symbol in exclude:
            continue
        bars = alpaca.get_minute_bars(symbol, limit=SCAN_LOOKBACK_BARS)
        score, direction, entry, stop_loss, take_profit = _score_symbol(bars)
        if score < FUTURES_MIN_SCORE:
            continue
        ranked.append(
            (
                score,
                Signal(
                    ticker=symbol,
                    direction=direction,
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                    strength=_strength_for(score),
                    confidence=round(score, 6),
                    price=entry,
                    source="futures_scanner",
                ),
            )
        )

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    best = [signal for _, signal in ranked[:FUTURES_TOP_N]]

    log.info(
        "Scanned %d symbol(s) — %d cleared score gate, trading top %d.",
        len(FUTURES_UNIVERSE),
        len(ranked),
        len(best),
    )
    return best
