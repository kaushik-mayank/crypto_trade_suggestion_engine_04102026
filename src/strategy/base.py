"""The strategy interface, lifecycle and outcome types (specs/13 §1-§2, specs/07 §8).

All 14 strategies (12 tradeable + 2 controls) share one interface so arbitration and risk can treat
them uniformly while each keeps its own logic. Two points carried deliberately:

``expansion_seeking`` is **a declaration, not a special case in the risk engine.** C3-A sets it true,
everything else false, and the ``VOL_EXTREME`` veto reads the flag — so the risk engine contains no
strategy names (specs/08 §8).

``SetupState`` is **derived, not accumulated.** ``X*`` is recomputed from closed 1m bars since
``armed_at_ms`` on every evaluation, so a restart rebuilds it exactly, backtest and live agree, and
the audit can reconstruct it at any past moment without having stored it (specs/13 §2).

The lifecycle names what the system does and does not claim: ``TRIGGERED`` means **the entry condition
occurred**, not that anyone entered. This is a suggestion system and it tracks a hypothetical trade so
it can report invalidation, stop and target events (specs/07 §8.1).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from core.errors import StrategyError
from features.value import FeatureSet
from normalize.canonical import Product, Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot
from strategy.act import ActState, Direction
from strategy.conditions import ConditionResult, summarise

COMPONENT = "strategy.base"


class Lifecycle(str, Enum):
    """specs/07 §8. ``CANDIDATE`` is internal and never published."""

    CANDIDATE = "CANDIDATE"
    SETUP_DETECTED = "SETUP_DETECTED"
    ENTER_NOW = "ENTER_NOW"
    TRIGGERED = "TRIGGERED"
    # terminal
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    SUPPRESSED = "SUPPRESSED"
    MISSED = "MISSED"
    STOPPED = "STOPPED"
    TARGET_HIT = "TARGET_HIT"
    HANDED_OFF = "HANDED_OFF"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def active(self) -> bool:
        return self in (Lifecycle.SETUP_DETECTED, Lifecycle.ENTER_NOW, Lifecycle.TRIGGERED)


TERMINAL_STATES = frozenset({
    Lifecycle.EXPIRED, Lifecycle.INVALIDATED, Lifecycle.SUPPRESSED, Lifecycle.MISSED,
    Lifecycle.STOPPED, Lifecycle.TARGET_HIT, Lifecycle.HANDED_OFF,
})

#: Legal transitions (specs/07 §8). Enforced, because an illegal transition means the lifecycle is
#: being driven by something other than the state machine -- and the audit trail would then describe
#: a history that never happened.
TRANSITIONS: dict[Lifecycle, frozenset[Lifecycle]] = {
    Lifecycle.CANDIDATE: frozenset({Lifecycle.SETUP_DETECTED, Lifecycle.SUPPRESSED}),
    Lifecycle.SETUP_DETECTED: frozenset({Lifecycle.ENTER_NOW, Lifecycle.EXPIRED,
                                         Lifecycle.INVALIDATED, Lifecycle.SUPPRESSED}),
    Lifecycle.ENTER_NOW: frozenset({Lifecycle.TRIGGERED, Lifecycle.MISSED, Lifecycle.INVALIDATED,
                                    Lifecycle.EXPIRED, Lifecycle.SUPPRESSED}),
    Lifecycle.TRIGGERED: frozenset({Lifecycle.STOPPED, Lifecycle.TARGET_HIT,
                                    Lifecycle.INVALIDATED, Lifecycle.HANDED_OFF}),
}


def assert_transition(current: Lifecycle, target: Lifecycle, *, signal_id: str) -> None:
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise StrategyError(
            f"illegal lifecycle transition {current.value} -> {target.value}",
            component=COMPONENT, operation="assert_transition",
            context={"signal_id": signal_id, "from": current.value, "to": target.value,
                     "allowed": sorted(s.value for s in allowed)},
            likely_cause="a lifecycle change was applied out of order, or a terminal state was revived",
            remediation="drive the lifecycle only through the state machine; the audit trail must "
                        "describe a history that actually happened",
        )


@dataclass
class SetupState:
    """Persisted per armed setup (specs/13 §2). ``x_star`` and ``e_t`` are *cached* derivations."""

    signal_id: str
    strategy_id: str
    asset: str
    product: Product
    direction: Direction
    armed_at_ms: int
    armed_snapshot_id: str
    expires_at_ms: int
    lifecycle: Lifecycle = Lifecycle.SETUP_DETECTED
    x_star: Decimal | None = None
    e_t: Decimal | None = None
    original_entry_price: Decimal | None = None      # set at TRIGGERED
    triggered_at_ms: int | None = None
    parameter_hash: str = ""
    strategy_version: str = ""
    #: Free-form, strategy-owned facts needed to re-evaluate invalidation (the level a structural stop
    #: references, the funding multiple at arming). Serialised with the state.
    context: dict[str, Any] = field(default_factory=dict)

    def transition(self, target: Lifecycle, *, at_ms: int, reason: str = "") -> None:
        assert_transition(self.lifecycle, target, signal_id=self.signal_id)
        self.lifecycle = target
        self.context.setdefault("transitions", []).append(
            {"to": target.value, "at_ms": at_ms, "reason": reason})

    def expired(self, now_ms: int) -> bool:
        return now_ms >= self.expires_at_ms

    def to_dict(self) -> dict[str, Any]:
        return {"signal_id": self.signal_id, "strategy_id": self.strategy_id, "asset": self.asset,
                "product": self.product.value, "direction": self.direction.value,
                "armed_at_ms": self.armed_at_ms, "armed_snapshot_id": self.armed_snapshot_id,
                "expires_at_ms": self.expires_at_ms, "lifecycle": self.lifecycle.value,
                "x_star": str(self.x_star) if self.x_star is not None else None,
                "e_t": str(self.e_t) if self.e_t is not None else None,
                "original_entry_price": (str(self.original_entry_price)
                                         if self.original_entry_price is not None else None),
                "triggered_at_ms": self.triggered_at_ms,
                "parameter_hash": self.parameter_hash, "strategy_version": self.strategy_version,
                "context": dict(self.context)}


@dataclass(frozen=True)
class RawSignal:
    """What a strategy publishes before the risk engine sizes it (specs/08 §1).

    Levels are ``Decimal`` and **immutable**: the risk engine may veto or scale a signal but never
    alters a level a strategy produced (specs/08 §1, enforced in PHASE-21).
    """

    signal_id: str
    strategy_id: str
    strategy_version: str
    asset: str
    product: Product
    direction: Direction
    lifecycle: Lifecycle
    as_of_ms: int
    signal_timeframe: Timeframe
    entry: Decimal | None                       # the ACT trigger, or None before arming
    stop: Decimal | None
    targets: tuple[Decimal, ...] = ()
    act: ActState | None = None
    conditions: tuple[ConditionResult, ...] = ()
    #: The reasoning the UI shows, in the strategy's own words.
    rationale: str = ""
    snapshot_id: str = ""
    parameter_hash: str = ""
    code_version: str = ""
    regime: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> Decimal | None:
        """``R`` — the distance from entry to stop. The denominator of every R:R gate."""
        if self.entry is None or self.stop is None:
            return None
        return abs(self.entry - self.stop)

    def reward_to_risk(self, target_index: int = 0) -> Decimal | None:
        r = self.risk_per_unit
        if r is None or r == 0 or target_index >= len(self.targets):
            return None
        return abs(self.targets[target_index] - self.entry) / r    # type: ignore[operator]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id, "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version, "asset": self.asset,
            "product": self.product.value, "direction": self.direction.value,
            "lifecycle": self.lifecycle.value, "as_of_ms": self.as_of_ms,
            "signal_timeframe": self.signal_timeframe.value,
            "entry": str(self.entry) if self.entry is not None else None,
            "stop": str(self.stop) if self.stop is not None else None,
            "targets": [str(t) for t in self.targets],
            "risk_per_unit": str(self.risk_per_unit) if self.risk_per_unit is not None else None,
            "reward_to_risk": _rr(self.reward_to_risk()),
            "act": self.act.to_dict() if self.act else None,
            "conditions": [c.to_dict() for c in self.conditions],
            "conditions_summary": summarise(self.conditions),
            "rationale": self.rationale, "snapshot_id": self.snapshot_id,
            "parameter_hash": self.parameter_hash, "code_version": self.code_version,
            "regime": dict(self.regime), "detail": dict(self.detail),
        }


def _rr(value: Decimal | None) -> str | None:
    return None if value is None else str(round(value, 4))


# ---- outcomes ------------------------------------------------------------------------------
#
# Every outcome carries the full condition evaluation, including NoSetup: without the rejection
# record, "Class 1 never fired" is an observation with no diagnosis (specs/13 §1).


@dataclass(frozen=True)
class StrategyOutcome:
    kind: str
    conditions: tuple[ConditionResult, ...] = ()
    signal: RawSignal | None = None
    act: ActState | None = None
    reason: str = ""
    #: The setup state this evaluation created or advanced, for the runner to persist. ``None`` for
    #: ``NoSetup``: there is nothing to remember about a setup that was never armed.
    state: SetupState | None = None

    @property
    def summary(self) -> dict[str, Any]:
        return summarise(self.conditions)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason,
                "state": self.state.to_dict() if self.state else None,
                "conditions": [c.to_dict() for c in self.conditions],
                "summary": self.summary,
                "signal": self.signal.to_dict() if self.signal else None,
                "act": self.act.to_dict() if self.act else None}


def no_setup(conditions: Sequence[ConditionResult], reason: str = "") -> StrategyOutcome:
    return StrategyOutcome(kind="NoSetup", conditions=tuple(conditions), reason=reason)


def setup(signal: RawSignal, conditions: Sequence[ConditionResult],
          state: SetupState | None = None, reason: str = "") -> StrategyOutcome:
    return StrategyOutcome(kind="Setup", conditions=tuple(conditions), signal=signal,
                           act=signal.act, state=state, reason=reason)


def update(signal: RawSignal, act_state: ActState | None,
           conditions: Sequence[ConditionResult] = (),
           state: SetupState | None = None) -> StrategyOutcome:
    return StrategyOutcome(kind="Update", conditions=tuple(conditions), signal=signal,
                           act=act_state, state=state)


def terminal(reason: str, conditions: Sequence[ConditionResult] = (),
             signal: RawSignal | None = None,
             state: SetupState | None = None) -> StrategyOutcome:
    return StrategyOutcome(kind="Terminal", conditions=tuple(conditions), reason=reason,
                           signal=signal, state=state)


@runtime_checkable
class Strategy(Protocol):
    """specs/13 §1. Implemented by all 14 strategies; arbitration and risk see only this."""

    id: str
    version: str
    strategy_class: int
    tier: str                       # "A" backtestable, "B" needs Tier B data
    tradeable: bool                 # False for the two controls
    expansion_seeking: bool         # exempts from the VOL_EXTREME veto (specs/08 §8 Tier 3)
    signal_timeframe: Timeframe     # only 1h or 1d may originate setups (specs/02 §3)
    required_features: tuple[str, ...]

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome: ...


def assert_originating_timeframe(strategy_id: str, timeframe: Timeframe) -> None:
    """Only 1h and 1d may originate setups (specs/02 §3).

    15m and 1m exist for triggering and execution checks. A setup originating on 15m would make the
    system a different system than the one the specs validate.
    """
    if timeframe not in (Timeframe.H1, Timeframe.D1):
        raise StrategyError(
            f"{strategy_id} declares signal_timeframe={timeframe.value}",
            component=COMPONENT, operation="assert_originating_timeframe",
            context={"strategy_id": strategy_id, "timeframe": timeframe.value,
                     "allowed": ["1h", "1d"]},
            likely_cause="a strategy was written to originate setups on a trigger timeframe",
            remediation="only 1h and 1d may originate setups (specs/02 §3); 15m and 1m are for "
                        "triggering and execution checks",
        )
