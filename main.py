"""Orchestrator — the single script Railway runs on the cron schedule.

Runs once, executes today's signals through risk management and Alpaca, posts
Discord updates, then exits cleanly with code 0.

    python main.py            # live paper trading
    python main.py --dry-run  # read + size signals only; no orders, no Discord
"""

from __future__ import annotations

import argparse
import sys

# Load .env before any project module reads os.environ — risk_manager resolves
# its thresholds at import time, so this must run first.
from dotenv import load_dotenv

load_dotenv()

import risk_manager  # noqa: E402 — must import after load_dotenv()
from alpaca_client import AlpacaClient  # noqa: E402
from discord_notifier import DiscordNotifier  # noqa: E402
from logger import log  # noqa: E402
from supabase_client import fetch_signals  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Railway Auto-Trader")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and size signals but submit no orders and send no Discord messages.",
    )
    parser.add_argument(
        "--force-market-open",
        action="store_true",
        help="Bypass the market-hours gate (for after-hours end-to-end testing). "
        "Orders submitted while closed are queued by Alpaca for the next open.",
    )
    return parser.parse_args()


def run(dry_run: bool, force_market_open: bool = False) -> int:
    notifier = DiscordNotifier(enabled=not dry_run)

    # 1. Connect to Alpaca and confirm the market is open today.
    alpaca = AlpacaClient()
    if dry_run:
        log.info("[DRY RUN] Skipping market-open gate.")
    elif force_market_open:
        log.warning("[FORCE] Bypassing market-open gate — orders may queue until open.")
    elif not alpaca.is_market_open_today():
        log.info("Market is closed today — exiting silently.")
        return 0

    # 2. Fetch current account equity (drives position sizing + circuit breaker).
    starting_equity = alpaca.get_equity()
    log.info("Account equity: $%.2f", starting_equity)

    # 3. Poll the live signals API.
    signals = fetch_signals()
    if not signals:
        log.info("No signals returned — nothing to trade.")
        notifier.session_start(0, [])
        notifier.session_end(0, 0.0, starting_equity)
        return 0

    # 4. Session start notification.
    notifier.session_start(len(signals), [s.ticker for s in signals])

    trades_placed = 0
    halted = False

    # 5. Process each signal.
    for signal in signals:
        if halted:
            notifier.trade_skipped(signal.ticker, "Trading halted (circuit breaker).")
            continue

        entry_price = alpaca.get_latest_price(signal.ticker)
        if entry_price is None:
            log.warning("Skipping %s — no price (not tradable on Alpaca?).", signal.ticker)
            notifier.trade_skipped(signal.ticker, "No price available on Alpaca.")
            continue

        decision = risk_manager.evaluate(signal, entry_price, starting_equity)
        if not decision.approved:
            log.info("Skipping %s — %s", signal.ticker, decision.reason)
            notifier.trade_skipped(signal.ticker, decision.reason or "Risk rejected.")
            continue

        plan = decision.plan
        if dry_run:
            log.info(
                "[DRY RUN] %s %s x%d @ $%.2f  TP=$%.2f  SL=$%.2f  risk=$%.2f",
                plan.side.upper(), plan.ticker, plan.qty, plan.entry,
                plan.take_profit, plan.stop_loss, plan.risk_dollars,
            )
            trades_placed += 1
            continue

        try:
            order = alpaca.submit_bracket_order(plan)
            trades_placed += 1
            log.info("Placed %s order %s (status=%s)", plan.ticker, order.id, order.status)
            notifier.trade_placed(plan, order.id)
        except Exception as exc:  # noqa: BLE001 — one bad order shouldn't kill the run
            log.error("Order failed for %s: %s", plan.ticker, exc)
            notifier.trade_skipped(plan.ticker, f"Order error: {exc}")
            continue

        # 5b. Circuit breaker check after each placed order.
        current_equity = alpaca.get_equity()
        if risk_manager.circuit_breaker_tripped(starting_equity, current_equity):
            drawdown = (starting_equity - current_equity) / starting_equity
            log.warning("Circuit breaker tripped — drawdown %.2f%%", drawdown * 100)
            notifier.circuit_breaker(drawdown)
            halted = True

    # 6. Session end summary.
    final_equity = starting_equity if dry_run else alpaca.get_equity()
    net_pnl = final_equity - starting_equity
    log.info(
        "Done. trades=%d  net_pnl=$%.2f  final_equity=$%.2f",
        trades_placed, net_pnl, final_equity,
    )
    if not dry_run:
        notifier.session_end(trades_placed, net_pnl, final_equity)

    return 0


def main() -> None:
    args = parse_args()
    try:
        exit_code = run(dry_run=args.dry_run, force_market_open=args.force_market_open)
    except Exception as exc:  # noqa: BLE001 — log and surface a non-zero exit
        log.exception("Fatal error: %s", exc)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
