"""Arbitration: conflict resolution, merging, dedup and the portfolio view (specs/07)."""
from arbitration.pipeline import (
    ArbitrationEvent,
    ArbitrationResult,
    arbitrate,
    dedup_class4,
    expose_portfolio,
    merge_same_direction,
    rank_within_class,
    resolve_conflicts,
)

__all__ = ["ArbitrationEvent", "ArbitrationResult", "arbitrate", "dedup_class4",
           "expose_portfolio", "merge_same_direction", "rank_within_class", "resolve_conflicts"]
