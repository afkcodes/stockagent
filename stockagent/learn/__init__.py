"""Auto-learning / evidence-backed feedback loop.

Phase 1 (Capture): record a frozen decision-context + realized-outcome snapshot
for every closed trade into `trade_reviews`. Pure data — no behaviour change.

Phase 2 (Mine): aggregate the reviews corpus into `agent_reliability` and
`learned_patterns` with win rate, expectancy (R) and Wilson confidence. Still
read-only — nothing is applied to live picks yet.

See docs/autolearn_design.md for the full design.
"""
