"""Strict agent protocol — every agent emits the same structured verdict.

Design principles:
- Heartless: no "I feel" or "appears to" language allowed in output. Cite numbers.
- Strict: critical missing data → AgentVerdict(verdict='no_data') NOT a guess.
- Auditable: every verdict persists to agent_outputs with the input snapshot
  so we can later analyze which agents were right/wrong.
- Composable: orchestrator combines verdicts via formula, never via LLM.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

Verdict = Literal["bullish", "bearish", "neutral", "avoid", "no_data"]


class AgentVerdict(BaseModel):
    """Strict schema. Every agent returns exactly this shape."""

    agent: str = Field(..., description="agent name, e.g. 'technical'")
    symbol: str
    verdict: Verdict
    conviction: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., max_length=2000)
    flags: list[str] = Field(default_factory=list, description="specific concerns")
    evidence: dict[str, Any] = Field(default_factory=dict, description="numeric facts the verdict was based on")
    model: str = ""
    is_veto: bool = Field(default=False, description="if True, coordinator must reject the trade regardless of others")


@dataclass
class CombinedVerdict:
    """Output of orchestrator combine step. Pure formula, deterministic."""

    symbol: str
    final_verdict: Verdict
    conviction: float            # weighted-average of bullish-direction agent convictions
    bullish_weight: float        # sum of weights of bullish agents
    bearish_weight: float        # sum of weights of bearish agents
    veto_agents: list[str]       # any agent that vetoed (verdict=avoid with is_veto=True)
    no_data_agents: list[str]    # agents that couldn't evaluate
    disagreement: float          # stdev of agent convictions; 0 = unanimous
    per_agent: dict[str, AgentVerdict]


class Agent(ABC):
    """Base class for all agents. Subclasses set name, model, weight."""

    name: str = "abstract"
    model: str = ""
    weight: float = 1.0

    @abstractmethod
    def evaluate(self, symbol: str, context: dict[str, Any]) -> AgentVerdict:
        """Return a structured verdict for `symbol`.

        `context` is a free-form dict the orchestrator populates with whatever an agent
        might need (signal record, recent bars, fundamentals snapshot, news items, etc.).
        Agents must:
          - return verdict='no_data' if a critical input is missing (don't guess)
          - cite specific numeric values in `evidence`
          - never emit prose without numbers backing it
        """
        ...
