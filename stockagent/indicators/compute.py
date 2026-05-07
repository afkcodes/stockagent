"""Technical indicators via pandas-ta. Operates on single-symbol or multi-symbol frames."""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd
import pandas_ta as ta


def _safe_assign(g: pd.DataFrame, col: str, series) -> None:
    """pandas-ta returns None (not NaN-filled Series) when input is too short.
    Coerce to a NaN-filled float column so downstream `<=` etc. don't blow up."""
    if series is None:
        g[col] = np.nan
    else:
        g[col] = pd.to_numeric(series, errors="coerce")


def _add_to_single(g: pd.DataFrame, indicators: Iterable[str]) -> pd.DataFrame:
    """g: date-indexed single-symbol frame with at least open/high/low/close/volume."""
    g = g.sort_index().copy()
    inds = set(indicators)

    for name in inds:
        if name.startswith("ema"):
            _safe_assign(g, name, ta.ema(g["close"], length=int(name[3:])))
        elif name.startswith("sma"):
            _safe_assign(g, name, ta.sma(g["close"], length=int(name[3:])))
        elif name == "rsi14":
            _safe_assign(g, "rsi14", ta.rsi(g["close"], length=14))
        elif name == "atr14":
            _safe_assign(g, "atr14", ta.atr(g["high"], g["low"], g["close"], length=14))
        elif name == "macd":
            m = ta.macd(g["close"])
            for col, idx in [("macd", 0), ("macd_signal", 1), ("macd_hist", 2)]:
                _safe_assign(g, col, m.iloc[:, idx] if m is not None and len(m.columns) > idx else None)
        elif name == "bbands":
            bb = ta.bbands(g["close"], length=20, std=2)
            for col, idx in [("bb_lower", 0), ("bb_mid", 1), ("bb_upper", 2)]:
                _safe_assign(g, col, bb.iloc[:, idx] if bb is not None and len(bb.columns) > idx else None)
        elif name.startswith("vol_sma"):
            _safe_assign(g, name, ta.sma(g["volume"], length=int(name[len("vol_sma"):])))
        elif name == "adx14":
            adx = ta.adx(g["high"], g["low"], g["close"], length=14)
            _safe_assign(g, "adx14", adx.iloc[:, 0] if adx is not None else None)
        else:
            raise ValueError(f"unknown indicator: {name!r}")
    return g


def add_indicators(df: pd.DataFrame, indicators: Iterable[str]) -> pd.DataFrame:
    """Add indicator columns. Works for both single-symbol (date-indexed) and
    multi-index (symbol, date) frames. Per-symbol grouping prevents cross-symbol leakage."""
    inds = list(indicators)
    if not inds:
        return df

    if isinstance(df.index, pd.MultiIndex) and "symbol" in df.index.names:
        return df.groupby(level="symbol", group_keys=False).apply(
            lambda g: _add_to_single(g.droplevel("symbol"), inds).pipe(
                lambda x: x.assign(symbol=g.index.get_level_values("symbol")[0]).set_index("symbol", append=True).swaplevel().sort_index()
            )
        )
    return _add_to_single(df, inds)
