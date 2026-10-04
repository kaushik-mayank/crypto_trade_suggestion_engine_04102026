"""Conditions as first-class objects (specs/13 §4).

> **Evaluate every condition, always. Never stop at the first failure.**

A signal failing one condition is diagnostically different from one failing five, and the difference
is exactly what tells you whether a threshold is slightly tight or the mechanism is absent
(specs/03 §11). Short-circuiting would save microseconds and destroy that information — and every
outcome, including ``NoSetup``, carries the full evaluation so the audit can answer *"why did it
reject the alternatives?"*.

A condition whose feature is not ``OK`` returns ``passed=False`` with ``reason`` carrying the
feature's status — **never a silent false**. That distinction is the whole reason the feature layer
refuses rather than returning a number.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.errors import FeatureNotAvailable, StrategyError
from features.value import FeatureSet
from normalize.canonical import Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot

COMPONENT = "strategy.conditions"


@dataclass(frozen=True, slots=True)
class ConditionResult:
    name: str
    passed: bool
    value: Any = None                   # what was measured
    threshold: Any = None               # what it was compared to
    reason: str | None = None           # set when a feature was not OK, or why it failed
    description: str = ""

    @property
    def unavailable(self) -> bool:
        """True when the condition could not be evaluated at all, as opposed to evaluating false.

        The distinction matters downstream: five conditions that *failed* is a market that does not
        match; five that were *unavailable* is a data problem wearing the same coat.
        """
        return self.value is None and self.reason is not None and not self.passed

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed,
                "value": _plain(self.value), "threshold": _plain(self.threshold),
                "reason": self.reason, "description": self.description,
                "unavailable": self.unavailable}


def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


@dataclass
class ConditionContext:
    """Everything a condition may read. Narrow on purpose: no store, no clock, no orders."""

    asset: str
    snapshot: MarketSnapshot
    features: FeatureSet
    regime: RegimeState
    params: dict[str, Any]
    #: Values a strategy computed earlier in its own evaluation and wants conditions to reuse.
    scratch: dict[str, Any] = field(default_factory=dict)

    @property
    def as_of_ms(self) -> int:
        return self.snapshot.as_of_ms

    def feature(self, name: str, timeframe: Timeframe | None = None, horizon: str = "-") -> float:
        """A feature value, or :class:`FeatureNotAvailable` carrying the status and reason."""
        return self.features.value(name, timeframe, horizon)

    def param(self, name: str) -> Any:
        if name not in self.params:
            raise StrategyError(
                f"parameter {name!r} is not registered but a condition asked for it",
                component=COMPONENT, operation="param",
                context={"requested": name, "available": sorted(self.params)[:40]},
                likely_cause="a condition was written against a parameter that is not in "
                             "config/parameters.toml",
                remediation="register it (the cap is 30) or correct the name",
            )
        return self.params[name]


#: A condition body returns ``(passed, value, threshold)`` or raises to signal unavailability.
Body = Callable[[ConditionContext], "tuple[bool, Any, Any]"]


@dataclass(frozen=True)
class Condition:
    """One named, described, individually evaluated condition."""

    name: str
    description: str
    body: Body
    required_features: tuple[str, ...] = ()

    def evaluate(self, ctx: ConditionContext) -> ConditionResult:
        """Evaluate, converting an unavailable feature into a failed-with-reason result.

        Only :class:`FeatureNotAvailable` is absorbed this way. A programming error still escapes:
        turning every exception into ``passed=False`` would make a bug indistinguishable from a
        market that simply did not qualify, and the system would go quiet for the wrong reason.
        """
        try:
            passed, value, threshold = self.body(ctx)
        except FeatureNotAvailable as e:
            return ConditionResult(name=self.name, passed=False, reason=e.message,
                                   description=self.description)
        except StrategyError as e:
            # A condition may declare itself inapplicable (no structure yet, no prior level).
            return ConditionResult(name=self.name, passed=False, reason=e.message,
                                   description=self.description)
        return ConditionResult(name=self.name, passed=bool(passed), value=value,
                               threshold=threshold, description=self.description)


def evaluate_all(conditions: Sequence[Condition], ctx: ConditionContext) -> list[ConditionResult]:
    """Every condition, in order, with no short-circuiting (specs/13 §4)."""
    return [c.evaluate(ctx) for c in conditions]


def summarise(results: Sequence[ConditionResult]) -> dict[str, Any]:
    """The diagnostic summary the audit and the dashboard both need."""
    failed = [r for r in results if not r.passed]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": [r.name for r in failed],
        "unavailable": [r.name for r in results if r.unavailable],
        "all_passed": not failed,
        #: The first failure by declaration order, which is the one specs/09 §11 counts for the
        #: "which condition blocks most often" analysis. Recorded, not used to short-circuit.
        "first_failure": failed[0].name if failed else None,
    }


# ---- building blocks ---------------------------------------------------------------------
#
# Small factories so a strategy reads as a list of thresholds rather than a wall of closures, and so
# every condition automatically carries a description and the feature it depends on.


def threshold(name: str, description: str, feature: str, *, timeframe: Timeframe | None,
              horizon: str = "-", op: str, limit_param: str | None = None,
              limit: float | None = None, transform: Callable[[float], float] | None = None
              ) -> Condition:
    """``feature <op> limit`` where the limit is a registered parameter or a fixed number.

    ``op`` is one of ``>``, ``>=``, ``<``, ``<=``. A fixed ``limit`` is permitted only where the spec
    states a structural number (``MR_VR < 1`` compares to a random walk, not to a tuned threshold);
    anything tunable must come from the registry, which is what ``limit_param`` is for.
    """
    ops: dict[str, Callable[[float, float], bool]] = {
        ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    }
    if op not in ops:
        raise StrategyError(
            f"unknown comparison {op!r}",
            component=COMPONENT, operation="threshold",
            context={"op": op, "known": sorted(ops)},
            likely_cause="a typo in a condition definition",
            remediation=f"use one of {sorted(ops)}",
        )
    if (limit_param is None) == (limit is None):
        raise StrategyError(
            f"condition {name} must name exactly one of limit_param or limit",
            component=COMPONENT, operation="threshold",
            context={"name": name, "limit_param": limit_param, "limit": limit},
            likely_cause="a tunable threshold was hardcoded, or a structural one was parameterised",
            remediation="tunable thresholds live in the parameter registry; structural constants "
                        "(like comparing a variance ratio to 1) are stated inline with a comment",
        )

    def body(ctx: ConditionContext) -> tuple[bool, Any, Any]:
        measured = ctx.feature(feature, timeframe, horizon)
        if transform is not None:
            measured = transform(measured)
        bound = float(ctx.param(limit_param)) if limit_param else float(limit)  # type: ignore[arg-type]
        return ops[op](measured, bound), measured, bound

    return Condition(name=name, description=description, body=body, required_features=(feature,))


def regime_axis(name: str, description: str, axis: str, *, op: str, limit: float) -> Condition:
    """A raw-axis regime threshold — the kind that **survives degraded mode** (specs/12 §10)."""
    ops = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
           "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}

    def body(ctx: ConditionContext) -> tuple[bool, Any, Any]:
        value = ctx.regime.axis_values.get(axis)
        if value is None:
            raise StrategyError(
                f"regime axis {axis} is unavailable: {ctx.regime.reason or 'unclassified'}",
                component=COMPONENT, operation=name,
                context={"axis": axis, "regime_reason": ctx.regime.reason},
                likely_cause="the axis feature was not OK, so the regime could not be read",
                remediation="the strategy declines and records this as the failing condition",
            )
        return ops[op](value, limit), value, limit

    return Condition(name=name, description=description, body=body)


def dwell_stable(name: str = "regime_dwell_stable") -> Condition:
    """"The regime cell has been stable for at least its median dwell" — C1-B (3), C2-A (7), C3-B (7).

    **This is the condition that disappears in degraded mode** (specs/12 §10). It fails with an
    explicit reason there rather than silently passing: a stability filter that defaults to "stable"
    when it cannot be measured is worse than no filter, because it looks like one.
    """
    def body(ctx: ConditionContext) -> tuple[bool, Any, Any]:
        r = ctx.regime
        if r.degraded:
            raise StrategyError(
                "the regime model is degraded, so cell dwell stability cannot be evaluated; "
                "specs/12 §10 drops this condition and keeps the raw-axis thresholds",
                component=COMPONENT, operation=name,
                context={"model_version": r.model_version},
                likely_cause="the regime model failed its validity tests for this asset (F-033)",
                remediation="nothing to fix: the strategy runs with reduced selectivity",
            )
        if r.cell is None or r.median_dwell is None:
            raise StrategyError(
                # the reason is what reaches the audit record, so the rule goes in the message
                f"no regime cell at this as_of, so dwell stability is unknown and the gate must not "
                f"default to open: {r.reason or 'unclassified'}",
                component=COMPONENT, operation=name,
                context={"regime_reason": r.reason},
                likely_cause="an axis feature was unavailable",
                remediation="the strategy declines and records this as the failing condition",
            )
        return r.dwell_bars >= r.median_dwell, r.dwell_bars, r.median_dwell

    return Condition(name=name,
                     description="the regime cell has held for at least its median dwell",
                     body=body)
