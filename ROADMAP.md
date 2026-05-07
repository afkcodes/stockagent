# stockagent Roadmap

What's built, what's missing, and what would actually move the needle on profitability.

This is a living doc. The system runs end-to-end today; everything below is *enhancement*, not "fix the broken part." Honest impact estimates included.

---

## Part 1 — What's built (operational today)

### Data foundation
- **2.83M rows** of NSE EQ bhavcopy with delivery data, 2020-01-01 → present, full universe (~3,135 symbols/day, no survivorship bias)
- **Patched nselib** — fixed the no-timeout HTTP layer that was hanging us 4-5 minutes per cold call; shared session brings every call to ~0.13s
- **NSE live screens** (top gainers/losers, volume gainers, most-active, circuit hitters) via `market_movers.py`
- **Liquid-universe filter** — defines tradeable names by turnover/price/trades, available but proven inferior to Nifty 500 for our specific strategy

### Strategy + backtester
- **Long-only daily-bar engine** with realistic costs (Zerodha-equivalent: STT, exchange fees, GST, stamp, slippage 5 bps)
- **Position sizing locked** at ₹1L capital / 20% max alloc per stock / 5% max risk per trade
- **Walk-forward validation** (rolling 18mo train / 6mo test, 8 windows over 2020-2026)
- **One walk-forward survivor**: RSI mean-reversion on Nifty 500 (median Sharpe +0.69, 100% positive windows, median CAGR 16.5%)

### Agent layer
- **Multimodal technical judge** (gemini-3-flash-preview) reads a 60-day candle chart + indicator snapshot, returns structured verdict + conviction. Filters falling-knife setups while letting normal pullbacks through.
- **Coordinator** ranks signals by conviction, applies sizing constraints, persists every decision with full reasoning to `coordinator_decisions`.

### Operational
- **Paper-trade ledger** — closed-loop daily simulation with stop/signal/time exits, MTM, idempotent replay
- **`daily-tick`** — single command for the daily routine: refresh data → fetch movers → process paper trades → generate next-day watchlist
- **Discovery** — surface non-Nifty500 movers via `market-movers discover --exclude-nifty500`

---

## Part 2 — What's NOT built (originally planned)

These were in the original phased plan (memory) but didn't get built. Each has a real reason for not being done yet, mostly because the system runs without them.

### Other LLM agents (Phase 3 originally specified 4 agents; only technical exists)
- **Fundamental agent** — would evaluate P/E, ROE, debt/equity, sales/profit growth, promoter holding, pledged %. Acts as a quality filter for sub-Nifty500 names where TA alone is unreliable.
- **Sentiment/news agent** — scans recent news per symbol (Moneycontrol RSS, ET Markets) and flags concerns: regulatory issues, earnings warnings, rating downgrades. Most useful for held positions, not entry filtering.
- **Macro/sector agent** — looks at FII/DII flows, sector rotation, India VIX, broader index regime. Would inform position-sizing or whether to deploy at all on a given day.

### Operational visibility
- **Telegram bot** — push the daily watchlist + paper-trade NAV to phone every evening
- **Streamlit dashboard** — local web view of equity curve, open positions, agent reasoning, mover screens

### Strategy / portfolio
- **Trailing stops** — current stops are static. Trailing capture more upside on winners.
- **Multi-strategy ensemble** — only RSI mean-reversion lives; the framework supports more
- **Sector concentration cap** — currently 5 picks could all be financials, no diversification rule
- **Earnings calendar avoidance** — entries within 5 days of an earnings date carry massive event risk

### Live trading rail (Phase 6, gated by paper validation)
- Kite Connect / Fyers / Upstox order placement
- Reconciliation between paper picks and actual fills
- Tax-aware accounting (STCG vs LTCG)

---

## Part 3 — High-impact improvements, ranked

This is the ordered list of what would most likely improve real-money returns. Effort estimates assume current architecture.

### Tier 1 — Build these next (high impact, ≤4 hours each)

#### 1. Sector concentration cap (~1 hour)
**Problem:** 5 picks today could all be NBFCs in a Nifty PSU rally. When the sector rolls over, all 5 stop out simultaneously. Single-day -25% drawdown possible from concentration alone.
**Build:** add a `sector` column to a stock-info table (one-time scrape from NSE), add a `max_picks_per_sector=2` constraint in `coordinator.run_coordinator`. Drop the lowest-conviction pick when a sector overflows.
**Expected impact:** smoother equity curve, lower max-DD. Won't increase returns but will reduce scary drawdowns by maybe 20-30%.

#### 2. Earnings date avoidance (~2 hours)
**Problem:** RSI signal fires the day before an earnings result. Stock gaps -8% on disappointing numbers. Stop hit, locked-in 5% loss instead of letting mean-reversion play out.
**Build:** scrape NSE corporate-actions / Moneycontrol for upcoming earnings dates → `earnings_calendar` table. In the coordinator, exclude any pick where earnings fall within the next 5 trading days.
**Expected impact:** real. Earnings gaps are a sizable fraction of all stop-outs in our paper replays. Conservatively saves 5-10% of the gross stops.

#### 3. Fundamental agent (~3 hours)
**Problem:** the LLM judges based on chart only. A stock with falling fundamentals (rising debt, declining sales, fraud-pattern shareholding) might still LOOK like a clean pullback on the chart. Agent would catch that.
**Build:** scrape screener.in for each Nifty 500 name (one-time + monthly refresh), populate `fundamentals` table (already in schema). New agent module reads the snapshot and votes bullish/neutral/bearish on quality. Coordinator weights tech_verdict + fundamental_verdict.
**Expected impact:** filters maybe 5-10% of bad signals, mostly in midcap segment. Marginal on Nifty 50 (already-high quality), bigger on Nifty 500.

#### 4. Trailing stops on winners (~1-2 hours)
**Problem:** static stop means we lock in 1:2 R:R but never let a 1:5 winner breathe. Mean-reversion bounces sometimes turn into multi-week trends; we exit at our 2-ATR target and miss the rest.
**Build:** when an open position is up >+5%, switch from static stop to ATR-based trailing stop (1.5×ATR below current close). Update on each `process_day`.
**Expected impact:** reasonable. Backtester needs to be re-run to confirm, but expected lift of 10-20% on per-trade-PnL on the winning subset.

#### 5. Telegram daily alerts (~30 min)
**Problem:** you have to remember to run `daily-tick`. Easy to forget. No remote visibility.
**Build:** simple `python-telegram-bot` integration. After `daily-tick` runs, push a formatted summary (NAV, open positions, tomorrow's picks) to the configured chat ID. Cron the daily-tick at 15:40 IST.
**Expected impact:** operational, not strategy. But missing 3 days of daily-tick = missing 3 days of paper validation = real cost.

### Tier 2 — Build later (medium impact or higher complexity)

#### 6. Sentiment/news agent on held positions (~3-4 hours)
Read RSS feeds for held names, flag concerning items (rating downgrade, regulatory, earnings warning). Doesn't generate entries; surfaces "you should manually review this position now" alerts.
Cost: minimal — one LLM call per held position per day.
Expected impact: prevents a chunk of disaster losses; rarely improves returns directly.

#### 7. Multi-timeframe confluence (~2 hours)
Add weekly RSI(14) check to the entry rule. Only fire daily RSI<30 entry if weekly RSI > 40 (i.e., not in a major downtrend). This is what "the trend filter" tried to do and failed at — multi-timeframe should work better because it uses LONGER trend context.
Re-run walk-forward to confirm before shipping.

#### 8. Parameter sweep walk-forward (~2-3 hours)
Currently walk-forward validates ONE parameter set (RSI 30/60). Sweeping over (oversold ∈ [25,28,30,32], overbought ∈ [55,60,65,70]) per train window, picking the best for each test window, is a more rigorous out-of-sample test. Catches parameter overfitting and may surface better defaults.

#### 9. Macro/sector agent (~3 hours)
Reads FII/DII net flows, India VIX, sector index relative strength. Modulates `max_picks` and `total_deployed` based on regime — fewer picks in high VIX, more in low-VIX trending markets.
Effort: data is in nselib already; just need the agent prompt.
Impact: real but only in regime transitions.

#### 10. Streamlit dashboard (~2-3 hours)
Equity curve, open positions, recent trades, mover screens, agent reasoning logs. Localhost only. Replaces the CLI for daily review.
No strategy impact; just operational quality of life.

### Tier 3 — Bigger projects, defer until paper validates

#### 11. Live broker integration (Kite Connect or Fyers)
Hard-gated by ≥1-2 months of clean paper-trade history. Don't even consider before then.
Effort: 1-2 days when ready. Auth flow + order placement + reconciliation.

#### 12. F&O / options layer
Covered calls on held positions (income generation), or hedging via Nifty puts during high-VIX regimes.
Conceptually nice but adds significant complexity. Strict ROI question — does the income meaningfully beat the brokerage and tax cost?

#### 13. Pairs/relative-value strategy
Cointegrated stock pairs (e.g., HDFC Bank vs ICICI). Statistically richer than mean-reversion on raw price. Different walk-forward needed.
Long project, only worth it if RSI mean-reversion stops working.

#### 14. Reinforcement-learning position sizing
Adapt sizing based on recent drawdown and win streaks. Risk: introduces a black box. RL on sparse trading rewards is finicky and easy to overfit.
Skip unless explicitly desired.

---

## Part 4 — Honest limitations of the current system

These are real but acceptable for now. Calling them out so they're not surprises later.

1. **Single strategy.** RSI mean-reversion only. If this strategy stops working (regime change, structural shift), the whole system goes flat. No diversification across strategies.

2. **No survivorship bias for 2020+, but pre-2020 data was discarded.** We have 6 years of clean data. Strategies that need 10+ years (e.g., long-term momentum) can't be tested honestly.

3. **No options/derivatives.** Only delivery equity. Misses obvious hedges and income generation, but adds tax + complexity.

4. **Paper-trade simulation simplifies real-world.** Stop fills assume min(open, stop), which is generous on big gap-downs. Slippage is fixed 5 bps — small caps can be 25-50 bps in reality. Brokerage cost is approximate.

5. **LLM verdicts are non-deterministic.** Same chart at different temperatures may get different scores. We mitigate with temperature=0.2 but there's still noise. Walk-forward can't replay LLM calls cheaply.

6. **No tax modeling.** STCG (15%) vs LTCG (10% over ₹1L exemption) significantly changes realized returns. Paper P&L is gross.

7. **Universe is current-Nifty-500.** Index rebalances semi-annually; we don't track historical membership. For pre-rebalance backtests this introduces minor look-ahead.

8. **Single-account assumption.** All sizing assumes one paper account at ₹1L. Going to ₹10L or splitting across accounts requires re-tuning.

9. **No real-time intraday.** Stops are checked end-of-day against the bar low. Real intraday execution might fill earlier or later. Not material for swing horizons.

10. **No order-book / liquidity adjustment.** A ₹20K position in a microcap may move price 1-2%. We assume infinite liquidity at the close.

---

## Part 5 — Suggested execution order

**Now → next 2 weeks:**
1. Update `.env` model (5 minutes — see ACTIONS.md)
2. Run `daily-tick` daily for at least 10 trading days
3. Watch the picks, validate they make sense
4. Don't build anything yet — gather real data first

**Weeks 3-4 (assuming daily-tick is producing reasonable picks):**
5. Build Telegram alerts (Tier 1 #5) — 30 min, removes friction
6. Build sector concentration cap (Tier 1 #1) — biggest risk-reduction win

**Month 2 (paper P&L tracking):**
7. Earnings date avoidance (Tier 1 #2)
8. Trailing stops (Tier 1 #4)
9. Re-run walk-forward with new rules to confirm they help

**Month 2-3:**
10. Fundamental agent (Tier 1 #3)
11. Sentiment agent on held positions (Tier 2 #6)
12. Streamlit dashboard if you want better visibility

**Month 3+ (only if paper P&L is positive and stable):**
13. Live broker integration (Tier 3 #11) — start at HALF the position size

---

## Part 6 — What would make this *significantly* better, honestly

If I had to bet on the single change that would most increase profitability, it'd be one of these three:

**(a) Earnings date avoidance.** Low-effort, demonstrably reduces a known loss source. Best risk/reward on the build.

**(b) Multi-timeframe filter (weekly RSI > 40).** Replaces the failed trend-filter with a smarter one. If walk-forward confirms, this could lift Sharpe meaningfully.

**(c) The fundamental agent.** Would let us EXPAND the universe past Nifty 500 safely (the original liquid-universe experiment failed because of quality, not breadth — fundamentals fix that). Done well, this opens up smallcap mean-reversion which is where the true edge probably lives.

(a) is the safest bet, (b) the most uncertain, (c) the most ambitious.
