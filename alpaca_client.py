"""Thin wrapper around the official ``alpaca-py`` SDK, pointed at Paper Trading.

Responsibilities:
- Authenticate with the paper account.
- Confirm the market is open today (handles holidays + DST automatically).
- Report current account equity for the risk manager.
- Submit atomic bracket orders (entry + take-profit + stop-loss in one call).
- Report open positions for the end-of-day summary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from logger import log
from risk_manager import TradePlan

ET = pytz.timezone("America/New_York")


@dataclass
class PlacedOrder:
    """Lightweight view of a submitted Alpaca order."""

    id: str
    ticker: str
    side: str
    qty: int
    status: str


@dataclass
class Bar:
    """A single OHLCV candle, normalized for the scanner."""

    open: float
    high: float
    low: float
    close: float
    volume: float


class AlpacaClient:
    def __init__(self) -> None:
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment."
            )

        # paper=True targets the paper-trading base URL.
        self.trading = TradingClient(api_key, secret_key, paper=True)
        self.data = StockHistoricalDataClient(api_key, secret_key)

    # ------------------------------------------------------------------ market
    def is_market_open_today(self) -> bool:
        """True when Alpaca's calendar lists today as a trading session.

        Using the calendar (rather than a hard-coded UTC time) means market
        holidays and EST/EDT drift are handled by Alpaca, not by us.
        """
        today = datetime.now(ET).date()
        calendar = self.trading.get_calendar(
            GetCalendarRequest(start=today, end=today)
        )
        if not calendar:
            log.info("Market closed today (%s) — no calendar session.", today)
            return False

        # Also confirm the clock reports the market as currently open, so a
        # pre-open or post-close cron firing exits cleanly.
        clock = self.trading.get_clock()
        if not clock.is_open:
            log.info(
                "Market not currently open (next open: %s).", clock.next_open
            )
        return bool(clock.is_open)

    # ------------------------------------------------------------------ account
    def get_equity(self) -> float:
        account = self.trading.get_account()
        return float(account.equity)

    # ------------------------------------------------------------------ pricing
    def get_latest_price(self, ticker: str) -> float | None:
        """Latest trade price for ``ticker``; None if unavailable/untradable."""
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=ticker)
            latest = self.data.get_stock_latest_trade(request)
            return float(latest[ticker].price)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            log.warning("Could not fetch price for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------ scanning
    def get_minute_bars(self, symbol: str, limit: int = 20) -> list[Bar]:
        """Most recent ``limit`` one-minute OHLCV bars for ``symbol``.

        Returns an empty list (rather than raising) when the symbol has no data
        or is not available on the configured feed, so the scanner can simply
        skip it and keep moving across the rest of the universe.
        """
        try:
            # Pull a generous window so we still get ``limit`` bars even with
            # gaps (low-volume minutes, halts). Newest bars are kept below.
            end = datetime.now(ET)
            start = end - timedelta(minutes=limit * 3 + 15)
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
            )
            barset = self.data.get_stock_bars(request)
            raw = barset.data.get(symbol, []) if hasattr(barset, "data") else []
            bars = [
                Bar(
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume),
                )
                for b in raw
            ]
            return bars[-limit:]
        except Exception as exc:  # noqa: BLE001 — skip this symbol, not the scan
            log.warning("Could not fetch bars for %s: %s", symbol, exc)
            return []

    def get_open_symbols(self) -> set[str]:
        """Set of symbols the account currently holds an open position in.

        Used by the scanning loop to avoid stacking a fresh entry on a symbol
        we are already in.
        """
        try:
            return {str(p.symbol).upper() for p in self.trading.get_all_positions()}
        except Exception as exc:  # noqa: BLE001 — treat as "unknown, hold nothing"
            log.warning("Could not list open positions: %s", exc)
            return set()

    # ------------------------------------------------------------------ orders
    def submit_bracket_order(self, plan: TradePlan) -> PlacedOrder:
        """Submit an atomic bracket order (entry + TP + SL)."""
        order_request = LimitOrderRequest(
            symbol=plan.ticker,
            qty=plan.qty,
            side=OrderSide.BUY if plan.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            limit_price=plan.entry,
            take_profit=TakeProfitRequest(limit_price=plan.take_profit),
            stop_loss=StopLossRequest(stop_price=plan.stop_loss),
        )
        order = self.trading.submit_order(order_request)
        return PlacedOrder(
            id=str(order.id),
            ticker=plan.ticker,
            side=plan.side,
            qty=plan.qty,
            status=str(order.status),
        )

    # ------------------------------------------------------------------ summary
    def get_open_positions(self) -> list:
        return self.trading.get_all_positions()
