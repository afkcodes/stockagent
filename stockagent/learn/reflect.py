"""Layer 4 (Reflect): LLM post-mortems on losses, retrieved on similar setups.

For each losing trade_review an "analyst" LLM reads the frozen decision context
+ realized outcome and writes a structured lesson into `trade_lessons`, tagged
with the review's decision-time bucket keys. On future similar setups those
lessons are surfaced to the agents as READ-ONLY context.

Hard rule (docs/autolearn_design.md §3 Layer 4): lessons enrich judgment only.
They never touch the deterministic conviction/size arithmetic — only mined
statistics do that. This module is therefore safe to run with no behaviour
risk: generation is offline/batch, retrieval is additive prompt context.
"""
from __future__ import annotations

import json

from loguru import logger
from sqlalchemy import bindparam, text

from stockagent.agents.base import LLMUnavailableError, call_llm, parse_json_safely
from stockagent.config import settings
from stockagent.db.session import get_engine
from stockagent.learn import mine

SYSTEM_PROMPT = (
    "You are a trading post-mortem analyst. You are given ONE closed losing trade: "
    "the decision-time evidence (what the agents saw) and the realized outcome "
    "(including R-multiple, max adverse/favorable excursion, and how the index and "
    "sector moved over the same window). Your job is to extract a single, specific, "
    "transferable lesson — not generic advice.\n\n"
    "Crucially, separate ALPHA from BETA: if the index/sector fell similarly, the pick "
    "was not necessarily wrong; say so. Identify which signal (if any) misled, the "
    "regime it happened in, and a concrete thing to watch for on similar future setups.\n\n"
    "Respond ONLY as JSON: {\"lesson\": <=300 chars actionable lesson, "
    "\"misleading_signal\": short, \"regime\": short, \"watch_for\": short, "
    "\"alpha_or_beta\": one of 'alpha'|'beta'|'mixed'}."
)


# ---------------------------------------------------------------------------
# Loading + prompt
# ---------------------------------------------------------------------------

def _review(conn, review_id: int) -> dict | None:
    r = conn.execute(text("SELECT * FROM trade_reviews WHERE id = :id"), {"id": review_id}).mappings().first()
    return dict(r) if r else None


def _decision_keys(review: dict) -> list[str]:
    """Bucket keys for retrieval — decision-time only (drop the post-hoc exit dim)."""
    return [f"{dim}={val}" for dim, val in mine._feature_buckets(review) if dim != "exit"]


def _format_review(review: dict) -> str:
    def f(x, fmt="{}"):
        return fmt.format(x) if x is not None else "na"
    agents_summary = ""
    if review.get("context_json"):
        try:
            agents = json.loads(review["context_json"]).get("agents", {}) or {}
            parts = [f"{a}={(v or {}).get('verdict')}@{(v or {}).get('conviction')}" for a, v in agents.items()]
            agents_summary = ", ".join(parts) if parts else "none recorded"
        except (json.JSONDecodeError, TypeError):
            agents_summary = "unparseable"
    return (
        f"Symbol: {review.get('symbol')}  Sector: {f(review.get('sector'))}\n"
        f"Held: {f(review.get('entry_date'))} → {f(review.get('exit_date'))} "
        f"({f(review.get('holding_days'))} days), exit reason: {f(review.get('exit_reason'))}\n"
        f"P&L: ₹{f(review.get('pnl_inr'), '{:.0f}')} ({f(review.get('pnl_pct'), '{:.2f}')}%), "
        f"R-multiple: {f(review.get('r_multiple'), '{:.2f}')}\n"
        f"Excursion: MAE {f(review.get('mae_pct'), '{:.2f}')}%, MFE {f(review.get('mfe_pct'), '{:.2f}')}%\n"
        f"Regime over same window: index {f(review.get('index_ret_pct'), '{:.2f}')}%, "
        f"sector {f(review.get('sector_ret_pct'), '{:.2f}')}%, "
        f"excess vs index {f(review.get('excess_ret_pct'), '{:.2f}')}%\n"
        f"Decision: conviction {f(review.get('conviction'), '{:.2f}')}, "
        f"RSI@entry {f(review.get('rsi_entry'), '{:.1f}')}, R:R {f(review.get('rr_ratio'), '{:.2f}')}, "
        f"VIX {f(review.get('vix_state'))}, Nifty {f(review.get('nifty_trend'))}\n"
        f"Agents at decision: {agents_summary}"
    )


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

def _has_lesson(conn, review_id: int) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM trade_lessons WHERE trade_review_id = :id LIMIT 1"
    ), {"id": review_id}).first() is not None


def generate_lesson(review_id: int, *, force: bool = False) -> str | None:
    """LLM post-mortem for one losing review → trade_lessons (one row per decision
    key, sharing the lesson text). Returns the lesson, or None if skipped/failed."""
    engine = get_engine()
    with engine.connect() as conn:
        review = _review(conn, review_id)
        if review is None:
            logger.warning(f"reflect: review {review_id} not found")
            return None
        if not force and _has_lesson(conn, review_id):
            logger.debug(f"reflect: review {review_id} already has a lesson; skip")
            return None

    try:
        resp = call_llm(
            model=settings.model_coordinator,
            system=SYSTEM_PROMPT,
            user=_format_review(review),
            response_format="json_object",
            max_tokens=400,
        )
    except LLMUnavailableError:
        logger.warning("reflect: OPENROUTER_API_KEY not set; cannot generate lessons")
        return None
    except Exception as e:
        logger.warning(f"reflect: LLM failed for review {review_id}: {type(e).__name__}: {e}")
        return None

    parsed = parse_json_safely(resp.content)
    lesson = (parsed.get("lesson") or "").strip()
    if not lesson:
        logger.warning(f"reflect: empty lesson for review {review_id}")
        return None
    # Fold the structured fields into one retrievable note.
    extra = " | ".join(
        f"{k}: {parsed[k]}" for k in ("misleading_signal", "regime", "watch_for", "alpha_or_beta")
        if parsed.get(k)
    )
    full = f"{lesson}" + (f"  [{extra}]" if extra else "")

    keys = _decision_keys(review) or ["symbol=" + str(review.get("symbol"))]
    with engine.begin() as conn:
        if force:
            conn.execute(text("DELETE FROM trade_lessons WHERE trade_review_id = :id"), {"id": review_id})
        for key in keys:
            conn.execute(text(
                """INSERT INTO trade_lessons (trade_review_id, symbol, pattern_key, lesson, model)
                   VALUES (:rid, :sym, :key, :lesson, :model)"""
            ), {"rid": review_id, "sym": review.get("symbol"), "key": key,
                "lesson": full, "model": resp.model})
    logger.info(f"reflect: lesson for {review.get('symbol')} (review {review_id}) over {len(keys)} keys")
    return full


def reflect_recent_losses(*, limit: int = 20, source: str = "live", force: bool = False) -> int:
    """Generate lessons for recent losing reviews that lack one. Returns count."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(
            """SELECT tr.id FROM trade_reviews tr
               WHERE tr.is_win = 0 AND tr.source = :src
                 AND (:force = 1 OR NOT EXISTS (
                     SELECT 1 FROM trade_lessons tl WHERE tl.trade_review_id = tr.id))
               ORDER BY tr.exit_date DESC
               LIMIT :lim"""
        ), {"src": source, "force": 1 if force else 0, "lim": limit}).scalars().all()
    done = 0
    for rid in rows:
        if generate_lesson(rid, force=force):
            done += 1
    logger.info(f"reflect_recent_losses: generated {done}/{len(rows)} lessons (source={source})")
    return done


# ---------------------------------------------------------------------------
# Retrieve (read-only context for future setups)
# ---------------------------------------------------------------------------

def retrieve_lessons(features: dict, *, limit: int = 3) -> list[dict]:
    """Lessons whose pattern_key matches this candidate's decision-time buckets,
    de-duplicated by trade (most specific/most recent first). Read-only."""
    keys = [f"{dim}={val}" for dim, val in mine._feature_buckets(features) if dim != "exit"]
    if not keys:
        return []
    q = text(
        """SELECT trade_review_id, symbol, pattern_key, lesson
           FROM trade_lessons WHERE pattern_key IN :keys
           ORDER BY id DESC"""
    ).bindparams(bindparam("keys", expanding=True))
    with get_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(q, {"keys": keys}).mappings().all()]
    seen, out = set(), []
    for r in rows:
        if r["trade_review_id"] in seen:
            continue
        seen.add(r["trade_review_id"])
        out.append(r)
        if len(out) >= limit:
            break
    return out


def format_lessons_for_prompt(lessons: list[dict]) -> str:
    """Compact, clearly-bounded block for an agent prompt. Empty string if none."""
    if not lessons:
        return ""
    lines = [f"- ({l['pattern_key']}) {l['lesson']}" for l in lessons]
    return (
        "PAST LESSONS from losing trades in similar setups (context only — "
        "weigh them, do not mechanically obey):\n" + "\n".join(lines)
    )
