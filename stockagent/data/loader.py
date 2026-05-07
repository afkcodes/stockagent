"""Load OHLCV from SQLite into pandas DataFrames in shapes the rest of the pipeline expects."""
from __future__ import annotations

from datetime import date
from typing import Iterable

import pandas as pd
from sqlalchemy import text

from stockagent.db.session import get_engine

_DEFAULT_COLS = ("open", "high", "low", "close", "volume", "deliverable_pct")


def load_prices(
    symbols: str | Iterable[str] | None = None,
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    exchange: str = "NSE",
    columns: tuple[str, ...] = _DEFAULT_COLS,
) -> pd.DataFrame:
    """Long-format frame indexed by (symbol, date) ascending. Empty if no data."""
    where = ["exchange = :exchange"]
    params: dict = {"exchange": exchange}

    if isinstance(symbols, str):
        symbols = [symbols]
    if symbols is not None:
        syms = list(symbols)
        if not syms:
            return pd.DataFrame(columns=list(columns)).set_index(
                pd.MultiIndex.from_arrays([[], []], names=["symbol", "date"])
            )
        keys = [f":s{i}" for i in range(len(syms))]
        where.append(f"symbol IN ({', '.join(keys)})")
        for i, s in enumerate(syms):
            params[f"s{i}"] = s

    if start:
        where.append("date >= :start")
        params["start"] = str(start)
    if end:
        where.append("date <= :end")
        params["end"] = str(end)

    cols_sql = ", ".join(columns)
    sql = text(
        f"SELECT symbol, date, {cols_sql} FROM prices "
        f"WHERE {' AND '.join(where)} ORDER BY symbol, date"
    )

    engine = get_engine()
    df = pd.read_sql(sql, engine, params=params)
    if df.empty:
        return df.set_index(["symbol", "date"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index(["symbol", "date"]).sort_index()


def pivot_close(df: pd.DataFrame, column: str = "close") -> pd.DataFrame:
    """Pivot long → wide: rows are dates, columns are symbols, values are `column`."""
    return df[column].unstack(level="symbol").sort_index()


def trading_days(df: pd.DataFrame) -> pd.DatetimeIndex:
    return df.index.get_level_values("date").unique().sort_values()


def per_symbol(df: pd.DataFrame) -> Iterable[tuple[str, pd.DataFrame]]:
    """Iterate (symbol, single-symbol date-indexed frame)."""
    for sym, g in df.groupby(level="symbol", sort=False):
        yield sym, g.droplevel("symbol").sort_index()
