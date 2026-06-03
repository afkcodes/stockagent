"""Auto-learning / evidence-backed feedback loop.

Phase 1 (Capture): record a frozen decision-context + realized-outcome snapshot
for every closed trade into `trade_reviews`. Pure data — no behaviour change.

Phase 2 (Mine): aggregate the reviews corpus into `agent_reliability` and
`learned_patterns` with win rate, expectancy (R) and Wilson confidence. Still
read-only — nothing is applied to live picks yet.

Phase 3 (Shadow): compute a bounded conviction/size multiplier per live
candidate from active patterns and log it to `decision_adjustments`. Applied
to picks only when `settings.autolearn_active` is flipped (Phase 4).

Phase 5 (Reflect): LLM post-mortems on losses → `trade_lessons`, surfaced as
read-only context on similar future setups. Enriches judgment only; never
touches the deterministic arithmetic.

See docs/autolearn_design.md for the full design.
"""
