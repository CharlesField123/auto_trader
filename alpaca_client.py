"""Thin wrapper around the official ``alpaca-py`` SDK, pointed at Paper Trading.

Responsibilities:
- Authenticate with the paper account.
- Confirm the equity market is open today (for the daily stock bot).
- Report account state (equity, cash, open positions) for the risk manager.
- Submit equity bracket orders, and crypto buy/close orders (crypto has no
  bracket/OCO support on Alpaca, so exits are managed by the scan loop).
- Fetch 1-minute bars for the scanner.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz
from alpaca.data.historical import (
    CryptoHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from logger import log
from risk_manager import TradePlan

ET = pytz.timezone("America/New_York")


def normalize_symbol(symbol: str) -> str:
    """Canonical form for matching ('BTC/USD' and 'BTCUSD' → 'BTCUSD')."""
    return symbol.replace("/", "").upper()


@dataclass
class PlacedOrder:
    """Lightweight view of a submitted Alpaca order."""

    id: str
    ticker: str
    side: str
    qty: float
    status: str


@dataclass
class Bar:
    """A single OHLCV candle, normalized for the scanner."""

    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    """Normalized open position for the risk manager and exit logic."""

    symbol: str            # broker symbol, e.g. "BTCUSD"
    qty: float
    market_value: float    # absolute dollar value of the position
    unrealized_plpc: float  # unrealized P&L as a fraction, e.g. 0.025 = +2.5%
    asset_class: str       # "crypto", "us_equity", … — what kind of position

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == "crypto"


@dataclass
class AccountState:
    """Snapshot of buying power and exposure for portfolio-level risk checks."""

    equity: float
    cash: float            # available (non-marginable) buying power
    open_positions: int
    gross_exposure: float  # sum of |market_value| across open positions


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
        self.crypto_data = CryptoHistoricalDataClient(api_key, secret_key)

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

    def get_positions(self) -> list[Position]:
        """Normalized open positions (symbol, qty, value, P&L%, asset class)."""
        try:
            return [
                Position(
                    symbol=normalize_symbol(str(p.symbol)),
                    qty=float(p.qty),
                    market_value=abs(float(p.market_value)),
                    unrealized_plpc=float(p.unrealized_plpc),
                    asset_class=str(
                        getattr(p.asset_class, "value", p.asset_class)
                    ).lower(),
                )
                for p in self.trading.get_all_positions()
            ]
        except Exception as exc:  # noqa: BLE001 — treat as "unknown, hold nothing"
            log.warning("Could not list open positions: %s", exc)
            return []

    def get_crypto_positions(self) -> list[Position]:
        """Open crypto positions only — the sleeve this bot manages."""
        return [p for p in self.get_positions() if p.is_crypto]

    def get_account_state(self) -> AccountState:
        """Equity, available cash, and crypto exposure for portfolio risk gates.

        Position count and gross exposure are scoped to the bot's own crypto
        sleeve so unrelated equity holdings in the account don't consume its
        risk budget or trip its caps.
        """
        account = self.trading.get_account()
        crypto = self.get_crypto_positions()
        # Crypto is non-marginable, so cash (not margin buying power) is the cap.
        cash = float(
            getattr(account, "non_marginable_buying_power", None) or account.cash
        )
        return AccountState(
            equity=float(account.equity),
            cash=cash,
            open_positions=len(crypto),
            gross_exposure=sum(p.market_value for p in crypto),
        )

    # ------------------------------------------------------------------ pricing
    def get_latest_price(self, ticker: str) -> float | None:
        """Latest stock trade price for ``ticker``; None if unavailable."""
        try:
            request = StockLatestTradeRequest(symbol_or_symbols=ticker)
            latest = self.data.get_stock_latest_trade(request)
            return float(latest[ticker].price)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            log.warning("Could not fetch price for %s: %s", ticker, exc)
            return None

    def get_crypto_latest_price(self, symbol: str) -> float | None:
        """Latest crypto trade price for ``symbol`` (e.g. 'BTC/USD')."""
        try:
            request = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            latest = self.crypto_data.get_crypto_latest_trade(request)
            return float(latest[symbol].price)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            log.warning("Could not fetch crypto price for %s: %s", symbol, exc)
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

    def get_crypto_minute_bars(self, symbol: str, limit: int = 20) -> list[Bar]:
        """Most recent ``limit`` one-minute OHLCV bars for a crypto ``symbol``.

        Same contract as :meth:`get_minute_bars` but on the 24/7 crypto feed —
        returns an empty list on any error so the scanner skips the symbol.
        """
        try:
            end = datetime.now(ET)
            start = end - timedelta(minutes=limit * 3 + 15)
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
            )
            barset = self.crypto_data.get_crypto_bars(request)
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
            log.warning("Could not fetch crypto bars for %s: %s", symbol, exc)
            return []

    def get_open_symbols(self) -> set[str]:
        """Normalized symbols the account currently holds a position in.

        Used by the scanning loop to avoid stacking a fresh entry on a symbol
        we are already in.
        """
        return {p.symbol for p in self.get_positions()}

    # ------------------------------------------------------------------ orders
    def submit_crypto_buy(self, plan: TradePlan) -> PlacedOrder:
        """Submit a crypto market buy.

        Alpaca crypto supports neither bracket/OCO nor short selling, so this is
        a plain long entry; the take-profit and stop-loss are enforced by the
        scan loop closing the position when P&L crosses the configured levels.
        """
        order_request = MarketOrderRequest(
            symbol=plan.ticker,
            qty=plan.qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        order = self.trading.submit_order(order_request)
        return PlacedOrder(
            id=str(order.id),
            ticker=plan.ticker,
            side="buy",
            qty=plan.qty,
            status=str(order.status),
        )

    def cancel_open_orders_for(self, symbol: str) -> None:
        """Cancel any open orders on ``symbol`` (frees qty held_for_orders)."""
        target = normalize_symbol(symbol)
        try:
            orders = self.trading.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.warning("Could not list open orders for %s: %s", symbol, exc)
            return
        for order in orders:
            if normalize_symbol(str(order.symbol)) == target:
                try:
                    self.trading.cancel_order_by_id(order.id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Cancel failed for order %s: %s", order.id, exc)

    def close_position(self, symbol: str) -> None:
        """Liquidate the full position in ``symbol`` with a market order.

        If the position's quantity is locked by resting orders (Alpaca returns
        an "insufficient qty available / held_for_orders" error), cancel those
        orders and retry once so the close can go through.
        """
        try:
            self.trading.close_position(symbol)
        except Exception as exc:  # noqa: BLE001 — recover from held-for-orders
            log.info(
                "Close of %s blocked (%s) — cancelling open orders and retrying.",
                symbol, exc,
            )
            self.cancel_open_orders_for(symbol)
            self.trading.close_position(symbol)

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
