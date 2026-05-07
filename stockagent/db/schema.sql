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
