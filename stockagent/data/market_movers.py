"""NSE live-analysis screens — for discovery and confluence flagging.

Caveats:
- These are NSE *live* endpoints. They show snapshots of intraday state during market
  hours; after-hours they freeze on the day's last update. Best run after 15:35 IST.
- Endpoints are unofficial (power the website). NSE can change them without notice.
- We use the patched session (timeouts + cookie reuse) so calls are fast and robust.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Iterable

import pandas as pd
from loguru import logger
from nselib import capital_market
from nselib.libutil import nse_urlfetch
from sqlalchemy import text

from stockagent.db.session import get_engine

CATEGORIES = (
    "most_active_value",
    "most_active_volume",
    "top_gainers",
    "top_losers",
    "volume_gainers",
    "price_band_upper",
    "price_band_lower",
)

# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> dict | None:
    try:
        r = nse_urlfetch(url)
        if r.status_code != 200:
            logger.warning(f"fetch {url}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        logger.warning(f"fetch {url}: {e}")
        return None


def fetch_most_active(by: str = "value") -> pd.DataFrame:
    """nselib wrapper. by ∈ {'value', 'volume'}."""
    df = capital_market.most_active_equities(fetch_by=by)
    if df is None or len(df) == 0:
        return pd.DataFrame()
    return df.head(20).reset_index(drop=True)


def fetch_top_gainers_losers(kind: str = "gainers") -> pd.DataFrame:
    # nselib spells losers as 'loosers' — preserve the typo or it raises ValueError.
    nselib_arg = "loosers" if kind == "losers" else kind
    df = capital_market.top_gainers_or_losers(to_get=nselib_arg)
    if df is None or len(df) == 0:
        return pd.DataFrame()
    return df.head(20).reset_index(drop=True)


def fetch_volume_gainers() -> pd.DataFrame:
    j = _fetch_json("https://www.nseindia.com/api/live-analysis-volume-gainers")
    if not j or "data" not in j:
        return pd.DataFrame()
    return pd.DataFrame(j["data"]).head(20).reset_index(drop=True)


def fetch_price_band_hitters() -> dict[str, pd.DataFrame]:
    """Returns {'upper': df, 'lower': df}. Empty frames if endpoint fails."""
    j = _fetch_json("https://www.nseindia.com/api/live-analysis-price-band-hitter")
    out = {"upper": pd.DataFrame(), "lower": pd.DataFrame()}
    if not j:
        return out
    for side in ("upper", "lower"):
        section = j.get(side, {})
        all_sec = section.get("AllSec", {}) if isinstance(section, dict) else {}
        rows = all_sec.get("data", []) if isinstance(all_sec, dict) else []
        if rows:
            out[side] = pd.DataFrame(rows).head(20).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _row_for_persist(category: str, rank: int, r: dict, d: date) -> dict:
    """Normalize varied response schemas into our market_movers row shape."""
    sym = r.get("symbol") or r.get("Symbol") or ""
    ltp = r.get("ltp") or r.get("lastPrice") or r.get("last_price") or r.get("LTP")
    pchange = r.get("pChange") or r.get("perChange") or r.get("p_change") or r.get("change")
    vol = r.get("totalTradedVolume") or r.get("trade_quantity") or r.get("volume") or r.get("quantity_traded")
    turnover = r.get("totalTradedValue") or r.get("turnover") or r.get("trade_value")
    try:
        ltp = float(ltp) if ltp is not None else None
    except (TypeError, ValueError):
        ltp = None
    try:
        pchange = float(pchange) if pchange is not None else None
    except (TypeError, ValueError):
        pchange = None
    try:
        vol = int(vol) if vol is not None else None
    except (TypeError, ValueError):
        vol = None
    try:
        turnover = float(turnover) if turnover is not None else None
    except (TypeError, ValueError):
        turnover = None
    return {
        "date": str(d),
        "category": category,
        "rank": rank,
        "symbol": str(sym).upper(),
        "ltp": ltp,
        "pchange": pchange,
        "volume": vol,
        "turnover": turnover,
        "raw_json": json.dumps({k: v for k, v in r.items() if isinstance(v, (str, int, float, bool, type(None)))}),
    }


def _upsert_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = text(
        """
        INSERT INTO market_movers (date, category, rank, symbol, ltp, pchange, volume, turnover, raw_json)
        VALUES (:date, :category, :rank, :symbol, :ltp, :pchange, :volume, :turnover, :raw_json)
        ON CONFLICT(date, category, rank) DO UPDATE SET
          symbol=excluded.symbol, ltp=excluded.ltp, pchange=excluded.pchange,
          volume=excluded.volume, turnover=excluded.turnover, raw_json=excluded.raw_json
        """
    )
    engine = get_engine()
    with engine.begin() as c:
        c.execute(sql, rows)
    return len(rows)


def fetch_and_persist_all(*, as_of: date | None = None) -> dict[str, int]:
    """Fetch all 7 screens and persist to market_movers. Returns counts per category."""
    as_of = as_of or date.today()
    counts: dict[str, int] = {}

    def _do(category: str, fetch_fn) -> None:
        """Each category is independent — one failure shouldn't sink the rest."""
        try:
            df = fetch_fn()
        except Exception as e:
            logger.warning(f"{category} fetch failed: {e}")
            counts[category] = -1
            return
        if df is None or len(df) == 0:
            counts[category] = 0
            return
        rows = [_row_for_persist(category, i, r, as_of) for i, r in enumerate(df.to_dict(orient="records"))]
        rows = [r for r in rows if r["symbol"]]
        try:
            counts[category] = _upsert_rows(rows)
        except Exception as e:
            logger.warning(f"{category} persist failed: {e}")
            counts[category] = -1

    _do("most_active_value", lambda: fetch_most_active("value"))
    _do("most_active_volume", lambda: fetch_most_active("volume"))
    _do("top_gainers", lambda: fetch_top_gainers_losers("gainers"))
    _do("top_losers", lambda: fetch_top_gainers_losers("losers"))
    _do("volume_gainers", fetch_volume_gainers)

    try:
        pb = fetch_price_band_hitters()
    except Exception as e:
        logger.warning(f"price_band fetch failed: {e}")
        pb = {"upper": pd.DataFrame(), "lower": pd.DataFrame()}
    _do("price_band_upper", lambda: pb["upper"])
    _do("price_band_lower", lambda: pb["lower"])
    return counts


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def confluence_flags(symbol: str, as_of: date) -> list[str]:
    """Return human-readable flags for a symbol on a given date.
    e.g. ['top_losers#3', 'volume_gainers#7'] => fell hard on heavy volume."""
    sql = text("SELECT category, rank FROM market_movers WHERE symbol = :s AND date = :d ORDER BY category, rank")
    engine = get_engine()
    with engine.connect() as c:
        rows = c.execute(sql, {"s": symbol.upper(), "d": str(as_of)}).fetchall()
    return [f"{cat}#{rank+1}" for cat, rank in rows]


def discover_unwatched(
    *,
    as_of: date,
    held_or_picked: Iterable[str],
    universe_filter: set[str] | None = None,
    categories: tuple[str, ...] = ("top_gainers", "top_losers", "volume_gainers"),
    limit: int = 15,
) -> pd.DataFrame:
    """Show today's notable movers NOT already in the user's watchlist/holdings.
    Optionally filter to a known universe (e.g. Nifty 500) — if None, returns all."""
    held_set = {s.upper() for s in held_or_picked}
    cats_in = ", ".join(f"'{c}'" for c in categories)
    sql = text(
        f"""
        SELECT category, rank, symbol, ltp, pchange, volume, turnover
        FROM market_movers
        WHERE date = :d AND category IN ({cats_in})
        ORDER BY category, rank
        """
    )
    engine = get_engine()
    with engine.connect() as c:
        df = pd.read_sql(sql, c, params={"d": str(as_of)})
    if df.empty:
        return df
    df = df[~df["symbol"].isin(held_set)]
    if universe_filter is not None:
        df = df[df["symbol"].isin({s.upper() for s in universe_filter})]
    return df.head(limit).reset_index(drop=True)
