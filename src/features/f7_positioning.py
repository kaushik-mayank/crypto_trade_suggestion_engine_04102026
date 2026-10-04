"""Family 7 — crowd positioning (specs/02, **Tier B**).

``POS_DIVERGENCE`` is the most differentiated input this venue offers. An exchange publishing *both*
account-count-weighted and notional-value-weighted positioning is unusual, and the spread between them
separates participation from size: strongly positive means many small accounts are long while larger
capital is not; strongly negative means concentrated large longs against a dispersed crowd. Neither is
a signal alone, but they are different market states and **no OHLCV feature can distinguish them.**

The fundamental limitation, which no processing removes and which every value here carries in its
detail: the population is **CoinDCX's own users**, not the market. This measures one venue's retail
base, which may or may not proxy anything broader.

P8 passed — the endpoint is public (F-019) — so the family is retained. Had it failed, specs/02 is
explicit: delete the family, do not substitute a proxy.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from features.numerics import require_finite, safe_std
from features.spec import NO_HORIZON, ComputeContext, FeatureSpec, NotApplicable

PIT = (None,)
POPULATION_CAVEAT = "CoinDCX users only, not the market"

DAY_MS = 86_400_000


def _latest(ctx: ComputeContext) -> Any:
    if ctx.snapshot is None:
        raise NotApplicable("no snapshot attached, so Tier B cannot be read")
    return ctx.snapshot.tier_b("positioning", ctx.asset)


def _history(ctx: ComputeContext) -> tuple[Any, ...]:
    if ctx.snapshot is None:
        raise NotApplicable("no snapshot attached")
    return ctx.snapshot.tier_b_series("positioning", ctx.asset)


def pos_long_cnt(ctx: ComputeContext) -> float:
    """``count_percent.long`` as reported — the share of *accounts* that are long."""
    row = _latest(ctx)
    ctx.detail = {"ts_ms": row.ts_ms, "units": "percent of accounts", "population": POPULATION_CAVEAT}
    return require_finite(float(row.long_count_pct), what="POS_LONG_CNT")


def pos_long_val(ctx: ComputeContext) -> float:
    """``value_percent.long`` as reported — the share of *notional* that is long."""
    row = _latest(ctx)
    ctx.detail = {"ts_ms": row.ts_ms, "units": "percent of notional", "population": POPULATION_CAVEAT}
    return require_finite(float(row.long_value_pct), what="POS_LONG_VAL")


def pos_divergence(ctx: ComputeContext) -> float:
    """``count_percent.long - value_percent.long``.

    Positive: many small accounts long, larger capital not. Negative: concentrated large longs against
    a dispersed crowd.
    """
    row = _latest(ctx)
    ctx.detail = {"ts_ms": row.ts_ms, "long_count_pct": float(row.long_count_pct),
                  "long_value_pct": float(row.long_value_pct),
                  "interpretation": "many small longs" if row.divergence > 0 else
                                    "concentrated large longs" if row.divergence < 0 else "balanced",
                  "population": POPULATION_CAVEAT}
    return require_finite(float(row.divergence), what="POS_DIVERGENCE")


def _z(field: str, label: str):
    def compute(ctx: ComputeContext) -> float:
        row = _latest(ctx)
        history = _history(ctx)
        values = np.array([float(getattr(r, field)) for r in history], dtype=np.float64)
        if values.size < 30:
            raise NotApplicable(
                f"a positioning z-score needs a trailing sample; {values.size} snapshot(s) recorded so "
                "far (this is a snapshot endpoint with no history before recording started)")
        span_d = (history[-1].ts_ms - history[0].ts_ms) / DAY_MS
        sd = safe_std(values, ddof=1, what=f"{label} history")
        ctx.detail = {"observations": int(values.size), "history_days": round(span_d, 2),
                      "mean": float(values.mean()), "std": sd, "population": POPULATION_CAVEAT,
                      "caveat": "unreliable until months of recording exist (specs/02 Family 7)"}
        return require_finite((float(getattr(row, field)) - float(values.mean())) / sd,
                              what=f"POS_Z({label})")

    compute.__name__ = f"pos_z_{label}"
    return compute


def _roc(days: int):
    def compute(ctx: ComputeContext) -> float:
        row = _latest(ctx)
        history = _history(ctx)
        if not history:
            raise NotApplicable("no positioning history recorded yet")
        cutoff = row.ts_ms - days * DAY_MS
        earlier = [r for r in history if r.ts_ms <= cutoff]
        if not earlier:
            oldest = history[0].ts_ms
            raise NotApplicable(
                f"no positioning snapshot exists {days} day(s) before the current one; recorded history "
                f"reaches back {(row.ts_ms - oldest) / DAY_MS:.2f} day(s)")
        base = earlier[-1]
        ctx.detail = {"days": days, "from_ts_ms": base.ts_ms, "to_ts_ms": row.ts_ms,
                      "from_divergence": float(base.divergence),
                      "to_divergence": float(row.divergence),
                      "actual_span_days": round((row.ts_ms - base.ts_ms) / DAY_MS, 3),
                      "population": POPULATION_CAVEAT}
        return require_finite(float(row.long_count_pct - base.long_count_pct),
                              what=f"POS_ROC({days}d)")

    compute.__name__ = f"pos_roc_{days}d"
    return compute


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="POS_LONG_CNT", version="1.0.0", family=7, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("positioning",),
                needs_history=False, compute=pos_long_cnt,
                description="percent of accounts long (CoinDCX users)"),
    FeatureSpec(name="POS_LONG_VAL", version="1.0.0", family=7, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("positioning",),
                needs_history=False, compute=pos_long_val,
                description="percent of notional long (CoinDCX users)"),
    FeatureSpec(name="POS_DIVERGENCE", version="1.0.0", family=7, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("positioning",),
                needs_history=False, compute=pos_divergence,
                description="count-weighted minus value-weighted long share"),
    FeatureSpec(name="POS_Z_CNT", version="1.0.0", family=7, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("positioning",),
                needs_history=True, compute=_z("long_count_pct", "count"),
                description="z-score of the count-weighted long share"),
    FeatureSpec(name="POS_Z_VAL", version="1.0.0", family=7, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("positioning",),
                needs_history=True, compute=_z("long_value_pct", "value"),
                description="z-score of the value-weighted long share"),
    FeatureSpec(name="POS_ROC_1D", version="1.0.0", family=7, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("positioning",),
                needs_history=True, compute=_roc(1),
                description="change in the count-weighted long share over 1 day"),
    FeatureSpec(name="POS_ROC_3D", version="1.0.0", family=7, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("positioning",),
                needs_history=True, compute=_roc(3),
                description="change in the count-weighted long share over 3 days"),
)
