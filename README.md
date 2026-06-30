# Railway Auto-Trader: StockAI → Alpaca Paper Trading Bot

Automated pipeline that runs on Railway, with **two modes** sharing one risk →
execution → Discord flow:

- **Daily stock bot** (`python main.py`) — every market day at 9:30 AM ET it
  reads today's signals from the Supabase `signals` table, applies risk
  management, and places **paper** bracket orders via Alpaca.
- **Crypto scanner** (`python main.py --crypto`) — a long-running **24/5**
  worker that **scans a crypto universe every 60 seconds (1-minute bars)** all
  day Monday–Friday, ranks pairs to find the **best** opportunities right now,
  and trades them automatically.

> [!IMPORTANT]
> **Why crypto, not futures?** Alpaca offers no futures product. Its asset
> classes are US stocks/ETFs, options, and crypto — and **crypto is the only one
> that trades 24/7**, so it's what powers the always-on scanner. Crypto on Alpaca
> is also **long-only** with **no bracket/OCO orders**, which shapes the design
> below (long entries + loop-managed exits).

## Files

| File | Role |
|---|---|
| `main.py` | Orchestrator — daily one-shot **or** continuous 24/5 crypto loop |
| `crypto_scanner.py` | Ranks the crypto universe on 1-min bars (best first) |
| `supabase_client.py` | Reads today's signals from Supabase |
| `risk_manager.py` | Position sizing, portfolio limits + circuit breaker |
| `alpaca_client.py` | Bracket (stock) / buy + close (crypto) + 1-min bars |
| `discord_notifier.py` | Discord webhook rich embeds |
| `logger.py` | Structured console logging |

## Crypto scanner (24/5, 1-minute scanning)

```bash
python main.py --crypto                              # live, rescans every 60s
python main.py --crypto --dry-run --max-cycles 1     # one offline scan, no orders
python main.py --crypto --interval 30                # custom cadence (seconds)
```

Each cycle pulls the last `SCAN_LOOKBACK_BARS` one-minute bars for every pair in
`CRYPTO_UNIVERSE` and scores them:

```
score = rate_of_change over the window × volume_confirmation_factor
```

Crypto is **long-only** on Alpaca, so only positive (bullish), volume-backed
momentum scores. The top `CRYPTO_TOP_N` pairs clearing `CRYPTO_MIN_SCORE` are
bought. Notifications are identical to the original: one 🚀 session start, a
✅/⚠️ per candidate, 🔴 on circuit breaker, and a 🏁 session-end summary.

**24/5 operation:** the worker stays alive continuously and scans around the
clock Monday–Friday (ET), idling over the weekend. It rolls a fresh session at
each ET day boundary — emitting a 🏁 summary for the day that's ending and a 🚀
start for the new one.

### Risk management

Layered so no single signal can over-deploy or blow up the account:

| Control | Default | Where |
|---|---|---|
| Per-trade risk | 1% of equity | `MAX_RISK_PCT` |
| Per-position notional cap | 10% of equity | `MAX_POSITION_PCT` |
| Max concurrent positions | 5 | `MAX_OPEN_POSITIONS` |
| Gross-exposure ceiling | 50% of equity | `MAX_GROSS_EXPOSURE_PCT` |
| Buying-power + min-notional checks | live cash / $1 | `MIN_NOTIONAL` |
| Daily circuit breaker | halt new entries at −3%/day | `DAILY_DRAWDOWN_HALT_PCT` |
| Stop-loss / take-profit | −1% / +2% | `CRYPTO_STOP_LOSS_PCT` / `CRYPTO_TAKE_PROFIT_PCT` |
| Re-entry cooldown | 15 min after a close | `REENTRY_COOLDOWN_MIN` |

Because Alpaca crypto has **no bracket/OCO orders**, stop-loss and take-profit
are enforced by the loop itself: every cycle it checks each open position's
unrealized P&L and **closes** the ones that hit their stop or target (then a
cooldown prevents instantly re-buying the same symbol). The per-day circuit
breaker resets each session, so a trip pauses new entries only until the next
trading day while exits keep winding down open risk.

## Environment variables

Set these in the Railway dashboard (locally, copy `.env.example` → `.env`):

| Variable | Notes |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API (daily bot only) |
| `SUPABASE_SERVICE_KEY` | **service_role** key (bypasses RLS; daily bot only) |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca → Paper Trading |
| `DISCORD_WEBHOOK_URL` | Discord → Integrations → Webhooks |
| Risk vars | `MAX_RISK_PCT`, `MAX_POSITION_PCT`, `MAX_OPEN_POSITIONS`, `MAX_GROSS_EXPOSURE_PCT`, `DAILY_DRAWDOWN_HALT_PCT`, `MIN_NOTIONAL` |
| Crypto scanner | `CRYPTO_UNIVERSE`, `CRYPTO_TOP_N`, `SCAN_LOOKBACK_BARS`, `CRYPTO_MIN_SCORE`, `CRYPTO_STOP_LOSS_PCT`, `CRYPTO_TAKE_PROFIT_PCT`, `REENTRY_COOLDOWN_MIN` |

See `.env.example` for every variable with its default. `main.py` auto-loads
`.env` via `python-dotenv`; Railway env vars take precedence.

## Local dry run

```bash
pip install -r requirements.txt
python main.py --crypto --dry-run --max-cycles 1   # one crypto scan, no orders
python main.py --dry-run                           # daily stock bot, no orders
```

Sizes candidates and prints the planned orders. Zero trades submitted, zero
Discord messages sent.

## Deploy to Railway

1. Push to GitHub → connect to a new Railway project (Dockerfile builder).
2. Set all environment variables in the Railway dashboard.
3. **Crypto scanner (default):** `railway.toml` runs `python main.py --crypto`
   as an always-on worker that rescans every 60s, 24/5. Confirm logs show scans
   + orders submitted.
4. **Daily stock bot instead:** remove `startCommand` in `railway.toml` and set
   `cronSchedule = "30 13 * * 1-5"` (13:30 UTC). The Alpaca calendar check
   handles EST/DST drift and holidays.
