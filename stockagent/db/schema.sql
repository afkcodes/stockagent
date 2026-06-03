PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS prices (
    symbol           TEXT NOT NULL,
    exchange         TEXT NOT NULL,
    date             TEXT NOT NULL,
    open             REAL,
    high             REAL,
    low              REAL,
    close            REAL,
    prev_close       REAL,
    volume           INTEGER,
    turnover         REAL,
    trades           INTEGER,
    deliverable_qty  INTEGER,
    deliverable_pct  REAL,
    series           TEXT,
    source           TEXT,
    PRIMARY KEY (symbol, exchange, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date          ON prices(date);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_date   ON prices(symbol, date);

CREATE TABLE IF NOT EXISTS fundamentals (
    symbol             TEXT NOT NULL,
    as_of_date         TEXT NOT NULL,
    market_cap         REAL,
    pe                 REAL,
    peg                REAL,
    pb                 REAL,
    roe                REAL,
    roce               REAL,
    debt_equity        REAL,
    promoter_holding   REAL,
    pledged_pct        REAL,
    sales_growth_3y    REAL,
    profit_growth_3y   REAL,
    raw_json           TEXT,
    PRIMARY KEY (symbol, as_of_date)
);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT,
    source       TEXT,
    url          TEXT UNIQUE,
    title        TEXT,
    published_at TEXT,
    body         TEXT,
    fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_news_symbol  ON news(symbol);
CREATE INDEX IF NOT EXISTS idx_news_pubdate ON news(published_at);

CREATE TABLE IF NOT EXISTS corporate_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    ex_date     TEXT NOT NULL,
    action_type TEXT,
    details     TEXT,
    UNIQUE (symbol, ex_date, action_type)
);

CREATE TABLE IF NOT EXISTS fii_dii_activity (
    date        TEXT PRIMARY KEY,
    fii_buy     REAL,
    fii_sell    REAL,
    fii_net     REAL,
    dii_buy     REAL,
    dii_sell    REAL,
    dii_net     REAL,
    raw_json    TEXT
);

CREATE TABLE IF NOT EXISTS holidays (
    date       TEXT NOT NULL,
    segment    TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (date, segment)
);

CREATE TABLE IF NOT EXISTS index_constituents (
    index_name TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    weight_pct REAL,
    PRIMARY KEY (index_name, symbol, as_of_date)
);

CREATE TABLE IF NOT EXISTS agent_outputs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    agent           TEXT NOT NULL,        -- 'technical' | 'fundamental' | 'sentiment' | 'macro'
    model           TEXT,
    prompt_version  TEXT,
    verdict         TEXT,                 -- 'bullish' | 'bearish' | 'neutral'
    conviction      REAL,                 -- 0..1
    reasoning       TEXT,
    structured_json TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_outputs_run    ON agent_outputs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_outputs_symbol ON agent_outputs(symbol);

CREATE TABLE IF NOT EXISTS coordinator_decisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    final_verdict      TEXT NOT NULL,
    conviction         REAL,
    entry              REAL,
    stop_loss          REAL,
    target             REAL,
    position_size_inr  REAL,
    qty                INTEGER,
    horizon_days       INTEGER,
    agent_disagreement REAL,
    rationale          TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_coord_run_symbol ON coordinator_decisions(run_id, symbol);

CREATE TABLE IF NOT EXISTS paper_trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id    INTEGER REFERENCES coordinator_decisions(id),
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,            -- BUY | SELL
    qty            INTEGER NOT NULL,
    entry_price    REAL,
    entry_date     TEXT,
    exit_price     REAL,
    exit_date      TEXT,
    exit_reason    TEXT,                     -- sl | target | time | manual
    pnl_inr        REAL,
    pnl_pct        REAL,
    slippage_bps   REAL,
    brokerage_inr  REAL,
    initial_stop   REAL,                     -- stop at entry, immune to trailing (for R-multiple)
    status         TEXT NOT NULL DEFAULT 'open',  -- open | closed
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);

CREATE TABLE IF NOT EXISTS portfolio_state (
    date                TEXT PRIMARY KEY,
    cash_inr            REAL NOT NULL,
    deployed_inr        REAL NOT NULL,
    open_positions_json TEXT,
    nav_inr             REAL NOT NULL,
    day_pnl_inr         REAL,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT NOT NULL,
    params_json     TEXT,
    universe        TEXT,
    start_date      TEXT,
    end_date        TEXT,
    total_return_pct REAL,
    cagr            REAL,
    sharpe          REAL,
    max_drawdown_pct REAL,
    win_rate        REAL,
    num_trades      INTEGER,
    metrics_json    TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS backfill_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT,
    exchange    TEXT,
    job         TEXT,
    error       TEXT,
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS market_movers (
    date       TEXT NOT NULL,
    category   TEXT NOT NULL,        -- most_active_value | most_active_volume | top_gainers | top_losers | volume_gainers | price_band_upper | price_band_lower
    rank       INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    ltp        REAL,
    pchange    REAL,
    volume     INTEGER,
    turnover   REAL,
    raw_json   TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, category, rank)
);
CREATE INDEX IF NOT EXISTS idx_movers_symbol ON market_movers(symbol);
CREATE INDEX IF NOT EXISTS idx_movers_date_cat ON market_movers(date, category);

-- ---------------------------------------------------------------------------
-- Auto-learning / evidence-backed feedback loop (see docs/autolearn_design.md)
-- ---------------------------------------------------------------------------

-- One row per CLOSED trade: frozen decision context joined to realized outcome.
-- This is the learning corpus. See learn/capture.py.
CREATE TABLE IF NOT EXISTS trade_reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id          INTEGER NOT NULL REFERENCES paper_trades(id),
    decision_id       INTEGER REFERENCES coordinator_decisions(id),
    run_id            TEXT,
    symbol            TEXT NOT NULL,
    sector            TEXT,
    source            TEXT NOT NULL DEFAULT 'live',   -- live | backtest
    -- outcome
    entry_date        TEXT,
    exit_date         TEXT,
    holding_days      INTEGER,
    exit_reason       TEXT,
    pnl_inr           REAL,
    pnl_pct           REAL,
    initial_risk_inr  REAL,                           -- (entry - initial_stop) * qty
    r_multiple        REAL,                           -- pnl_inr / initial_risk_inr
    mae_pct           REAL,                           -- max adverse excursion (worst unrealized %)
    mfe_pct           REAL,                           -- max favorable excursion (best unrealized %)
    -- regime attribution (alpha vs beta)
    index_ret_pct     REAL,                           -- Nifty 50 proxy return over hold window
    sector_ret_pct    REAL,                           -- sector-peer return over hold window
    excess_ret_pct    REAL,                           -- pnl_pct - index_ret_pct
    -- frozen decision context
    conviction        REAL,
    disagreement      REAL,
    rsi_entry         REAL,
    atr_pct_entry     REAL,
    rr_ratio          REAL,
    vix_state         TEXT,
    nifty_trend       TEXT,
    market_cap_band   TEXT,
    context_json      TEXT,                           -- full frozen snapshot (agents + evidence)
    -- labels
    is_win            INTEGER,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_id)
);
CREATE INDEX IF NOT EXISTS idx_treview_symbol ON trade_reviews(symbol);
CREATE INDEX IF NOT EXISTS idx_treview_source ON trade_reviews(source);

-- Aggregated agent reliability (Layer 2a). Recomputed on a rolling window.
CREATE TABLE IF NOT EXISTS agent_reliability (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent         TEXT NOT NULL,
    condition     TEXT NOT NULL,        -- e.g. 'bullish@conv>0.7'
    n             INTEGER,
    win_rate      REAL,
    avg_r         REAL,
    expectancy    REAL,
    wilson_lb     REAL,
    window_start  TEXT,
    window_end    TEXT,
    computed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent, condition)
);

-- Aggregated outcome patterns (Layer 2b).
CREATE TABLE IF NOT EXISTS learned_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key     TEXT NOT NULL,        -- canonical bucket id
    description     TEXT,
    n               INTEGER,
    win_rate        REAL,
    avg_r           REAL,
    expectancy      REAL,
    profit_factor   REAL,
    wilson_lb       REAL,
    conviction_mult REAL,
    size_mult       REAL,
    is_active       INTEGER DEFAULT 0,
    window_start    TEXT,
    window_end      TEXT,
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pattern_key)
);

-- Audit log of every adjustment applied (or shadow-computed) per live decision.
CREATE TABLE IF NOT EXISTS decision_adjustments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT,
    symbol           TEXT,
    base_conviction  REAL,
    adj_conviction   REAL,
    conviction_mult  REAL,
    size_mult        REAL,
    matched_patterns TEXT,                -- JSON list of {pattern_key, reason}
    shadow           INTEGER DEFAULT 1,   -- 1 = computed-but-not-applied
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_decadj_run ON decision_adjustments(run_id, symbol);

-- LLM narrative lessons (Layer 4). Read-only context for future setups.
CREATE TABLE IF NOT EXISTS trade_lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_review_id INTEGER REFERENCES trade_reviews(id),
    symbol          TEXT,
    pattern_key     TEXT,                 -- for retrieval on similar setups
    lesson          TEXT,
    model           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lesson_pattern ON trade_lessons(pattern_key);
