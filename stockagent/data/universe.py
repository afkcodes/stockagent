"""Universe filters — define what counts as "tradeable" rather than relying on
NSE's index lists. The liquid universe is the right concept for a swing trader:
"can I actually deploy ₹20K in this name without moving the price."
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from loguru import logger
from sqlalchemy import text

from stockagent.db.session import get_engine


def liquid_universe(
    as_of: date,
    *,
    lookback_days: int = 60,
    min_turnover_cr: float = 2.0,
    min_avg_price: float = 30.0,
    min_avg_trades: int = 1000,
    min_bars: int = 30,
) -> list[str]:
    """Symbols whose 60-day average meets all liquidity thresholds.

    `min_turnover_cr` is in ₹ crore (₹2 crore = ₹20,000,000 daily turnover).
    `min_bars` requires the symbol to have actively traded most of the lookback.
    """
    end = pd.Timestamp(as_of)
    start = end - pd.Timedelta(days=int(lookback_days * 1.5))  # weekends/holidays buffer
    min_turnover_inr = min_turnover_cr * 1_00_00_000

    sql = text(
        """
        SELECT symbol
        FROM prices
        WHERE date BETWEEN :start AND :end
        GROUP BY symbol
        HAVING COUNT(*) >= :min_bars
           AND AVG(turnover) >= :min_turn
           AND AVG(close)    >= :min_price
           AND AVG(trades)   >= :min_trades
        ORDER BY symbol
        """
    )
    engine = get_engine()
    with engine.connect() as c:
        syms = c.execute(sql, {
            "start": start.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d"),
            "min_bars": min_bars,
            "min_turn": min_turnover_inr,
            "min_price": min_avg_price,
            "min_trades": min_avg_trades,
        }).scalars().all()
    return list(syms)


def liquid_universe_summary(
    as_of: date,
    *,
    lookback_days: int = 60,
    min_turnover_cr: float = 2.0,
    min_avg_price: float = 30.0,
    min_avg_trades: int = 1000,
) -> pd.DataFrame:
    """Diagnostic: per-symbol stats over the lookback. Useful for tuning thresholds."""
    end = pd.Timestamp(as_of)
    start = end - pd.Timedelta(days=int(lookback_days * 1.5))
    sql = text(
        """
        SELECT symbol,
               AVG(turnover) AS avg_turnover,
               AVG(close)    AS avg_price,
               AVG(trades)   AS avg_trades,
               COUNT(*)      AS n_bars
        FROM prices
        WHERE date BETWEEN :start AND :end
        GROUP BY symbol
        ORDER BY avg_turnover DESC
        """
    )
    engine = get_engine()
    with engine.connect() as c:
        df = pd.read_sql(sql, c.connection, params={"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")})
    df["avg_turnover_cr"] = df["avg_turnover"] / 1_00_00_000
    df["passes"] = (
        (df["avg_turnover_cr"] >= min_turnover_cr)
        & (df["avg_price"] >= min_avg_price)
        & (df["avg_trades"] >= min_avg_trades)
    )
    return df
