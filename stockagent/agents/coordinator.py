"""Coordinator: pulls today's deterministic signals → asks technical agent for a
verdict on each → ranks by conviction → applies position-sizing and risk caps →
writes the ranked watchlist to coordinator_decisions."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date

import pandas as pd
from loguru import logger
from sqlalchemy import text

from stockagent.agents.technical import TechnicalVerdict, evaluate_signal_or_neutral, persist_verdict
from stockagent.config import settings
from stockagent.data.loader import load_prices
from stockagent.db.session import get_engine
from stockagent.signals.daily import Signal, generate_signals, latest_trading_day_in_db


@dataclass
class WatchlistEntry:
    symbol: str
    strategy: str
    entry: float
    stop: float
    target: float | None
    qty: int
    position_size_inr: float
    horizon_days: int
    verdict: str
    conviction: float
    rationale: str


def _recent_bars(symbol: str, as_of: date, n: int = 10) -> list[dict]:
    end = pd.Timestamp(as_of)
    start = end - pd.Timedelta(days=n * 2 + 10)  # plenty of margin
    df = load_prices(symbol, start=start.date(), end=as_of)
    if df.empty:
        return []
    df = df.droplevel("symbol").tail(n)
    return [
        {
            "date": d.strftime("%Y-%m-%d"),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume) if pd.notna(r.volume) else 0.0,
        }
        for d, r in df.iterrows()
    ]


def _size_position(entry: float, stop: float) -> tuple[int, float]:
    """Return (qty, allocated_inr) honoring 20% allocation cap and 5% risk cap."""
    if entry <= 0 or not math.isfinite(stop) or stop >= entry:
        return 0, 0.0
    stop_dist = entry - stop
    qty_by_risk = int((settings.capital_inr * settings.max_risk_per_trade_pct) // stop_dist)
    qty_by_alloc = int((settings.capital_inr * settings.max_allocation_pct) // entry)
    qty = max(0, min(qty_by_risk, qty_by_alloc))
    return qty, qty * entry


def run_coordinator(
    *,
    symbols: list[str],
    as_of: date | None = None,
    max_picks: int = 5,
    min_conviction: float = 0.3,  # permissive — we want LLM to filter only true falling knives
) -> list[WatchlistEntry]:
    """Build today's watchlist. `as_of` defaults to last DB date."""
    as_of = as_of or latest_trading_day_in_db()
    if as_of is None:
        raise RuntimeError("no prices in DB")

    run_id = f"wl-{as_of.isoformat()}-{uuid.uuid4().hex[:8]}"
    logger.info(f"coordinator run {run_id} for {as_of}, universe={len(symbols)}")

    raw_signals: list[Signal] = generate_signals(symbols=symbols, as_of=as_of)
    logger.info(f"raw signals: {len(raw_signals)}")
    if not raw_signals:
        return []

    # Per-signal: ask technical agent (or stub if no key)
    verdicts: dict[tuple[str, str], TechnicalVerdict] = {}
    for s in raw_signals:
        bars = _recent_bars(s.symbol, as_of)
        v = evaluate_signal_or_neutral(s, bars)
        verdicts[(s.symbol, s.strategy)] = v
        persist_verdict(run_id, s, v)

    # Rank by conviction (and only keep bullish or neutral-with-good-conviction)
    ranked = sorted(
        raw_signals,
        key=lambda s: verdicts[(s.symbol, s.strategy)].conviction,
        reverse=True,
    )

    picks: list[WatchlistEntry] = []
    used_capital = 0.0
    for s in ranked:
        if len(picks) >= max_picks:
            break
        v = verdicts[(s.symbol, s.strategy)]
        if v.verdict == "bearish":
            continue
        if v.conviction < min_conviction:
            continue
        qty, alloc = _size_position(s.entry_price, s.stop_price)
        if qty <= 0:
            continue
        if used_capital + alloc > settings.capital_inr:
            # over-capital → trim to available
            remaining = settings.capital_inr - used_capital
            if remaining < s.entry_price:
                continue
            qty = int(remaining // s.entry_price)
            alloc = qty * s.entry_price
            if qty <= 0:
                continue
        used_capital += alloc

        # Target: 2× the stop distance (1:2 risk/reward) by default
        stop_dist = s.entry_price - s.stop_price
        target = s.entry_price + 2 * stop_dist

        picks.append(WatchlistEntry(
            symbol=s.symbol,
            strategy=s.strategy,
            entry=s.entry_price,
            stop=s.stop_price,
            target=target,
            qty=qty,
            position_size_inr=alloc,
            horizon_days=20,
            verdict=v.verdict,
            conviction=v.conviction,
            rationale=v.reasoning or s.rationale,
        ))

    _persist_picks(run_id, as_of, picks)
    return picks


def _persist_picks(run_id: str, as_of: date, picks: list[WatchlistEntry]) -> None:
    if not picks:
        return
    sql = text(
        """
        INSERT INTO coordinator_decisions
            (run_id, symbol, final_verdict, conviction,
             entry, stop_loss, target, position_size_inr, qty,
             horizon_days, agent_disagreement, rationale)
        VALUES
            (:run_id, :symbol, :verdict, :conviction,
             :entry, :stop, :target, :pos, :qty,
             :horizon, :disagreement, :rationale)
        """
    )
    rows = [
        {
            "run_id": run_id,
            "symbol": p.symbol,
            "verdict": p.verdict,
            "conviction": p.conviction,
            "entry": p.entry,
            "stop": p.stop,
            "target": p.target,
            "pos": p.position_size_inr,
            "qty": p.qty,
            "horizon": p.horizon_days,
            "disagreement": 0.0,  # only one agent today; will use real var when more agents land
            "rationale": p.rationale,
        }
        for p in picks
    ]
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(sql, rows)
