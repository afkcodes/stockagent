# stockagent

A multi-agent paper-trading system for Indian equities (NSE). Researches stocks, generates signals, has a council of LLM agents judge them, sizes positions, and executes a paper-trade ledger autonomously every trading day.

Personal use only. Not financial advice. Reserve real money for after a real paper-trade track record.

---

## What this does

Every trading day at 16:30 IST (after NSE close + bhavcopy posted):

1. Pulls today's NSE bhavcopy with delivery data
2. Fetches NSE live screens (top gainers/losers, volume gainers, circuit hitters)
3. Refreshes the corporate-actions calendar
4. Processes yesterday's open paper positions (stop checks, signal exits, time stops, trailing stops)
5. Generates new signals on the Nifty 500 universe
6. Runs each candidate through a **council of four LLM agents** (technical, fundamental, sentiment, macro) in parallel
7. Combines their verdicts via a strict formula (no LLM in the final vote)
8. Applies sector concentration cap, macro deployment multiplier, and locked position sizing
9. Pushes the resulting watchlist + portfolio status to Telegram

The system is fully autonomous after deploy. You read the daily Telegram message; you don't intervene.

---

## Features

**Data**
- Full NSE EQ universe (~3,135 symbols) with delivery columns, 2020-present
- Daily bhavcopy refresh, holiday-aware, idempotent
- 7 NSE live-analysis screens (most-active, gainers/losers, volume gainers, price-band hitters)
- Corporate actions calendar (earnings, splits, bonuses, dividends)
- Sector mapping for all major indices
- Optional fundamentals (screener.in scrape) + news (Google News + Moneycontrol) per symbol

**Strategy**
- RSI mean-reversion on Nifty 500 (the sole walk-forward survivor — see "Validated results" below)
- Walk-forward validation framework with rolling train/test windows
- Multiple strategy variants available for ad-hoc backtesting (EMA crossover, Bollinger breakout, delivery anomaly, multi-timeframe — most rejected by walk-forward)

**Multi-agent council** (parallel, formula combine)
- Technical agent: multimodal — receives a 60-day candle chart + indicator snapshot + recent bars; weight 1.5
- Fundamental agent: PE/ROE/ROCE/debt/promoter quality gate via screener.in; hard veto on red flags; weight 1.0
- Sentiment agent: scans recent news headlines, hard veto on fraud/SEBI/auditor flags; weight 0.7
- Macro agent: pure-rule regime classifier (India VIX + Nifty trend); produces deployment multiplier; weight 0.5

**Risk management (mechanical, not LLM-decided)**
- ₹1,00,000 paper capital locked (configurable but enforced project-wide)
- Max 20% allocation per stock
- Max 5% capital risk per trade (drives stop-loss distance)
- Max 2 picks per sector (concentration cap)
- ±5 trading day avoidance window around earnings/dividends/splits
- Trailing stops: ratchet up to ATR-based stop after >+5% unrealized gain, never down
- Macro deployment multiplier scales `max_picks` in elevated-VIX or downtrend regimes

**Operational**
- One-shot deploy script for Ubuntu VPS (`deploy.sh`)
- Single daily command: `stockagent daily-tick`
- Telegram alerts after every run
- Weekly DB backups via cron
- Full audit trail: every agent verdict, every decision, every trade persisted to SQLite

---

## Architecture

### Daily flow

```
                  ┌──────────────────────────────────────┐
                  │  cron 16:30 IST Mon-Fri              │
                  │  $ stockagent daily-tick             │
                  └─────────────────┬────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
     ┌────────▼─────────┐  ┌────────▼─────────┐  ┌────────▼─────────┐
     │ 1. Bhav refresh  │  │ 2. Market movers │  │ 3. Corp actions  │
     │ (incremental)    │  │ (7 NSE screens)  │  │ (next 60 days)   │
     └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
              └─────────────────────┼─────────────────────┘
                                    │
                       ┌────────────▼────────────┐
                       │ 4. Process paper ledger │
                       │  - fill yesterday picks │
                       │  - check stops/signals  │
                       │  - update trailing stop │
                       │  - mark to market       │
                       └────────────┬────────────┘
                                    │
                       ┌────────────▼────────────┐
                       │ 5. Generate signals     │
                       │ RSI mean-reversion on   │
                       │ Nifty 500 (~504 names)  │
                       └────────────┬────────────┘
                                    │
                       ┌────────────▼────────────┐
                       │ 6. Event-avoidance      │
                       │ filter (mechanical)     │
                       └────────────┬────────────┘
                                    │
                       ┌────────────▼────────────┐
                       │ 7. AGENT ORCHESTRATOR   │
                       │   parallel ThreadPool   │
                       │   ┌──────────────────┐  │
                       │   │ technical (×1.5) │  │
                       │   │ fundamental (×1) │  │
                       │   │ sentiment (×0.7) │  │
                       │   │ macro (×0.5)     │  │
                       │   └────────┬─────────┘  │
                       │            │            │
                       │   ┌────────▼─────────┐  │
                       │   │ combine (formula)│  │
                       │   │ ─ vetoes kill    │  │
                       │   │ ─ weighted vote  │  │
                       │   │ ─ disagreement   │  │
                       │   │   penalty        │  │
                       │   └──────────────────┘  │
                       └────────────┬────────────┘
                                    │
                       ┌────────────▼────────────┐
                       │ 8. Sector cap +         │
                       │    macro multiplier +   │
                       │    position sizing      │
                       └────────────┬────────────┘
                                    │
                       ┌────────────▼────────────┐
                       │ 9. Persist + Telegram   │
                       └─────────────────────────┘
```

### The "heartless" combine

The orchestrator's `combine()` step is intentionally **a pure formula, never an LLM**. The reasoning:

- LLMs are good at judgment over heterogeneous data (chart, ratios, news)
- LLMs are bad at adversarially robust arithmetic
- A formula combine means the FINAL selection is auditable and reproducible — you can re-run the same agent verdicts and always get the same picks
- LLM judgment lives strictly inside individual agents

The combine logic:

1. If any agent returned `verdict='avoid'` with `is_veto=true`, the trade is rejected. No discussion.
2. Below quorum (fewer than 2 agents producing usable verdicts) → neutral, no action.
3. `net_score = (Σ bullish_conv × weight − Σ bearish_conv × weight) / total_weight`
4. If agents disagree wildly (`stdev(convictions) > 0.25`), conviction is dampened.
5. `net_score` mapped to final verdict against `min_combined_conviction` threshold.

### Strict agent contract

Every agent must:
- Output exactly one valid JSON object matching the `AgentVerdict` schema (verdict, conviction, reasoning, flags, evidence, is_veto)
- Cite specific numbers from input in `evidence` — every claim has a number behind it
- Phrases like "appears to" or "feels like" are explicitly forbidden in prompts
- If essential data is missing, return `verdict='no_data'` rather than guess
- Hard vetoes use `is_veto=true` — these are non-negotiable rejections (e.g., 50%+ promoter pledged, fraud headlines, true falling-knife chart)

This is what we mean by "heartless": agents make judgments based on data, not feelings; the orchestrator combines those judgments by arithmetic, not negotiation; the system rejects rather than guesses when uncertain.

---

## Validated results

Walk-forward validation across 8 rolling 6-month windows (2020-06-01 to 2026-05-06), Nifty 500 universe:

| Strategy                       | Median Return | Median Sharpe | Positive Windows |
| ------------------------------ | -------------:| -------------:| ----------------:|
| **rsi_mean_reversion (Nifty 500)** | **+7.89%**  | **+0.69**     | **100%**         |
| rsi_mean_reversion (Nifty 50)  |        -0.38% |         -0.58 |              50% |
| rsi_mean_reversion_filtered    |        -5.68% |         -1.06 |              38% |
| rsi_mean_reversion_mtf         |        +3.24% |         +0.04 |              75% |
| ema_crossover (Nifty 500)      |        -3.26% |         -0.51 |              38% |
| bollinger_breakout             |        -2.58% |         -0.35 |              38% |
| delivery_anomaly               |        -2.96% |         -0.78 |              38% |

**Only one strategy survived honest walk-forward validation**: RSI mean-reversion on Nifty 500. Aggregate backtests of EMA crossover *looked* great (+177% over 6 years) but were a compounding mirage — per-window median return was -0.05%. We learned to trust walk-forward.

We also learned that **trend-confirmation filters HURT** mean-reversion (tried twice, both times Sharpe collapsed). The strategy's edge is precisely buying when "the trend looks bad short-term" — filter that out and you filter the signal.

---

## Quick start (Ubuntu VPS)

Deploy in 5 commands.

```bash
# 1. From your local machine — push the project to your VPS
rsync -avz \
  --exclude='.venv' --exclude='data/*.db' --exclude='*.bak.*' \
  --exclude='__pycache__' --exclude='reports/charts' \
  --exclude='.git' \
  ~/projects/stockagent/ user@your-vps:~/stockagent/

# 2. SSH in
ssh user@your-vps
cd ~/stockagent

# 3. Run the deploy script (interactive)
./deploy.sh
```

`deploy.sh` handles 14 steps idempotently:
- Installs system deps (`uv`, `cron`, `tzdata`, build essentials)
- Sets timezone to Asia/Kolkata
- Syncs Python deps via `uv sync`
- Builds `.env` (preserves existing values)
- Tests OpenRouter API and Telegram bot
- Initializes DB schema
- Backfills 6 years of NSE bhav (~16 min one-time)
- Builds sector map + corporate-actions calendar
- Resets paper ledger for fresh start
- Smoke-tests `daily-tick`
- Installs cron entries (Mon-Fri 16:30 IST + Sun 00:00 backup)

Flags: `--non-interactive`, `--skip-backfill`, `--skip-cron`, `--skip-tg-test`.

For manual setup, see [DEPLOY.md](./DEPLOY.md).

---

## Daily usage

Once deployed, you don't run anything. Cron handles `daily-tick`. You receive a Telegram message every trading day around 16:30 IST.

For manual checks:

```bash
# Most-recent portfolio state
uv run stockagent paper-status

# P&L summary across any window
uv run stockagent paper-summary

# What stocks does the system favor most?
uv run stockagent symbol-profile --top 20

# Deep-dive on a single stock
uv run stockagent symbol-profile --symbol RELIANCE

# Today's market movers (after market close)
uv run stockagent market-movers fetch
uv run stockagent market-movers discover --exclude-nifty500

# Watch the cron log
tail -f ~/stockagent/logs/daily-tick.log

# Database row counts
uv run stockagent stats
```

For ad-hoc backtesting:

```bash
# Run a strategy on a date range
uv run stockagent backtest rsi_mean_reversion --universe nifty500 --start 2024-01-01

# Walk-forward validate a strategy
uv run stockagent walkforward rsi_mean_reversion --universe nifty500

# Replay paper trading over a historical window
uv run stockagent paper-replay --start 2024-01-01 --end 2024-06-30 --reset
```

---

## Project structure

```
stockagent/
├── stockagent/
│   ├── __init__.py            # patches nselib HTTP layer at import time
│   ├── nselib_patch.py        # shared session + 30s timeouts (fixes 4-min hangs)
│   ├── config.py              # locked constraints (capital, allocation, risk)
│   ├── cli.py                 # all CLI commands (Click)
│   │
│   ├── db/
│   │   ├── schema.sql         # full DB schema, idempotent
│   │   └── session.py         # SQLAlchemy engine + WAL pragma
│   │
│   ├── data/
│   │   ├── nse.py             # bhavcopy fetcher, holiday detection
│   │   ├── yf.py              # yfinance fallback (pre-2020 history)
│   │   ├── loader.py          # load_prices, pivot helpers
│   │   ├── universe.py        # liquid-universe filter (rejected by walk-forward)
│   │   ├── market_movers.py   # 7 NSE live screens
│   │   ├── sectors.py         # symbol → sector mapping
│   │   ├── events.py          # corporate-actions calendar
│   │   ├── screener.py        # screener.in fundamentals scrape
│   │   └── news.py            # Google News + Moneycontrol scrape
│   │
│   ├── indicators/
│   │   └── compute.py         # pandas-ta wrappers, per-symbol-safe groupby
│   │
│   ├── backtest/
│   │   ├── strategies.py      # 8 strategy variants — only RSI mean-rev validated
│   │   ├── costs.py           # Zerodha-equivalent cost model
│   │   ├── engine.py          # long-only daily-bar backtester
│   │   ├── metrics.py         # CAGR, Sharpe, Sortino, profit factor, per-regime
│   │   └── walkforward.py     # rolling train/test windows, the truth-teller
│   │
│   ├── signals/
│   │   └── daily.py           # generate_signals + VIABLE_STRATEGIES registry
│   │
│   ├── agents/
│   │   ├── protocol.py        # AgentVerdict schema, Agent ABC, CombinedVerdict
│   │   ├── orchestrator.py    # parallel runner + formula combine
│   │   ├── base.py            # OpenRouter client (with multimodal support)
│   │   ├── charts.py          # mplfinance candle chart → base64 PNG for vision LLM
│   │   ├── technical.py       # multimodal chart + indicator judge
│   │   ├── fundamental.py     # quality gate, hard veto on debt/pledged red flags
│   │   ├── sentiment.py       # news classifier, hard veto on fraud/SEBI flags
│   │   ├── macro.py           # rule-based regime classifier
│   │   └── coordinator.py     # full V2 watchlist pipeline
│   │
│   ├── paper_trade/
│   │   └── ledger.py          # closed-loop daily simulation
│   │
│   └── alerts/
│       └── telegram.py        # direct Bot API push, no extra deps
│
├── data/
│   └── stockagent.db          # SQLite, ~520MB after backfill
├── logs/                      # daily-tick.log written by cron
├── reports/charts/            # rendered charts (gitignored)
├── tests/
│   └── test_smoke.py          # DB schema integrity + live nselib check
├── scripts/
│   └── profile_bhav.py        # diagnostic for pipeline timing
├── deploy.sh                  # one-shot Ubuntu VPS deploy
├── pyproject.toml             # Python 3.12 deps via uv
├── ACTIONS.md                 # operator's daily playbook
├── DEPLOY.md                  # full deployment reference
├── ROADMAP.md                 # what's built, what's deferred, with effort estimates
└── README.md                  # this file
```

---

## Hard locks

These are baked into the system and enforced project-wide. They cannot be overridden during a paper-trade run:

| Lock                          | Value         | Where enforced                                                         |
| ----------------------------- | ------------- | ---------------------------------------------------------------------- |
| Capital                       | ₹5,00,000     | `config.py` reads from `.env`                                          |
| Max allocation per stock      | 20%           | `coordinator._size_position`, `paper_trade.ledger`                     |
| Max risk per trade            | 5% of capital | Same; drives stop-loss distance                                        |
| Max picks per sector          | 2             | `coordinator.run_coordinator`                                          |
| Live trading                  | DISABLED      | No broker integration code in repo. Real money is safe.                |
| Earnings/corp-action window   | ±5 days       | `data/events.is_in_avoid_window`                                       |
| Macro deployment multiplier   | 0.4 - 1.0     | `agents/macro.deployment_multiplier`                                   |

The "personal use" / "paper trading first" discipline is enforced by the absence of live-broker code, not by a flag. Going live requires writing the broker integration as a separate phase, after the trial period validates results.

---

## Honest limitations

These are real but acceptable for the current phase:

1. **Single strategy**: only RSI mean-reversion is in `VIABLE_STRATEGIES`. If this strategy stops working, the system goes flat. No diversification across strategies.
2. **6 years of clean data, no pre-2020**: deliberate — pre-2020 data via yfinance had survivorship bias and predated material market-structure changes (T+1 settlement, retail/SIP boom).
3. **No options/derivatives**. Only delivery equity. Misses obvious hedges (Nifty puts in high-VIX) and income tools (covered calls).
4. **Paper-trade simulation simplifies real-world execution**: stop fills assume `min(open, stop)`, which is generous on big gap-downs. Slippage is a fixed 5 bps; small caps in reality can be 25-50 bps.
5. **LLM verdicts are non-deterministic**. Same chart at temperature=0.2 may differ run-to-run. We can't replay LLM calls cheaply for walk-forward.
6. **No tax modeling**. STCG (15%) vs LTCG (10% over ₹1L exemption) significantly changes net returns.
7. **Universe is current Nifty 500**. Index rebalances semi-annually; we don't track historical membership.
8. **No real-time intraday execution**. Stops checked end-of-day against bar low. Real intraday execution might fill earlier or later.
9. **No order-book / liquidity adjustment**. A ₹20K position in a microcap may move price 1-2%; we assume infinite liquidity at the close.

See `ROADMAP.md` for which of these are tracked as future work and which are "decision-stable" (not worth fixing).

---

## Tech stack

- **Python 3.12+** (managed by uv)
- **uv** for package management and venv
- **SQLite** with WAL mode (single file, ~520 MB after backfill, no separate DB server)
- **SQLAlchemy 2.0** (Core, not ORM — raw text queries with parameterized binds)
- **pandas + pandas-ta** for indicators
- **mplfinance + matplotlib** (Agg backend, headless) for chart rendering
- **OpenAI SDK** (pointed at OpenRouter for multi-provider routing)
- **OpenRouter** for LLM access (Gemini 3 flash preview verified working; ~₹450/year for 4 agents × daily watchlist)
- **nselib** for NSE data (with our `nselib_patch.py` to fix HTTP timeouts and cookie reuse)
- **yfinance** for BSE / pre-2020 fallback
- **Click + Rich + loguru** for CLI ergonomics
- **cron** for scheduling

---

## Documentation

| File          | Purpose                                                                            |
| ------------- | ---------------------------------------------------------------------------------- |
| `ACTIONS.md`  | Operator's daily playbook. What to do today (`.env`), what to run daily.           |
| `DEPLOY.md`   | Full deployment reference. Manual steps if you don't want to use `deploy.sh`.      |
| `ROADMAP.md`  | What's built, what's deferred. Tier 1/2/3 priorities with effort estimates.        |
| `deploy.sh`   | Idempotent one-shot Ubuntu VPS deploy. Run from project root.                      |
| `README.md`   | This file.                                                                         |

---

## What this project is, and is not

**It is**:
- A personal-use research tool for systematic swing trading
- A multi-agent system where LLMs do judgment and formulas do arithmetic
- A paper-trading platform with end-to-end audit trail
- Designed for one user, one ₹5L paper account, deployed on a VPS, run autonomously for a month

**It is not**:
- Financial advice
- A live trading system (no broker code shipped)
- A SaaS product or hosted service
- Backed by any guaranteed returns
- Suitable for retirement money even if results look great

The system has been validated against historical data (walk-forward Sharpe +0.69 on the surviving strategy). Past performance is not predictive. The minimum bar before considering real money is one full month of clean paper-trade history showing the same characteristics walk-forward predicted.

That's the only honest framing.
