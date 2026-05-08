# Deploy — 1-month autonomous paper-trade run

Goal: ₹5L capital, system runs unattended for 1 month, Telegram updates daily, you review results at the end.

This deploys to **your local Linux machine**. The machine needs to be ON during cron times (after market close, ~16:30 IST). For true 24/7 unattended, see "Cloud option" at the end.

---

## Step 1 — Update `.env`

Open `~/projects/stockagent/.env` and set:

```bash
# Capital
CAPITAL_INR=500000

# OpenRouter (already set)
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

# Models — all four agents use working multimodal model
MODEL_TECHNICAL=google/gemini-3-flash-preview
MODEL_FUNDAMENTAL=google/gemini-3-flash-preview
MODEL_SENTIMENT=google/gemini-3-flash-preview
MODEL_MACRO=google/gemini-3-flash-preview
MODEL_COORDINATOR=google/gemini-3-flash-preview

# DB
STOCKAGENT_DB_PATH=data/stockagent.db

# Telegram (you must create the bot first — see Step 2)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

The `CAPITAL_INR=500000` cascades automatically:
- Max ₹1,00,000 per stock (20% allocation cap)
- Max ₹25,000 risk per trade (5% capital risk)
- All sizing math reads `settings.capital_inr` — no other code change needed

---

## Step 2 — Set up Telegram bot

1. Open Telegram → search `@BotFather` → start chat → `/newbot` → follow prompts. You'll get a token like `1234567:ABC...`. Put in `.env` as `TELEGRAM_BOT_TOKEN`.

2. Find your chat ID:
   - Send any message to your new bot from your Telegram account
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   - Look for `"chat":{"id": 123456789, ...}` — that number is your `TELEGRAM_CHAT_ID`. Put in `.env`.

3. Test it:
   ```bash
   cd ~/projects/stockagent
   uv run python -c "
   import stockagent
   from stockagent.alerts.telegram import send_telegram
   print('sent:', send_telegram('🔧 stockagent test ping'))
   "
   ```
   You should receive the message in Telegram instantly.

---

## Step 3 — Reset the paper ledger for a clean start

The DB has leftover state from earlier replays. Wipe it:

```bash
cd ~/projects/stockagent
uv run stockagent paper-reset    # type 'y' to confirm
```

This clears `paper_trades` and `portfolio_state`. Prices, agent_outputs, market_movers stay intact.

---

## Step 4 — Run daily-tick once manually as a smoke test

```bash
cd ~/projects/stockagent
uv run stockagent daily-tick
```

You should see:
- Bhav refresh (incremental)
- 7 NSE live screens fetched
- Corp actions calendar refreshed
- Sector map built (one-time)
- Paper-tick processes the latest trading day
- Multi-agent watchlist generated (or "no qualifying signals" if none today)
- Telegram message arrives

If anything fails, **fix it before scheduling cron** — autonomous runs that fail every day go unnoticed.

---

## Step 5 — Schedule cron (Linux)

```bash
mkdir -p ~/projects/stockagent/logs
crontab -e
```

Add this line (machine assumed to be on IST timezone — `timedatectl` to verify):

```cron
# stockagent: run daily-tick at 16:30 IST, Mon-Fri only
30 16 * * 1-5 cd /home/ashish/projects/stockagent && /home/ashish/.local/bin/uv run stockagent daily-tick >> /home/ashish/projects/stockagent/logs/daily-tick.log 2>&1
```

Why 16:30 IST: NSE closes 15:30. Bhav file usually posted by 15:50-16:00. 16:30 is safe.

Why Mon-Fri only (`1-5`): NSE doesn't trade on weekends. Saturday/Sunday runs would be no-ops but waste API calls.

Verify cron is active:
```bash
crontab -l
systemctl status cron       # or systemctl status crond
```

---

## Step 6 — Schedule weekly DB backup

Add another cron line:

```cron
# Weekly DB backup, Sunday midnight
0 0 * * 0 cp /home/ashish/projects/stockagent/data/stockagent.db /home/ashish/backups/stockagent-$(date +\%Y\%m\%d).db
```

Make sure `~/backups/` exists:
```bash
mkdir -p ~/backups
```

The DB is ~520 MB. Four weekly backups = ~2 GB.

---

## Step 7 — Monitoring during the month

You don't need to do anything daily — Telegram tells you what happened. But you can spot-check:

```bash
# Quick status
uv run stockagent paper-status

# Detailed P&L summary so far
uv run stockagent paper-summary

# Check the cron log
tail -50 ~/projects/stockagent/logs/daily-tick.log

# DB stats
uv run stockagent stats
```

If Telegram messages stop arriving for >2 trading days, check:
1. Machine is on at 16:30
2. Cron service is running
3. `logs/daily-tick.log` for errors

---

## Step 8 — End-of-month review

After 30 days (or whenever you want to see results):

```bash
cd ~/projects/stockagent
uv run stockagent paper-summary
```

Outputs:
- Final NAV vs ₹5,00,000 starting (with %)
- Realized P&L in ₹
- Win rate (W/L count), avg winner %, avg loser %, profit factor
- Per-sector P&L breakdown
- Max drawdown %
- Best 3 / worst 3 trades with dates and exit reasons

For a specific window:
```bash
uv run stockagent paper-summary --start 2026-05-10 --end 2026-06-10
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No Telegram messages | Bot token / chat_id wrong | Re-test with the python snippet in Step 2 |
| Cron didn't fire | Machine asleep / cron service down | Check `systemctl status cron`, log `journalctl -u cron --since today` |
| Daily-tick errors out | NSE rate-limit / model API issue | Check `logs/daily-tick.log`. Re-run manually next day to recover |
| NAV jumps unexpectedly | Stale state from earlier replay | `paper-reset`, then re-run daily-tick to rebuild |
| Same picks repeating for days | RSI signal stays oversold | Expected; positions stay until stop/signal/time exit |
| LLM agent always returns no_data | Model gated/quota | Check OpenRouter dashboard, swap model in `.env` |

---

## Cloud option (only if you want truly always-on)

Local cron requires your machine to be on at 16:30 IST. If that's unreliable:

**Cheap option (~$3-5/month):** AWS t4g.nano (ARM, 512MB RAM is enough), Hetzner CX11, or Oracle Cloud Free Tier (always-free ARM VM).

Setup:
1. Provision Linux VM, install Python 3.12 + uv
2. Clone repo, copy `.env` (mind the permissions — `chmod 600 .env`)
3. Set timezone: `sudo timedatectl set-timezone Asia/Kolkata`
4. Backfill bhav once (the same `backfill-bhav` you ran locally)
5. Set up the same cron lines

To get the DB to your local for review, set up rsync or a daily DB snapshot upload to S3/GDrive.

This is overkill for a 1-month trial. Local cron is fine if your machine is on most evenings.

---

## What Telegram messages you'll see

After every daily-tick (Mon-Fri ~16:30 IST):

```
📈 stockagent daily — 2026-05-10
NAV ₹501,234  (+0.25% from start)
🟢 day P&L ₹+1,234
open: 3  fills: 1  exits: stop=0 sig=0 time=0

Tomorrow's watchlist (4 picks)
1. RELIANCE  (Energy)
   ₹1,435.00 → stop ₹1,330.50 → tgt ₹1,644.00  R:R 1:2.0
   qty 70  ₹100,450  conv 0.61
2. TCS  (IT)
   ...
```

Days with no qualifying signals just show NAV + position status. That's normal.

---

## Hard locks during the run (memory-enforced)

These are not changeable without explicit instruction:
- **No live trading.** This is paper-only. The system has zero broker integration code. Your real money is safe.
- **Position sizing is fixed.** Max 20% per stock, max 5% risk per trade. Coordinator enforces.
- **Sector cap.** Max 2 positions per sector at any time.
- **Macro deployment scaling.** In high-VIX or downtrend regimes, max picks is reduced automatically.

---

## After the month, before you consider going live

The roadmap (`ROADMAP.md`) hard-gates live trading on ≥1 month of clean paper-trade history. After this run completes:
1. Run `paper-summary` — is total return positive? Sharpe sensible? Max DD bearable?
2. Compare to Nifty 50 / Nifty 500 returns over the same period (the benchmark)
3. If results are good AND you decide to go live, the next session is broker integration (Kite Connect or Fyers). Start with HALF position size for the first month of live trading.
4. If results are bad, we have real data to debug. Don't blame the system — we'll have actual trade-by-trade evidence of what worked and didn't.
