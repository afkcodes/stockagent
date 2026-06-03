"""Per-trade learning metrics: R-multiple, MAE/MFE, and regime attribution.

All functions are deterministic and read only from the local prices/fundamentals
tables — no network, no LLM. They are the numeric backbone of `trade_reviews`.

Definitions:
  R-multiple       realized pnl / initial risk (entry - initial_stop) * qty.
                   Normalizes every trade to "units of risk made/lost".
  MAE / MFE        Max Adverse / Favorable Excursion: worst / best unrealized %
                   reached during the hold (from daily high/low bars).
  Regime attribution
                   Return of the Nifty-50 equal-weight proxy and the symbol's
                   sector-peer proxy over the SAME hold window. Separates alpha
                   (our pick) from beta (the market/sector moved).
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd

from stockagent.data.loader import load_prices
from stockagent.data.sectors import get_sector_map


# ---------------------------------------------------------------------------
# Scalar metrics
# ---------------------------------------------------------------------------

def r_multiple(pnl_inr: float | None, initial_risk_inr: float | None) -> float | None:
    """pnl / initial_risk. None if risk is unknown/non-positive."""
    if pnl_inr is None or initial_risk_inr is None:
        return None
    if not math.isfinite(initial_risk_inr) or initial_risk_inr <= 0:
        return None
    return round(pnl_inr / initial_risk_inr, 4)


def initial_risk(entry_price: float | None, initial_stop: float | None, qty: int | None) -> float | None:
    """(entry - stop) * qty. None if inputs missing or stop >= entry (no real risk defined)."""
    if not entry_price or not initial_stop or not qty:
        return None
    dist = entry_price - initial_stop
    if dist <= 0:
        return None
    return dist * qty


def market_cap_band(market_cap_cr: float | None) -> str | None:
    """Coarse band from market cap in INR crore. None if unknown."""
    if market_cap_cr is None or not math.isfinite(market_cap_cr) or market_cap_cr <= 0:
        return None
    if market_cap_cr >= 50_000:
        return "large"
    if market_cap_cr >= 15_000:
        return "mid"
    return "small"


# ---------------------------------------------------------------------------
# Excursion (per-symbol, over the hold window)
# ---------------------------------------------------------------------------

def excursion(symbol: str, entry_date: date | str, exit_date: date | str, entry_price: float) -> tuple[float | None, float | None]:
    """Return (mae_pct, mfe_pct) over [entry_date, exit_date] inclusive.

    mae_pct = (min low  - entry) / entry * 100   (<= 0 typically)
    mfe_pct = (max high - entry) / entry * 100   (>= 0 typically)
    """
    if not entry_price or entry_price <= 0:
        return (None, None)
    df = load_prices(symbol, start=entry_date, end=exit_date)
    if df.empty:
        return (None, None)
    df = df.droplevel("symbol")
    try:
        lo = float(df["low"].min())
        hi = float(df["high"].max())
    except (KeyError, ValueError):
        return (None, None)
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return (None, None)
    mae = round((lo - entry_price) / entry_price * 100.0, 3)
    mfe = round((hi - entry_price) / entry_price * 100.0, 3)
    return (mae, mfe)


# ---------------------------------------------------------------------------
# Index / sector proxy series + window return
# ---------------------------------------------------------------------------

def equal_weight_series(symbols: list[str], start: date | str, end: date | str) -> pd.Series:
    """Equal-weighted mean of daily closes across `symbols`. Empty Series if no data."""
    if not symbols:
        return pd.Series(dtype=float)
    df = load_prices(list(symbols), start=start, end=end)
    if df.empty:
        return pd.Series(dtype=float)
    wide = df["close"].unstack(level="symbol")
    if wide.empty:
        return pd.Series(dtype=float)
    return wide.mean(axis=1).dropna().sort_index()


def window_return(series: pd.Series, start: date | str, end: date | str) -> float | None:
    """% change of `series` between `start` and `end`, using the last available
    value on-or-before each target date (as-of join). None if not computable."""
    if series is None or series.empty:
        return None
    s = series.sort_index()

    def _asof(ts) -> float | None:
        prior = s.loc[: pd.Timestamp(ts)]
        if len(prior):
            return float(prior.iloc[-1])
        return float(s.iloc[0]) if len(s) else None

    a = _asof(start)
    b = _asof(end)
    if a is None or b is None or a == 0:
        return None
    return round((b - a) / a * 100.0, 3)


# ---------------------------------------------------------------------------
# RegimeAttributor — caches index + sector proxies for efficient backfill
# ---------------------------------------------------------------------------

class RegimeAttributor:
    """Builds the Nifty-50 proxy once and sector proxies lazily, then answers
    per-trade window returns. Reuse a single instance across a backfill so the
    50-symbol index load and each sector load happen at most once."""

    def __init__(self, start: date | str, end: date | str, index_symbols: list[str] | None = None):
        self.start = start
        self.end = end
        if index_symbols is None:
            try:
                from stockagent.data.nse import fetch_constituents
                index_symbols = fetch_constituents("Nifty 50")
            except Exception:
                index_symbols = []
        self._index = equal_weight_series(index_symbols, start, end) if index_symbols else pd.Series(dtype=float)
        self._sector_map = {k.upper(): v for k, v in get_sector_map().items()}
        self._sector_to_syms: dict[str, list[str]] = {}
        for sym, sec in self._sector_map.items():
            self._sector_to_syms.setdefault(sec, []).append(sym)
        self._sector_series_cache: dict[str, pd.Series] = {}

    def index_return(self, entry_date: date | str, exit_date: date | str) -> float | None:
        return window_return(self._index, entry_date, exit_date)

    def sector_of(self, symbol: str) -> str | None:
        return self._sector_map.get(symbol.upper())

    def sector_return(self, symbol: str, entry_date: date | str, exit_date: date | str) -> float | None:
        sec = self.sector_of(symbol)
        if not sec:
            return None
        if sec not in self._sector_series_cache:
            syms = self._sector_to_syms.get(sec, [])
            self._sector_series_cache[sec] = (
                equal_weight_series(syms, self.start, self.end) if syms else pd.Series(dtype=float)
            )
        return window_return(self._sector_series_cache[sec], entry_date, exit_date)
