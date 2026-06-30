"""Position sizing, stop-loss validation, and the daily circuit breaker.

Every signal passes through here before an order can be placed. The manager
either returns a fully-specified :class:`TradePlan` or rejects the signal with a
human-readable reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

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


# Tunable via environment, with the plan's defaults.
MAX_RISK_PCT = _env_float("MAX_RISK_PCT", 0.01)            # 1% of equity per trade
DEFAULT_STOP_PCT = _env_float("DEFAULT_STOP_PCT", 0.05)    # 5% stop if DB has none
DAILY_DRAWDOWN_HALT_PCT = _env_float("DAILY_DRAWDOWN_HALT_PCT", 0.03)  # 3% halt
MIN_PRICE = _env_float("MIN_PRICE", 0.01)

# Portfolio-level limits (the "smart" guardrails beyond per-trade sizing).
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 5)           # diversification cap
MAX_GROSS_EXPOSURE_PCT = _env_float("MAX_GROSS_EXPOSURE_PCT", 0.50)  # ≤50% deployed
MAX_POSITION_PCT = _env_float("MAX_POSITION_PCT", 0.10)         # ≤10% equity/position
MIN_NOTIONAL = _env_float("MIN_NOTIONAL", 1.0)                   # Alpaca crypto min $1


@dataclass
class TradePlan:
    """A risk-approved order ready for Alpaca."""

    ticker: str
    side: str  # "buy" or "sell"
    qty: float  # whole shares for equities; fractional units for crypto
    entry: float
    take_profit: float
    stop_loss: float
    risk_dollars: float


@dataclass
class RiskDecision:
    """Outcome of evaluating a signal: either approved or skipped."""

    approved: bool
    plan: Optional[TradePlan] = None
    reason: Optional[str] = None


@dataclass
class Portfolio:
    """Live account exposure used for portfolio-level risk gates."""

    equity: float
    cash: float            # available buying power
    open_positions: int    # number of positions currently held
    gross_exposure: float  # summed dollar value of open positions


def portfolio_gate(portfolio: Portfolio, plan: TradePlan) -> Optional[str]:
    """Return a rejection reason if ``plan`` would breach a portfolio limit.

    Layered on top of per-trade sizing so no single approval can over-deploy the
    account: caps the number of concurrent positions, total gross exposure, and
    refuses orders that exceed buying power or fall below the venue minimum.
    """
    notional = plan.qty * plan.entry

    if plan.qty <= 0:
        return "Non-positive quantity"
    if notional < MIN_NOTIONAL:
        return f"Order notional ${notional:.2f} below ${MIN_NOTIONAL:.2f} minimum"
    if portfolio.open_positions >= MAX_OPEN_POSITIONS:
        return f"Max open positions ({MAX_OPEN_POSITIONS}) already held"
    if notional > portfolio.cash:
        return (
            f"Insufficient buying power: need ${notional:.2f}, "
            f"have ${portfolio.cash:.2f}"
        )
    exposure_cap = portfolio.equity * MAX_GROSS_EXPOSURE_PCT
    if portfolio.gross_exposure + notional > exposure_cap:
        return (
            f"Gross-exposure cap {MAX_GROSS_EXPOSURE_PCT:.0%} "
            f"(${exposure_cap:,.0f}) would be exceeded"
        )
    return None


def _default_stop(entry: float, is_long: bool) -> float:
    """Derive a stop-loss when the signal did not supply one."""
    if is_long:
        return round(entry * (1 - DEFAULT_STOP_PCT), 2)
    return round(entry * (1 + DEFAULT_STOP_PCT), 2)


def _default_take_profit(entry: float, stop: float, is_long: bool) -> float:
    """Derive a 2:1 reward-to-risk take-profit when none was supplied."""
    risk_per_share = abs(entry - stop)
    if is_long:
        return round(entry + 2 * risk_per_share, 2)
    return round(entry - 2 * risk_per_share, 2)


def evaluate(
    signal: Signal,
    entry_price: float,
    equity: float,
    *,
    fractional: bool = False,
    price_precision: int = 2,
) -> RiskDecision:
    """Build a :class:`TradePlan` for ``signal`` or reject it with a reason.

    ``fractional`` allows sub-unit quantities (crypto), where a single unit can
    cost far more than the per-trade risk budget. ``price_precision`` controls
    rounding of derived prices (2 decimals for equities, more for crypto pairs).
    """

    if entry_price < MIN_PRICE:
        return RiskDecision(
            approved=False,
            reason=f"Price ${entry_price:.4f} below minimum ${MIN_PRICE:.2f}",
        )

    is_long = signal.is_long
    side = "buy" if is_long else "sell"

    # Resolve stop loss — use the DB value if present and sane, else default.
    stop_loss = signal.stop_loss
    if stop_loss is None or stop_loss <= 0:
        stop_loss = _default_stop(entry_price, is_long)

    # Stop must sit on the correct side of entry, otherwise risk is undefined.
    if is_long and stop_loss >= entry_price:
        return RiskDecision(
            approved=False,
            reason=f"Long stop ${stop_loss:.2f} not below entry ${entry_price:.2f}",
        )
    if not is_long and stop_loss <= entry_price:
        return RiskDecision(
            approved=False,
            reason=f"Short stop ${stop_loss:.2f} not above entry ${entry_price:.2f}",
        )

    # Resolve take profit — DB value or a derived 2:1 target.
    take_profit = signal.take_profit
    if take_profit is None or take_profit <= 0:
        take_profit = _default_take_profit(entry_price, stop_loss, is_long)

    # Position size:  units = (equity × risk%) ÷ per-unit risk.
    risk_budget = equity * MAX_RISK_PCT
    per_share_risk = abs(entry_price - stop_loss)
    if per_share_risk <= 0:
        return RiskDecision(approved=False, reason="Zero per-share risk")

    # Cap each position's notional so a tight stop can't size up to the whole
    # account (notional = risk$ ÷ stop%, which explodes as the stop tightens).
    notional_cap_qty = (equity * MAX_POSITION_PCT) / entry_price
    raw_qty = min(risk_budget / per_share_risk, notional_cap_qty)
    qty = round(raw_qty, 6) if fractional else float(int(raw_qty))
    if qty <= 0:
        return RiskDecision(
            approved=False,
            reason=(
                f"Risk budget ${risk_budget:.2f} too small for "
                f"${per_share_risk:.2f}/unit risk"
            ),
        )

    plan = TradePlan(
        ticker=signal.ticker,
        side=side,
        qty=qty,
        entry=round(entry_price, price_precision),
        take_profit=round(take_profit, price_precision),
        stop_loss=round(stop_loss, price_precision),
        risk_dollars=round(qty * per_share_risk, 2),
    )
    return RiskDecision(approved=True, plan=plan)


def circuit_breaker_tripped(starting_equity: float, current_equity: float) -> bool:
    """True when the account is down more than the daily halt threshold."""
    if starting_equity <= 0:
        return False
    drawdown = (starting_equity - current_equity) / starting_equity
    return drawdown >= DAILY_DRAWDOWN_HALT_PCT
