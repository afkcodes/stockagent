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

**Auto-learning (evidence-backed feedback loop)**
- Captures every closed trade (R-multiple, MAE/MFE, alpha-vs-beta regime attribution) into `trade_reviews`
- Mines win rate / expectancy / Wilson-confidence patterns and per-agent reliability
- Computes bounded conviction/size adjustments in **shadow mode** (logged, not applied — kill-switch defaults off)
- LLM post-mortems on losses, resurfaced as read-only context on similar future setups
- See "Auto-learning" section below; full design in `docs/autolearn_design.md`

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

## How historical data is used (backfill vs backtest)

Two commands trip people up because both touch price history. Plain-terms split:

**`backfill` = collect the data.**
Downloads NSE daily prices (2020→present, ~3,100 stocks) into the local SQLite DB. It doesn't trade or decide anything — it just stocks the shelves. Run once to load everything, then a little each day (`daily-tick` step 1) to add the newest bar.

**`backtest` = test a strategy on that data.**
Takes a trading rule (e.g. "buy when RSI < 30"), replays it day-by-day over the stored history, and tallies the pretend P&L. No live market, no real money — a pure simulation answering "would this rule have worked?" This is how we decided to trade *only* RSI mean-reversion (see Validated results).

**The live system is NOT independent of the data — it leans on it every day.**
To judge whether a stock is a buy *today*, `daily-tick` loads that stock's recent months of prices and computes RSI/ATR on the spot. No history → no indicators → no signals. The price data underpins everything.

**But the live system IS independent of backtest *results*.**
The backtest was a one-time judging exercise. Once it told us "RSI mean-reversion is the only survivor," that choice was hard-coded. Day to day, the live system never re-reads backtest output — it just trades the chosen strategy.

```
HISTORICAL PRICE DATA  (backfill)
        │
        ├──→ OFFLINE: backtest / walkforward  → picked "RSI mean-reversion"   (one time)
        │
        └──→ LIVE every day: compute RSI/ATR → today's signals               (forever)
```

| Aspect                | `backfill`                       | `backtest`                          |
| --------------------- | -------------------------------- | ----------------------------------- |
| What it does          | Downloads & stores past prices   | Tests a trading rule on those prices |
| Touches the market?   | Yes — fetches real data          | No — pure simulation                |
| Makes trade decisions?| No                               | Yes, but pretend ones in the past   |
| Used live?            | **Yes** — daily, to compute signals | No — offline validation only     |
| Run it…               | First, then daily to top up      | When testing/validating an idea     |

A third, related use is **`paper-replay`**: it simulates the *whole* live system (signals + sizing + exits) over a past window to build a paper track record before risking anything real — see Daily usage. Note it runs the deterministic path (no LLM), so its trades carry no agent verdicts.

---

## Auto-learning (evidence-backed feedback loop)

The base system has **no memory of its own trades** — it judges each day from scratch. The auto-learning layer (`stockagent/learn/`) gives it that memory: it records every closed trade, mines evidence-backed patterns from the wins and losses, and feeds that evidence back into future decisions. Full design in [`docs/autolearn_design.md`](./docs/autolearn_design.md).

**Guiding principle — the same "heartless" ethos as the combine step:**

> **Statistics (deterministic, evidence-backed) move the arithmetic. LLMs only narrate and hypothesize.** Every self-adjustment is written to the DB with the evidence behind it, and is reversible / shadow-able.

So conviction and sizing are only ever moved by mined statistics with a confidence test — never directly by an LLM. The LLM's role is to write human-readable post-mortems, which enrich judgment but never touch the math.

### The five layers

| Phase | Layer | What it does | Behaviour change | Status |
| ----- | ----- | ------------ | ---------------- | ------ |
| 1 | **Capture** | On each close, snapshot the frozen decision context + realized outcome into `trade_reviews` (R-multiple, MAE/MFE, alpha-vs-beta regime attribution) | none (pure data) | ✅ built |
| 2 | **Mine** | Aggregate reviews into `learned_patterns` + `agent_reliability` — win rate, expectancy (R), profit factor, Wilson-CI confidence per bucket | none (read-only) | ✅ built |
| 3 | **Shadow** | Per live candidate, compute a bounded conviction/size multiplier from active patterns and log it to `decision_adjustments` — **applies nothing** | none (logged only) | ✅ built |
| 4 | **Activate** | Flip `autolearn_active`: the logged multipliers actually move conviction + size | **live** | ⏸ deliberately deferred |
| 5 | **Reflect** | An analyst LLM writes a structured lesson on each loss into `trade_lessons`; matching lessons resurface as read-only context to the agents on similar future setups | enriched context | ✅ built |

### Key metrics it computes (none existed before)

- **R-multiple** = `realized_pnl / initial_risk` — normalizes every trade to "units of risk made/lost", so a small win on a tight stop and a big win on a wide stop are comparable. The single most important learning signal.
- **MAE / MFE** — max adverse / favorable excursion during the hold ("was it deep underwater before it worked?").
- **Regime attribution** — index & sector return over the *same* window, so a loss in a −8% market (beta) isn't punished like a loss while the market rose (alpha). Without this the system would learn to hate good strategies caught in a bad tape.

### Guardrails (what separates learning from overfitting to noise)

- **No look-ahead** — patterns applied on day D use only trades closed before D.
- **Min sample + confidence** — a bucket is `is_active` only with ≥ `autolearn_min_n` trades AND a Wilson-CI test that it confidently beats (or trails) a coin flip. Thin/ambiguous buckets influence nothing.
- **Bounded multipliers** — conviction ∈ [0.5, 1.3], size ∈ [0.5, 1.5]; no single thin pattern can dominate.
- **Shadow-first + kill-switch** — `settings.autolearn_active` defaults **off**. The loop computes and logs against real picks until the log proves the adjustments would raise expectancy. Until then, **picks are byte-for-byte unchanged.**

### Why Phase 4 is not flipped yet

Activation is gated on real validation. The current corpus is ~368 *replay-experiment* trades (`source='backtest'`, deterministic, no agent verdicts) — useful bootstrap priors, not live edge. Flipping the switch on that would teach the system an artifact. The honest bottleneck is **trade volume**: the loop becomes statistically meaningful only after dozens of *live* trades accrue, at which point the shadow log is reviewed before trusting it with capital.

### CLI

```bash
uv run stockagent learn backfill    # snapshot closed trades → trade_reviews
uv run stockagent learn mine        # recompute learned_patterns + agent_reliability
uv run stockagent learn patterns    # inspect mined patterns (active first)
uv run stockagent learn shadow      # inspect the decision_adjustments audit log
uv run stockagent learn reflect     # LLM post-mortems on recent losses → trade_lessons
uv run stockagent learn lessons     # read the mined lessons
uv run stockagent learn report      # summarize the trade_reviews corpus
```

New tables: `trade_reviews`, `learned_patterns`, `agent_reliability`, `decision_adjustments`, `trade_lessons`. All decision lineage (`run_id` → `agent_outputs` → `coordinator_decisions` → `paper_trades`) already existed — the learning loop just reads it back.

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

For the auto-learning loop (see "Auto-learning" section):

```bash
uv run stockagent learn backfill    # snapshot closed trades → trade_reviews
uv run stockagent learn mine        # recompute patterns + agent reliability
uv run stockagent learn patterns    # inspect mined patterns (active first)
uv run stockagent learn shadow      # inspect the decision_adjustments audit log
uv run stockagent learn reflect     # LLM post-mortems on recent losses
uv run stockagent learn lessons     # read the mined lessons
```

---

## Telegram notifications

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`, every weekday at 16:30 IST you receive an HTML-formatted summary message. Format below.

### Typical day — new picks generated

```
📈 stockagent daily — 2026-05-12
NAV ₹5,01,234  (+0.25% from start)
🟢 day P&L ₹+1,234
open: 3  fills: 2  exits: stop=0 sig=1 time=0

Tomorrow's watchlist (4 picks)
1. RELIANCE  (Energy)
   ₹1,435.00 → stop ₹1,330.50 → tgt ₹1,644.00  R:R 1:2.0
   qty 70  ₹100,450  conv 0.61
2. TCS  (IT)
   ₹2,403.20 → stop ₹2,229.65 → tgt ₹2,750.30  R:R 1:2.0
   qty 41  ₹98,531  conv 0.58
3. PATANJALI  (FMCG)
   ₹529.75 → stop ₹488.48 → tgt ₹612.29  R:R 1:2.0
   qty 188  ₹99,593  conv 0.55
4. ZYDUSLIFE  (Pharma)
   ₹1,034.80 → stop ₹952.40 → tgt ₹1,199.60  R:R 1:2.0
   qty 96  ₹99,341  conv 0.52
```

### Day with no qualifying signals

```
📈 stockagent daily — 2026-05-13
NAV ₹5,01,234  (+0.25% from start)
🟢 day P&L ₹+0
open: 3  fills: 0  exits: stop=0 sig=0 time=0

No qualifying signals today.
```

This is normal — RSI<30 doesn't fire on every name every day, and the agent council vetoes borderline candidates.

### Bad day — stop-outs hit

```
📈 stockagent daily — 2026-05-15
NAV ₹4,89,200  (-2.16% from start)
🔴 day P&L ₹-3,250
open: 1  fills: 0  exits: stop=2 sig=0 time=0

Tomorrow's watchlist (1 pick)
1. INFY  (IT)
   ₹1,152.10 → stop ₹1,037.36 → tgt ₹1,381.59  R:R 1:2.0
   qty 86  ₹99,081  conv 0.54
```

### Field reference

| Line                          | Meaning                                                                       |
| ----------------------------- | ----------------------------------------------------------------------------- |
| `NAV ₹X,XX,XXX (+Y.YY%)`     | Total portfolio value vs starting capital                                     |
| `🟢 / 🔴 day P&L`            | Today's mark-to-market gain or loss in ₹                                       |
| `open: N`                     | Currently held positions (excluding any closed today)                         |
| `fills: N`                    | New positions opened today at market open                                     |
| `exits: stop=X sig=Y time=Z` | Position closures: stop-loss hit, exit-signal fired, or 30-day time stop      |
| `(Sector)`                    | Auto, Bank, FMCG, IT, Pharma, Metal, Energy, Consumer Durables, etc.          |
| `R:R 1:2.0`                   | Risk-reward — target is 2× the stop distance from entry                       |
| `conv 0.XX`                   | Combined conviction from the 4-agent council (0.0 to 1.0)                     |

### What you do NOT receive

- **Intra-day messages.** Cron runs once at 16:30 IST. No real-time alerts on open positions.
- **Weekend messages.** Cron is Mon-Fri only.
- **Error alerts.** Failures go to `~/stockagent/logs/daily-tick.log`. If you stop receiving Telegram messages for 2+ trading days, that's the signal to check logs.
- **Per-fill confirmations.** Trades fill silently; the daily summary shows the count.
- **Deep agent reasoning.** The push is concise. For full LLM reasoning per pick, run `stockagent symbol-profile --symbol XYZ` on the VPS.

Expected volume: **~22 messages over a 30-day trial** (5 weekdays × ~4.4 weeks). Easy to spot anomalies by skimming.

### Manual push (for spot-checks)

```bash
# Force a fresh daily-tick anytime — also re-pushes Telegram
uv run stockagent daily-tick --skip-bhav-refresh --skip-movers

# Or send a one-off message
uv run python -c "
import stockagent
from stockagent.alerts.telegram import send_telegram
send_telegram('Quick check — system alive')
"
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
│   ├── learn/                 # auto-learning feedback loop (see docs/autolearn_design.md)
│   │   ├── metrics.py         # R-multiple, MAE/MFE, regime attribution
│   │   ├── capture.py         # snapshot closed trades → trade_reviews
│   │   ├── mine.py            # aggregate → learned_patterns + agent_reliability
│   │   ├── apply.py           # shadow conviction/size multipliers → decision_adjustments
│   │   └── reflect.py         # LLM loss post-mortems → trade_lessons
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
| `docs/autolearn_design.md` | Full design of the auto-learning feedback loop (5 layers, data model, guardrails). |
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
