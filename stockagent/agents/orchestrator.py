"""Orchestrator: runs all agents in parallel against one signal, combines via formula.

The combine step is intentionally NOT an LLM. It's mechanical so the system's
final decisions are auditable and reproducible. LLMs handle judgment INSIDE each
agent; the orchestrator handles arithmetic.
"""
from __future__ import annotations

import json
import statistics
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import text

from stockagent.agents.protocol import Agent, AgentVerdict, CombinedVerdict, Verdict
from stockagent.db.session import get_engine


# Conviction needed for the combined verdict to be "actionable bullish".
DEFAULT_MIN_COMBINED_CONVICTION = 0.45
# Required minimum number of agents that must produce a non-no_data verdict.
DEFAULT_MIN_AGENTS_OK = 2
# Disagreement above this fraction reduces conviction (penalize divided councils).
DEFAULT_DISAGREEMENT_PENALTY_THRESHOLD = 0.25


@dataclass
class OrchestratorConfig:
    min_combined_conviction: float = DEFAULT_MIN_COMBINED_CONVICTION
    min_agents_ok: int = DEFAULT_MIN_AGENTS_OK
    disagreement_penalty_threshold: float = DEFAULT_DISAGREEMENT_PENALTY_THRESHOLD
    parallel: bool = True
    max_workers: int = 4


class AgentOrchestrator:
    """Run multiple agents on one signal in parallel; combine deterministically."""

    def __init__(self, agents: list[Agent], config: OrchestratorConfig | None = None):
        self.agents = agents
        self.config = config or OrchestratorConfig()

    def evaluate(self, symbol: str, context: dict[str, Any]) -> CombinedVerdict:
        """Run every agent on the same context, then combine."""
        verdicts: dict[str, AgentVerdict] = {}

        if self.config.parallel and len(self.agents) > 1:
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as ex:
                futures = {ex.submit(self._safe_eval, a, symbol, context): a for a in self.agents}
                for fut in as_completed(futures):
                    a = futures[fut]
                    try:
                        verdicts[a.name] = fut.result()
                    except Exception as e:
                        logger.warning(f"agent {a.name} raised: {e}")
                        verdicts[a.name] = AgentVerdict(
                            agent=a.name, symbol=symbol, verdict="no_data", conviction=0.0,
                            reasoning=f"agent error: {type(e).__name__}: {str(e)[:200]}",
                            model=a.model,
                        )
        else:
            for a in self.agents:
                verdicts[a.name] = self._safe_eval(a, symbol, context)

        combined = self.combine(symbol, verdicts)
        return combined

    def _safe_eval(self, agent: Agent, symbol: str, context: dict[str, Any]) -> AgentVerdict:
        try:
            return agent.evaluate(symbol, context)
        except Exception as e:
            logger.warning(f"agent {agent.name} for {symbol}: {e}")
            return AgentVerdict(
                agent=agent.name, symbol=symbol, verdict="no_data", conviction=0.0,
                reasoning=f"exception: {type(e).__name__}: {str(e)[:200]}",
                model=agent.model,
            )

    # ------------------------------------------------------------------
    # Combine — pure formula. No LLM. No subjective weighting.
    # ------------------------------------------------------------------
    def combine(self, symbol: str, verdicts: dict[str, AgentVerdict]) -> CombinedVerdict:
        # Step 1: collect vetoes — any veto = avoid the trade entirely.
        vetoes = [v.agent for v in verdicts.values() if v.is_veto and v.verdict == "avoid"]
        if vetoes:
            return CombinedVerdict(
                symbol=symbol, final_verdict="avoid", conviction=0.0,
                bullish_weight=0.0, bearish_weight=0.0,
                veto_agents=vetoes, no_data_agents=[],
                disagreement=0.0, per_agent=verdicts,
            )

        # Step 2: identify no_data agents — they don't vote.
        no_data = [v.agent for v in verdicts.values() if v.verdict == "no_data"]
        ok_verdicts = [v for v in verdicts.values() if v.verdict not in ("no_data", "avoid")]

        # Step 3: minimum quorum check.
        if len(ok_verdicts) < self.config.min_agents_ok:
            return CombinedVerdict(
                symbol=symbol, final_verdict="neutral", conviction=0.0,
                bullish_weight=0.0, bearish_weight=0.0,
                veto_agents=vetoes, no_data_agents=no_data,
                disagreement=0.0, per_agent=verdicts,
            )

        # Step 4: weighted vote. Bullish agents push toward 'bullish', bearish push toward 'bearish'.
        # Use the agent's declared weight as the scaling factor.
        agent_weights = {v.agent: self._weight_for(v.agent) for v in ok_verdicts}
        bullish_w = sum(agent_weights[v.agent] for v in ok_verdicts if v.verdict == "bullish")
        bearish_w = sum(agent_weights[v.agent] for v in ok_verdicts if v.verdict == "bearish")
        neutral_w = sum(agent_weights[v.agent] for v in ok_verdicts if v.verdict == "neutral")
        total_w = bullish_w + bearish_w + neutral_w
        if total_w == 0:
            return CombinedVerdict(
                symbol=symbol, final_verdict="neutral", conviction=0.0,
                bullish_weight=0.0, bearish_weight=0.0,
                veto_agents=vetoes, no_data_agents=no_data,
                disagreement=0.0, per_agent=verdicts,
            )

        # Step 5: bullish conviction = sum of (bullish_agent.conviction × weight) / total_weight
        # Bearish reduces conviction by their weighted conviction.
        bullish_score = sum(v.conviction * agent_weights[v.agent] for v in ok_verdicts if v.verdict == "bullish")
        bearish_score = sum(v.conviction * agent_weights[v.agent] for v in ok_verdicts if v.verdict == "bearish")
        net_score = (bullish_score - bearish_score) / total_w  # range [-1, +1] roughly

        # Step 6: disagreement penalty. If agents are split, dampen conviction.
        convictions = [v.conviction for v in ok_verdicts]
        disagreement = statistics.pstdev(convictions) if len(convictions) > 1 else 0.0
        if disagreement > self.config.disagreement_penalty_threshold:
            net_score *= max(0.0, 1.0 - (disagreement - self.config.disagreement_penalty_threshold))

        # Step 7: final verdict
        if net_score >= self.config.min_combined_conviction:
            final = "bullish"
        elif net_score <= -self.config.min_combined_conviction:
            final = "bearish"
        else:
            final = "neutral"

        return CombinedVerdict(
            symbol=symbol,
            final_verdict=final,
            conviction=round(max(0.0, min(1.0, abs(net_score))), 4),
            bullish_weight=bullish_w,
            bearish_weight=bearish_w,
            veto_agents=vetoes,
            no_data_agents=no_data,
            disagreement=round(disagreement, 4),
            per_agent=verdicts,
        )

    def _weight_for(self, agent_name: str) -> float:
        for a in self.agents:
            if a.name == agent_name:
                return a.weight
        return 1.0


def persist_orchestrator_run(run_id: str, combined: CombinedVerdict) -> None:
    """Save every agent's verdict + the combined output to agent_outputs."""
    engine = get_engine()
    sql = text(
        """
        INSERT INTO agent_outputs (run_id, symbol, agent, model, prompt_version,
                                   verdict, conviction, reasoning, structured_json)
        VALUES (:run_id, :symbol, :agent, :model, :version,
                :verdict, :conviction, :reasoning, :payload)
        """
    )
    rows = []
    for v in combined.per_agent.values():
        rows.append({
            "run_id": run_id,
            "symbol": v.symbol,
            "agent": v.agent,
            "model": v.model,
            "version": "v2",
            "verdict": v.verdict,
            "conviction": v.conviction,
            "reasoning": v.reasoning,
            "payload": json.dumps({"flags": v.flags, "evidence": v.evidence, "is_veto": v.is_veto}),
        })
    # Final combined verdict as a synthetic 'orchestrator' agent row
    rows.append({
        "run_id": run_id,
        "symbol": combined.symbol,
        "agent": "orchestrator",
        "model": "(combine)",
        "version": "v2",
        "verdict": combined.final_verdict,
        "conviction": combined.conviction,
        "reasoning": (
            f"bullish_w={combined.bullish_weight:.2f} bearish_w={combined.bearish_weight:.2f} "
            f"disagreement={combined.disagreement:.2f} "
            f"vetoes={combined.veto_agents} no_data={combined.no_data_agents}"
        ),
        "payload": json.dumps({
            "veto_agents": combined.veto_agents,
            "no_data_agents": combined.no_data_agents,
            "bullish_weight": combined.bullish_weight,
            "bearish_weight": combined.bearish_weight,
            "disagreement": combined.disagreement,
        }),
    })
    if rows:
        with engine.begin() as c:
            c.execute(sql, rows)
