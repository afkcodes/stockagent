"""Walk-forward validation.

Splits the full date range into rolling (train, test) windows. We don't actually
re-fit parameters here yet — these baselines have no learnable parameters — but we
DO measure stability: each test window's metrics are computed independently. If a
strategy's Sharpe / DD / win-rate fluctuate wildly across windows, the baseline is
not robust and we should not trust the aggregate number.

When we add parameter sweeps later, the train window picks the params and the test
window measures their out-of-sample performance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import pandas as pd
from rich.console import Console
from rich.table import Table

from stockagent.backtest.engine import BacktestResult, run_backtest
from stockagent.backtest.metrics import Metrics, compute_metrics, per_regime_returns
from stockagent.backtest.strategies import Strategy

console = Console()


@dataclass
class Window:
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def make_windows(
    start: date,
    end: date,
    *,
    train_months: int = 18,
    test_months: int = 6,
    step_months: int | None = None,
) -> list[Window]:
    """Generate rolling windows from `start` to `end`.
    Step defaults to test_months (non-overlapping test windows)."""
    step_months = step_months or test_months
    out: list[Window] = []
    cursor = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    while True:
        train_start = cursor
        train_end = train_start + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        test_start = train_end + pd.Timedelta(days=1)
        test_end = test_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)
        if test_end > end_ts:
            break
        out.append(Window(
            train_start=train_start.date(),
            train_end=train_end.date(),
            test_start=test_start.date(),
            test_end=test_end.date(),
        ))
        cursor = cursor + pd.DateOffset(months=step_months)
    return out


@dataclass
class WindowResult:
    window: Window
    metrics: Metrics
    result: BacktestResult


def run_walkforward(
    strategy_factory,  # callable returning a fresh Strategy per window
    *,
    symbols: list[str] | None = None,
    universe_fn=None,  # Callable[[date], list[str]] — recomputed at each window's test_start
    start: date,
    end: date,
    train_months: int = 18,
    test_months: int = 6,
) -> list[WindowResult]:
    if symbols is None and universe_fn is None:
        raise ValueError("provide either symbols or universe_fn")
    windows = make_windows(start, end, train_months=train_months, test_months=test_months)
    if not windows:
        raise ValueError("no windows fit in the requested range")

    out: list[WindowResult] = []
    for w in windows:
        strategy = strategy_factory()
        syms = symbols if symbols is not None else universe_fn(w.test_start)
        if not syms:
            continue
        res = run_backtest(strategy, symbols=syms, start=w.test_start, end=w.test_end)
        m = compute_metrics(res)
        # Stash universe size on the result for diagnostics
        res.universe = syms[:5] + ([f"... +{len(syms)-5} more"] if len(syms) > 5 else [])
        out.append(WindowResult(window=w, metrics=m, result=res))
    return out


def summarize(results: list[WindowResult]) -> dict:
    """Aggregate stats: median, std, % positive — the stability signal."""
    if not results:
        return {}
    rets = [r.metrics.total_return_pct for r in results]
    cagrs = [r.metrics.cagr_pct for r in results]
    sharpes = [r.metrics.sharpe for r in results]
    dds = [r.metrics.max_drawdown_pct for r in results]
    wins = [r.metrics.win_rate_pct for r in results]
    pos = sum(1 for r in rets if r > 0) / len(rets)
    return {
        "n_windows": len(results),
        "ret_median": pd.Series(rets).median(),
        "ret_mean": pd.Series(rets).mean(),
        "ret_std": pd.Series(rets).std(),
        "ret_min": min(rets),
        "ret_max": max(rets),
        "cagr_median": pd.Series(cagrs).median(),
        "sharpe_median": pd.Series(sharpes).median(),
        "sharpe_min": min(sharpes),
        "sharpe_max": max(sharpes),
        "dd_worst": min(dds),
        "win_median": pd.Series(wins).median(),
        "pct_positive_windows": pos * 100,
    }


def render_walkforward(strategy_name: str, results: list[WindowResult]) -> None:
    """Pretty per-window + aggregate table."""
    t = Table(title=f"Walk-forward: {strategy_name}", show_lines=False)
    t.add_column("Window")
    t.add_column("Test range")
    t.add_column("Ret %", justify="right")
    t.add_column("CAGR %", justify="right")
    t.add_column("Sharpe", justify="right")
    t.add_column("Max DD %", justify="right")
    t.add_column("Trades", justify="right")
    t.add_column("Win %", justify="right")

    for i, r in enumerate(results, 1):
        t.add_row(
            str(i),
            f"{r.window.test_start} → {r.window.test_end}",
            f"{r.metrics.total_return_pct:+.2f}",
            f"{r.metrics.cagr_pct:+.2f}",
            f"{r.metrics.sharpe:+.2f}",
            f"{r.metrics.max_drawdown_pct:+.2f}",
            str(r.metrics.num_trades),
            f"{r.metrics.win_rate_pct:.1f}",
        )
    console.print(t)

    s = summarize(results)
    if not s:
        return
    console.print(
        f"\n[bold]Aggregate ({s['n_windows']} windows)[/]\n"
        f"  Return:    median={s['ret_median']:+.2f}%   mean={s['ret_mean']:+.2f}%   "
        f"stdev={s['ret_std']:.2f}   range=[{s['ret_min']:+.2f}%, {s['ret_max']:+.2f}%]\n"
        f"  CAGR:      median={s['cagr_median']:+.2f}%\n"
        f"  Sharpe:    median={s['sharpe_median']:+.2f}   range=[{s['sharpe_min']:+.2f}, {s['sharpe_max']:+.2f}]\n"
        f"  Worst DD:  {s['dd_worst']:+.2f}%\n"
        f"  Win rate:  median={s['win_median']:.1f}%\n"
        f"  Positive windows: [bold]{s['pct_positive_windows']:.0f}%[/]"
    )
