"""Auto-learning / evidence-backed feedback loop.

Phase 1 (Capture): record a frozen decision-context + realized-outcome snapshot
for every closed trade into `trade_reviews`. Pure data — no behaviour change.

See docs/autolearn_design.md for the full design.
"""
