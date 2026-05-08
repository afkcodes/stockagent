"""Sentiment agent — scans recent news for a symbol, classifies tone + flags concerns.

Primary use: defensive alerts on HELD positions. Secondary: confluence on entry signals.

Operates on recent news headlines (last 10-14 days). The LLM only sees titles +
short excerpts so we stay strict and structured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from stockagent.agents.base import LLMUnavailableError, call_llm, parse_json_safely
from stockagent.agents.protocol import Agent, AgentVerdict
from stockagent.config import settings
from stockagent.data.news import fetch_recent_news, persist_news


SYSTEM_PROMPT = """You are a strict news sentiment analyst for Indian equity swing trading.

You receive recent news headlines for one stock. Classify the OVERALL tone for a
1-3 week swing horizon. Be conservative — most headlines are neutral/promotional.

OUTPUT RULES (strict):
- Output ONE valid JSON object. No code fences. No preamble. No trailing text.
- Cite specific headlines (with their index #) in `evidence.key_headlines`.
- If no headlines are provided OR all are >14 days old, return verdict="no_data".

Output schema:
{
  "verdict": "bullish" | "bearish" | "neutral" | "avoid" | "no_data",
  "conviction": <float 0..1>,
  "reasoning": "<2-3 short sentences>",
  "flags": ["<concise concern, e.g. 'rating_downgrade', 'fraud_investigation'>"],
  "evidence": {
    "n_headlines": <int>,
    "key_headlines": ["#3: <title excerpt>", "#7: <title>"],
    "tone_distribution": {"bullish": <count>, "bearish": <count>, "neutral": <count>}
  },
  "is_veto": false
}

VETO RULES (set is_veto=true and verdict="avoid"):
- SEBI investigation / fraud allegation / regulatory action
- Auditor resignation
- Promoter pledge increase or default
- Q-on-Q earnings disaster (50%+ profit drop) confirmed in headlines
- Credit rating downgrade to junk

Bearish (no veto, just bearish):
- Single negative analyst note or minor regulatory fine
- Sector weakness mentioned without company-specific issues
- Sales/profit miss but in line with sector

Default to neutral 0.5 when headlines are noisy but not directional.
"""


def _format_headlines(items: list) -> str:
    if not items:
        return "(no headlines)"
    lines = []
    for i, it in enumerate(items, 1):
        age = ""
        if it.published_at:
            delta = datetime.now(timezone.utc) - it.published_at
            age = f"  ({delta.days}d ago)"
        lines.append(f"  #{i}{age} [{it.source}] {it.title[:200]}")
    return "\n".join(lines)


class SentimentAgent(Agent):
    name = "sentiment"
    weight = 0.7  # softer weight than technical/fundamental — news is noisy

    def __init__(self):
        self.model = settings.model_sentiment

    def evaluate(self, symbol: str, context: dict[str, Any]) -> AgentVerdict:
        items = context.get("news_items")
        if items is None:
            try:
                items = fetch_recent_news(symbol, max_items=10)
                if items:
                    persist_news(items)
            except Exception as e:
                logger.warning(f"sentiment fetch news for {symbol}: {e}")
                items = []

        if not items:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning="no recent news headlines available",
                model=self.model,
            )

        try:
            resp = call_llm(
                model=self.model, system=SYSTEM_PROMPT,
                user=f"Symbol: {symbol}\n\nRecent headlines:\n{_format_headlines(items)}\n",
                max_tokens=400,
            )
        except LLMUnavailableError:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning="OPENROUTER_API_KEY not set", model=self.model,
            )
        except Exception as e:
            return AgentVerdict(
                agent=self.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning=f"sentiment LLM error: {type(e).__name__}: {str(e)[:160]}",
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
            flags=list(parsed.get("flags", []) or []),
            evidence=dict(parsed.get("evidence", {}) or {}),
            model=resp.model,
            is_veto=is_veto,
        )
