"""Coordinator V2 — orchestrates all agents on each candidate signal.

Flow per day:
  1. Generate deterministic signals (RSI mean-reversion on Nifty 500)
  2. Apply mechanical filters: corporate-action avoidance window
  3. For each surviving signal, run all agents in parallel via orchestrator
  4. Apply macro multiplier (deployment fraction)
  5. Apply sector concentration cap
  6. Apply position sizing (locked: ₹1L / 20% / 5%)
  7. Persist final picks to coordinator_decisions

The combine step inside the orchestrator is formula-based, not LLM. The coordinator's
filters are also mechanical. The LLMs only run INSIDE the agents — never on the
final selection.
"""
from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from loguru import logger
from sqlalchemy import text

from stockagent.agents.fundamental import FundamentalAgent
from stockagent.agents.macro import MacroAgent, deployment_multiplier
from stockagent.agents.orchestrator import (
    AgentOrchestrator, CombinedVerdict, OrchestratorConfig, persist_orchestrator_run,
)
from stockagent.agents.protocol import Agent
from stockagent.agents.sentiment import SentimentAgent
from stockagent.agents.technical import TechnicalAgent
from stockagent.config import settings
from stockagent.data.events import is_in_avoid_window
from stockagent.data.loader import load_prices
from stockagent.data.sectors import sector_for
from stockagent.db.session import get_engine
from stockagent.signals.daily import Signal, generate_signals, latest_trading_day_in_db


@dataclass
class WatchlistEntry:
    symbol: str
    sector: str
    strategy: str
    entry: float
    stop: float
    target: float | None
    qty: int
    position_size_inr: float
    horizon_days: int
    final_verdict: str
    conviction: float
    macro_multiplier: float
    rationale: str
    flags: list[str] = field(default_factory=list)
    per_agent: dict = field(default_factory=dict)


def _recent_bars(symbol: str, as_of: date, n: int = 10) -> list[dict]:
    end = pd.Timestamp(as_of)
    start = end - pd.Timedelta(days=n * 2 + 10)
    df = load_prices(symbol, start=start.date(), end=as_of)
    if df.empty:
        return []
    df = df.droplevel("symbol").tail(n)
    return [
        {
            "date": d.strftime("%Y-%m-%d"),
            "open": float(r.open), "high": float(r.high), "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume) if pd.notna(r.volume) else 0.0,
        }
        for d, r in df.iterrows()
    ]


def _size_position(entry: float, stop: float) -> tuple[int, float]:
    if entry <= 0 or not math.isfinite(stop) or stop >= entry:
        return 0, 0.0
    stop_dist = entry - stop
    qty_by_risk = int((settings.capital_inr * settings.max_risk_per_trade_pct) // stop_dist)
    qty_by_alloc = int((settings.capital_inr * settings.max_allocation_pct) // entry)
    qty = max(0, min(qty_by_risk, qty_by_alloc))
    return qty, qty * entry


def build_default_orchestrator(*, with_sentiment: bool = True) -> AgentOrchestrator:
    """Standard production agent set. Sentiment is opt-out for fast replays."""
    agents: list[Agent] = [TechnicalAgent(), FundamentalAgent(), MacroAgent()]
    if with_sentiment:
        agents.append(SentimentAgent())
    return AgentOrchestrator(agents)


def run_coordinator(
    *,
    symbols: list[str],
    as_of: date | None = None,
    max_picks: int = 5,
    max_picks_per_sector: int = 2,
    min_combined_conviction: float = 0.45,
    use_llm: bool = True,
    with_sentiment: bool = True,
) -> list[WatchlistEntry]:
    """Build today's watchlist using the multi-agent orchestrator."""
    as_of = as_of or latest_trading_day_in_db()
    if as_of is None:
        raise RuntimeError("no prices in DB")

    run_id = f"wl-{as_of.isoformat()}-{uuid.uuid4().hex[:8]}"
    logger.info(f"coordinator V2 run {run_id} for {as_of}, universe={len(symbols)}")

    raw_signals: list[Signal] = generate_signals(symbols=symbols, as_of=as_of)
    logger.info(f"raw signals: {len(raw_signals)}")
    if not raw_signals:
        return []

    # Mechanical filter: corporate actions avoidance window
    filtered: list[Signal] = []
    dropped_events: dict[str, str] = {}
    for s in raw_signals:
        skip, reason = is_in_avoid_window(s.symbol, as_of)
        if skip:
            dropped_events[s.symbol] = reason or "event"
        else:
            filtered.append(s)
    if dropped_events:
        logger.info(f"dropped {len(dropped_events)} signals near events: {dropped_events}")

    # Resolve macro multiplier ONCE per run (it's market-wide)
    macro_agent = MacroAgent()
    macro_verdict = macro_agent.evaluate("MARKET", {"as_of": as_of})
    macro_mult = deployment_multiplier(macro_verdict)
    effective_max_picks = max(1, int(round(max_picks * macro_mult)))
    logger.info(f"macro={macro_verdict.verdict} mult={macro_mult:.2f} → max_picks {max_picks}→{effective_max_picks}")

    # Build orchestrator
    orchestrator = build_default_orchestrator(with_sentiment=with_sentiment) if use_llm else None

    # Score each signal via orchestrator (or pass-through if no LLM)
    scored: list[tuple[Signal, CombinedVerdict | None]] = []
    for sig in filtered:
        if orchestrator is None:
            scored.append((sig, None))
            continue
        bars = _recent_bars(sig.symbol, as_of)
        sig_dict = {
            "strategy": sig.strategy,
            "rationale": sig.rationale,
            "bar_date": sig.bar_date.date(),
            "entry_price": sig.entry_price,
            "stop_price": sig.stop_price,
            "indicator_snapshot": sig.indicator_snapshot,
            "stop_dist_pct": (sig.entry_price - sig.stop_price) / sig.entry_price * 100,
        }
        ctx = {"signal": sig_dict, "recent_bars": bars, "as_of": as_of}
        combined = orchestrator.evaluate(sig.symbol, ctx)
        persist_orchestrator_run(run_id, combined)
        scored.append((sig, combined))

    # Filter: only bullish-with-conviction picks; vetoes are already neutralized by orchestrator
    candidates: list[tuple[Signal, CombinedVerdict | None, float]] = []
    for sig, combined in scored:
        if combined is None:
            # Pass-through deterministic (LLM disabled): treat as neutral 0.5
            candidates.append((sig, None, 0.5))
            continue
        if combined.final_verdict == "avoid":
            continue
        if combined.final_verdict == "bearish":
            continue
        if combined.conviction < min_combined_conviction:
            continue
        candidates.append((sig, combined, combined.conviction))

    # Rank by conviction (descending)
    candidates.sort(key=lambda x: x[2], reverse=True)

    # Apply sector concentration cap + position sizing + cash budget
    picks: list[WatchlistEntry] = []
    used_capital = 0.0
    sector_counts: dict[str, int] = defaultdict(int)
    for sig, combined, conv in candidates:
        if len(picks) >= effective_max_picks:
            break
        sector = sector_for(sig.symbol)
        if sector_counts[sector] >= max_picks_per_sector:
            logger.info(f"sector cap hit: skipping {sig.symbol} ({sector})")
            continue
        qty, alloc = _size_position(sig.entry_price, sig.stop_price)
        if qty <= 0:
            continue
        if used_capital + alloc > settings.capital_inr:
            remaining = settings.capital_inr - used_capital
            if remaining < sig.entry_price:
                continue
            qty = int(remaining // sig.entry_price)
            alloc = qty * sig.entry_price
            if qty <= 0:
                continue
        used_capital += alloc
        sector_counts[sector] += 1

        stop_dist = sig.entry_price - sig.stop_price
        target = sig.entry_price + 2 * stop_dist
        flags = []
        per_agent_summary = {}
        if combined:
            for a, v in combined.per_agent.items():
                per_agent_summary[a] = {"verdict": v.verdict, "conviction": v.conviction}
                flags.extend([f"{a}:{x}" for x in v.flags[:2]])

        rationale = (
            combined.per_agent.get("technical").reasoning
            if combined and combined.per_agent.get("technical")
            else sig.rationale
        )

        picks.append(WatchlistEntry(
            symbol=sig.symbol, sector=sector, strategy=sig.strategy,
            entry=sig.entry_price, stop=sig.stop_price, target=target,
            qty=qty, position_size_inr=alloc, horizon_days=20,
            final_verdict=combined.final_verdict if combined else "neutral",
            conviction=conv,
            macro_multiplier=macro_mult,
            rationale=rationale,
            flags=flags[:6],
            per_agent=per_agent_summary,
        ))

    _persist_picks(run_id, as_of, picks, macro_mult)
    return picks


def _persist_picks(run_id: str, as_of: date, picks: list[WatchlistEntry], macro_mult: float) -> None:
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
    rows = [{
        "run_id": run_id, "symbol": p.symbol,
        "verdict": p.final_verdict, "conviction": p.conviction,
        "entry": p.entry, "stop": p.stop, "target": p.target,
        "pos": p.position_size_inr, "qty": p.qty,
        "horizon": p.horizon_days, "disagreement": 0.0,
        "rationale": (p.rationale[:1500] if p.rationale else "") + f" [macro_mult={macro_mult:.2f}]",
    } for p in picks]
    with get_engine().begin() as c:
        c.execute(sql, rows)
