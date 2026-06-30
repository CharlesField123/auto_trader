"""Orchestrator — the entry point Railway runs.

Two modes, both sharing one risk → execution → Discord pipeline:

    python main.py                  # daily one-shot: trade today's StockAI signals
    python main.py --futures        # 24/5: scan futures every 60s, trade best
    python main.py --dry-run        # size signals only; no orders, no Discord
    python main.py --futures --dry-run --max-cycles 1   # one offline scan cycle

Daily mode runs once and exits 0 (the original cron behavior). Futures mode
stays alive and rescans on a fixed interval around the clock, Monday–Friday —
rolling a fresh session (and circuit-breaker baseline) each trading day and
idling over the weekend, until the process is interrupted.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

# Load .env before any project module reads os.environ — risk_manager resolves
# its thresholds at import time, so this must run first.
from dotenv import load_dotenv

load_dotenv()

import futures_scanner  # noqa: E402 — must import after load_dotenv()
import risk_manager  # noqa: E402 — must import after load_dotenv()
from alpaca_client import ET, AlpacaClient  # noqa: E402
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
        help="Stop the futures scanner after N cycles (0 = run continuously, 24/5).",
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


def _is_trading_day(now: datetime) -> bool:
    """True Monday–Friday in US/Eastern — the 24/5 futures window.

    ``weekday()`` is 0=Mon … 6=Sun, so <5 keeps Mon–Fri and idles the weekend.
    Futures trade nearly around the clock on weekdays, so we gate on the day,
    not on equity-market hours.
    """
    return now.weekday() < 5


WEEKEND_POLL_SECONDS = 300  # how often to re-check for the week to reopen


def run_scan_loop(
    dry_run: bool,
    interval: int,
    force_market_open: bool = False,
    max_cycles: int = 0,
) -> int:
    """Scan futures 24/5 — every ``interval`` seconds, all day Monday–Friday.

    Stays alive continuously: it rolls a fresh trading session at each ET day
    boundary and idles over the weekend, then resumes. Each session emits the
    same Discord flow as the original daily run — one 🚀 session-start, a ✅/⚠️
    per signal, 🔴 if that day's circuit breaker trips, and a 🏁 session-end
    summary when the day rolls over (or the weekend / shutdown arrives).

    The circuit breaker is per-day: its equity baseline resets each session, and
    tripping it pauses new entries only until the next trading day.
    """
    notifier = DiscordNotifier(enabled=not dry_run)
    alpaca = AlpacaClient()

    log.info(
        "Futures scanner live (24/5) — interval %ds, universe %s",
        interval, ", ".join(futures_scanner.FUTURES_UNIVERSE),
    )

    # Per-session state. ``session_date`` is None whenever no session is open.
    session_date = None
    day_baseline = 0.0       # equity at session start (daily circuit breaker)
    day_trades = 0
    halted_today = False
    cycle = 0

    def end_session() -> None:
        nonlocal session_date, day_trades
        if session_date is None:
            return
        final_equity = day_baseline if dry_run else alpaca.get_equity()
        net_pnl = final_equity - day_baseline
        log.info(
            "Session %s complete — trades=%d  net_pnl=$%.2f  equity=$%.2f",
            session_date, day_trades, net_pnl, final_equity,
        )
        notifier.session_end(day_trades, net_pnl, final_equity)
        session_date = None

    try:
        while True:
            cycle += 1
            now = datetime.now(ET)
            trading = dry_run or force_market_open or _is_trading_day(now)

            # Weekend (or outside window): close any open session and idle.
            if not trading:
                end_session()
                if max_cycles and cycle >= max_cycles:
                    break
                log.info("Outside 24/5 window (%s) — idling.", now.strftime("%a %H:%M ET"))
                time.sleep(WEEKEND_POLL_SECONDS)
                continue

            # Roll the session at each ET day boundary: close yesterday's,
            # reset the daily circuit-breaker baseline, open today's.
            today = now.date()
            first_scan = session_date != today
            if first_scan:
                end_session()
                session_date = today
                day_baseline = alpaca.get_equity()
                day_trades = 0
                halted_today = False
                log.info("New trading session %s — baseline equity $%.2f", today, day_baseline)

            # Breaker tripped earlier today: hold new entries until tomorrow.
            if halted_today:
                if max_cycles and cycle >= max_cycles:
                    break
                time.sleep(interval)
                continue

            # Skip symbols we already hold so we don't stack entries each minute.
            held = alpaca.get_open_symbols()
            signals = futures_scanner.scan(alpaca, exclude=held)

            if first_scan:
                notifier.session_start(len(signals), [s.ticker for s in signals])

            for signal in signals:
                placed, halted_today = _process_signal(
                    signal, alpaca, notifier,
                    dry_run=dry_run, starting_equity=day_baseline, halted=halted_today,
                )
                if placed:
                    day_trades += 1
            if halted_today:
                log.warning("Circuit breaker tripped — pausing new entries until next session.")

            if max_cycles and cycle >= max_cycles:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down futures scanner.")

    end_session()
    log.info("Scanner stopped after %d cycle(s).", cycle)
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
