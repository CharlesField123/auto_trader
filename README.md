# Railway Auto-Trader: StockAI → Alpaca Paper Trading Bot

Automated pipeline that runs on Railway, with **two modes** sharing one risk →
execution → Discord flow:

- **Daily stock bot** (`python main.py`) — every market day at 9:30 AM ET it
  reads today's signals from the Supabase `signals` table, applies risk
  management, and places **paper** bracket orders via Alpaca.
- **Futures scanner** (`python main.py --futures`) — a long-running worker that
  **scans a futures universe every 60 seconds (1-minute bars)**, ranks symbols
  to find the **best** opportunities right now, and trades them automatically.

## Files

| File | Role |
|---|---|
| `main.py` | Orchestrator — daily one-shot **or** continuous futures loop |
| `futures_scanner.py` | Ranks the futures universe on 1-min bars (best first) |
| `supabase_client.py` | Reads today's signals from Supabase |
| `risk_manager.py` | Position sizing + circuit breaker |
| `alpaca_client.py` | Bracket orders + 1-min bars via Alpaca Paper API |
| `discord_notifier.py` | Discord webhook rich embeds |
| `logger.py` | Structured console logging |

## Futures scanner (1-minute scanning)

```bash
python main.py --futures                              # live, rescans every 60s
python main.py --futures --dry-run --max-cycles 1     # one offline scan, no orders
python main.py --futures --interval 30                # custom cadence (seconds)
```

Each cycle pulls the last `SCAN_LOOKBACK_BARS` one-minute bars for every symbol
in `FUTURES_UNIVERSE` and scores them:

```
score = |rate_of_change over the window| × volume_confirmation_factor
```

The move's sign sets the direction (up → long, down → short). The top
`FUTURES_TOP_N` symbols clearing `FUTURES_MIN_SCORE` are traded, each with a
volatility-based stop (`STOP_ATR_MULT` × average 1-min range) and a 2:1
take-profit. They flow through the **same** risk manager and Alpaca bracket-order
path as the stock bot; symbols already held are skipped so the loop doesn't
stack entries every minute. Notifications are identical: one 🚀 session start, a
✅/⚠️ per candidate, 🔴 on circuit breaker, and a 🏁 session-end summary.

> [!NOTE]
> Alpaca's standard feed serves equities/crypto, so `FUTURES_UNIVERSE` defaults
> to liquid **futures-tracking ETF proxies** (ES→SPY, NQ→QQQ, RTY→IWM, GC→GLD,
> CL→USO, …), tradable on Alpaca paper today. Point it at native futures roots
> (`MES,MNQ,MYM,M2K,MGC,MCL`) on a feed/broker that serves futures data.

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
| `FUTURES_UNIVERSE` | Futures mode — symbols to scan, default ETF proxies |
| `FUTURES_TOP_N` | Futures mode — best N to trade, default `3` |
| `SCAN_LOOKBACK_BARS` | Futures mode — 1-min bars per scan, default `20` |
| `FUTURES_MIN_SCORE` | Futures mode — score gate, default `0.0008` |
| `STOP_ATR_MULT` | Futures mode — stop = mult × avg range, default `1.5` |

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
3. **Futures scanner (default):** `railway.toml` runs `python main.py --futures`
   as a worker that rescans every 60s while the market is open and exits at the
   close (restart it each day). Confirm logs show scans + orders submitted.
4. **Daily stock bot instead:** remove `startCommand` in `railway.toml` and set
   `cronSchedule = "30 13 * * 1-5"` (13:30 UTC). The Alpaca calendar check
   handles EST/DST drift and holidays either way.
