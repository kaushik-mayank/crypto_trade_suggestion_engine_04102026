"""The Decimal/float boundary (specs/12 §3), as two named functions rather than scattered casts.

> ``Decimal -> float64`` on entry to feature computation.
> ``float64 -> Decimal -> round to tick`` on any value that becomes a published price.

Named, because a cast written inline is invisible at review and this is the seam where a price can
quietly lose the exactness the store went to trouble to keep. Rounding direction is not cosmetic:
specs/08 §10 rounds **stops away from entry and targets toward it**, so that tick rounding can only
ever make a published level more conservative, never less.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum

import numpy as np

from core.errors import FeatureError, UnitError

COMPONENT = "features.convert"


class RoundMode(str, Enum):
    """Which way tick rounding must err (specs/08 §10)."""

    STOP_LONG = "stop_long"        # below entry -> round down, further away
    STOP_SHORT = "stop_short"      # above entry -> round up, further away
    TARGET_LONG = "target_long"    # above entry -> round down, nearer
    TARGET_SHORT = "target_short"  # below entry -> round up, nearer
    NEAREST = "nearest"            # for display only, never for a level


def to_float(value: Decimal | float | int) -> float:
    """``Decimal -> float64`` at the entry to computation.

    A non-finite input is a bug, not a number: it would propagate as NaN through every statistic
    downstream and surface far from its cause.
    """
    try:
        f = float(value)
    except (TypeError, ValueError, InvalidOperation) as e:
        raise FeatureError(
            f"cannot convert {value!r} to float for computation",
            component=COMPONENT, operation="to_float",
            context={"value": repr(value), "type": type(value).__name__},
            likely_cause="a None or a non-numeric string reached the feature layer",
            remediation="reject the row at normalization; the feature layer assumes numbers",
            cause=e,
        ) from e
    if not math.isfinite(f):
        raise FeatureError(
            f"non-finite value {f} entering feature computation",
            component=COMPONENT, operation="to_float",
            context={"value": repr(value)},
            likely_cause="an infinity or NaN was stored, or a Decimal('NaN') survived normalization",
            remediation="find the source row; features must not begin from a non-finite input",
        )
    return f


def closes_to_array(values: list[Decimal]) -> np.ndarray:
    """A float64 array for numpy/scipy. One conversion point, so the boundary stays auditable."""
    return np.fromiter((to_float(v) for v in values), dtype=np.float64, count=len(values))


def to_price(value: float, *, tick: Decimal, mode: RoundMode) -> Decimal:
    """``float64 -> Decimal -> round to tick``, in the direction ``mode`` requires.

    ``tick`` comes from live instrument metadata (invariant 1) — never a constant.
    """
    if not math.isfinite(value):
        raise FeatureError(
            f"refusing to publish a non-finite price ({value})",
            component=COMPONENT, operation="to_price",
            context={"value": str(value), "mode": mode.value},
            likely_cause="a degenerate computation produced inf/nan and was not caught by its guard",
            remediation="the feature that produced this must return NUMERICAL_FAILURE instead",
        )
    if tick <= 0:
        raise UnitError(
            f"tick size must be positive, got {tick}",
            component=COMPONENT, operation="to_price",
            context={"tick": str(tick)},
            likely_cause="instrument metadata was defaulted instead of fetched",
            remediation="fetch markets_details for this instrument; tick size is never assumed",
        )
    with localcontext() as ctx:
        ctx.prec = 38
        d = Decimal(repr(value))          # repr, not str(float): shortest round-trip representation
        steps = d / tick
        if mode in (RoundMode.STOP_LONG, RoundMode.TARGET_LONG):
            n = steps.to_integral_value(rounding="ROUND_FLOOR")
        elif mode in (RoundMode.STOP_SHORT, RoundMode.TARGET_SHORT):
            n = steps.to_integral_value(rounding="ROUND_CEILING")
        else:
            n = steps.to_integral_value(rounding="ROUND_HALF_EVEN")
        return (n * tick).normalize() + Decimal(0)


def to_quantity(value: float, *, increment: Decimal) -> Decimal:
    """Position size, always rounded **down** to the increment: rounding up would silently exceed the
    risk budget the size was derived from (specs/08 §3)."""
    if increment <= 0:
        raise UnitError(
            f"quantity increment must be positive, got {increment}",
            component=COMPONENT, operation="to_quantity",
            context={"increment": str(increment)},
            likely_cause="instrument metadata was defaulted instead of fetched",
            remediation="fetch markets_details; the increment is never assumed",
        )
    if not math.isfinite(value) or value < 0:
        raise FeatureError(
            f"invalid quantity {value}",
            component=COMPONENT, operation="to_quantity",
            context={"value": str(value)},
            likely_cause="a sizing computation produced a negative or non-finite quantity",
            remediation="the risk engine must refuse the signal rather than publish this",
        )
    with localcontext() as ctx:
        ctx.prec = 38
        d = Decimal(repr(value))
        return ((d / increment).to_integral_value(rounding="ROUND_FLOOR") * increment).normalize() + Decimal(0)
