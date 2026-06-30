"""Orchestrator — the entry point Railway runs.

Two modes, both sharing one risk → execution → Discord pipeline:

    python main.py                  # daily one-shot: trade today's StockAI signals
    python main.py --futures        # continuous: scan futures every 60s, trade best
    python main.py --dry-run        # size signals only; no orders, no Discord
    python main.py --futures --dry-run --max-cycles 1   # one offline scan cycle

Daily mode runs once and exits 0 (the original cron behavior). Futures mode
stays alive and rescans on a fixed interval until the market closes, the circuit
breaker trips, or the process is interrupted.
"""

from __future__ import annotations

import argparse
import sys
import time

# Load .env before any project module reads os.environ — risk_manager resolves
# its thresholds at import time, so this must run first.
from dotenv import load_dotenv

load_dotenv()

import futures_scanner  # noqa: E402 — must import after load_dotenv()
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
    parser.add_argument(
        "--futures",
        action="store_true",
        help="Run the continuous futures scanner instead of the daily StockAI run.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between futures scan cycles (default: 60 = 1-minute bars).",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Stop the futures scanner after N cycles (0 = run until market close).",
    )
    return parser.parse_args()


def _process_signal(
    signal,
    alpaca: AlpacaClient,
    notifier: DiscordNotifier,
    *,
    dry_run: bool,
    starting_equity: float,
    halted: bool,
) -> tuple[bool, bool]:
    """Run one signal through risk → execution → notifications.

    Shared by daily and futures modes so both emit the identical Discord flow.
    Returns ``(placed, halted)`` where ``placed`` is True when an order was
    submitted (or would be, in a dry run) and ``halted`` reflects the circuit
    breaker after this signal.
    """
    if halted:
        notifier.trade_skipped(signal.ticker, "Trading halted (circuit breaker).")
        return False, True

    entry_price = alpaca.get_latest_price(signal.ticker)
    if entry_price is None:
        log.warning("Skipping %s — no price (not tradable on Alpaca?).", signal.ticker)
        notifier.trade_skipped(signal.ticker, "No price available on Alpaca.")
        return False, halted

    decision = risk_manager.evaluate(signal, entry_price, starting_equity)
    if not decision.approved:
        log.info("Skipping %s — %s", signal.ticker, decision.reason)
        notifier.trade_skipped(signal.ticker, decision.reason or "Risk rejected.")
        return False, halted

    plan = decision.plan
    if dry_run:
        log.info(
            "[DRY RUN] %s %s x%d @ $%.2f  TP=$%.2f  SL=$%.2f  risk=$%.2f",
            plan.side.upper(), plan.ticker, plan.qty, plan.entry,
            plan.take_profit, plan.stop_loss, plan.risk_dollars,
        )
        return True, halted

    try:
        order = alpaca.submit_bracket_order(plan)
        log.info("Placed %s order %s (status=%s)", plan.ticker, order.id, order.status)
        notifier.trade_placed(plan, order.id)
    except Exception as exc:  # noqa: BLE001 — one bad order shouldn't kill the run
        log.error("Order failed for %s: %s", plan.ticker, exc)
        notifier.trade_skipped(plan.ticker, f"Order error: {exc}")
        return False, halted

    # Circuit breaker check after each placed order.
    current_equity = alpaca.get_equity()
    if risk_manager.circuit_breaker_tripped(starting_equity, current_equity):
        drawdown = (starting_equity - current_equity) / starting_equity
        log.warning("Circuit breaker tripped — drawdown %.2f%%", drawdown * 100)
        notifier.circuit_breaker(drawdown)
        halted = True
    return True, halted


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
        placed, halted = _process_signal(
            signal, alpaca, notifier,
            dry_run=dry_run, starting_equity=starting_equity, halted=halted,
        )
        if placed:
            trades_placed += 1

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


def run_scan_loop(
    dry_run: bool,
    interval: int,
    force_market_open: bool = False,
    max_cycles: int = 0,
) -> int:
    """Continuously scan futures every ``interval`` seconds and trade the best.

    Emits the same Discord flow as the daily run: one 🚀 session-start when the
    loop begins, a ✅/⚠️ per signal each cycle, 🔴 if the circuit breaker trips,
    and a 🏁 session-end summary on shutdown. Exits when the market closes, the
    breaker halts trading, ``max_cycles`` is reached, or it is interrupted.
    """
    notifier = DiscordNotifier(enabled=not dry_run)
    alpaca = AlpacaClient()

    # Gate on market hours up front, mirroring the daily run.
    if dry_run:
        log.info("[DRY RUN] Skipping market-open gate.")
    elif force_market_open:
        log.warning("[FORCE] Bypassing market-open gate — orders may queue until open.")
    elif not alpaca.is_market_open_today():
        log.info("Market is closed today — futures scanner exiting silently.")
        return 0

    # Session baseline for the daily circuit breaker (down X% on the day).
    starting_equity = alpaca.get_equity()
    log.info(
        "Futures scanner live — equity $%.2f, interval %ds, universe %s",
        starting_equity, interval, ", ".join(futures_scanner.FUTURES_UNIVERSE),
    )

    total_trades = 0
    halted = False
    started = False
    cycle = 0

    try:
        while True:
            cycle += 1

            # Stop cleanly when the market closes mid-session.
            if not dry_run and not force_market_open and not alpaca.is_market_open_today():
                log.info("Market closed — ending futures scan session.")
                break

            # Skip symbols we already hold so we don't stack entries each minute.
            held = alpaca.get_open_symbols()
            signals = futures_scanner.scan(alpaca, exclude=held)

            # One session-start, on the first cycle, like the original run.
            if not started:
                notifier.session_start(len(signals), [s.ticker for s in signals])
                started = True

            for signal in signals:
                placed, halted = _process_signal(
                    signal, alpaca, notifier,
                    dry_run=dry_run, starting_equity=starting_equity, halted=halted,
                )
                if placed:
                    total_trades += 1

            if halted:
                log.warning("Circuit breaker active — halting scan loop.")
                break
            if max_cycles and cycle >= max_cycles:
                log.info("Reached max cycles (%d) — stopping.", max_cycles)
                break

            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down futures scanner.")

    # Safety net: announce a start even if the loop broke before its first cycle.
    if not started:
        notifier.session_start(0, [])

    final_equity = starting_equity if dry_run else alpaca.get_equity()
    net_pnl = final_equity - starting_equity
    log.info(
        "Scanner done. cycles=%d  trades=%d  net_pnl=$%.2f  final_equity=$%.2f",
        cycle, total_trades, net_pnl, final_equity,
    )
    notifier.session_end(total_trades, net_pnl, final_equity)
    return 0


def main() -> None:
    args = parse_args()
    try:
        if args.futures:
            exit_code = run_scan_loop(
                dry_run=args.dry_run,
                interval=args.interval,
                force_market_open=args.force_market_open,
                max_cycles=args.max_cycles,
            )
        else:
            exit_code = run(
                dry_run=args.dry_run, force_market_open=args.force_market_open
            )
    except Exception as exc:  # noqa: BLE001 — log and surface a non-zero exit
        log.exception("Fatal error: %s", exc)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
