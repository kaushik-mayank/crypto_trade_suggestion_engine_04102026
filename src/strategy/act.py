"""ACT — the Arrest-Confirmation Trigger (specs/03 §2, specs/13 §3).

**One implementation, nine consumers. A second implementation anywhere is a defect.**

A setup identifies *that* something is dislocated; it says nothing about *when* the dislocation stops
widening. Entering on detection alone catches a falling knife; entering at a fixed offset invents a
number. ACT derives the entry level from the market's own behaviour:

    X*(t) = min{ X(s) : t0 <= s <= t }      running adverse extreme since arming (LONG)
    E(t)  = X*(t) + delta * U(t)            the live trigger
    ENTER_NOW  <=>  X(t) >= E(t), confirmed on a closed 1-minute bar

and mirrors for SHORT. Four properties follow, and each is tested:

1. **It ratchets.** ``E(t)`` is monotone non-increasing for a LONG, so the system never chases and
   never triggers during a continuous decline — only after price has retraced ``delta * U`` off its
   extreme.
2. **Risk is set by the market.** The stop sits beyond ``X*``, so ``R`` is however far the dislocation
   actually extended. A violent overshoot produces a large ``R``, which then fails the minimum R:R
   gate and is rejected automatically — a quality filter that emerges from the mechanism rather than
   being bolted on.
3. **It separates the two states cleanly**: ``SETUP_DETECTED`` (armed, ``E(t)`` published and moving)
   versus ``ENTER_NOW`` (crossed on a confirmed close).
4. **Its failure mode is bounded and known.** In a V-shaped reversal it enters late, giving up
   ``delta * U`` of the move. specs/03 §2 is explicit: **do not add a predictive early-entry path.**
   That reintroduces the unbounded risk the primitive exists to remove.

Pure: no state, no I/O, no clock. ``X*`` is recomputed from closed bars each time rather than
accumulated, so a restart rebuilds it exactly and live and backtest agree by construction
(specs/13 §2).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from core.errors import StrategyError

COMPONENT = "strategy.act"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


@dataclass(frozen=True, slots=True)
class ActState:
    """What ACT computed. Published on every armed setup so the UI can show the live trigger."""

    direction: Direction
    x_star: Decimal                 # the running adverse extreme since arming
    e_t: Decimal                    # the live entry trigger
    current: Decimal                # the latest observation of the series
    unit: Decimal                   # ATR on the trigger timeframe, or the spread's sigma
    delta: Decimal
    bars_since_armed: int
    triggered: bool
    #: Distance still to travel before the trigger fires, in units of ``unit``. Negative once
    #: triggered. This is what the dashboard shows as "how close are we".
    distance_units: Decimal = Decimal(0)

    @property
    def gap(self) -> Decimal:
        """``|E(t) - X*|`` — always ``delta * unit`` by construction; asserted in the tests."""
        return abs(self.e_t - self.x_star)

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction.value, "x_star": str(self.x_star), "e_t": str(self.e_t),
                "current": str(self.current), "unit": str(self.unit), "delta": str(self.delta),
                "bars_since_armed": self.bars_since_armed, "triggered": self.triggered,
                "distance_units": str(self.distance_units)}


def act(direction: Direction, series: Sequence[Decimal], armed_at_idx: int, unit: Decimal,
        delta: Decimal, *, adverse: Sequence[Decimal] | None = None) -> ActState:
    """Compute the trigger state from a closed-bar series.

    ``series`` is the confirming observation — closed 1m closes, or the residual spread for C1-C.
    ``adverse`` optionally supplies the series the running extreme is taken from (closed 1m *lows*
    for a LONG, *highs* for a SHORT); when omitted the closes are used for both, which is the
    conservative reading and the one C1-C needs, since a spread has no high or low.

    Raises rather than guessing on bad inputs: a non-positive ``unit`` would make ``E(t) == X*`` and
    turn the trigger into "any tick back", which is not what the mechanism means.
    """
    n = len(series)
    if n == 0:
        raise StrategyError(
            "ACT was given an empty series",
            component=COMPONENT, operation="act",
            context={"armed_at_idx": armed_at_idx},
            likely_cause="no closed 1m bars exist since the setup was armed",
            remediation="wait for the first closed bar after arming; the setup stays SETUP_DETECTED",
        )
    if not 0 <= armed_at_idx < n:
        raise StrategyError(
            f"armed_at_idx {armed_at_idx} is outside a series of {n}",
            component=COMPONENT, operation="act",
            context={"armed_at_idx": armed_at_idx, "series_length": n},
            likely_cause="the arming time does not fall inside the window that was loaded",
            remediation="load the window from armed_at_ms onwards before calling act()",
        )
    if unit <= 0:
        raise StrategyError(
            f"ACT unit must be positive, got {unit}",
            component=COMPONENT, operation="act",
            context={"unit": str(unit), "delta": str(delta), "direction": direction.value},
            likely_cause="ATR on the trigger timeframe was zero or unavailable, or the spread's "
                         "sigma collapsed",
            remediation="a zero unit makes E(t) == X* -- the trigger would fire on any tick back. "
                        "The setup must wait or be invalidated, not entered",
        )
    if delta <= 0:
        raise StrategyError(
            f"ACT delta must be positive, got {delta}",
            component=COMPONENT, operation="act",
            context={"delta": str(delta)},
            likely_cause="the act_trigger_multiple parameter is unset or zero",
            remediation="delta is a registered parameter calibrated in Phase 16; zero would mean "
                        "entering at the extreme itself",
        )

    watch = adverse if adverse is not None else series
    if len(watch) != n:
        raise StrategyError(
            f"the adverse series has {len(watch)} points, the observation series {n}",
            component=COMPONENT, operation="act",
            context={"series": n, "adverse": len(watch)},
            likely_cause="lows and closes were extracted over different windows",
            remediation="both must come from the same closed-bar window",
        )

    window = watch[armed_at_idx:]
    if direction is Direction.LONG:
        x_star = min(window)
        e_t = x_star + delta * unit
        current = series[-1]
        triggered = current >= e_t
        distance = (e_t - current) / unit
    else:
        x_star = max(window)
        e_t = x_star - delta * unit
        current = series[-1]
        triggered = current <= e_t
        distance = (current - e_t) / unit

    return ActState(direction=direction, x_star=x_star, e_t=e_t, current=current, unit=unit,
                    delta=delta, bars_since_armed=n - armed_at_idx, triggered=triggered,
                    distance_units=distance)
