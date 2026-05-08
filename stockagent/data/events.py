"""Corporate-action and earnings calendar — for trade avoidance windows.

We avoid entries within `_AVOID_DAYS` of any known event for the symbol:
- Earnings results (quarterly)
- Ex-dividend / record dates
- Split / bonus / rights ex-dates
- Mergers / scheme of arrangement effective dates
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import pandas as pd
from loguru import logger
from sqlalchemy import text

from stockagent.db.session import get_engine

_AVOID_DAYS = 5  # exclude entries within this many trading days of an event

# nselib's event categories we treat as "avoid window"
_AVOID_PURPOSES = (
    "results",        # quarterly results
    "earnings",       # earnings calendar (NSE event)
    "dividend",
    "bonus",
    "split",
    "rights",
    "scheme",
    "merger",
    "buyback",
)


def refresh_corporate_actions(*, lookahead_days: int = 60) -> int:
    """Pull corporate actions for the next 60 days from nselib and persist."""
    try:
        from nselib import capital_market
    except ImportError:
        return 0
    today = date.today()
    end = today + timedelta(days=lookahead_days)
    try:
        df = capital_market.corporate_action_data(
            from_date=today.strftime("%d-%m-%Y"),
            to_date=end.strftime("%d-%m-%Y"),
        )
    except Exception as e:
        logger.warning(f"corporate_action_data fetch failed: {e}")
        return 0
    if df is None or df.empty:
        return 0

    df = df.rename(columns={c: c.strip() for c in df.columns})
    sym_col = next((c for c in df.columns if c.lower() == "symbol"), None)
    ex_col = next((c for c in df.columns if "ex" in c.lower() and "date" in c.lower()), None)
    purpose_col = next((c for c in df.columns if "purpose" in c.lower() or "subject" in c.lower()), None)
    if not sym_col or not ex_col:
        logger.warning(f"corporate_action_data unexpected schema: {list(df.columns)}")
        return 0

    rows = []
    for _, r in df.iterrows():
        sym = str(r[sym_col]).strip().upper()
        ex_raw = str(r[ex_col]).strip()
        try:
            ex_date = pd.to_datetime(ex_raw, dayfirst=True, errors="coerce").date()
        except Exception:
            continue
        if not sym or pd.isna(ex_date):
            continue
        purpose = str(r[purpose_col]).strip() if purpose_col else ""
        rows.append({
            "symbol": sym, "ex_date": str(ex_date),
            "action_type": _classify_purpose(purpose), "details": purpose[:1000],
        })

    if not rows:
        return 0
    sql = text(
        """INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, action_type, details)
           VALUES (:symbol, :ex_date, :action_type, :details)"""
    )
    with get_engine().begin() as c:
        c.execute(sql, rows)
    logger.info(f"corporate actions refreshed: {len(rows)} events")
    return len(rows)


def _classify_purpose(purpose: str) -> str:
    p = purpose.lower()
    for k in _AVOID_PURPOSES:
        if k in p:
            return k
    return "other"


def is_in_avoid_window(symbol: str, as_of: date, days: int = _AVOID_DAYS) -> tuple[bool, str | None]:
    """Returns (skip?, reason). Skip if any tracked event for `symbol` falls within ±`days`."""
    sql = text(
        """SELECT ex_date, action_type, details FROM corporate_actions
           WHERE symbol = :s AND ex_date BETWEEN :from_d AND :to_d
           ORDER BY ex_date"""
    )
    from_d = (as_of - timedelta(days=days)).isoformat()
    to_d = (as_of + timedelta(days=days)).isoformat()
    with get_engine().connect() as c:
        row = c.execute(sql, {"s": symbol.upper(), "from_d": from_d, "to_d": to_d}).mappings().first()
    if not row:
        return False, None
    if row["action_type"] in _AVOID_PURPOSES:
        return True, f"{row['action_type']} on {row['ex_date']} ({row['details'][:60]})"
    return False, None


def filter_out_event_symbols(symbols: Iterable[str], as_of: date) -> tuple[list[str], dict[str, str]]:
    """Return (kept_symbols, dropped_with_reason)."""
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for s in symbols:
        skip, reason = is_in_avoid_window(s, as_of)
        if skip:
            dropped[s] = reason or "event"
        else:
            kept.append(s)
    return kept, dropped
