"""Technical analyst agent.

Job: given a candidate signal from a deterministic strategy, evaluate the underlying
chart context and decide whether the setup is genuinely high-quality. Returns a
structured verdict so the coordinator can rank/filter signals.

The prompt deliberately constrains the model to the pre-computed indicators we hand
it — no chart drawing, no random invocation of unrelated TA. We are using the LLM
as a JUDGE over numeric inputs, not as an indicator computer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger

from stockagent.agents.base import LLMUnavailableError, call_llm, parse_json_safely
from stockagent.config import settings
from stockagent.signals.daily import Signal


SYSTEM_PROMPT = """You are an experienced Indian-equity swing trader judging RSI MEAN-REVERSION setups.

You receive (a) a candidate signal where RSI(14) just crossed below 30 (oversold), and
(b) a 60-day daily candlestick chart with EMA20/EMA50, volume, RSI panel.

CRITICAL CONTEXT: this is a mean-reversion strategy. We BUY pullbacks. Almost every
mean-reversion entry will look like "recent downtrend on the chart" — that's the point.
A 5-15% pullback over 1-3 weeks is NORMAL and tradeable. We are NOT looking for confirmed
uptrends. Do NOT reject signals just because the last 2-4 weeks were down.

Reject (bearish) ONLY if you see a TRUE falling-knife scenario:
- Stock has lost 25%+ over 4-6 weeks with NO failed-down attempts (relentless straight-line decline)
- Massive distribution volume + repeated gap-downs + breaking multi-month support
- Price below the 200-day reference and freshly making new yearly lows
- Earnings-disaster pattern: huge gap-down followed by drift lower on heavy volume

Lean bullish (conviction 0.5-0.8) when:
- Stock pulled back into oversold from a previously rising structure (EMA50 still rising or flat)
- Big lower wicks at recent lows (rejection of further downside)
- Volume on the dip looks like normal selling, not panic (no climactic spike)
- Stock pulled back to a prior support shelf, prior breakout zone, or rising EMA50

Default to bullish 0.55-0.65 when the setup is "ordinary pullback in liquid name" —
that IS the strategy's edge. The deterministic backtest proved 100% positive windows on
Nifty 500 RSI mean-reversion. Your job is to filter the worst 10-20% (true falling knives),
NOT to second-guess every entry.

Output ONLY valid JSON, no preamble, no code fences:
{
  "verdict": "bullish" | "bearish" | "neutral",
  "conviction": <float 0..1>,
  "reasoning": "<one short paragraph>",
  "key_concerns": ["<bullet 1>", "<bullet 2>"]
}
"""


@dataclass
class TechnicalVerdict:
    symbol: str
    verdict: str  # "bullish" | "bearish" | "neutral"
    conviction: float
    reasoning: str
    key_concerns: list[str]
    model: str


def _format_user_prompt(signal: Signal, recent_bars: list[dict]) -> str:
    bars_str = "\n".join(
        f"  {b['date']}: O={b['open']:.2f} H={b['high']:.2f} L={b['low']:.2f} "
        f"C={b['close']:.2f} V={int(b['volume']):,}"
        for b in recent_bars
    )
    snap = signal.indicator_snapshot
    snap_lines = "\n".join(f"  {k}: {v:.4f}" for k, v in snap.items() if k not in ("open", "high", "low", "close", "volume"))
    return (
        f"Symbol: {signal.symbol}\n"
        f"Strategy fired: {signal.strategy}\n"
        f"Trigger rationale: {signal.rationale}\n"
        f"Bar date: {signal.bar_date.date()}\n"
        f"Entry close: {signal.entry_price:.2f}    Suggested stop: {signal.stop_price:.2f}\n"
        f"Stop distance: {(signal.entry_price - signal.stop_price) / signal.entry_price * 100:.2f}%\n\n"
        f"Last 10 daily bars:\n{bars_str}\n\n"
        f"Indicators on bar date:\n{snap_lines}\n"
    )


def evaluate_signal(signal: Signal, recent_bars: list[dict]) -> TechnicalVerdict:
    """Multimodal evaluation: text snapshot + 60-day chart image → structured verdict.
    Raises LLMUnavailableError if no API key, or RuntimeError if response is unusable."""
    from stockagent.agents.charts import render_signal_chart

    model = settings.model_technical
    user_prompt = _format_user_prompt(signal, recent_bars)
    chart_url = render_signal_chart(signal.symbol, signal.bar_date.date())
    # Drop response_format=json_object — multimodal kimi-k2.5 returns empty when set.
    resp = call_llm(
        model=model,
        system=SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=500,
        image_data_url=chart_url,
    )
    parsed = parse_json_safely(resp.content)
    if not parsed or ("verdict" not in parsed and "conviction" not in parsed):
        raise RuntimeError(f"model {resp.model!r} returned no usable JSON (content: {resp.content[:120]!r})")
    return TechnicalVerdict(
        symbol=signal.symbol,
        verdict=str(parsed.get("verdict", "neutral")).lower(),
        conviction=float(parsed.get("conviction", 0.5)),
        reasoning=str(parsed.get("reasoning", "")),
        key_concerns=list(parsed.get("key_concerns", []) or []),
        model=resp.model,
    )


def evaluate_signal_or_neutral(signal: Signal, recent_bars: list[dict]) -> TechnicalVerdict:
    """Wrapper: if LLM is unavailable, return a neutral verdict with conviction 0.5
    so the deterministic signal still flows through the coordinator unfiltered."""
    try:
        return evaluate_signal(signal, recent_bars)
    except LLMUnavailableError:
        return TechnicalVerdict(
            symbol=signal.symbol,
            verdict="neutral",
            conviction=0.5,
            reasoning="LLM unavailable; passing through deterministic signal.",
            key_concerns=[],
            model="(stub)",
        )
    except Exception as e:
        logger.warning(f"technical agent failed for {signal.symbol}: {e}")
        # Pass through at 0.5 (same as no-key stub) so a flaky/broken LLM doesn't
        # silently filter out every signal. Better to surface picks than hide them.
        return TechnicalVerdict(
            symbol=signal.symbol,
            verdict="neutral",
            conviction=0.5,
            reasoning=f"agent unreachable; passing through. ({type(e).__name__})",
            key_concerns=[],
            model="(error)",
        )


def persist_verdict(run_id: str, signal: Signal, v: TechnicalVerdict) -> int:
    """Save the agent output to agent_outputs."""
    from sqlalchemy import text
    from stockagent.db.session import get_engine

    sql = text(
        """
        INSERT INTO agent_outputs (run_id, symbol, agent, model, prompt_version,
                                   verdict, conviction, reasoning, structured_json)
        VALUES (:run_id, :symbol, :agent, :model, :version,
                :verdict, :conviction, :reasoning, :payload)
        """
    )
    payload = {
        "run_id": run_id,
        "symbol": signal.symbol,
        "agent": "technical",
        "model": v.model,
        "version": "v1",
        "verdict": v.verdict,
        "conviction": v.conviction,
        "reasoning": v.reasoning,
        "payload": json.dumps({"key_concerns": v.key_concerns, "rationale_input": signal.rationale}),
    }
    engine = get_engine()
    with engine.begin() as conn:
        res = conn.execute(sql, payload)
        return res.lastrowid or 0
