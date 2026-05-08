"""Fundamental agent — quality filter on entries.

Reads the fundamentals snapshot from DB (refreshed monthly via screener.in scrape).
Applies mechanical thresholds for quality + asks the LLM to weigh trends. Vetoes
on hard red flags (very high debt, high pledged%, fraud-pattern signs).
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from stockagent.agents.base import LLMUnavailableError, call_llm, parse_json_safely
from stockagent.agents.protocol import Agent, AgentVerdict
from stockagent.config import settings
from stockagent.data.screener import Fundamentals, get_or_fetch


SYSTEM_PROMPT = """You are a strict fundamental-quality analyst for Indian equities.

You receive one symbol's fundamentals snapshot. Decide whether the COMPANY is
healthy enough to be a candidate for a 1-3 week mean-reversion swing trade.

You are NOT picking long-term value plays. You are filtering OUT sick companies
where a chart bounce can be wiped out by a fundamental gap-down (rating downgrade,
solvency news, fraud disclosure).

OUTPUT RULES (strict):
- Output ONE valid JSON object. No code fences. No preamble. No trailing text.
- Cite specific numeric values from the input in `evidence`. Numbers only.
- If essential ratios are missing (no PE, no ROE, no debt info), return verdict="no_data".

Output schema:
{
  "verdict": "bullish" | "bearish" | "neutral" | "avoid" | "no_data",
  "conviction": <float 0..1>,
  "reasoning": "<2-4 short sentences>",
  "flags": ["<concise concern>"],
  "evidence": {
    "pe": <number or null>,
    "roe": <number or null>,
    "roce": <number or null>,
    "debt_equity": <number or null>,
    "promoter_holding_pct": <number or null>,
    "pledged_pct": <number or null>,
    "profit_growth_3y_pct": <number or null>,
    "quality_score": <0..1>
  },
  "is_veto": false
}

VETO RULES (set is_veto=true and verdict="avoid"):
- Pledged percent > 50%  (high promoter pledging — fraud / margin call risk)
- Debt-to-equity > 3.0   (extreme leverage)
- Promoter holding < 15% AND pledged > 0  (low conviction owners with margin risk)
- Profit growth 3Y < -25% AND sales growth 3Y < -10%  (collapsing business)

Verdict mapping:
- bullish 0.7-0.85: clean ratios — ROE > 15%, debt/eq < 0.6, growing top + bottom line, no pledge
- bullish 0.5-0.7: solid but not standout — ROE 10-15% or moderate debt
- neutral 0.3-0.5: mixed signals; weak growth but not declining
- bearish 0.5-0.7: weak fundamentals (low ROE, declining growth) but not catastrophic
- avoid + is_veto=true: see VETO RULES

Default to bullish 0.55 for "ordinary Nifty 500 company with mixed but not bad ratios"
because we are filtering, not selecting.
"""


def _format_user_prompt(f: Fundamentals) -> str:
    rf = f.red_flags()
    return (
        f"Symbol: {f.symbol}\n"
        f"As of: {f.as_of_date}\n"
        f"Quality score (rule-based, 0-1): {f.quality_score():.2f}\n"
        f"Mechanical red flags: {', '.join(rf) if rf else 'none'}\n\n"
        f"Ratios:\n"
        f"  Market cap (cr):     {f.market_cap}\n"
        f"  P/E:                 {f.pe}\n"
        f"  P/B:                 {f.pb}\n"
        f"  PEG:                 {f.peg}\n"
        f"  ROE (%):             {f.roe}\n"
        f"  ROCE (%):            {f.roce}\n"
        f"  Debt/Equity:         {f.debt_equity}\n"
        f"  Promoter holding %:  {f.promoter_holding}\n"
        f"  Pledged %:           {f.pledged_pct}\n"
        f"  Sales growth 3Y %:   {f.sales_growth_3y}\n"
        f"  Profit growth 3Y %:  {f.profit_growth_3y}\n"
    )


class FundamentalAgent(Agent):
    name = "fundamental"
    weight = 1.0

    def __init__(self):
        self.model = settings.model_fundamental

    def evaluate(self, symbol: str, context: dict[str, Any]) -> AgentVerdict:
        try:
            f = get_or_fetch(symbol)
        except Exception as e:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning=f"fundamentals fetch failed: {type(e).__name__}: {str(e)[:160]}",
                model=self.model,
            )

        if f is None:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning="no fundamentals available from screener.in (delisted / SME / scrape blocked)",
                model=self.model,
            )

        # Mechanical veto gates BEFORE the LLM call — saves a roundtrip.
        rf = f.red_flags()
        veto_flags = [x for x in rf if any(k in x for k in ("high_pledged", "high_debt"))]
        if veto_flags:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="avoid", conviction=0.0,
                reasoning=f"mechanical veto: {', '.join(veto_flags)}",
                flags=veto_flags,
                evidence={
                    "pe": f.pe, "roe": f.roe, "debt_equity": f.debt_equity,
                    "promoter_holding_pct": f.promoter_holding, "pledged_pct": f.pledged_pct,
                    "quality_score": f.quality_score(),
                },
                model=self.model, is_veto=True,
            )

        try:
            resp = call_llm(
                model=self.model, system=SYSTEM_PROMPT, user=_format_user_prompt(f),
                max_tokens=400,
            )
        except LLMUnavailableError:
            # No LLM? fall back to mechanical quality_score.
            qs = f.quality_score()
            verdict = "bullish" if qs > 0.55 else "bearish" if qs < 0.4 else "neutral"
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict=verdict, conviction=qs,
                reasoning=f"LLM unavailable; using mechanical quality_score={qs:.2f}",
                flags=rf,
                evidence={
                    "pe": f.pe, "roe": f.roe, "debt_equity": f.debt_equity,
                    "quality_score": qs,
                },
                model="(rule-based)",
            )
        except Exception as e:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning=f"fundamental LLM error: {type(e).__name__}: {str(e)[:160]}",
                model=self.model,
            )

        parsed = parse_json_safely(resp.content)
        if not parsed or "verdict" not in parsed:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning=f"unusable LLM response: {resp.content[:160]!r}",
                model=resp.model,
            )

        verdict = str(parsed.get("verdict", "neutral")).lower()
        if verdict not in ("bullish", "bearish", "neutral", "avoid", "no_data"):
            verdict = "neutral"
        is_veto = bool(parsed.get("is_veto", False)) and verdict == "avoid"
        return AgentVerdict(
            agent=self.name, symbol=symbol, verdict=verdict,
            conviction=max(0.0, min(1.0, float(parsed.get("conviction", 0.5)))),
            reasoning=str(parsed.get("reasoning", ""))[:1800],
            flags=list(parsed.get("flags", []) or []) + rf,
            evidence={
                **(dict(parsed.get("evidence", {}) or {})),
                "quality_score": f.quality_score(),
            },
            model=resp.model,
            is_veto=is_veto,
        )
