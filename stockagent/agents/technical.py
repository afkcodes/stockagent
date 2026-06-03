"""Technical analyst agent — multimodal, strict, evidence-based.

Reads (a) candidate signal + indicator snapshot + recent OHLCV bars,
(b) a 60-day candle chart with EMA/volume/RSI panels.

Returns AgentVerdict per the strict protocol. Refuses to evaluate without
sufficient context. Cites specific numeric values in `evidence`.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from stockagent.agents.base import LLMUnavailableError, call_llm, parse_json_safely
from stockagent.agents.charts import render_signal_chart
from stockagent.agents.protocol import Agent, AgentVerdict
from stockagent.config import settings


SYSTEM_PROMPT = """You are a strict, evidence-based technical-analysis judge for Indian equities (NSE).

You evaluate one symbol's RSI mean-reversion candidate signal. You receive:
  (1) numeric indicator snapshot at the bar of the signal,
  (2) the last 10 daily OHLCV bars,
  (3) a 60-day candlestick chart with EMA20/EMA50, volume panel, RSI(14) panel.

CONTEXT: this is a MEAN-REVERSION strategy. We BUY pullbacks. Most entries naturally
look like "recent decline" — that is the strategy's design, not a defect. You are
filtering only the worst 10-20% (TRUE falling knives).

OUTPUT RULES — strictly enforced:
- Output ONE valid JSON object. No code fences. No preamble. No trailing text.
- Cite specific numbers from the provided data in `evidence` (RSI value, EMA values,
  ATR, volume, prior support price, etc.). Do NOT use phrases like "appears to" or
  "feels like" — every claim must reference a number you were given.
- If critical data is missing or chart is unreadable, return verdict="no_data" with
  a reason. Do NOT guess.

Output schema:
{
  "verdict": "bullish" | "bearish" | "neutral" | "avoid" | "no_data",
  "conviction": <float 0..1>,
  "reasoning": "<2-4 short sentences citing specific numbers>",
  "flags": ["<concise concern 1>", "<concern 2>"],
  "evidence": {
    "rsi14": <float or null>,
    "ema20": <float or null>,
    "ema50": <float or null>,
    "atr_pct": <float or null>,
    "trend_assessment": "uptrend" | "downtrend" | "range" | "transition",
    "support_level": <float or null>,
    "volume_pattern": "drying_up" | "panic" | "distribution" | "accumulation" | "normal"
  },
  "is_veto": false
}

VETO RULES (set is_veto=true and verdict="avoid"):
- Stock has lost ≥25% over the last 4-6 weeks with NO failed-down attempts (relentless straight-line decline)
- Repeated gap-downs (3+ in 20 days) breaking multi-month support
- Earnings-disaster pattern: huge gap-down followed by drift lower on heavy volume
- ATR/price > 8% (catastrophically unstable)
- Chart unreadable or data inconsistent

VERDICT THRESHOLDS:
- bullish, conviction 0.65-0.85: ordinary pullback in a liquid name; volume drying on dip,
  lower-wick rejection, EMA50 still rising or flat
- bullish, conviction 0.5-0.65: pullback is steeper but not clearly a downtrend; mixed signals
- neutral, conviction 0.3-0.5: hard to tell; signal is borderline
- bearish, conviction 0.5-0.85: clear bearish structure but not catastrophic (no veto)
- avoid + is_veto=true: see VETO RULES

Default to bullish 0.6 for "ordinary RSI<30 pullback in a liquid Nifty 500 name with
intact higher-timeframe structure" — the deterministic backtest proved this works
(median Sharpe +0.69 across 8 walk-forward windows, 100% positive). Your job is to
filter true outliers, not to second-guess every entry.
"""


def _format_user_prompt(symbol: str, signal_dict: dict, recent_bars: list[dict]) -> str:
    bars_str = "\n".join(
        f"  {b['date']}: O={b['open']:.2f} H={b['high']:.2f} L={b['low']:.2f} "
        f"C={b['close']:.2f} V={int(b['volume']):,}"
        for b in recent_bars
    )
    snap = signal_dict.get("indicator_snapshot", {})
    snap_lines = "\n".join(f"  {k}: {v:.4f}" for k, v in snap.items() if isinstance(v, (int, float)))
    return (
        f"Symbol: {symbol}\n"
        f"Strategy: {signal_dict.get('strategy', 'rsi_mean_reversion')}\n"
        f"Trigger: {signal_dict.get('rationale', '')}\n"
        f"Bar date: {signal_dict.get('bar_date', '')}\n"
        f"Entry close: {signal_dict.get('entry_price', 0):.2f}    "
        f"Suggested stop: {signal_dict.get('stop_price', 0):.2f}    "
        f"Stop distance: {signal_dict.get('stop_dist_pct', 0):.2f}%\n\n"
        f"Last 10 daily bars:\n{bars_str}\n\n"
        f"Indicators on bar date:\n{snap_lines}\n"
    )


class TechnicalAgent(Agent):
    name = "technical"
    weight = 1.5  # technical signal is the strategy's source-of-truth, weighted highest

    def __init__(self):
        self.model = settings.model_technical

    def evaluate(self, symbol: str, context: dict[str, Any]) -> AgentVerdict:
        signal_dict = context.get("signal") or {}
        recent_bars = context.get("recent_bars") or []
        bar_date = signal_dict.get("bar_date")

        if not signal_dict or not recent_bars or not bar_date:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning="missing signal/bars/bar_date in context",
                model=self.model,
            )

        try:
            chart_url = render_signal_chart(symbol, bar_date)
        except Exception as e:
            logger.warning(f"chart render failed for {symbol}: {e}")
            chart_url = None

        user_prompt = _format_user_prompt(symbol, signal_dict, recent_bars)
        # Read-only enrichment: lessons from past losses in similar setups. Never
        # changes the deterministic arithmetic — only what the judge gets to see.
        prior_lessons = context.get("prior_lessons") or []
        if prior_lessons:
            from stockagent.learn.reflect import format_lessons_for_prompt
            block = format_lessons_for_prompt(prior_lessons)
            if block:
                user_prompt = f"{user_prompt}\n\n{block}"

        try:
            resp = call_llm(
                model=self.model, system=SYSTEM_PROMPT, user=user_prompt,
                max_tokens=600, image_data_url=chart_url,
            )
        except LLMUnavailableError:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning="OPENROUTER_API_KEY not set", model=self.model,
            )
        except Exception as e:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning=f"LLM call failed: {type(e).__name__}: {str(e)[:160]}",
                model=self.model,
            )

        parsed = parse_json_safely(resp.content)
        if not parsed or ("verdict" not in parsed and "conviction" not in parsed):
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning=f"unusable LLM response: {resp.content[:160]!r}",
                model=resp.model,
            )

        verdict = str(parsed.get("verdict", "neutral")).lower()
        if verdict not in ("bullish", "bearish", "neutral", "avoid", "no_data"):
            verdict = "neutral"
        conviction = float(parsed.get("conviction", 0.5))
        is_veto = bool(parsed.get("is_veto", False)) and verdict == "avoid"
        return AgentVerdict(
            agent=self.name, symbol=symbol, verdict=verdict,
            conviction=max(0.0, min(1.0, conviction)),
            reasoning=str(parsed.get("reasoning", ""))[:1800],
            flags=list(parsed.get("flags", []) or []),
            evidence=dict(parsed.get("evidence", {}) or {}),
            model=resp.model,
            is_veto=is_veto,
        )


# ----------------------------------------------------------------------
# Backwards-compat shims for the original signal-driven coordinator (cli watchlist).
# Old code path used `evaluate_signal()` directly; the new orchestrator-based
# coordinator uses TechnicalAgent.evaluate(). Both work.
# ----------------------------------------------------------------------

def evaluate_signal(signal, recent_bars: list[dict]):
    """Legacy entry point used by the original coordinator.run_coordinator."""
    from dataclasses import dataclass

    @dataclass
    class _Verdict:
        symbol: str
        verdict: str
        conviction: float
        reasoning: str
        key_concerns: list
        model: str

    agent = TechnicalAgent()
    sig_dict = {
        "strategy": getattr(signal, "strategy", "rsi_mean_reversion"),
        "rationale": getattr(signal, "rationale", ""),
        "bar_date": getattr(signal, "bar_date", None) and signal.bar_date.date(),
        "entry_price": float(getattr(signal, "entry_price", 0.0)),
        "stop_price": float(getattr(signal, "stop_price", 0.0)),
        "indicator_snapshot": getattr(signal, "indicator_snapshot", {}),
        "stop_dist_pct": (
            (signal.entry_price - signal.stop_price) / signal.entry_price * 100
            if getattr(signal, "entry_price", 0) > 0
            else 0
        ),
    }
    v = agent.evaluate(signal.symbol, {"signal": sig_dict, "recent_bars": recent_bars})
    return _Verdict(
        symbol=v.symbol, verdict=v.verdict, conviction=v.conviction,
        reasoning=v.reasoning, key_concerns=v.flags, model=v.model,
    )


def evaluate_signal_or_neutral(signal, recent_bars: list[dict]):
    """Legacy wrapper — never raises. Falls back to neutral 0.5 on any error."""
    try:
        return evaluate_signal(signal, recent_bars)
    except Exception as e:
        logger.warning(f"technical legacy shim error: {e}")
        from dataclasses import dataclass

        @dataclass
        class _V:
            symbol: str
            verdict: str
            conviction: float
            reasoning: str
            key_concerns: list
            model: str

        return _V(symbol=signal.symbol, verdict="neutral", conviction=0.5,
                  reasoning=f"shim fallback: {e}", key_concerns=[], model="(error)")


def persist_verdict(run_id: str, signal, v) -> int:
    """Legacy compatibility — kept so old coordinator code still works."""
    import json
    from sqlalchemy import text
    from stockagent.db.session import get_engine

    sql = text(
        """INSERT INTO agent_outputs (run_id, symbol, agent, model, prompt_version,
                                       verdict, conviction, reasoning, structured_json)
           VALUES (:run_id, :symbol, :agent, :model, :version,
                   :verdict, :conviction, :reasoning, :payload)"""
    )
    with get_engine().begin() as c:
        res = c.execute(sql, {
            "run_id": run_id, "symbol": signal.symbol, "agent": "technical",
            "model": v.model, "version": "v2", "verdict": v.verdict,
            "conviction": v.conviction, "reasoning": v.reasoning,
            "payload": json.dumps({"key_concerns": list(getattr(v, "key_concerns", []))}),
        })
        return res.lastrowid or 0
