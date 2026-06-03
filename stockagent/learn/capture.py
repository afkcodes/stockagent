"""Capture a frozen decision-context + realized-outcome snapshot for closed trades.

`record_trade_review(trade_id)` is called from the ledger right after a trade is
marked closed; `backfill_reviews()` seeds the corpus from already-closed trades.

Idempotent: re-recording a trade upserts on the UNIQUE(trade_id) constraint.

This module ONLY reads and writes data — it never influences live decisions.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
from loguru import logger
from sqlalchemy import text

from stockagent.db.session import get_engine
from stockagent.learn import metrics


# ---------------------------------------------------------------------------
# Small DB helpers
# ---------------------------------------------------------------------------

def _trade_row(conn, trade_id: int) -> dict | None:
    r = conn.execute(text(
        """SELECT id, decision_id, symbol, qty, entry_price, entry_date,
                  exit_price, exit_date, exit_reason, pnl_inr, pnl_pct,
                  initial_stop, status
           FROM paper_trades WHERE id = :id"""
    ), {"id": trade_id}).mappings().first()
    return dict(r) if r else None


def _decision_row(conn, decision_id: int | None) -> dict | None:
    if decision_id is None:
        return None
    r = conn.execute(text(
        """SELECT id, run_id, conviction, agent_disagreement,
                  entry, stop_loss, target, rationale
           FROM coordinator_decisions WHERE id = :id"""
    ), {"id": decision_id}).mappings().first()
    return dict(r) if r else None


def _agent_context(conn, run_id: str | None, symbol: str) -> dict:
    """All agent verdicts for this (run_id, symbol), keyed by agent name."""
    if not run_id:
        return {}
    rows = conn.execute(text(
        """SELECT agent, verdict, conviction, reasoning, structured_json
           FROM agent_outputs WHERE run_id = :run AND symbol = :sym"""
    ), {"run": run_id, "sym": symbol}).mappings().all()
    out: dict = {}
    for r in rows:
        evidence, flags = {}, []
        if r["structured_json"]:
            try:
                payload = json.loads(r["structured_json"])
                evidence = payload.get("evidence", {}) or {}
                flags = payload.get("flags", []) or []
            except json.JSONDecodeError:
                pass
        out[r["agent"]] = {
            "verdict": r["verdict"],
            "conviction": r["conviction"],
            "evidence": evidence,
            "flags": flags,
        }
    return out


def _latest_market_cap(conn, symbol: str) -> float | None:
    r = conn.execute(text(
        """SELECT market_cap FROM fundamentals
           WHERE symbol = :s AND market_cap IS NOT NULL
           ORDER BY as_of_date DESC LIMIT 1"""
    ), {"s": symbol}).scalar()
    return float(r) if r is not None else None


def _holding_days(entry_date, exit_date) -> int | None:
    if not entry_date or not exit_date:
        return None
    try:
        return (pd.Timestamp(exit_date).date() - pd.Timestamp(entry_date).date()).days
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Core: record one closed trade
# ---------------------------------------------------------------------------

_UPSERT = text(
    """
    INSERT INTO trade_reviews
        (trade_id, decision_id, run_id, symbol, sector, source,
         entry_date, exit_date, holding_days, exit_reason, pnl_inr, pnl_pct,
         initial_risk_inr, r_multiple, mae_pct, mfe_pct,
         index_ret_pct, sector_ret_pct, excess_ret_pct,
         conviction, disagreement, rsi_entry, atr_pct_entry, rr_ratio,
         vix_state, nifty_trend, market_cap_band, context_json, is_win)
    VALUES
        (:trade_id, :decision_id, :run_id, :symbol, :sector, :source,
         :entry_date, :exit_date, :holding_days, :exit_reason, :pnl_inr, :pnl_pct,
         :initial_risk_inr, :r_multiple, :mae_pct, :mfe_pct,
         :index_ret_pct, :sector_ret_pct, :excess_ret_pct,
         :conviction, :disagreement, :rsi_entry, :atr_pct_entry, :rr_ratio,
         :vix_state, :nifty_trend, :market_cap_band, :context_json, :is_win)
    ON CONFLICT(trade_id) DO UPDATE SET
        decision_id=excluded.decision_id, run_id=excluded.run_id, sector=excluded.sector,
        source=excluded.source, exit_date=excluded.exit_date, holding_days=excluded.holding_days,
        exit_reason=excluded.exit_reason, pnl_inr=excluded.pnl_inr, pnl_pct=excluded.pnl_pct,
        initial_risk_inr=excluded.initial_risk_inr, r_multiple=excluded.r_multiple,
        mae_pct=excluded.mae_pct, mfe_pct=excluded.mfe_pct,
        index_ret_pct=excluded.index_ret_pct, sector_ret_pct=excluded.sector_ret_pct,
        excess_ret_pct=excluded.excess_ret_pct, conviction=excluded.conviction,
        disagreement=excluded.disagreement, rsi_entry=excluded.rsi_entry,
        atr_pct_entry=excluded.atr_pct_entry, rr_ratio=excluded.rr_ratio,
        vix_state=excluded.vix_state, nifty_trend=excluded.nifty_trend,
        market_cap_band=excluded.market_cap_band, context_json=excluded.context_json,
        is_win=excluded.is_win
    """
)


def record_trade_review(
    trade_id: int,
    *,
    source: str = "live",
    attributor: "metrics.RegimeAttributor | None" = None,
) -> bool:
    """Snapshot a CLOSED trade into trade_reviews. Returns True if written.

    `attributor` lets a backfill reuse one Nifty-50/sector proxy across all trades;
    for a single live call it is built on the fly over the trade's own window.
    """
    engine = get_engine()
    with engine.connect() as conn:
        t = _trade_row(conn, trade_id)
        if t is None:
            logger.warning(f"trade_review: trade {trade_id} not found")
            return False
        if t["status"] != "closed" or not t["exit_date"]:
            logger.debug(f"trade_review: trade {trade_id} not closed yet, skipping")
            return False

        decision = _decision_row(conn, t["decision_id"])
        run_id = decision["run_id"] if decision else None
        agents = _agent_context(conn, run_id, t["symbol"])
        market_cap = _latest_market_cap(conn, t["symbol"])

    symbol = t["symbol"]
    entry_price = t["entry_price"]
    qty = t["qty"]

    # initial_stop: prefer the value frozen at entry; fall back to the decision's
    # stop (best-effort for historical trades, where it may have been trailed).
    init_stop = t["initial_stop"]
    if init_stop is None and decision:
        init_stop = decision.get("stop_loss")

    risk = metrics.initial_risk(entry_price, init_stop, qty)
    r_mult = metrics.r_multiple(t["pnl_inr"], risk)
    mae, mfe = metrics.excursion(symbol, t["entry_date"], t["exit_date"], entry_price)

    attr = attributor or metrics.RegimeAttributor(t["entry_date"], t["exit_date"])
    index_ret = attr.index_return(t["entry_date"], t["exit_date"])
    sector_ret = attr.sector_return(symbol, t["entry_date"], t["exit_date"])
    sector = attr.sector_of(symbol)
    excess = None
    if t["pnl_pct"] is not None and index_ret is not None:
        excess = round(t["pnl_pct"] - index_ret, 3)

    # Context extraction from agent evidence (present only for LLM-scored trades)
    tech = agents.get("technical", {}).get("evidence", {})
    rsi_entry = tech.get("rsi14")
    atr_pct_entry = tech.get("atr_pct")
    macro_ev = agents.get("macro", {}).get("evidence", {})
    vix_state = macro_ev.get("vix_state")
    nifty_trend = macro_ev.get("trend_state")

    rr_ratio = None
    if decision and decision.get("target") and entry_price and init_stop:
        denom = entry_price - init_stop
        if denom > 0:
            rr_ratio = round((decision["target"] - entry_price) / denom, 3)

    disagreement = decision.get("agent_disagreement") if decision else None

    context_json = json.dumps({
        "decision": {
            "conviction": decision.get("conviction") if decision else None,
            "rationale": (decision.get("rationale") or "")[:1000] if decision else None,
            "entry": decision.get("entry") if decision else None,
            "stop_loss": init_stop,
            "target": decision.get("target") if decision else None,
        },
        "agents": agents,
        "market_cap_cr": market_cap,
    }, default=str)

    params = {
        "trade_id": trade_id,
        "decision_id": t["decision_id"],
        "run_id": run_id,
        "symbol": symbol,
        "sector": sector,
        "source": source,
        "entry_date": str(t["entry_date"]) if t["entry_date"] else None,
        "exit_date": str(t["exit_date"]) if t["exit_date"] else None,
        "holding_days": _holding_days(t["entry_date"], t["exit_date"]),
        "exit_reason": t["exit_reason"],
        "pnl_inr": t["pnl_inr"],
        "pnl_pct": t["pnl_pct"],
        "initial_risk_inr": risk,
        "r_multiple": r_mult,
        "mae_pct": mae,
        "mfe_pct": mfe,
        "index_ret_pct": index_ret,
        "sector_ret_pct": sector_ret,
        "excess_ret_pct": excess,
        "conviction": decision.get("conviction") if decision else None,
        "disagreement": disagreement,
        "rsi_entry": rsi_entry,
        "atr_pct_entry": atr_pct_entry,
        "rr_ratio": rr_ratio,
        "vix_state": vix_state,
        "nifty_trend": nifty_trend,
        "market_cap_band": metrics.market_cap_band(market_cap),
        "context_json": context_json,
        "is_win": 1 if (t["pnl_inr"] is not None and t["pnl_inr"] > 0) else 0,
    }

    with engine.begin() as conn:
        conn.execute(_UPSERT, params)
    return True


# ---------------------------------------------------------------------------
# Backfill: seed the corpus from already-closed trades
# ---------------------------------------------------------------------------

def backfill_reviews(*, source: str = "live", limit: int | None = None) -> int:
    """Record a trade_review for every closed paper_trade. Idempotent (upsert).

    Builds ONE RegimeAttributor across the full date span so the Nifty-50 and
    sector proxies load once rather than per-trade. Returns count processed.
    """
    engine = get_engine()
    with engine.connect() as conn:
        span = conn.execute(text(
            """SELECT MIN(entry_date) AS lo, MAX(exit_date) AS hi
               FROM paper_trades WHERE status = 'closed'
                 AND entry_date IS NOT NULL AND exit_date IS NOT NULL"""
        )).mappings().first()
        ids = conn.execute(text(
            """SELECT id FROM paper_trades
               WHERE status = 'closed' AND entry_date IS NOT NULL AND exit_date IS NOT NULL
               ORDER BY exit_date"""
            + (f" LIMIT {int(limit)}" if limit else "")
        )).scalars().all()

    if not ids:
        logger.info("backfill_reviews: no closed trades")
        return 0

    lo = span["lo"] if span and span["lo"] else None
    hi = span["hi"] if span and span["hi"] else None
    logger.info(f"backfill_reviews: {len(ids)} closed trades over {lo}..{hi}; building proxies once")
    attr = metrics.RegimeAttributor(lo, hi)

    done = 0
    for tid in ids:
        try:
            if record_trade_review(tid, source=source, attributor=attr):
                done += 1
        except Exception as e:
            logger.warning(f"backfill_reviews: trade {tid} failed: {e}")
    logger.info(f"backfill_reviews: recorded {done}/{len(ids)} trade_reviews")
    return done
