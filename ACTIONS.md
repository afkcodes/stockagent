# Action Items — stockagent

Your reference for what to do, in priority order.

---

## ⚠️ Do this now (one-time)

**Update `.env`** — change the model lines so the LLM agent actually works:
```
MODEL_TECHNICAL=google/gemini-3-flash-preview
MODEL_COORDINATOR=google/gemini-3-flash-preview
```
The current `moonshotai/kimi-k2.5` returns empty content on our long multimodal prompts. Gemini 3 flash preview is verified working: 2.7s/call, ~$0.0007/call (~₹0.06).

After saving `.env`, sanity-check:
```
uv run stockagent watchlist --as-of 2025-12-15 --max-picks 5 --min-conviction 0.3
```
Expect: 5 picks shown with non-empty rationales like "controlled pullback... volume drying up...".

---

## Daily routine — after 3:35 PM IST every trading day

Run **one command**:
```
cd ~/projects/stockagent
uv run stockagent daily-tick
```

What happens:
1. Pulls today's NSE bhav (catches up since last run)
2. Fetches the 7 NSE live-mover screens (top gainers/losers, volume gainers, etc.)
3. Processes the paper ledger (fills, stop checks, exits, MTM)
4. Generates tomorrow's watchlist with LLM verdicts + confluence flags

Output is your action list for the next session's open. Cost per run: ~₹1.5.

**If you want to actually trade those picks**, place the orders manually at next-day open with your broker. Use the `entry`, `stop`, `target`, and `qty` from the output exactly — they already respect the ₹1L / 20% / 5% locks.

---

## Weekly check-in

```
uv run stockagent paper-status
```
Shows current NAV, open positions, last 10 closed trades. Confirms the system is running and tracking P&L.

If NAV stops updating, something is wrong with the daily-tick — check logs.

---

## Useful commands (reference)

| Command | What it does |
|---|---|
| `stockagent daily-tick` | Full daily routine (the main command) |
| `stockagent paper-status` | Current portfolio + recent trades |
| `stockagent watchlist --as-of YYYY-MM-DD` | Re-run watchlist for any historical date |
| `stockagent market-movers fetch` | Pull live screens (auto-run by daily-tick) |
| `stockagent market-movers discover --exclude-nifty500` | Spot interesting non-Nifty500 movers |
| `stockagent market-movers show --category top_losers --limit 8` | View any specific screen |
| `stockagent paper-replay --start 2024-01-01 --end 2024-09-30 --reset` | Backtest the live strategy on a date range |
| `stockagent backtest rsi_mean_reversion --universe nifty500 --start ... --end ...` | One-shot backtest, no walk-forward |
| `stockagent walkforward rsi_mean_reversion --universe nifty500` | 8-window robustness test |
| `stockagent stats` | DB row counts and date range |
| `stockagent backfill-bhav --years 5 --universe all` | Re-pull historical bhav (rare) |

---

## What to watch

**Good signs (system working):**
- `daily-tick` produces 0-5 picks with bullish verdicts and reasoning that cites specific chart features
- Open positions get exited via stops, signals, or time limits — not stuck open forever
- Paper-status NAV moves with the market

**Yellow flags (investigate):**
- Every signal getting rejected (verdict=bearish or conviction <0.3 for everything) → prompt may need rebalancing
- Same symbols reappearing as picks for many days → strategy is over-firing on one name
- Large drawdown >15% on paper → market regime change; check walk-forward windows

**Red flags (stop and check):**
- `daily-tick` errors out (some external endpoint changed)
- NAV jumps inexplicably — usually a stale state issue, run `paper-reset` after backing up the DB
- LLM returning empty on every call — re-run the model bake-off probe

---

## Before going live with real money

**Required gates** (do not skip):
1. **At least 1 month of clean paper-trade history.** Memory says ≥1-2 months. Period.
2. **Paper-trade NAV trending positive** — not just one lucky window, but several weeks
3. **Drawdowns matching walk-forward expectations** — if paper draws down 30% when walk-forward predicted max 23%, something is off
4. **Manual review of every trade** for at least the first 2 weeks — confirm fills, stops, exits all behave as expected

When you decide to go live:
- Open a Zerodha (Kite Connect ₹500/mo) or Fyers (free, requires KYC) account
- Both are noted in memory as Phase 6 candidates
- Start with **half the position size** — ₹10K per stock instead of ₹20K — for the first month
- Keep paper trading running in parallel as a control

---

## Files to back up regularly

- `data/stockagent.db` — your entire DB (prices, paper trades, agent outputs, decisions). Currently ~515 MB. Copy to cloud weekly.
- `.env` — your API key. Never commit. Already in `.gitignore`.

```
cp data/stockagent.db ~/backups/stockagent-$(date +%Y%m%d).db
```

---

## When something breaks

| Symptom | Likely cause | Fix |
|---|---|---|
| `OPENROUTER_API_KEY not set` | `.env` missing or wrong path | Confirm `.env` in `~/projects/stockagent/` |
| LLM returns empty on every call | Model is broken/gated | Switch `MODEL_TECHNICAL` to deepseek/deepseek-chat or back to gemini-3-flash-preview |
| `nselib: Data not found` | Querying a holiday | Expected — should be auto-skipped. If not, file an issue. |
| `bhav fetch hangs >30s` | NSE anti-bot pause | We have a 30s timeout patch — restart should clear cookies |
| `paper-status` shows no NAV | No daily-tick has run yet | Run `daily-tick` once |
| Imports fail after pulling new code | Deps changed | `uv sync` |
| Charts won't render | matplotlib backend issue | We force `Agg` backend in `agents/charts.py` — should be fine headless |

---

## What's still NOT built (future)

- **Telegram alerts** — push the daily watchlist to your phone after each run. ~30 min build when you want it.
- **Live broker integration** (Phase 6) — Kite Connect / Fyers / Upstox. Not until paper trades validate ≥1 month.
- **Sentiment / news agent** — wired in `.env` (`MODEL_SENTIMENT=google/gemini-3-flash-preview`) but agent itself not implemented yet. Would scan news for held positions and flag concerns.
- **Multi-strategy ensemble** — we ship only `rsi_mean_reversion` (the only walk-forward survivor). When you want to add another, it goes through walk-forward first.
