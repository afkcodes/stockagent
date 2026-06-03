# Auto-Learning / Evidence-Backed Feedback Loop — Design

**Status:** Design (pre-implementation)
**Author:** drafted with Claude, 2026-06-03
**Target base commit:** `f5a06c3`

---

## 1. Goal & non-goals

**Goal.** Give the system a memory of its own trades so it learns from mistakes,
references that evidence on future similar setups, and tilts conviction + position
size toward higher-expectancy trades — to compound realized profit over time.

**Non-goals.**
- Not replacing the deterministic `combine()` formula with a black box. The combine
  step stays a pure formula; learning enters as a **separate, logged, reversible
  adjustment layer** after it.
- Not promising fast P&L lift. The binding constraint is **trade volume**, not design.
  This is a compounding investment that becomes statistically meaningful only after
  dozens of closed trades accrue. See §9 (Honest risk assessment).

### Guiding principle

> **Statistics (deterministic, evidence-backed) move the arithmetic.
> LLMs only narrate and hypothesize.**
> Every self-adjustment is written to the DB with the evidence behind it, and is
> reversible / shadow-able.

This preserves the project's core philosophy (README): *LLMs judge, a pure formula
decides, picks are reproducible and auditable.*

---

## 2. Why this fits the existing architecture

Every decision already has a complete, traceable lineage — nothing about *why* a
trade was taken is lost; it is simply never read back:

```
agent_outputs(run_id, symbol, agent, verdict, conviction, structured_json)
        │  (run_id)
coordinator_decisions(id, run_id, symbol, conviction, entry, stop, target, qty, …)
        │  (id → decision_id)
paper_trades(id, decision_id, symbol, entry_price/date, exit_price/date, exit_reason, pnl, status)
        │  (date)
portfolio_state(date, nav, cash, deployed, day_pnl)
```

The learning loop reads this lineage back, scores outcomes, and feeds evidence
forward into the next decision. The hooks it needs already exist.

**Key existing facts (with anchors):**
- Final conviction is set at `coordinator.py:178` (`combined.conviction`).
- Position sizing happens at `coordinator.py:194` (`_size_position`).
- `agent_disagreement` is **currently hardcoded `0.0`** at `coordinator.py:259` —
  a bug we fix in Phase 1 (the real value exists on `CombinedVerdict.disagreement`).
- Trade exits (where outcomes become known) are written in
  `paper_trade/ledger.py` `process_day()`, step 1 (`ledger.py:339-405`).

---

## 3. The five layers

### Layer 1 — Capture (freeze evidence at decision + outcome)

When a trade **closes**, write one `trade_reviews` row joining the *frozen decision
context* to the *realized outcome*. Context is snapshotted so it is immune to later
price/indicator revisions.

Computed metrics (none of these exist today):

- **R-multiple** = `realized_pnl / initial_risk_inr`, where
  `initial_risk_inr = (entry_price − stop_loss) × qty`.
  The single most important learning signal — normalizes every trade to "units of
  risk made/lost," so a ₹500 win on a tight stop and a ₹5,000 win on a wide stop are
  comparable.
- **MAE / MFE** (Maximum Adverse / Favorable Excursion) — the worst and best
  unrealized P&L during the hold, from daily OHLCV between entry_date and exit_date.
  Tells us "was it deep underwater before it worked?" / "did we exit too early?"
- **Regime attribution** — index return and sector return over the *same* holding
  window. Separates *alpha* loss (our pick was bad) from *beta* loss (the whole
  market fell). A losing pick during a −8% market crash is **not** the same mistake
  as a losing pick while the market rose. Without this, the system would "learn" to
  hate good strategies that happened to run in a bad tape.

### Layer 2 — Mine (outcomes → evidence-backed patterns)

A deterministic aggregation job (no LLM). Two products:

**(a) Agent reliability — built FIRST, most data-efficient.**
Pools across *all* trades, so it reaches significance fastest:
> "When `technical` says bullish at conviction > 0.7, what is the realized win rate
> and avg R-multiple?"
Output → `agent_reliability` table. This is what lets us recalibrate the hardcoded
agent weights (1.5 / 1.0 / 0.7 / 0.5) with evidence instead of priors.

**(b) Pattern buckets — comes online as data accrues.**
Bucket closed trades by feature and compute stats into `learned_patterns`:
- Buckets: sector, RSI-entry band, ATR%/volatility band, macro regime
  (VIX state × Nifty trend), conviction band, agent-agreement profile, R:R band,
  market-cap band.
- Per bucket: **win rate, avg R-multiple, expectancy, profit factor, n,** and a
  **confidence measure (Wilson lower bound / t-stat)** so we never act on noise.

**Alternative kept open (not committed):** a single regression predicting R-multiple
from features uses thin data more efficiently than disjoint buckets, at the cost of
interpretability. We start with buckets+reliability (interpretable, matches the
"auditable" ethos); revisit regression if buckets stay data-starved.

### Layer 3 — Apply (close the loop: conviction tilt + sizing, logged)

**Chosen influence mode: conviction tilt + position sizing (no hard gates initially).**

After `combine()` produces conviction (at `coordinator.py:178`), a new
`apply_learned_adjustments()` step:
1. Looks up which active `learned_patterns` / `agent_reliability` rows the candidate
   matches (only patterns meeting min-n + confidence thresholds).
2. Computes a **conviction multiplier** and a **size multiplier**, each bounded
   (e.g. conviction ∈ [0.5, 1.3], size ∈ [0.5, 1.5]) to prevent any single thin
   pattern from dominating.
3. **Writes the adjustment + human-readable reason to `decision_adjustments`**, e.g.:
   > `INFY: matched loss-pattern {RSI<20 ∧ VIX-panic ∧ smallcap}: expectancy −0.4R
   > over 14 trades (Wilson LB 71%) → conviction ×0.62, size ×0.75`

This is auditable, *and* the logged adjustment becomes future training data (did the
penalty help?). No hard veto for now — marginal trades are down-weighted, not blocked.
(Hard gates remain a future toggle; deliberately deferred.)

**Shadow mode** (default ON at first): the adjustments are computed and logged, but
the *applied* conviction/size still use the un-adjusted values. A flag flips to
activate. We validate that shadow adjustments would have improved expectancy before
trusting them with live capital.

### Layer 4 — Reflect (LLM lessons, stats-led)

**Chosen: include, but stats never bend to the LLM.**

For each loss (or a batch), an LLM "analyst" reviews the frozen context + outcome and
writes a structured lesson into `trade_lessons`:
- hypothesis: which signal misled us, what regime, what to watch for.
- Stored with the bucket keys so it is retrievable.

On future *similar* setups, matching lessons are surfaced as **read-only context** to
the agents ("3 past losses in similar setups; lesson: …"). The lessons enrich
judgment and human review; they **never** directly change conviction/size arithmetic
— only the deterministic stats do that. Adds LLM API cost per loss (bounded, since
losses are infrequent).

---

## 4. Data model (new tables)

```sql
-- One row per CLOSED trade. The learning corpus.
CREATE TABLE IF NOT EXISTS trade_reviews (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER NOT NULL REFERENCES paper_trades(id),
    decision_id         INTEGER REFERENCES coordinator_decisions(id),
    run_id              TEXT,
    symbol              TEXT NOT NULL,
    sector              TEXT,
    source              TEXT NOT NULL DEFAULT 'live',   -- 'live' | 'backtest'
    -- outcome
    entry_date          TEXT,
    exit_date           TEXT,
    holding_days        INTEGER,
    exit_reason         TEXT,                           -- stop | time | signal | target | manual
    pnl_inr             REAL,
    pnl_pct             REAL,
    initial_risk_inr    REAL,                           -- (entry-stop)*qty
    r_multiple          REAL,                           -- pnl_inr / initial_risk_inr
    mae_pct             REAL,                           -- max adverse excursion (worst unrealized %)
    mfe_pct             REAL,                           -- max favorable excursion (best unrealized %)
    -- regime attribution (beta vs alpha)
    index_ret_pct       REAL,                           -- index return over hold window
    sector_ret_pct      REAL,                           -- sector return over hold window
    excess_ret_pct      REAL,                           -- trade pnl_pct - index_ret_pct
    -- frozen decision context
    conviction          REAL,
    disagreement        REAL,
    rsi_entry           REAL,
    atr_pct_entry       REAL,
    rr_ratio            REAL,
    vix_state           TEXT,
    nifty_trend         TEXT,
    market_cap_band     TEXT,
    context_json        TEXT,                           -- full frozen snapshot (agents+evidence)
    -- labels
    is_win              INTEGER,                        -- pnl_inr > 0
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_id)
);
CREATE INDEX IF NOT EXISTS idx_treview_symbol ON trade_reviews(symbol);
CREATE INDEX IF NOT EXISTS idx_treview_source ON trade_reviews(source);

-- Aggregated agent reliability (Layer 2a). Recomputed on a rolling window.
CREATE TABLE IF NOT EXISTS agent_reliability (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,
    condition       TEXT NOT NULL,        -- e.g. 'bullish@conv>0.7'
    n               INTEGER,
    win_rate        REAL,
    avg_r           REAL,
    expectancy      REAL,
    wilson_lb       REAL,                 -- lower bound of win-rate CI
    window_start    TEXT,
    window_end      TEXT,
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent, condition)
);

-- Aggregated outcome patterns (Layer 2b).
CREATE TABLE IF NOT EXISTS learned_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key     TEXT NOT NULL,        -- canonical bucket id, e.g. 'sector=IT|rsi=<20|vix=panic'
    description     TEXT,                 -- human-readable
    n               INTEGER,
    win_rate        REAL,
    avg_r           REAL,
    expectancy      REAL,
    profit_factor   REAL,
    wilson_lb       REAL,
    conviction_mult REAL,                 -- bounded multiplier this pattern implies
    size_mult       REAL,
    is_active       INTEGER DEFAULT 0,    -- only true if n>=min and confidence>=thresh
    window_start    TEXT,
    window_end      TEXT,
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pattern_key)
);

-- Audit log of every adjustment applied (or shadow-computed) per live decision.
CREATE TABLE IF NOT EXISTS decision_adjustments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT,
    symbol              TEXT,
    base_conviction     REAL,
    adj_conviction      REAL,
    conviction_mult     REAL,
    size_mult           REAL,
    matched_patterns    TEXT,             -- JSON list of pattern_keys + reasons
    shadow              INTEGER DEFAULT 1, -- 1 = computed-but-not-applied
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- LLM narrative lessons (Layer 4). Read-only context for future setups.
CREATE TABLE IF NOT EXISTS trade_lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_review_id INTEGER REFERENCES trade_reviews(id),
    symbol          TEXT,
    pattern_key     TEXT,                 -- for retrieval on similar setups
    lesson          TEXT,                 -- structured hypothesis
    model           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 5. Guardrails (what separates learning from overfitting to noise)

1. **No look-ahead leakage.** When applying patterns on date D, only use trades
   *closed before* D. Patterns are recomputed on a rolling, recency-weighted window.
2. **Minimum sample size + confidence.** A pattern/reliability row is `is_active=1`
   only if `n >= MIN_N` (start ~8–10) **and** its Wilson lower bound clears a
   threshold. Below that it is recorded but not applied.
3. **Bounded multipliers.** conviction ∈ [0.5, 1.3], size ∈ [0.5, 1.5]; no single
   thin pattern can dominate.
4. **Regime attribution.** `excess_ret_pct` lets us learn from *alpha*, not punish a
   good pick caught in a market-wide drop.
5. **Shadow mode first.** Compute + log, don't apply, until validated. Kill-switch
   flag always present (`settings.autolearn_active`).
6. **Re-validation.** Periodic backtest confirms the loop raises expectancy
   out-of-sample, not just in-sample win rate.

---

## 6. Integration points (file-by-file)

| Area | File | Change |
|---|---|---|
| Schema | `stockagent/db/schema.sql` | Add the 5 tables above |
| Capture | `stockagent/paper_trade/ledger.py` | After an exit is written (`ledger.py:390-402`), call `record_trade_review(trade_id, …)` |
| Metrics | `stockagent/learn/metrics.py` (new) | `r_multiple`, `mae_mfe`, `regime_attribution` from daily bars |
| Capture impl | `stockagent/learn/capture.py` (new) | `record_trade_review()`, `backfill_reviews()` |
| Mine | `stockagent/learn/mine.py` (new) | `recompute_agent_reliability()`, `recompute_patterns()` |
| Apply | `stockagent/learn/apply.py` (new) | `apply_learned_adjustments(symbol, combined, ctx) -> (conv_mult, size_mult, reasons)` |
| Apply hook | `stockagent/agents/coordinator.py:178` | Multiply conviction by `conv_mult` (shadow-aware); log to `decision_adjustments`; thread `size_mult` into `_size_position` at `:194` |
| Bug fix | `stockagent/agents/coordinator.py:259` | Persist real `combined.disagreement`, not `0.0` |
| Reflect | `stockagent/learn/reflect.py` (new) | LLM lesson generation + retrieval |
| CLI | `stockagent/cli.py` | `stockagent learn backfill` / `learn mine` / `learn report` |
| Config | `stockagent/config.py` | `autolearn_active: bool=False`, `autolearn_min_n: int=8`, window length |

---

## 7. Bootstrapping the corpus

Live data is currently **0 closed trades** (8 open). To seed priors:
- `learn backfill` ingests the **backtest trade ledger** into `trade_reviews` with
  `source='backtest'`, plus any historical live closed trades.
- Backtest trades are **labeled distinctly** and may be **down-weighted** in mining,
  because backtest picks are deterministic while live picks are LLM-driven — transfer
  is imperfect. They seed priors; they do not replace live evidence.

---

## 8. Phased rollout

| Phase | Scope | Behavior change | Ships |
|---|---|---|---|
| **1. Capture** | schema + ledger hook + metrics + disagreement fix + backfill | none (pure data) | first |
| **2. Mine** | agent_reliability + patterns + `learn report` CLI | none (read-only) | second |
| **3. Shadow** | `apply_learned_adjustments` computes + logs, not applied | none to picks | third |
| **4. Activate** | flip `autolearn_active`: conviction tilt + sizing live | live | after validation |
| **5. Reflect** | LLM lessons + retrieval into prompts | enriched context | last |

Build order requested: **design doc (this) → Phase 1**.

---

## 9. Honest risk assessment

- **Architecture & capture layer: high confidence (~90%).** R-multiple, MAE/MFE,
  regime attribution, and logged-deterministic adjustments are textbook-correct and
  low-risk regardless of downstream payoff.
- **Time-to-payoff on live P&L: moderate (~45–55%).** The limiter is **trade
  volume**, not design. ≤5 picks/day + fine-grained buckets ⇒ data-starved buckets
  for months (curse of dimensionality). Mitigations baked in: build **agent
  reliability first** (data-efficient), **bootstrap from backtest**, enforce
  **min-n + confidence**, run **shadow mode** before trusting.
- **Failure mode to watch:** learning noise from thin data. Guardrails prevent acting
  on it but cannot manufacture signal that is not there yet. Expect this to compound
  over *many* months, not deliver a quick win.

---

## 10. Open questions (resolve before/within Phase 2–3)

1. Rolling window length & recency decay for mining (e.g. last 12 months, half-life
   weighting?).
2. Exact `MIN_N` and Wilson-LB thresholds for `is_active`.
3. Backtest down-weight factor in mining.
4. Whether to recalibrate agent *weights* directly, or apply per-agent *trust
   multipliers* (slower-moving, safer) — leaning trust-multipliers.
5. Regression model as an eventual replacement/supplement for disjoint buckets.
