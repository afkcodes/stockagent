"""Daily signal generator.

Runs each viable strategy on the most recent bar of every symbol in the requested
universe and emits today's candidate signals (one row per symbol per strategy hit).
Output is written to a long-format DataFrame for the coordinator to consume.

We only ship strategies that survived walk-forward — currently `rsi_mean_reversion`
on Nifty 500. Adding a new strategy is a one-line registry entry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd
from loguru import logger

from stockagent.backtest.strategies import STRATEGIES, Strategy
from stockagent.data.loader import load_prices, per_symbol
from stockagent.indicators.compute import add_indicators


# Strategies cleared by walk-forward (median Sharpe > 0 AND >50% positive windows).
VIABLE_STRATEGIES: dict[str, dict] = {
    "rsi_mean_reversion": {
        "universe": "Nifty 500",
        "wf_median_sharpe": 0.69,
        "wf_pct_positive": 100.0,
        "wf_median_cagr_pct": 16.5,
    },
}


@dataclass
class Signal:
    symbol: str
    strategy: str
    bar_date: pd.Timestamp
    entry_price: float
    stop_price: float
    rationale: str
    indicator_snapshot: dict


def _last_bar_signal(df: pd.DataFrame, strat: Strategy) -> Signal | None:
    """If the strategy says enter on the most-recent bar, build a Signal record."""
    sig = strat.signals(df)
    if not bool(sig.entry.iloc[-1]):
        return None
    last = df.iloc[-1]
    stop_px = float(sig.stop_price.iloc[-1]) if pd.notna(sig.stop_price.iloc[-1]) else float("nan")
    if not math.isfinite(stop_px) or stop_px <= 0 or stop_px >= float(last["close"]):
        return None
    snapshot = {k: float(v) for k, v in last.items() if pd.notna(v) and isinstance(v, (int, float))}
    return Signal(
        symbol=str(df.index.name) if df.index.name == "date" else "",  # filled by caller
        strategy=strat.name,
        bar_date=df.index[-1],
        entry_price=float(last["close"]),
        stop_price=stop_px,
        rationale=_rationale(strat, last, snapshot),
        indicator_snapshot=snapshot,
    )


def _rationale(strat: Strategy, last: pd.Series, snapshot: dict) -> str:
    if strat.name.startswith("rsi_mean_reversion"):
        return f"RSI(14)={last.get('rsi14', float('nan')):.1f} crossed below {strat.oversold}; ATR={last.get('atr14', float('nan')):.2f}"
    if strat.name.startswith("ema_crossover"):
        return f"EMA{strat.fast}={last.get(f'ema{strat.fast}', float('nan')):.2f} > EMA{strat.slow}={last.get(f'ema{strat.slow}', float('nan')):.2f}"
    if strat.name.startswith("bollinger_breakout"):
        return f"close={last.get('close', float('nan')):.2f} > BB_upper={last.get('bb_upper', float('nan')):.2f} on volume confirmation"
    return f"{strat.name} signal"


def generate_signals(
    *,
    symbols: list[str],
    as_of: date,
    strategies: list[str] | None = None,
    warmup_days: int = 250,
) -> list[Signal]:
    """Run viable strategies and return all entry signals on `as_of`.
    `as_of` should be a real trading day in the prices table; signals reflect that bar's close."""
    strategies = strategies or list(VIABLE_STRATEGIES.keys())
    end_ts = pd.Timestamp(as_of)
    load_start = (end_ts - pd.Timedelta(days=int(warmup_days * 1.5))).date()

    prices = load_prices(symbols, start=load_start, end=as_of)
    if prices.empty:
        logger.warning("no price data for requested as_of date")
        return []

    out: list[Signal] = []
    for strat_name in strategies:
        cls = STRATEGIES[strat_name]
        strat = cls()
        enriched = add_indicators(prices, strat.indicators)
        for sym, g in per_symbol(enriched):
            # Trim to data up to & including as_of (no look-ahead; useful when called for backtest replay).
            g = g.loc[:end_ts]
            if g.empty or g.index[-1] != end_ts:
                continue
            s = _last_bar_signal(g, strat)
            if s is None:
                continue
            s.symbol = sym
            out.append(s)
    return out


def latest_trading_day_in_db() -> date | None:
    """Most recent date present in `prices`. Convenience for `--as-of` defaulting."""
    from sqlalchemy import text
    from stockagent.db.session import get_engine
    engine = get_engine()
    with engine.connect() as c:
        row = c.execute(text("SELECT MAX(date) FROM prices")).scalar()
    if not row:
        return None
    return pd.Timestamp(row).date()
