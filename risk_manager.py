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


# Tunable via environment, with the plan's defaults.
MAX_RISK_PCT = _env_float("MAX_RISK_PCT", 0.01)            # 1% of equity per trade
DEFAULT_STOP_PCT = _env_float("DEFAULT_STOP_PCT", 0.05)    # 5% stop if DB has none
DAILY_DRAWDOWN_HALT_PCT = _env_float("DAILY_DRAWDOWN_HALT_PCT", 0.03)  # 3% halt
MIN_PRICE = _env_float("MIN_PRICE", 0.01)


@dataclass
class TradePlan:
    """A risk-approved order ready for Alpaca."""

    ticker: str
    side: str  # "buy" or "sell"
    qty: int
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


def evaluate(signal: Signal, entry_price: float, equity: float) -> RiskDecision:
    """Build a :class:`TradePlan` for ``signal`` or reject it with a reason."""

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

    # Position size:  shares = (equity × risk%) ÷ per-share risk.
    risk_budget = equity * MAX_RISK_PCT
    per_share_risk = abs(entry_price - stop_loss)
    if per_share_risk <= 0:
        return RiskDecision(approved=False, reason="Zero per-share risk")

    qty = int(risk_budget // per_share_risk)
    if qty < 1:
        return RiskDecision(
            approved=False,
            reason=(
                f"Risk budget ${risk_budget:.2f} too small for "
                f"${per_share_risk:.2f}/share risk"
            ),
        )

    plan = TradePlan(
        ticker=signal.ticker,
        side=side,
        qty=qty,
        entry=round(entry_price, 2),
        take_profit=take_profit,
        stop_loss=round(stop_loss, 2),
        risk_dollars=round(qty * per_share_risk, 2),
    )
    return RiskDecision(approved=True, plan=plan)


def circuit_breaker_tripped(starting_equity: float, current_equity: float) -> bool:
    """True when the account is down more than the daily halt threshold."""
    if starting_equity <= 0:
        return False
    drawdown = (starting_equity - current_equity) / starting_equity
    return drawdown >= DAILY_DRAWDOWN_HALT_PCT
