"""NSE data fetcher built on the `nselib` library.

Two paths:
- Per-symbol: `fetch_price_history(symbol, from_date, to_date)` using the archives
  endpoint. Handy for one-off catch-up; ~90-day silent cap so we chunk at 60 days.
- Bulk by date: `fetch_bhav(trade_date)` returns all NSE equities for one day.
  Far more efficient for universe-wide backfill (~250 calls/year vs thousands).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Iterable

import pandas as pd
from loguru import logger
from sqlalchemy import text
from tenacity import retry, stop_after_attempt, wait_exponential

import nselib
from nselib import capital_market, indices

from stockagent.db.session import get_engine

_DATE_FMT = "%d-%m-%Y"

# Map nselib's price/volume/delivery columns to our normalized schema.
_PRICE_COL_MAP = {
    "Symbol": "symbol",
    "Series": "series",
    "Date": "date",
    "PrevClose": "prev_close",
    "Prev Close": "prev_close",
    "OpenPrice": "open",
    "Open Price": "open",
    "HighPrice": "high",
    "High Price": "high",
    "LowPrice": "low",
    "Low Price": "low",
    "ClosePrice": "close",
    "Close Price": "close",
    "TotalTradedQuantity": "volume",
    "Total Traded Quantity": "volume",
    "Turnover": "turnover",
    "TurnoverInRs": "turnover",
    "Turnover ₹": "turnover",
    "No.ofTrades": "trades",
    "No. of Trades": "trades",
    "DeliverableQty": "deliverable_qty",
    "Deliverable Qty": "deliverable_qty",
    "%DlyQttoTradedQty": "deliverable_pct",
    "% Dly Qt to Traded Qty": "deliverable_pct",
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True)
def fetch_price_history(symbol: str, from_date: date, to_date: date) -> pd.DataFrame:
    """Fetch daily OHLCV+delivery for `symbol` between dates inclusive."""
    raw = capital_market.price_volume_and_deliverable_position_data(
        symbol=symbol,
        from_date=from_date.strftime(_DATE_FMT),
        to_date=to_date.strftime(_DATE_FMT),
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    return _normalize_prices(raw, symbol)


def _normalize_prices(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    # nselib's first header arrives with a UTF-8 BOM and embedded quotes, e.g. '﻿"Symbol"'.
    df = df.rename(columns=lambda c: c.replace("﻿", "").replace('"', "").strip())
    rename_map = {c: _PRICE_COL_MAP[c] for c in df.columns if c in _PRICE_COL_MAP}
    df = df.rename(columns=rename_map)
    df = df.loc[:, ~df.columns.duplicated()]
    target_cols = list(dict.fromkeys(_PRICE_COL_MAP.values()))
    keep = [c for c in target_cols if c in df.columns]
    df = df[keep].copy()
    df["symbol"] = symbol
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce", dayfirst=True).dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close", "prev_close", "turnover", "deliverable_pct"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce")
    for col in ("volume", "trades", "deliverable_qty"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce").astype("Int64")
    df = df.dropna(subset=["date", "close"])
    return df


_INDEX_NAME_ALIASES = {
    "NIFTY 50": "Nifty 50",
    "NIFTY50": "Nifty 50",
    "NIFTY NEXT 50": "Nifty Next 50",
    "NIFTY 100": "Nifty 100",
    "NIFTY 200": "Nifty 200",
    "NIFTY 500": "Nifty 500",
    "NIFTY500": "Nifty 500",
    "NIFTY MIDCAP 150": "Nifty Midcap 150",
    "NIFTY SMALLCAP 250": "Nifty Smallcap 250",
}


def fetch_constituents(index_name: str = "Nifty 50") -> list[str]:
    """Get current constituent symbols for an index. nselib's index_name is case-sensitive."""
    canonical = _INDEX_NAME_ALIASES.get(index_name.strip().upper(), index_name)
    if canonical == "Nifty 50":
        df = capital_market.nifty50_equity_list()
        return _extract_symbols(df)
    df = indices.constituent_stock_list(index_category="BroadMarketIndices", index_name=canonical)
    return _extract_symbols(df)


def _extract_symbols(df: pd.DataFrame) -> list[str]:
    if df is None or len(df) == 0:
        return []
    for col in ("Symbol", "symbol", "SYMBOL"):
        if col in df.columns:
            return df[col].dropna().astype(str).str.strip().tolist()
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


def fetch_fii_dii() -> pd.DataFrame:
    return capital_market.fii_dii_trading_activity()


def fetch_holidays() -> pd.DataFrame:
    return nselib.trading_holiday_calendar()


def upsert_prices(df: pd.DataFrame, exchange: str = "NSE", source: str = "nselib") -> int:
    if df is None or len(df) == 0:
        return 0
    df = df.copy()
    df["exchange"] = exchange
    df["source"] = source

    cols = [
        "symbol", "exchange", "date", "open", "high", "low", "close", "prev_close",
        "volume", "turnover", "trades", "deliverable_qty", "deliverable_pct",
        "series", "source",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None

    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    update_cols = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("symbol", "exchange", "date"))
    sql = text(
        f"INSERT INTO prices ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT(symbol, exchange, date) DO UPDATE SET {update_cols}"
    )

    rows = df[cols].astype(object).where(pd.notnull(df[cols]), None).to_dict(orient="records")
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def backfill_symbol(
    symbol: str,
    *,
    years: int = 5,
    end: date | None = None,
    chunk_days: int = 60,
    sleep_sec: float = 0.6,
) -> int:
    """NSE archives endpoint silently caps each request near ~3 months.
    We chunk at 60 days for safety and still get full delivery columns.
    A small sleep between chunks avoids per-symbol throttle (last-chunk truncation)."""
    end = end or date.today()
    start = end - timedelta(days=years * 365)
    total = 0
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=chunk_days), end)
        try:
            df = fetch_price_history(symbol, cur, chunk_end)
            n = upsert_prices(df)
            total += n
            logger.debug(f"{symbol} {cur}..{chunk_end}: {n} rows")
        except Exception as e:
            logger.warning(f"{symbol} {cur}..{chunk_end} failed: {e}")
            _log_backfill_error(symbol, "NSE", "price_history", str(e))
        cur = chunk_end + timedelta(days=1)
        if cur < end:
            time.sleep(sleep_sec)
    return total


# ---------------------------------------------------------------------------
# Bulk-by-date path via bhav_copy_with_delivery
# ---------------------------------------------------------------------------

# bhavcopy uses different column names than the archives endpoint.
_BHAV_COL_MAP = {
    "SYMBOL": "symbol",
    "SERIES": "series",
    "DATE1": "date",
    "PREV_CLOSE": "prev_close",
    "OPEN_PRICE": "open",
    "HIGH_PRICE": "high",
    "LOW_PRICE": "low",
    "CLOSE_PRICE": "close",
    "TTL_TRD_QNTY": "volume",
    "TURNOVER_LACS": "turnover_lacs",
    "NO_OF_TRADES": "trades",
    "DELIV_QTY": "deliverable_qty",
    "DELIV_PER": "deliverable_pct",
}

_HOLIDAY_RE_HINTS = ("Data not found", "change the trade_date")


class HolidayError(Exception):
    """Raised when nselib reports no data for a date (weekend/holiday)."""


def _is_holiday_msg(err: Exception) -> bool:
    if isinstance(err, HolidayError):
        return True
    msg = str(err)
    return any(h in msg for h in _HOLIDAY_RE_HINTS)


def _should_retry(retry_state) -> bool:
    if not retry_state.outcome.failed:
        return False
    return not _is_holiday_msg(retry_state.outcome.exception())


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=20),
    reraise=True,
    retry=_should_retry,
)
def fetch_bhav(trade_date: date, series: tuple[str, ...] = ("EQ",)) -> pd.DataFrame:
    """Fetch the daily bhavcopy with delivery info for one trading day. Filters by series.
    Holidays raise HolidayError immediately (no retries) so the caller can skip cheaply."""
    try:
        raw = capital_market.bhav_copy_with_delivery(trade_date=trade_date.strftime(_DATE_FMT))
    except Exception as e:
        if _is_holiday_msg(e):
            raise HolidayError(str(e)) from e
        raise
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    df = _normalize_bhav(raw, series=series)
    # NSE quietly returns the previous trading day's bhav for a holiday query.
    # Treat any whole-frame date mismatch as a holiday so we don't double-write yesterday.
    expected = trade_date.strftime("%Y-%m-%d")
    if not df.empty and (df["date"] != expected).all():
        actual = df["date"].iloc[0]
        raise HolidayError(f"requested {expected} but server returned {actual}")
    return df


def _normalize_bhav(df: pd.DataFrame, series: tuple[str, ...] = ("EQ",)) -> pd.DataFrame:
    df = df.rename(columns=lambda c: c.replace("﻿", "").replace('"', "").strip())
    rename_map = {c: _BHAV_COL_MAP[c] for c in df.columns if c in _BHAV_COL_MAP}
    df = df.rename(columns=rename_map)
    df = df.loc[:, ~df.columns.duplicated()]
    target_cols = list(dict.fromkeys(_BHAV_COL_MAP.values()))
    keep = [c for c in target_cols if c in df.columns]
    df = df[keep].copy()

    if "series" in df.columns and series:
        df = df[df["series"].isin(series)]

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce", dayfirst=True).dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close", "prev_close", "turnover_lacs", "deliverable_pct"):
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "").str.strip(), errors="coerce"
            )
    for col in ("volume", "trades", "deliverable_qty"):
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "").str.strip(), errors="coerce"
            ).astype("Int64")

    # Bhav reports turnover in lakhs; convert to raw INR for consistency with archives.
    if "turnover_lacs" in df.columns:
        df["turnover"] = df["turnover_lacs"] * 100_000.0
        df = df.drop(columns=["turnover_lacs"])

    df = df.dropna(subset=["symbol", "date", "close"])
    return df


def backfill_bhav_range(
    start: date,
    end: date,
    *,
    symbols: set[str] | None = None,
    series: tuple[str, ...] = ("EQ",),
    sleep_sec: float = 0.2,
) -> dict[str, int]:
    """Iterate weekdays in [start, end] inclusive, pull the day's bhavcopy, upsert.

    `symbols` (optional) — restrict to a set (e.g. Nifty 500). When None, store entire EQ universe.
    Returns counts: {'rows', 'days_attempted', 'days_success', 'days_skipped'}.
    """
    rows = days_attempted = days_success = days_skipped = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            days_attempted += 1
            t0 = time.time()
            try:
                df = fetch_bhav(cur, series=series)
                if symbols and not df.empty:
                    df = df[df["symbol"].isin(symbols)]
                if not df.empty:
                    n = upsert_prices(df, exchange="NSE", source="nselib_bhav")
                    rows += n
                    days_success += 1
                    logger.info(f"bhav {cur}: {n} rows ({time.time()-t0:.1f}s)")
                else:
                    days_skipped += 1
            except HolidayError:
                days_skipped += 1
                logger.info(f"bhav {cur}: holiday (skipped)")
            except Exception as e:
                logger.warning(f"bhav {cur} failed: {e}")
                _log_backfill_error("*", "NSE", "bhav", str(e))
            time.sleep(sleep_sec)
        cur += timedelta(days=1)
    logger.info(
        f"bhav range {start}..{end}: attempted={days_attempted} "
        f"success={days_success} skipped(holiday)={days_skipped} rows={rows}"
    )
    return {
        "rows": rows,
        "days_attempted": days_attempted,
        "days_success": days_success,
        "days_skipped": days_skipped,
    }


def _log_backfill_error(symbol: str, exchange: str, job: str, error: str) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO backfill_errors (symbol, exchange, job, error) "
                "VALUES (:symbol, :exchange, :job, :error)"
            ),
            {"symbol": symbol, "exchange": exchange, "job": job, "error": error[:1000]},
        )
