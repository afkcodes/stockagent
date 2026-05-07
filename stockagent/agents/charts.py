"""Render candlestick + indicator charts as base64 PNG for multimodal LLM input.

Designed for the technical-judge use case: 60 daily candles with EMA20/50 overlay,
volume panel, and RSI(14) panel with 30/70 reference lines. Compact enough to fit
in a single LLM call, detailed enough to read pattern structure.
"""
from __future__ import annotations

import base64
import io
from datetime import date

import matplotlib

matplotlib.use("Agg")  # no GUI; required for headless rendering
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from loguru import logger

from stockagent.data.loader import load_prices
from stockagent.indicators.compute import add_indicators


def render_signal_chart(
    symbol: str,
    as_of: date,
    *,
    lookback_days: int = 60,
    width: int = 10,
    height: int = 8,
    dpi: int = 80,
) -> str | None:
    """Returns a base64-encoded PNG data URL, or None if data is insufficient.
    The chart shows 60 daily candles + EMA(20)/EMA(50) overlay + volume + RSI(14)."""
    end = pd.Timestamp(as_of)
    # Pull extra warmup so EMA50/RSI are valid at the first plotted bar
    start = end - pd.Timedelta(days=lookback_days * 4)
    df = load_prices(symbol, start=start.date(), end=as_of)
    if df.empty:
        logger.warning(f"render_signal_chart: no data for {symbol} as_of {as_of}")
        return None
    df = df.droplevel("symbol").sort_index()
    df = add_indicators(df, ["ema20", "ema50", "rsi14"])
    df = df.tail(lookback_days)
    if len(df) < 20:
        return None

    df_mpf = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
    })
    df_mpf.index = pd.DatetimeIndex(df_mpf.index)

    rsi = df["rsi14"]
    # mplfinance addplots: EMAs on price panel; RSI on its own panel
    addplots = [
        mpf.make_addplot(df_mpf["ema20"], color="#ff8c00", width=1.0, label="EMA20"),
        mpf.make_addplot(df_mpf["ema50"], color="#1e90ff", width=1.0, label="EMA50"),
        mpf.make_addplot(rsi, panel=2, color="#8a2be2", ylabel="RSI(14)"),
        mpf.make_addplot(pd.Series(30, index=df.index), panel=2, color="#888", linestyle="--", width=0.6),
        mpf.make_addplot(pd.Series(70, index=df.index), panel=2, color="#888", linestyle="--", width=0.6),
    ]

    buf = io.BytesIO()
    title = f"{symbol}  —  bar of {df.index[-1].date()}"
    try:
        mpf.plot(
            df_mpf,
            type="candle",
            style="charles",
            addplot=addplots,
            volume=True,
            volume_panel=1,
            panel_ratios=(4, 1, 2),
            title=title,
            savefig=dict(fname=buf, dpi=dpi, bbox_inches="tight"),
            figsize=(width, height),
            tight_layout=True,
        )
    except Exception as e:
        logger.warning(f"mpf.plot failed for {symbol}: {e}")
        plt.close("all")
        return None
    plt.close("all")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"
