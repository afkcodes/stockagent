"""Performance metrics for a BacktestResult."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockagent.backtest.engine import BacktestResult


@dataclass
class Metrics:
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    num_trades: int
    win_rate_pct: float
    avg_winner_pct: float
    avg_loser_pct: float
    profit_factor: float
    avg_trade_pct: float
    exposure_pct: float
    final_nav: float
    starting_capital: float


def compute_metrics(result: BacktestResult, *, risk_free_rate: float = 0.07) -> Metrics:
    """Compute all summary metrics. risk_free_rate is annual decimal (default 7% Indian G-sec)."""
    eq = result.equity_curve
    nav = eq["nav"].astype(float)
    starting = result.starting_capital
    final = float(nav.iloc[-1]) if len(nav) else starting

    total_return = (final - starting) / starting
    n_days = max((result.end - result.start).days, 1)
    years = n_days / 365.25
    cagr = (final / starting) ** (1 / years) - 1 if years > 0 and final > 0 else 0.0

    daily_ret = nav.pct_change().dropna()
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        excess = daily_ret - (risk_free_rate / 252)
        sharpe = (excess.mean() / daily_ret.std()) * math.sqrt(252)
        downside = daily_ret[daily_ret < 0].std()
        sortino = (excess.mean() / downside) * math.sqrt(252) if downside > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    running_max = nav.cummax()
    dd = (nav - running_max) / running_max
    max_dd = float(dd.min()) if len(dd) else 0.0

    closed = [t for t in result.trades if t.exit_price is not None]
    n_trades = len(closed)
    winners = [t for t in closed if t.pnl_pct > 0]
    losers = [t for t in closed if t.pnl_pct <= 0]
    win_rate = len(winners) / n_trades if n_trades else 0.0
    avg_winner = np.mean([t.pnl_pct for t in winners]) if winners else 0.0
    avg_loser = np.mean([t.pnl_pct for t in losers]) if losers else 0.0
    avg_trade = np.mean([t.pnl_pct for t in closed]) if closed else 0.0

    gross_win = sum(t.pnl_inr for t in winners)
    gross_loss = abs(sum(t.pnl_inr for t in losers))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)

    deployed = (eq["mtm"] > 0).sum() / len(eq) if len(eq) else 0.0

    return Metrics(
        total_return_pct=total_return * 100,
        cagr_pct=cagr * 100,
        sharpe=float(sharpe),
        sortino=float(sortino),
        max_drawdown_pct=max_dd * 100,
        num_trades=n_trades,
        win_rate_pct=win_rate * 100,
        avg_winner_pct=avg_winner * 100,
        avg_loser_pct=avg_loser * 100,
        profit_factor=float(profit_factor) if math.isfinite(profit_factor) else float("inf"),
        avg_trade_pct=avg_trade * 100,
        exposure_pct=deployed * 100,
        final_nav=final,
        starting_capital=starting,
    )


REGIMES: list[tuple[str, str, str]] = [
    ("covid_crash", "2020-02-15", "2020-04-30"),
    ("recovery", "2020-05-01", "2021-12-31"),
    ("correction_2022", "2022-01-01", "2022-12-31"),
    ("bull_2023_24", "2023-01-01", "2024-09-30"),
    ("mixed_2024_26", "2024-10-01", "2026-12-31"),
]


def persist_run(result: BacktestResult, m: Metrics, *, params: dict | None = None) -> int:
    """Save the run summary into backtest_runs. Returns inserted row id."""
    import json

    from sqlalchemy import text

    from stockagent.db.session import get_engine

    sql = text(
        """
        INSERT INTO backtest_runs
            (strategy, params_json, universe, start_date, end_date,
             total_return_pct, cagr, sharpe, max_drawdown_pct, win_rate, num_trades,
             metrics_json)
        VALUES
            (:strategy, :params, :universe, :start, :end,
             :total, :cagr, :sharpe, :dd, :win, :n,
             :metrics)
        """
    )
    payload = {
        "strategy": result.strategy_name,
        "params": json.dumps(params or {}),
        "universe": ",".join(result.universe[:50]) + ("..." if len(result.universe) > 50 else ""),
        "start": str(result.start),
        "end": str(result.end),
        "total": m.total_return_pct,
        "cagr": m.cagr_pct,
        "sharpe": m.sharpe,
        "dd": m.max_drawdown_pct,
        "win": m.win_rate_pct,
        "n": m.num_trades,
        "metrics": json.dumps(m.__dict__),
    }
    engine = get_engine()
    with engine.begin() as conn:
        res = conn.execute(sql, payload)
        return res.lastrowid


def per_regime_returns(result: BacktestResult) -> pd.DataFrame:
    """Annualized return % per regime, with date coverage."""
    eq = result.equity_curve.copy()
    if eq.empty:
        return pd.DataFrame()
    eq.index = pd.to_datetime(eq.index)
    rows = []
    for name, s, e in REGIMES:
        mask = (eq.index >= pd.Timestamp(s)) & (eq.index <= pd.Timestamp(e))
        slice_ = eq[mask]
        if len(slice_) < 2:
            continue
        nav0 = float(slice_["nav"].iloc[0])
        nav1 = float(slice_["nav"].iloc[-1])
        days = (slice_.index[-1] - slice_.index[0]).days
        years = max(days / 365.25, 1e-6)
        cagr = (nav1 / nav0) ** (1 / years) - 1 if nav0 > 0 else 0.0
        rows.append({
            "regime": name,
            "start": slice_.index[0].date(),
            "end": slice_.index[-1].date(),
            "ret_pct": (nav1 / nav0 - 1) * 100,
            "cagr_pct": cagr * 100,
            "days": days,
        })
    return pd.DataFrame(rows)
