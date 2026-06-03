"""Layer 3 (Apply, shadow-first): turn active learned_patterns into a bounded
conviction/size adjustment for a live candidate, and log it.

By default `settings.autolearn_active` is False, so this layer runs in SHADOW
mode: the adjustment is computed and written to `decision_adjustments` for
audit/validation, but the caller leaves conviction and sizing untouched. Flip
the flag (Phase 4) to let the same computed multipliers actually move picks.

Pattern keys are built with the EXACT same bucketing as the miner
(`learn.mine._feature_buckets`) so a candidate matches the rows mining produced.
The post-hoc `exit` dimension is intentionally excluded — exit reason is an
outcome, not known at decision time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import bindparam, text

from stockagent.db.session import get_engine
from stockagent.learn import mine

# Combined-multiplier bounds (mirror the per-pattern guardrails in mine._multipliers).
CONV_BOUNDS = (0.5, 1.3)
SIZE_BOUNDS = (0.5, 1.5)


@dataclass
class Adjustment:
    symbol: str
    base_conviction: float
    conviction_mult: float
    size_mult: float
    adj_conviction: float
    matched: list[dict] = field(default_factory=list)  # one entry per matched pattern
    shadow: bool = True

    @property
    def is_noop(self) -> bool:
        return not self.matched


def decision_features(
    *,
    sector: str | None = None,
    conviction: float | None = None,
    rr_ratio: float | None = None,
    rsi_entry: float | None = None,
    atr_pct_entry: float | None = None,
    vix_state: str | None = None,
    nifty_trend: str | None = None,
    market_cap_band: str | None = None,
) -> dict:
    """Review-shaped dict of DECISION-TIME features (no exit_reason). Fed to the
    shared bucketer so the candidate's pattern keys match mined rows exactly."""
    return {
        "sector": sector,
        "conviction": conviction,
        "rr_ratio": rr_ratio,
        "rsi_entry": rsi_entry,
        "atr_pct_entry": atr_pct_entry,
        "vix_state": vix_state,
        "nifty_trend": nifty_trend,
        "market_cap_band": market_cap_band,
        # exit_reason deliberately absent — it is an outcome, not a feature.
    }


def _fetch_active(conn, keys: list[str]) -> list[dict]:
    if not keys:
        return []
    q = text(
        """SELECT pattern_key, n, win_rate, avg_r, conviction_mult, size_mult
           FROM learned_patterns
           WHERE is_active = 1 AND pattern_key IN :keys"""
    ).bindparams(bindparam("keys", expanding=True))
    return [dict(r) for r in conn.execute(q, {"keys": keys}).mappings().all()]


def _clamp(x: float, lo_hi: tuple[float, float]) -> float:
    lo, hi = lo_hi
    return round(max(lo, min(hi, x)), 4)


def compute_adjustment(symbol: str, base_conviction: float, features: dict, *, shadow: bool = True) -> Adjustment:
    """Combine all active patterns the candidate matches into one bounded
    conviction/size multiplier. Pure read — does not write anything."""
    keys = [f"{dim}={val}" for dim, val in mine._feature_buckets(features)]
    engine = get_engine()
    with engine.connect() as conn:
        active = _fetch_active(conn, keys)

    conv_mult, size_mult = 1.0, 1.0
    matched = []
    for p in active:
        conv_mult *= p["conviction_mult"] if p["conviction_mult"] is not None else 1.0
        size_mult *= p["size_mult"] if p["size_mult"] is not None else 1.0
        matched.append({
            "pattern_key": p["pattern_key"], "n": p["n"],
            "win_rate": p["win_rate"], "avg_r": p["avg_r"],
            "conviction_mult": p["conviction_mult"], "size_mult": p["size_mult"],
        })

    conv_mult = _clamp(conv_mult, CONV_BOUNDS)
    size_mult = _clamp(size_mult, SIZE_BOUNDS)
    adj_conv = round(max(0.0, min(1.0, base_conviction * conv_mult)), 4)
    return Adjustment(
        symbol=symbol, base_conviction=round(base_conviction, 4),
        conviction_mult=conv_mult, size_mult=size_mult, adj_conviction=adj_conv,
        matched=matched, shadow=shadow,
    )


def persist_adjustment(run_id: str, adj: Adjustment) -> None:
    """Audit-log one adjustment. Always called (even when no pattern matched and
    even in shadow mode) so the log is a complete record of what learning saw."""
    with get_engine().begin() as conn:
        conn.execute(text(
            """INSERT INTO decision_adjustments
                   (run_id, symbol, base_conviction, adj_conviction,
                    conviction_mult, size_mult, matched_patterns, shadow)
               VALUES (:run_id,:symbol,:base,:adj,:cmult,:smult,:matched,:shadow)"""
        ), {
            "run_id": run_id, "symbol": adj.symbol,
            "base": adj.base_conviction, "adj": adj.adj_conviction,
            "cmult": adj.conviction_mult, "smult": adj.size_mult,
            "matched": json.dumps(adj.matched),
            "shadow": 1 if adj.shadow else 0,
        })
    if adj.matched:
        keys = ", ".join(m["pattern_key"] for m in adj.matched)
        mode = "shadow" if adj.shadow else "LIVE"
        logger.info(
            f"[{mode}] {adj.symbol}: matched [{keys}] → conv×{adj.conviction_mult} "
            f"size×{adj.size_mult} ({adj.base_conviction}→{adj.adj_conviction})"
        )
