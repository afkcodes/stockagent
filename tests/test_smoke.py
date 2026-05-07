"""Phase 1 smoke tests. Runs against a temp SQLite + a tiny live nselib fetch.

The live fetch can flake if NSE is throttling — mark with `network` and skip in CI later.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "smoke.db"
    monkeypatch.setenv("STOCKAGENT_DB_PATH", str(db))
    # Re-import settings & engine so they pick up the env override.
    import importlib
    import stockagent.config as cfg
    import stockagent.db.session as sess
    importlib.reload(cfg)
    importlib.reload(sess)
    sess.init_db()
    return db


def test_db_init_creates_tables(tmp_db: Path):
    import stockagent.db.session as sess
    from sqlalchemy import text
    with sess.get_engine().connect() as conn:
        names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
    for required in [
        "prices", "fundamentals", "news", "agent_outputs",
        "coordinator_decisions", "paper_trades", "portfolio_state",
        "backtest_runs", "backfill_errors", "config",
    ]:
        assert required in names, f"missing table {required}"


@pytest.mark.network
def test_nse_fetch_one_symbol(tmp_db: Path):
    from stockagent.data.nse import fetch_price_history, upsert_prices
    end = date.today()
    start = end - timedelta(days=15)
    df = fetch_price_history("RELIANCE", start, end)
    assert not df.empty, "expected at least one trading day in last 15 days"
    assert {"symbol", "date", "open", "high", "low", "close", "volume"}.issubset(df.columns)
    assert (df["close"] > 0).all()
    n = upsert_prices(df)
    assert n == len(df)
