"""Macro/regime agent — emits a market-wide deployment multiplier (0..1).

Rule-based by default (mechanical, fully reproducible). Optionally annotated by
the LLM for human-readable reasoning, but the *number* is always rules.

Inputs:
  - India VIX (fear gauge)
  - FII / DII net flows (institutional positioning)
  - Nifty 50 distance from 50/200 SMA (trend regime)

Output:
  - verdict 'bullish' (risk-on) / 'neutral' / 'bearish' (risk-off)
  - conviction = the deployment multiplier we want the coordinator to apply
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
from loguru import logger
from sqlalchemy import text

from stockagent.agents.protocol import Agent, AgentVerdict
from stockagent.data.loader import load_prices
from stockagent.db.session import get_engine


def _fetch_vix(as_of: date | None = None, lookback_days: int = 60) -> pd.DataFrame:
    """India VIX via nselib. Returns recent daily close. Rule-based; no LLM here."""
    try:
        from nselib import capital_market
    except ImportError:
        return pd.DataFrame()
    end = as_of or date.today()
    start = end - timedelta(days=lookback_days * 2)
    try:
        df = capital_market.india_vix_data(
            from_date=start.strftime("%d-%m-%Y"),
            to_date=end.strftime("%d-%m-%Y"),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        # nselib actually returns: TIMESTAMP, INDEX_NAME, OPEN_INDEX_VAL, CLOSE_INDEX_VAL, ...
        df = df.rename(columns={c: c.strip() for c in df.columns})
        date_col = next((c for c in df.columns if c.upper() in ("TIMESTAMP", "DATE")), None)
        close_col = next((c for c in df.columns if c.upper() in ("CLOSE_INDEX_VAL", "CLOSE")), None)
        if not date_col or not close_col:
            logger.warning(f"VIX unexpected columns: {list(df.columns)}")
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df[date_col], dayfirst=True, errors="coerce")
        df["close"] = pd.to_numeric(df[close_col].astype(str).str.replace(",", ""), errors="coerce")
        return df.dropna(subset=["date", "close"]).sort_values("date")
    except Exception as e:
        logger.warning(f"VIX fetch failed: {e}")
        return pd.DataFrame()


def _fetch_nifty_trend(as_of: date | None = None, lookback_days: int = 250) -> dict | None:
    """Nifty 50 close vs 50/200 SMA. Reads from our own prices table — must have NIFTY 50 ETF
    or a constituent-mean proxy. We compute equal-weighted Nifty 50 mean as proxy."""
    end = pd.Timestamp(as_of or date.today())
    start = end - pd.Timedelta(days=int(lookback_days * 1.5))
    try:
        from stockagent.data.nse import fetch_constituents
        n50 = fetch_constituents("Nifty 50")
    except Exception as e:
        logger.warning(f"nifty constituents fetch: {e}")
        return None
    df = load_prices(n50, start=start.date(), end=as_of or date.today())
    if df.empty:
        return None
    # Equal-weighted price index from Nifty 50 closes
    wide = df["close"].unstack(level="symbol")
    if wide.empty:
        return None
    idx = wide.mean(axis=1).dropna()
    if len(idx) < 200:
        return None
    last = float(idx.iloc[-1])
    sma50 = float(idx.tail(50).mean())
    sma200 = float(idx.tail(200).mean())
    return {
        "last": last,
        "sma50": sma50,
        "sma200": sma200,
        "above_sma50_pct": (last - sma50) / sma50 * 100,
        "above_sma200_pct": (last - sma200) / sma200 * 100,
        "sma50_above_sma200": sma50 > sma200,
    }


def _classify_regime(vix_close: float | None, trend: dict | None) -> tuple[str, float, dict, list[str]]:
    """Mechanical rule classifier. Returns (verdict, conviction/multiplier, evidence, flags).

    Conviction here doubles as the DEPLOYMENT MULTIPLIER:
      1.0 = full deployment as designed
      0.7 = deploy 70% of normal (e.g., 3-4 picks instead of 5)
      0.4 = defensive, only top-conviction picks
      0.0 = avoid new entries entirely
    """
    flags: list[str] = []
    evidence: dict = {}

    # VIX rules
    vix_state = "unknown"
    if vix_close is not None:
        evidence["vix_close"] = round(vix_close, 2)
        if vix_close < 14:
            vix_state = "low"  # complacent — be careful, but ok to deploy
        elif vix_close < 20:
            vix_state = "normal"
        elif vix_close < 28:
            vix_state = "elevated"
            flags.append(f"vix_elevated_{vix_close:.1f}")
        else:
            vix_state = "panic"
            flags.append(f"vix_panic_{vix_close:.1f}")
    evidence["vix_state"] = vix_state

    # Trend rules
    trend_state = "unknown"
    if trend is not None:
        evidence.update({
            "nifty50_proxy_last": round(trend["last"], 2),
            "above_sma50_pct": round(trend["above_sma50_pct"], 2),
            "above_sma200_pct": round(trend["above_sma200_pct"], 2),
        })
        if trend["above_sma200_pct"] > 5 and trend["sma50_above_sma200"]:
            trend_state = "uptrend"
        elif trend["above_sma200_pct"] < -5:
            trend_state = "downtrend"
            flags.append("nifty_below_200sma")
        else:
            trend_state = "range"
    evidence["trend_state"] = trend_state

    # Combined regime
    if vix_state == "panic" or trend_state == "downtrend":
        return "bearish", 0.4, evidence, flags
    if vix_state == "elevated" and trend_state != "uptrend":
        return "neutral", 0.6, evidence, flags
    if trend_state == "uptrend" and vix_state in ("low", "normal"):
        return "bullish", 1.0, evidence, flags
    if trend_state == "range":
        return "neutral", 0.7, evidence, flags
    return "neutral", 0.7, evidence, flags


class MacroAgent(Agent):
    """Pure-rule macro agent. No LLM dependency."""

    name = "macro"
    weight = 0.5  # macro is a global modifier, not a per-stock voter — keep weight low

    def __init__(self):
        self.model = "(rule-based)"
        self._cached: AgentVerdict | None = None
        self._cached_date: date | None = None

    def evaluate(self, symbol: str, context: dict[str, Any]) -> AgentVerdict:
        # Macro is global — same answer for every symbol on a given day. Cache.
        as_of = context.get("as_of") or date.today()
        if self._cached is not None and self._cached_date == as_of:
            return AgentVerdict(**{**self._cached.model_dump(), "symbol": symbol})

        # Fetch inputs
        vix_df = _fetch_vix(as_of)
        vix_close = float(vix_df["close"].iloc[-1]) if not vix_df.empty else None
        trend = _fetch_nifty_trend(as_of)

        verdict, conviction, evidence, flags = _classify_regime(vix_close, trend)
        out = AgentVerdict(
            agent=self.name, symbol=symbol, verdict=verdict,
            conviction=conviction,
            reasoning=(
                f"Regime classifier: VIX={evidence.get('vix_state','?')} "
                f"trend={evidence.get('trend_state','?')} → "
                f"deployment multiplier {conviction:.2f}"
            ),
            flags=flags, evidence=evidence,
            model=self.model,
        )
        self._cached = out
        self._cached_date = as_of
        return out


def deployment_multiplier(verdict: AgentVerdict) -> float:
    """Convert the macro verdict's conviction to a 0..1 deployment multiplier."""
    if verdict.verdict == "bearish":
        return min(verdict.conviction, 0.5)  # cap defense at 50% even if rule says lower
    return verdict.conviction
