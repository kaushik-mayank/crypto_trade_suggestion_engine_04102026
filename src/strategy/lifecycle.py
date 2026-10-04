"""Managing an armed setup: trigger, expiry, invalidation (specs/07 §8, specs/13 §5).

Shared by every strategy, because the lifecycle belongs to the system rather than to any strategy. A
strategy supplies its *invalidation predicates*; when they fire, and what happens then, is decided here.

The cadence follows specs/13 §5: setup conditions are re-evaluated only on a **1h or 1d bar close**,
while an armed setup is re-checked on **every 1m close** for the trigger, for price invalidation and
for ``MISSED``. That asymmetry is deliberate — re-evaluating setup conditions every minute would let a
setup flicker in and out between bar closes, and the arming decision is defined on closed signal-timeframe
bars.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from core.logging import get_logger
from strategy.act import ActState, Direction
from strategy.base import Lifecycle, SetupState

log = get_logger("strategy.lifecycle")
COMPONENT = "strategy.lifecycle"


@dataclass(frozen=True)
class Invalidation:
    """One named reason an armed setup dies, with the evidence that killed it."""

    name: str
    fired: bool
    detail: str
    value: Any = None
    threshold: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "fired": self.fired, "detail": self.detail,
                "value": str(self.value) if self.value is not None else None,
                "threshold": str(self.threshold) if self.threshold is not None else None}


#: A predicate returns an :class:`Invalidation`; it is always called, never short-circuited, so the
#: audit records every check rather than only the first one that fired.
Predicate = Callable[[], Invalidation]


@dataclass(frozen=True)
class Progress:
    """What happened to an armed setup on this evaluation."""

    lifecycle: Lifecycle
    changed: bool
    reason: str
    invalidations: tuple[Invalidation, ...] = ()
    act: ActState | None = None

    @property
    def terminal(self) -> bool:
        return self.lifecycle.terminal

    def to_dict(self) -> dict[str, Any]:
        return {"lifecycle": self.lifecycle.value, "changed": self.changed, "reason": self.reason,
                "invalidations": [i.to_dict() for i in self.invalidations],
                "act": self.act.to_dict() if self.act else None}


def advance(state: SetupState, *, act_state: ActState, now_ms: int, price: Decimal,
            predicates: tuple[Predicate, ...] = (),
            stop: Decimal | None = None, targets: tuple[Decimal, ...] = ()) -> Progress:
    """Move an armed setup along the state machine, or leave it where it is.

    Order matters and is the spec's: **invalidation and expiry are checked before the trigger**, so a
    setup whose thesis died in the same minute its trigger fired does not enter. That ordering is the
    difference between "we would have entered" and "we entered into a dead thesis".
    """
    invalidations = tuple(p() for p in predicates)          # every predicate, always
    fired = [i for i in invalidations if i.fired]

    if fired:
        target = Lifecycle.INVALIDATED
        reason = "; ".join(f"{i.name}: {i.detail}" for i in fired)
        state.transition(target, at_ms=now_ms, reason=reason)
        log.info("setup_invalidated", signal_id=state.signal_id, strategy=state.strategy_id,
                 asset=state.asset, reasons=[i.name for i in fired], lifecycle=target.value)
        return Progress(lifecycle=target, changed=True, reason=reason, invalidations=invalidations,
                        act=act_state)

    if state.lifecycle in (Lifecycle.SETUP_DETECTED, Lifecycle.ENTER_NOW) and state.expired(now_ms):
        reason = (f"the setup window elapsed at {state.expires_at_ms} without triggering; a stale "
                  "setup is no longer the setup that was detected")
        state.transition(Lifecycle.EXPIRED, at_ms=now_ms, reason=reason)
        log.info("setup_expired", signal_id=state.signal_id, strategy=state.strategy_id,
                 asset=state.asset, armed_at_ms=state.armed_at_ms)
        return Progress(lifecycle=Lifecycle.EXPIRED, changed=True, reason=reason,
                        invalidations=invalidations, act=act_state)

    state.x_star, state.e_t = act_state.x_star, act_state.e_t

    if state.lifecycle is Lifecycle.TRIGGERED:
        return _manage_triggered(state, act_state, now_ms, price, stop, targets, invalidations)

    if state.lifecycle is Lifecycle.SETUP_DETECTED and act_state.triggered:
        reason = (f"price reached the ACT trigger {act_state.e_t} "
                  f"({act_state.delta} x {act_state.unit} above X*={act_state.x_star})")
        state.transition(Lifecycle.ENTER_NOW, at_ms=now_ms, reason=reason)
        log.info("enter_now", signal_id=state.signal_id, strategy=state.strategy_id,
                 asset=state.asset, e_t=str(act_state.e_t), x_star=str(act_state.x_star))
        return Progress(lifecycle=Lifecycle.ENTER_NOW, changed=True, reason=reason,
                        invalidations=invalidations, act=act_state)

    if state.lifecycle is Lifecycle.ENTER_NOW:
        # specs/07 §8.2: price moving past the trigger confirms the entry condition occurred. This is
        # a suggestion system: TRIGGERED tracks a hypothetical trade, it does not claim anyone entered.
        state.transition(Lifecycle.TRIGGERED, at_ms=now_ms,
                         reason="the entry condition occurred and is now tracked hypothetically")
        state.original_entry_price = act_state.e_t
        state.triggered_at_ms = now_ms
        log.info("triggered", signal_id=state.signal_id, strategy=state.strategy_id,
                 asset=state.asset, entry=str(act_state.e_t))
        return Progress(lifecycle=Lifecycle.TRIGGERED, changed=True,
                        reason="the entry condition occurred", invalidations=invalidations,
                        act=act_state)

    return Progress(lifecycle=state.lifecycle, changed=False,
                    reason=f"waiting: {act_state.distance_units:.3f} units from the trigger",
                    invalidations=invalidations, act=act_state)


def _manage_triggered(state: SetupState, act_state: ActState, now_ms: int, price: Decimal,
                      stop: Decimal | None, targets: tuple[Decimal, ...],
                      invalidations: tuple[Invalidation, ...]) -> Progress:
    """A hypothetical position: check the stop and the first target against the current price."""
    long = state.direction is Direction.LONG
    if stop is not None and ((long and price <= stop) or (not long and price >= stop)):
        reason = f"price {price} reached the stop {stop}"
        state.transition(Lifecycle.STOPPED, at_ms=now_ms, reason=reason)
        log.info("stopped", signal_id=state.signal_id, strategy=state.strategy_id,
                 asset=state.asset, price=str(price), stop=str(stop))
        return Progress(lifecycle=Lifecycle.STOPPED, changed=True, reason=reason,
                        invalidations=invalidations, act=act_state)
    if targets and ((long and price >= targets[0]) or (not long and price <= targets[0])):
        reason = f"price {price} reached T1 {targets[0]}"
        state.transition(Lifecycle.TARGET_HIT, at_ms=now_ms, reason=reason)
        log.info("target_hit", signal_id=state.signal_id, strategy=state.strategy_id,
                 asset=state.asset, price=str(price), target=str(targets[0]))
        return Progress(lifecycle=Lifecycle.TARGET_HIT, changed=True, reason=reason,
                        invalidations=invalidations, act=act_state)
    return Progress(lifecycle=Lifecycle.TRIGGERED, changed=False,
                    reason=f"tracking hypothetically at {price}", invalidations=invalidations,
                    act=act_state)
