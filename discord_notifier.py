"""Posts color-coded rich embeds to Discord via a webhook URL.

No bot token required — a webhook is a fire-and-forget HTTP POST. All methods
degrade gracefully: a webhook failure is logged but never crashes a trade run,
and an unset webhook turns every call into a no-op (handy for ``--dry-run``).
"""

from __future__ import annotations

import os
from typing import Optional

import requests

from logger import log
from risk_manager import TradePlan

# Embed colors (decimal) per the plan's event table.
COLOR_BLUE = 0x3498DB     # Session start
COLOR_GREEN = 0x2ECC71    # Trade placed
COLOR_YELLOW = 0xF1C40F   # Trade skipped
COLOR_RED = 0xE74C3C      # Circuit breaker
COLOR_PURPLE = 0x9B59B6   # Session end


class DiscordNotifier:
    def __init__(self, enabled: bool = True) -> None:
        self.webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        # Disabled when no URL configured or explicitly muted (dry-run).
        self.enabled = enabled and bool(self.webhook_url)
        if enabled and not self.webhook_url:
            log.warning("DISCORD_WEBHOOK_URL not set — Discord notifications off.")

    def _send(self, embed: dict) -> None:
        if not self.enabled:
            return
        try:
            response = requests.post(
                self.webhook_url, json={"embeds": [embed]}, timeout=10
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Discord webhook failed: %s", exc)

    @staticmethod
    def _embed(title: str, description: str, color: int,
               fields: Optional[list[dict]] = None) -> dict:
        embed = {"title": title, "description": description, "color": color}
        if fields:
            embed["fields"] = fields
        return embed

    # ------------------------------------------------------------------ events
    def session_start(self, signal_count: int, tickers: list[str]) -> None:
        listing = ", ".join(tickers) if tickers else "none"
        self._send(
            self._embed(
                "🚀 StockAI Bot is live",
                f"**{signal_count}** signal(s) found today.\n{listing}",
                COLOR_BLUE,
            )
        )

    def trade_placed(self, plan: TradePlan, order_id: str) -> None:
        self._send(
            self._embed(
                f"✅ Trade Placed — {plan.ticker}",
                f"Order `{order_id}` submitted.",
                COLOR_GREEN,
                fields=[
                    {"name": "Side", "value": plan.side.upper(), "inline": True},
                    {"name": "Shares", "value": str(plan.qty), "inline": True},
                    {"name": "Entry", "value": f"${plan.entry:.2f}", "inline": True},
                    {"name": "Take Profit", "value": f"${plan.take_profit:.2f}", "inline": True},
                    {"name": "Stop Loss", "value": f"${plan.stop_loss:.2f}", "inline": True},
                    {"name": "Risk", "value": f"${plan.risk_dollars:.2f}", "inline": True},
                ],
            )
        )

    def trade_skipped(self, ticker: str, reason: str) -> None:
        self._send(
            self._embed(
                f"⚠️ Trade Skipped — {ticker}",
                reason,
                COLOR_YELLOW,
            )
        )

    def circuit_breaker(self, drawdown_pct: float) -> None:
        self._send(
            self._embed(
                "🔴 Circuit Breaker Tripped",
                f"Account down {drawdown_pct:.1%} — trading halted for today.",
                COLOR_RED,
            )
        )

    def session_end(self, trades_placed: int, net_pnl: float,
                    final_equity: float) -> None:
        self._send(
            self._embed(
                "🏁 Session Complete",
                "End-of-run summary.",
                COLOR_PURPLE,
                fields=[
                    {"name": "Trades Placed", "value": str(trades_placed), "inline": True},
                    {"name": "Net P&L", "value": f"${net_pnl:,.2f}", "inline": True},
                    {"name": "Final Equity", "value": f"${final_equity:,.2f}", "inline": True},
                ],
            )
        )
