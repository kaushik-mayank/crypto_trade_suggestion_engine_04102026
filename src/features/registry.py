"""The feature registry: every feature the engine knows, validated at import.

Registration is a list, not a decorator, because the *order* is load-bearing — a feature's
dependencies must be computed before it — and a list makes that order reviewable. The validation here
fails at import rather than at the first computation: a duplicate name or a dependency declared after
its consumer is a programming error that should never reach a running system.
"""
from __future__ import annotations

from collections.abc import Sequence

from core.errors import ConfigError, FeatureError
from features import (
    f1_trend,
    f2_volatility,
    f3_structure,
    f4_deviation,
    f5_cross,
    f6_funding,
    f7_positioning,
    f8_liquidity,
    f9_volume,
)
from features.spec import NO_HORIZON, FeatureSpec
from features.value import FeatureKey
from normalize.canonical import Timeframe

COMPONENT = "features.registry"

#: Every registered feature, in computation order.
FEATURES: tuple[FeatureSpec, ...] = (
    *f1_trend.SPECS,
    *f2_volatility.SPECS,      # ATR first: Families 3 and 4 scale by it
    *f3_structure.SPECS,
    *f4_deviation.SPECS,
    *f5_cross.SPECS,
    *f9_volume.SPECS,
    # Tier B last: point-in-time, no bar window, and nothing in Tier A may depend on them.
    *f6_funding.SPECS,
    *f7_positioning.SPECS,
    *f8_liquidity.SPECS,
)

BY_NAME: dict[str, FeatureSpec] = {}


def _validate(specs: Sequence[FeatureSpec]) -> None:
    seen: set[str] = set()
    available: set[tuple[str, str, Timeframe | None]] = set()
    for spec in specs:
        if spec.name in seen:
            raise FeatureError(
                f"feature {spec.name} is registered twice",
                component=COMPONENT, operation="validate",
                context={"name": spec.name},
                likely_cause="two family modules define the same feature name",
                remediation="names are the audit trail's identifiers; make them unique",
            )
        seen.add(spec.name)
        for dep_name, dep_horizon in spec.depends_on:
            for tf in spec.timeframes:
                if (dep_name, dep_horizon, tf) not in available:
                    where = tf.value if tf is not None else "point-in-time"
                    raise FeatureError(
                        f"{spec.name} depends on {dep_name}/{dep_horizon} at {where}, which is not "
                        "registered earlier for that timeframe",
                        component=COMPONENT, operation="validate",
                        context={"feature": spec.name, "dependency": f"{dep_name}/{dep_horizon}",
                                 "timeframe": where},
                        likely_cause="the dependency is registered after its consumer, or not for this "
                                     "timeframe",
                        remediation="move the dependency earlier in FEATURES and register it for every "
                                    "timeframe its consumers use",
                    )
        for tf in spec.timeframes:
            for h in spec.horizons:
                available.add((spec.name, h, tf))
        if spec.horizons == NO_HORIZON and spec.lookback_horizon not in ("short", "medium", "long"):
            raise FeatureError(
                f"{spec.name} declares lookback_horizon={spec.lookback_horizon!r}",
                component=COMPONENT, operation="validate",
                context={"feature": spec.name, "lookback_horizon": spec.lookback_horizon},
                likely_cause="a typo; a horizonless feature still needs a window size",
                remediation='use "short", "medium" or "long"',
            )
        BY_NAME[spec.name] = spec


_validate(FEATURES)


def lookback_name(timeframe: Timeframe, horizon: str) -> str:
    """The registry parameter that holds this lookback (parameters 1-9, all ``fixed``)."""
    return f"lookback_{timeframe.value}_{horizon}"


def lookback_for(params: dict[str, object], timeframe: Timeframe, horizon: str) -> int:
    name = lookback_name(timeframe, horizon)
    if name not in params:
        raise ConfigError(
            f"no registered lookback for {timeframe.value}/{horizon}",
            component=COMPONENT, operation="lookback_for",
            context={"expected_parameter": name,
                     "available": sorted(k for k in params if k.startswith("lookback_"))},
            likely_cause="a feature is registered for a timeframe whose lookbacks are not in "
                         "config/parameters.toml",
            remediation=f"add {name} to the parameter registry, or stop registering features for "
                        f"{timeframe.value}",
        )
    return int(params[name])            # type: ignore[call-overload]


def specs_for(timeframes: Sequence[Timeframe | None] | None = None,
              tiers: Sequence[str] = ("A", "B")) -> tuple[FeatureSpec, ...]:
    return tuple(s for s in FEATURES
                 if s.tier in tiers
                 and (timeframes is None or any(tf in timeframes for tf in s.timeframes)))


def all_keys(timeframes: Sequence[Timeframe | None] | None = None) -> list[FeatureKey]:
    return [k for s in FEATURES for k in s.keys(timeframes)]


def tier_a_names() -> tuple[str, ...]:
    """Backtestable features. specs/02 §2 forbids a Tier A feature depending on a Tier B one, because
    that would make it unbacktestable without saying so; the test asserts it."""
    return tuple(s.name for s in FEATURES if s.tier == "A")


def summary() -> dict[str, object]:
    return {
        "features": len(FEATURES),
        "values_per_asset": len(all_keys()),
        "by_family": {f: sum(1 for s in FEATURES if s.family == f)
                      for f in sorted({s.family for s in FEATURES})},
        "by_tier": {t: sum(1 for s in FEATURES if s.tier == t) for t in ("A", "B")},
        "vectorised": sum(1 for s in FEATURES if s.vectorised is not None),
        "names": [s.name for s in FEATURES],
    }
