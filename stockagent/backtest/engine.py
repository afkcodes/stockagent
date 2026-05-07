"""Long-only daily-bar backtester.

Position sizing honors the locked constraints:
- Max ₹20K per stock (20% of ₹1L capital)
- Max ₹5K risk per trade (5% of capital), driven by stop distance

Entry/exit at next-day open (avoids look-ahead). Stops checked intraday on bar low.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from loguru import logger

from stockagent.backtest.costs import CostModel
from stockagent.backtest.strategies import Strategy, StrategySignals
from stockagent.config import settings
from stockagent.data.loader import load_prices, per_symbol
from stockagent.indicators.compute import add_indicators


@dataclass
class Trade:
    symbol: str
    qty: int
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # 'signal' | 'stop' | 'eod'
    stop: float | None = None
    pnl_inr: float = 0.0
    pnl_pct: float = 0.0
    costs_inr: float = 0.0


@dataclass
class BacktestResult:
    strategy_name: str
    universe: list[str]
    start: date
    end: date
    trades: list[Trade]
    equity_curve: pd.DataFrame  # date, cash, mtm, nav
    final_nav: float
    starting_capital: float


@dataclass
class _OpenPosition:
    qty: int
    entry_price: float
    entry_date: pd.Timestamp
    stop: float
    cost_paid: float


def _size_position(
    *,
    entry_price: float,
    stop_price: float,
    capital: float,
    max_alloc_pct: float,
    max_risk_pct: float,
    cash_available: float,
) -> int:
    """Return integer qty respecting both 5% risk-per-trade and 20% allocation cap.
    Returns 0 if the trade can't be sized sensibly."""
    if entry_price <= 0 or not math.isfinite(stop_price) or stop_price >= entry_price:
        return 0
    stop_dist = entry_price - stop_price
    if stop_dist <= 0:
        return 0
    qty_by_risk = int((capital * max_risk_pct) // stop_dist)
    qty_by_alloc = int((capital * max_alloc_pct) // entry_price)
    qty_by_cash = int(cash_available // entry_price)
    return max(0, min(qty_by_risk, qty_by_alloc, qty_by_cash))


def run_backtest(
    strategy: Strategy,
    *,
    symbols: list[str],
    start: date | str,
    end: date | str,
    capital: float | None = None,
    max_alloc_pct: float | None = None,
    max_risk_pct: float | None = None,
    costs: CostModel | None = None,
    warmup_days: int = 250,
) -> BacktestResult:
    """Run `strategy` across `symbols` over [start, end]. Loads with extra warmup so
    indicators are valid at the strategy's first decision day."""
    capital = capital if capital is not None else settings.capital_inr
    max_alloc_pct = max_alloc_pct if max_alloc_pct is not None else settings.max_allocation_pct
    max_risk_pct = max_risk_pct if max_risk_pct is not None else settings.max_risk_per_trade_pct
    costs = costs or CostModel()

    start_d = pd.Timestamp(start)
    end_d = pd.Timestamp(end)
    load_start = start_d - pd.Timedelta(days=warmup_days * 1.5)

    prices = load_prices(symbols, start=load_start.date(), end=end_d.date())
    if prices.empty:
        raise ValueError("no price data for requested universe/range")

    enriched = add_indicators(prices, strategy.indicators)

    # Per-symbol signals -> a multi-index frame matching prices.
    sig_frames = []
    for sym, g in per_symbol(enriched):
        s = strategy.signals(g)
        sig_frames.append(
            pd.DataFrame(
                {
                    "entry": s.entry,
                    "exit": s.exit,
                    "stop_price": s.stop_price,
                },
                index=g.index,
            ).assign(symbol=sym).set_index("symbol", append=True).swaplevel().sort_index()
        )
    sig = pd.concat(sig_frames).sort_index()

    # Master panel: prices + signals (right-join so we have OHLCV alongside signals).
    panel = enriched.join(sig, how="left").sort_index()

    # Iterate dates within [start_d, end_d]; entries/exits use next-day open.
    all_dates = panel.index.get_level_values("date").unique().sort_values()
    in_range = all_dates[(all_dates >= start_d) & (all_dates <= end_d)]
    if len(in_range) == 0:
        raise ValueError("no trading days within requested window after warmup")

    cash = float(capital)
    open_positions: dict[str, _OpenPosition] = {}
    trades: list[Trade] = []
    equity_rows: list[dict] = []
    pending_entries: list[tuple[str, float]] = []  # symbol, stop_price; filled at next-day open
    pending_exits: list[tuple[str, str]] = []  # symbol, reason; filled at next-day open

    # Build a fast date->bar lookup
    date_grouped = {d: panel.xs(d, level="date") for d in in_range}

    prev_date = None
    for d in in_range:
        bars = date_grouped[d]  # frame indexed by symbol for this date

        # 1. Execute pending entries / exits at today's open
        if pending_entries:
            for sym, stop in pending_entries:
                if sym in open_positions or sym not in bars.index:
                    continue
                bar = bars.loc[sym]
                open_px = float(bar["open"])
                if not math.isfinite(open_px):
                    continue
                fill = costs.slip_buy(open_px)
                qty = _size_position(
                    entry_price=fill,
                    stop_price=stop,
                    capital=capital,
                    max_alloc_pct=max_alloc_pct,
                    max_risk_pct=max_risk_pct,
                    cash_available=cash,
                )
                if qty <= 0:
                    continue
                value = qty * fill
                buy_cost = costs.buy_cost(value)
                if cash < value + buy_cost:
                    continue
                cash -= value + buy_cost
                open_positions[sym] = _OpenPosition(
                    qty=qty, entry_price=fill, entry_date=d, stop=stop, cost_paid=buy_cost
                )
                trades.append(
                    Trade(symbol=sym, qty=qty, entry_date=d, entry_price=fill, stop=stop, costs_inr=buy_cost)
                )
            pending_entries = []

        if pending_exits:
            for sym, reason in pending_exits:
                pos = open_positions.get(sym)
                if pos is None or sym not in bars.index:
                    continue
                bar = bars.loc[sym]
                open_px = float(bar["open"])
                if not math.isfinite(open_px):
                    continue
                fill = costs.slip_sell(open_px)
                value = pos.qty * fill
                sell_cost = costs.sell_cost(value)
                cash += value - sell_cost
                t = trades[next(i for i in range(len(trades) - 1, -1, -1) if trades[i].symbol == sym and trades[i].exit_price is None)]
                t.exit_date = d
                t.exit_price = fill
                t.exit_reason = reason
                t.costs_inr += sell_cost
                t.pnl_inr = pos.qty * (fill - pos.entry_price) - t.costs_inr
                t.pnl_pct = (fill - pos.entry_price) / pos.entry_price
                del open_positions[sym]
            pending_exits = []

        # 2. Intraday stop checks for currently-held positions (today's bar)
        for sym in list(open_positions.keys()):
            if sym not in bars.index:
                continue
            pos = open_positions[sym]
            bar = bars.loc[sym]
            low = float(bar["low"])
            if math.isfinite(pos.stop) and low <= pos.stop:
                # Assume fill at stop (gap-down risk simplified — fills at min(open, stop))
                fill = min(float(bar["open"]), pos.stop)
                fill = costs.slip_sell(fill)
                value = pos.qty * fill
                sell_cost = costs.sell_cost(value)
                cash += value - sell_cost
                t = trades[next(i for i in range(len(trades) - 1, -1, -1) if trades[i].symbol == sym and trades[i].exit_price is None)]
                t.exit_date = d
                t.exit_price = fill
                t.exit_reason = "stop"
                t.costs_inr += sell_cost
                t.pnl_inr = pos.qty * (fill - pos.entry_price) - t.costs_inr
                t.pnl_pct = (fill - pos.entry_price) / pos.entry_price
                del open_positions[sym]

        # 3. Generate next-day pending orders from today's signals
        # Entries: signal True today, and we don't already hold the symbol
        entries_today = bars[bars["entry"] == True]  # noqa: E712
        for sym, row in entries_today.iterrows():
            if sym not in open_positions:
                stop_px = float(row["stop_price"])
                if math.isfinite(stop_px) and stop_px > 0:
                    pending_entries.append((sym, stop_px))

        # Exits: signal True today for currently-held symbols
        exits_today = bars[bars["exit"] == True]  # noqa: E712
        for sym, _ in exits_today.iterrows():
            if sym in open_positions:
                pending_exits.append((sym, "signal"))

        # 4. Mark-to-market and equity curve
        mtm = 0.0
        for sym, pos in open_positions.items():
            if sym in bars.index:
                close = float(bars.loc[sym, "close"])
                if math.isfinite(close):
                    mtm += pos.qty * close
        equity_rows.append({"date": d, "cash": cash, "mtm": mtm, "nav": cash + mtm})
        prev_date = d

    # Force-close any remaining positions at last close (eod)
    if open_positions and prev_date is not None:
        bars = date_grouped[prev_date]
        for sym, pos in list(open_positions.items()):
            if sym not in bars.index:
                continue
            close = float(bars.loc[sym, "close"])
            fill = costs.slip_sell(close)
            value = pos.qty * fill
            sell_cost = costs.sell_cost(value)
            cash += value - sell_cost
            t = trades[next(i for i in range(len(trades) - 1, -1, -1) if trades[i].symbol == sym and trades[i].exit_price is None)]
            t.exit_date = prev_date
            t.exit_price = fill
            t.exit_reason = "eod"
            t.costs_inr += sell_cost
            t.pnl_inr = pos.qty * (fill - pos.entry_price) - t.costs_inr
            t.pnl_pct = (fill - pos.entry_price) / pos.entry_price
            del open_positions[sym]

    eq = pd.DataFrame(equity_rows).set_index("date")

    return BacktestResult(
        strategy_name=strategy.name,
        universe=list(symbols),
        start=start_d.date(),
        end=end_d.date(),
        trades=trades,
        equity_curve=eq,
        final_nav=float(eq["nav"].iloc[-1]) if len(eq) else float(capital),
        starting_capital=float(capital),
    )
