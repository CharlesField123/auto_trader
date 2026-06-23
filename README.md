# Railway Auto-Trader: StockAI → Alpaca Paper Trading Bot

Automated pipeline that runs on Railway. Every market day at 9:30 AM ET it reads
today's signals from the Supabase `signals` table, applies risk management,
places **paper** bracket orders via Alpaca, and posts Discord notifications.

## Files

| File | Role |
|---|---|
| `main.py` | Orchestrator — runs once and exits |
| `supabase_client.py` | Reads today's signals from Supabase |
| `risk_manager.py` | Position sizing + circuit breaker |
| `alpaca_client.py` | Bracket orders via Alpaca Paper API |
| `discord_notifier.py` | Discord webhook rich embeds |
| `logger.py` | Structured console logging |

## Environment variables

Set these in the Railway dashboard (locally, copy `.env.example` → `.env`):

| Variable | Notes |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API |
| `SUPABASE_SERVICE_KEY` | **service_role** key (bypasses RLS) |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca → Paper Trading |
| `DISCORD_WEBHOOK_URL` | Discord → Integrations → Webhooks |
| `MAX_RISK_PCT` | Optional, default `0.01` (1%) |
| `DEFAULT_STOP_PCT` | Optional, default `0.05` (5%) |
| `DAILY_DRAWDOWN_HALT_PCT` | Optional, default `0.03` (3%) |
| `MIN_PRICE` | Optional, default `0.01` |

> `main.py` auto-loads `.env` via `python-dotenv` on startup, so a local
> `.env` file is picked up automatically. Railway injects these as real env
> vars, which take precedence over any committed file.

## Local dry run

```bash
pip install -r requirements.txt
python main.py --dry-run
```

Reads today's signals and prints calculated bracket-order parameters. Zero
trades submitted, zero Discord messages sent.

## Deploy to Railway

1. Push to GitHub → connect to a new Railway project (Dockerfile builder).
2. Set all environment variables in the Railway dashboard.
3. Manually trigger a run; confirm logs show signals read + orders submitted.
4. Cron `30 13 * * 1-5` (13:30 UTC) runs it each weekday; the Alpaca market
   calendar check handles EST/DST drift and holidays.
