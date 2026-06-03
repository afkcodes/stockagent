"""Layer 2 (Mine): aggregate closed-trade reviews into evidence-backed stats.

Deterministic, no LLM, no network. Two products, both recomputed in full from
the `trade_reviews` corpus over a rolling window:

  recompute_agent_reliability()  -> agent_reliability   (per agent verdict x conviction)
  recompute_patterns()           -> learned_patterns    (per feature bucket)

Every output row carries n, win_rate, avg_r (expectancy in R), profit_factor and
a Wilson confidence interval on the win rate, so downstream layers (Phase 3) act
only on statistically-supported buckets. A bucket is `is_active=1` only when it
has >= MIN_N trades AND its win-rate CI confidently clears (boost) or sits below
(penalty) a coin flip — never on thin or ambiguous data.

This module ONLY reads/writes learning tables. It never influences live picks.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from statistics import mean

from loguru import logger
from sqlalchemy import bindparam, text

from stockagent.config import settings
from stockagent.db.session import get_engine

# Wilson score interval z (one-sided ~95%). Small enough to let ~8-trade buckets
# clear the bar, large enough to reject noise.
WILSON_Z = 1.64
# Directional activation thresholds on the win-rate CI.
WILSON_HI = 0.55   # boost: lower bound must exceed this
WILSON_LO = 0.45   # penalty: upper bound must sit below this


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def wilson_bounds(wins: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """(lower, upper) Wilson score interval for a binomial proportion."""
    if n <= 0:
        return (0.0, 1.0)
    phat = wins / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    lo = (centre - margin) / denom
    hi = (centre + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _bucket_stats(rows: list[dict]) -> dict:
    """Win rate, avg R (expectancy), profit factor and Wilson CI for a bucket."""
    n = len(rows)
    wins = sum(1 for r in rows if r["is_win"])
    win_rate = wins / n if n else 0.0
    r_vals = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    avg_r = round(mean(r_vals), 4) if r_vals else None
    gross_profit = sum(r["pnl_inr"] for r in rows if (r["pnl_inr"] or 0) > 0)
    gross_loss = -sum(r["pnl_inr"] for r in rows if (r["pnl_inr"] or 0) < 0)
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None
    lb, ub = wilson_bounds(wins, n)
    return {
        "n": n,
        "win_rate": round(win_rate, 4),
        "avg_r": avg_r,
        "expectancy": avg_r,        # per-trade expectancy in R units
        "profit_factor": profit_factor,
        "wilson_lb": round(lb, 4),
        "wilson_ub": round(ub, 4),
    }


def _is_active(st: dict, min_n: int) -> bool:
    """Confidently better OR worse than a coin flip, on enough trades."""
    if st["n"] < min_n:
        return False
    return st["wilson_lb"] >= WILSON_HI or st["wilson_ub"] <= WILSON_LO


def _multipliers(st: dict) -> tuple[float, float]:
    """Bounded conviction/size multipliers a bucket implies (Phase 3 consumes
    these). Driven by expectancy in R; falls back to win-rate edge when R is
    unknown. Bounds match docs/autolearn_design.md guardrails."""
    base = st["avg_r"]
    if base is None:
        base = (st["win_rate"] - 0.5) * 2.0   # +/-1 at the extremes
    conv = _clamp(1.0 + 0.15 * base, 0.5, 1.3)
    size = _clamp(1.0 + 0.20 * base, 0.5, 1.5)
    return (round(conv, 3), round(size, 3))


# ---------------------------------------------------------------------------
# Feature bucketing for learned_patterns
# ---------------------------------------------------------------------------

def _conv_band(v: float | None) -> str | None:
    if v is None:
        return None
    return "lo" if v < 0.5 else ("mid" if v < 0.7 else "hi")


def _rr_band(v: float | None) -> str | None:
    if v is None:
        return None
    return "poor" if v < 1 else ("fair" if v < 2 else ("good" if v < 3 else "rich"))


def _rsi_band(v: float | None) -> str | None:
    if v is None:
        return None
    if v < 30:
        return "oversold"
    if v < 45:
        return "weak"
    if v < 60:
        return "mid"
    if v < 75:
        return "strong"
    return "overbought"


def _atr_band(v: float | None) -> str | None:
    if v is None:
        return None
    return "low" if v < 2 else ("med" if v < 4 else "high")


def _feature_buckets(r: dict) -> list[tuple[str, str]]:
    """All (dimension, value) buckets a review belongs to. Dimensions with no
    data for this review are skipped, so buckets only form where evidence exists."""
    out: list[tuple[str, str]] = []
    if r.get("sector"):
        out.append(("sector", r["sector"]))
    if (cb := _conv_band(r.get("conviction"))):
        out.append(("conv", cb))
    if (rb := _rr_band(r.get("rr_ratio"))):
        out.append(("rr", rb))
    if (rsib := _rsi_band(r.get("rsi_entry"))):
        out.append(("rsi", rsib))
    if (atrb := _atr_band(r.get("atr_pct_entry"))):
        out.append(("atr", atrb))
    if r.get("vix_state"):
        out.append(("vix", r["vix_state"]))
    if r.get("nifty_trend"):
        out.append(("nifty", r["nifty_trend"]))
    if r.get("market_cap_band"):
        out.append(("cap", r["market_cap_band"]))
    if r.get("exit_reason"):
        out.append(("exit", r["exit_reason"]))
    return out


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------

def _window_clause(window_days: int | None) -> tuple[str, dict]:
    if not window_days:
        return ("", {})
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    return (" AND exit_date >= :cutoff", {"cutoff": cutoff})


def _load_reviews(conn, window_days: int | None, sources: list[str] | None) -> list[dict]:
    where = "WHERE 1=1"
    params: dict = {}
    wc, wp = _window_clause(window_days)
    where += wc
    params.update(wp)
    if sources:
        where += " AND source IN :srcs"
        params["srcs"] = list(sources)
    q = text(
        f"""SELECT trade_id, symbol, sector, source, conviction, disagreement,
                   rsi_entry, atr_pct_entry, rr_ratio, vix_state, nifty_trend,
                   market_cap_band, exit_reason, entry_date, exit_date,
                   r_multiple, pnl_inr, excess_ret_pct, is_win, context_json
            FROM trade_reviews {where}"""
    )
    if sources:
        q = q.bindparams(bindparam("srcs", expanding=True))
    rows = conn.execute(q, params).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Layer 2a: agent reliability
# ---------------------------------------------------------------------------

def recompute_agent_reliability(
    *, window_days: int | None = None, sources: list[str] | None = None
) -> int:
    """Rebuild agent_reliability from trade_reviews. One row per (agent, condition)
    where condition = '<verdict>@<conv_band>' plus a pooled '<verdict>@all'."""
    if window_days is None:
        window_days = settings.autolearn_window_days
    engine = get_engine()
    with engine.connect() as conn:
        reviews = _load_reviews(conn, window_days, sources)

    # Bucket reviews by (agent, condition). Each review's agent context is frozen
    # in context_json under "agents": {name: {verdict, conviction, ...}}.
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in reviews:
        agents = {}
        if r.get("context_json"):
            try:
                agents = json.loads(r["context_json"]).get("agents", {}) or {}
            except (json.JSONDecodeError, TypeError):
                agents = {}
        for agent, info in agents.items():
            verdict = (info or {}).get("verdict")
            if not verdict:
                continue
            cb = _conv_band((info or {}).get("conviction")) or "na"
            for cond in (f"{verdict}@{cb}", f"{verdict}@all"):
                buckets.setdefault((agent, cond), []).append(r)

    lo, hi = _corpus_span(reviews)
    out_rows = []
    for (agent, cond), rows in buckets.items():
        st = _bucket_stats(rows)
        out_rows.append({
            "agent": agent, "condition": cond, **st,
            "window_start": lo, "window_end": hi,
        })

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_reliability"))
        for row in out_rows:
            conn.execute(text(
                """INSERT INTO agent_reliability
                       (agent, condition, n, win_rate, avg_r, expectancy, wilson_lb,
                        window_start, window_end)
                   VALUES (:agent,:condition,:n,:win_rate,:avg_r,:expectancy,:wilson_lb,
                           :window_start,:window_end)"""
            ), row)
    logger.info(f"agent_reliability: {len(out_rows)} rows over {lo}..{hi}")
    return len(out_rows)


# ---------------------------------------------------------------------------
# Layer 2b: learned patterns
# ---------------------------------------------------------------------------

def recompute_patterns(
    *, window_days: int | None = None, sources: list[str] | None = None
) -> int:
    """Rebuild learned_patterns: one row per single-feature bucket with stats,
    bounded multipliers, and an is_active flag gated on n + Wilson confidence."""
    if window_days is None:
        window_days = settings.autolearn_window_days
    min_n = settings.autolearn_min_n
    engine = get_engine()
    with engine.connect() as conn:
        reviews = _load_reviews(conn, window_days, sources)

    buckets: dict[str, list[dict]] = {}
    for r in reviews:
        for dim, val in _feature_buckets(r):
            buckets.setdefault(f"{dim}={val}", []).append(r)

    lo, hi = _corpus_span(reviews)
    out_rows = []
    for key, rows in buckets.items():
        st = _bucket_stats(rows)
        active = _is_active(st, min_n)
        conv_mult, size_mult = _multipliers(st)
        # Inactive buckets carry neutral multipliers so a stale activation can
        # never leak influence once data thins out.
        if not active:
            conv_mult, size_mult = 1.0, 1.0
        excess = [r["excess_ret_pct"] for r in rows if r["excess_ret_pct"] is not None]
        desc = (
            f"{key}: n={st['n']} win={st['win_rate']*100:.0f}% "
            f"avgR={st['avg_r'] if st['avg_r'] is not None else 'na'} "
            f"PF={st['profit_factor'] if st['profit_factor'] is not None else 'na'} "
            f"excess={round(mean(excess), 2) if excess else 'na'}%"
        )
        out_rows.append({
            "pattern_key": key, "description": desc,
            "n": st["n"], "win_rate": st["win_rate"], "avg_r": st["avg_r"],
            "expectancy": st["expectancy"], "profit_factor": st["profit_factor"],
            "wilson_lb": st["wilson_lb"], "conviction_mult": conv_mult,
            "size_mult": size_mult, "is_active": 1 if active else 0,
            "window_start": lo, "window_end": hi,
        })

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM learned_patterns"))
        for row in out_rows:
            conn.execute(text(
                """INSERT INTO learned_patterns
                       (pattern_key, description, n, win_rate, avg_r, expectancy,
                        profit_factor, wilson_lb, conviction_mult, size_mult,
                        is_active, window_start, window_end)
                   VALUES (:pattern_key,:description,:n,:win_rate,:avg_r,:expectancy,
                           :profit_factor,:wilson_lb,:conviction_mult,:size_mult,
                           :is_active,:window_start,:window_end)"""
            ), row)
    active_n = sum(r["is_active"] for r in out_rows)
    logger.info(f"learned_patterns: {len(out_rows)} buckets ({active_n} active) over {lo}..{hi}")
    return len(out_rows)


def _corpus_span(reviews: list[dict]) -> tuple[str | None, str | None]:
    """(earliest entry_date, latest exit_date) over the mined reviews, as ISO
    strings for the window_start/window_end audit columns."""
    entries = [str(r["entry_date"]) for r in reviews if r.get("entry_date")]
    exits = [str(r["exit_date"]) for r in reviews if r.get("exit_date")]
    return (min(entries) if entries else None, max(exits) if exits else None)


def recompute_all(*, window_days: int | None = None, sources: list[str] | None = None) -> dict:
    """Run both miners. Returns {'agent_reliability': n, 'learned_patterns': n}."""
    return {
        "agent_reliability": recompute_agent_reliability(window_days=window_days, sources=sources),
        "learned_patterns": recompute_patterns(window_days=window_days, sources=sources),
    }
