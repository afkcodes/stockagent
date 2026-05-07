"""yfinance fetcher — used for pre-2020 history (NSE removed those legacy URLs)
and for BSE-only names. No delivery columns — OHLCV only."""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from stockagent.data.nse import upsert_prices


def _ticker(symbol: str, exchange: str) -> str:
    suffix = ".NS" if exchange.upper() == "NSE" else ".BO"
    return f"{symbol}{suffix}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15), reraise=True)
def fetch_yf_history(
    symbol: str,
    exchange: str = "NSE",
    *,
    start: date,
    end: date,
) -> pd.DataFrame:
    t = yf.Ticker(_ticker(symbol, exchange))
    df = t.history(
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
        actions=False,
    )
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.reset_index()
    df = df.rename(
        columns={
            "Date": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    df["symbol"] = symbol
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df[["symbol", "date", "open", "high", "low", "close", "volume"]]


def backfill_symbols_yf(
    symbols: list[str],
    *,
    start: date,
    end: date,
    exchange: str = "NSE",
    sleep_sec: float = 0.1,
) -> dict[str, int]:
    """Iterate symbols and pull their daily history into prices via yfinance."""
    rows = symbols_done = symbols_empty = symbols_failed = 0
    failed: list[str] = []
    for i, s in enumerate(symbols, 1):
        t0 = time.time()
        try:
            df = fetch_yf_history(s, exchange=exchange, start=start, end=end)
        except Exception as e:
            logger.warning(f"yf {s} failed: {e}")
            symbols_failed += 1
            failed.append(s)
            continue
        if df.empty:
            symbols_empty += 1
            logger.debug(f"yf {s}: empty")
        else:
            n = upsert_prices(df, exchange=exchange.upper(), source="yfinance")
            rows += n
            symbols_done += 1
            logger.info(f"yf {s} ({i}/{len(symbols)}): {n} rows ({time.time()-t0:.1f}s)")
        time.sleep(sleep_sec)
    if failed:
        logger.warning(f"yf failures: {failed[:20]}{'...' if len(failed) > 20 else ''}")
    return {
        "rows": rows,
        "symbols_done": symbols_done,
        "symbols_empty": symbols_empty,
        "symbols_failed": symbols_failed,
        "failed": failed,
    }
