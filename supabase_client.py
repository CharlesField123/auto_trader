"""Fetches live trading signals from the StockAI signals API.

The signals are produced by a Supabase Edge Function and served as a single
JSON payload from:

    {SUPABASE_URL}/functions/v1/make-server-b834e05f/signals

The response has three arrays — ``buySignals``, ``sellSignals`` and
``activeSignals`` — each a list of analyzed tickers. We poll this endpoint live
(rather than reading the ``signals_cache_v2`` KV row, which can lag behind) so
each run trades on the freshest data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

from logger import log

# Override-able for other deployments; defaults to the known StockAI function.
SIGNALS_API_PATH = os.environ.get(
    "SIGNALS_API_PATH", "/functions/v1/make-server-b834e05f/signals"
)

# Which arrays to trade. Buy + Sell are fully analyzed (TP/SL/confidence);
# Active are weaker (often no TP/SL) and get the risk manager's default stops.
SIGNAL_ARRAYS = ("buySignals", "sellSignals", "activeSignals")


@dataclass
class Signal:
    """One analyzed ticker, normalized for the pipeline."""

    ticker: str
    direction: str  # "long" or "short"
    take_profit: Optional[float]
    stop_loss: Optional[float]
    strength: Optional[str]   # "STRONG" / "MODERATE" / etc.
    confidence: Optional[float]
    price: Optional[float]    # snapshot price from the signal source
    source: str               # which array it came from

    @property
    def is_long(self) -> bool:
        return self.direction.lower() in ("long", "buy", "bull", "bullish")


def _normalize_direction(raw: Optional[str]) -> str:
    """Map the API's BUY/SELL into a canonical 'long' / 'short'."""
    value = (raw or "buy").strip().lower()
    if value in ("short", "sell", "bear", "bearish"):
        return "short"
    return "long"


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _api_url() -> str:
    base = os.environ.get("SUPABASE_URL")
    if not base:
        raise RuntimeError("SUPABASE_URL must be set in the environment.")
    return base.rstrip("/") + SIGNALS_API_PATH


def _auth_headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_KEY must be set in the environment.")
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _parse_signal(row: dict, source: str) -> Optional[Signal]:
    ticker = (row.get("symbol") or row.get("name") or "").strip().upper()
    if not ticker:
        log.warning("Skipping signal with no symbol: %s", row)
        return None
    return Signal(
        ticker=ticker,
        direction=_normalize_direction(row.get("signal") or row.get("recommendation")),
        take_profit=_to_float(row.get("takeProfit")),
        stop_loss=_to_float(row.get("stopLoss")),
        strength=row.get("strength"),
        confidence=_to_float(row.get("confidence")),
        price=_to_float(row.get("price")),
        source=source,
    )


def fetch_signals() -> list[Signal]:
    """Poll the live signals endpoint and return all actionable signals.

    De-duplicates by ticker (keeping the first occurrence across buy → sell →
    active), since the same symbol can appear in more than one array.
    """
    url = _api_url()
    log.info("Polling signals API: %s", url)

    response = requests.get(url, headers=_auth_headers(), timeout=20)
    response.raise_for_status()
    payload = response.json()

    signals: list[Signal] = []
    seen: set[str] = set()
    for array_name in SIGNAL_ARRAYS:
        for row in payload.get(array_name, []) or []:
            signal = _parse_signal(row, source=array_name)
            if signal is None or signal.ticker in seen:
                continue
            seen.add(signal.ticker)
            signals.append(signal)

    counts = {name: len(payload.get(name, []) or []) for name in SIGNAL_ARRAYS}
    log.info(
        "Fetched %d unique signal(s) — buy=%d sell=%d active=%d",
        len(signals),
        counts.get("buySignals", 0),
        counts.get("sellSignals", 0),
        counts.get("activeSignals", 0),
    )
    return signals
