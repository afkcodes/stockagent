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


def _normalize_event_df(df, *, src_label: str) -> list[dict]:
    """Convert an nselib events/corporate-actions dataframe into our row schema."""
    if df is None or len(df) == 0:
        return []
    df = df.rename(columns={c: c.strip() for c in df.columns})
    sym_col = next((c for c in df.columns if c.lower() == "symbol"), None)
    # Date column priority: exDate (corp_actions) → date (event_calendar) → any *date* col
    date_candidates = ("exDate", "ex_date", "date")
    ex_col = next((c for c in df.columns if c in date_candidates), None)
    if not ex_col:
        ex_col = next((c for c in df.columns if "date" in c.lower()), None)
    purpose_col = next(
        (c for c in df.columns if c.lower() in ("subject", "purpose", "description", "bm_desc")),
        None,
    )
    if not sym_col or not ex_col:
        logger.warning(f"{src_label} unexpected schema: {list(df.columns)}")
        return []

    rows: list[dict] = []
    for _, r in df.iterrows():
        sym = str(r[sym_col]).strip().upper()
        ex_raw = str(r[ex_col]).strip()
        if not sym or not ex_raw or ex_raw in ("-", "nan", "None"):
            continue
        try:
            ex_date = pd.to_datetime(ex_raw, dayfirst=True, errors="coerce").date()
        except Exception:
            continue
        if pd.isna(ex_date):
            continue
        purpose = str(r[purpose_col]).strip() if purpose_col else ""
        rows.append({
            "symbol": sym,
            "ex_date": str(ex_date),
            "action_type": _classify_purpose(purpose),
            "details": purpose[:1000],
        })
    return rows


def refresh_corporate_actions(*, lookahead_days: int = 60) -> int:
    """Pull corporate actions + earnings event calendar for the next N days.

    Combines two nselib endpoints:
      - corporate_actions_for_equity: dividends, splits, bonuses, mergers
      - event_calendar_for_equity:   board meetings, results dates
    """
    try:
        from nselib import capital_market
    except ImportError:
        return 0

    today = date.today()
    end = today + timedelta(days=lookahead_days)
    from_str = today.strftime("%d-%m-%Y")
    to_str = end.strftime("%d-%m-%Y")

    all_rows: list[dict] = []

    try:
        df_ca = capital_market.corporate_actions_for_equity(from_date=from_str, to_date=to_str)
        all_rows.extend(_normalize_event_df(df_ca, src_label="corporate_actions_for_equity"))
    except Exception as e:
        logger.warning(f"corporate_actions_for_equity fetch failed: {e}")

    try:
        df_ev = capital_market.event_calendar_for_equity(from_date=from_str, to_date=to_str)
        all_rows.extend(_normalize_event_df(df_ev, src_label="event_calendar_for_equity"))
    except Exception as e:
        logger.warning(f"event_calendar_for_equity fetch failed: {e}")

    if not all_rows:
        return 0

    sql = text(
        """INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, action_type, details)
           VALUES (:symbol, :ex_date, :action_type, :details)"""
    )
    with get_engine().begin() as c:
        c.execute(sql, all_rows)
    logger.info(f"corporate actions + events refreshed: {len(all_rows)} entries")
    return len(all_rows)


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
