"""What a feature *is*: its declaration and the context it computes in.

Kept separate from :mod:`features.registry` so the family modules can import these types while the
registry imports the families — no cycle, and each family file reads as a list of formulas.

A feature declares everything the engine needs to schedule it: which timeframes and lookbacks it
applies to, how many bars beyond the lookback it needs, and which other features it depends on.
Dependencies are declared rather than fetched, so the engine can order the computation and propagate
a dependency's status instead of a second-hand failure. ``VOL_RATIO`` is not "unavailable"; it is
``WARMING because VOL_CC@1h/long is WARMING``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from core.errors import FeatureError
from features.numerics import Degenerate
from features.value import FeatureKey, FeatureStatus, FeatureValue
from features.windows import Window
from normalize.canonical import Product, Timeframe
from snapshot.models import MarketSnapshot, SeriesKey

COMPONENT = "features.spec"

#: Shared lookbacks (specs/02 §4), by timeframe and horizon. Calendar-anchored, **not tuned**; the
#: values live in the parameter registry as ``lookback_<tf>_<horizon>`` (parameters 1-9, all fixed)
#: and are read from there, this map only names which exist.
HORIZONS: tuple[str, ...] = ("short", "medium", "long")
NO_HORIZON: tuple[str, ...] = ("-",)


class DependencyNotOK(Exception):
    """A declared dependency is not OK. Carries its status so the engine can propagate it verbatim."""

    def __init__(self, dep: FeatureKey, status: FeatureStatus, reason: str | None):
        self.dep = dep
        self.status = status
        self.reason = reason
        super().__init__(f"{dep} is {status.value}: {reason}")


class NotApplicable(Exception):
    """This feature has nothing to report here, and that is not a failure.

    ``STR_SEQ`` before two pivots of each type exist is the case specs/02 Family 3 names as
    "undefined": reporting it as a numerical failure would imply something broke.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class ComputeContext:
    """Everything one feature computation may read. Deliberately narrow.

    No store, no clock, no ``as_of`` arithmetic of its own: the window was already cut to ``as_of``
    by the snapshot builder, so a feature cannot reach past it even by mistake.
    """

    asset: str
    product: Product
    #: ``None`` for a Tier B (point-in-time) feature, which is not derived from bars at all.
    timeframe: Timeframe | None
    horizon: str
    lookback: int
    #: ``None`` for a Tier B feature: there is no bar window to hold.
    window: Window | None
    params: Mapping[str, Any]
    #: Parameters whose registry status is ``unset`` — declared but never calibrated. Reading one
    #: raises rather than using its placeholder value, because ``swing_atr_multiple = 0.0`` would make
    #: every bar a pivot and the resulting structure would look computed.
    unset: frozenset[str] = frozenset()
    #: Extracts another series' window at the depth *this* consumer needs (Family 5 cross-asset,
    #: ``STR_DIST_PDH`` cross-timeframe). Per-consumer rather than one deep window shared by all:
    #: a 24-bar correlation must not be disabled because a 720-bar window has an old hole in it.
    window_provider: Callable[[SeriesKey, int, int], Any] | None = None
    #: Values already computed in this pass, for declared dependencies.
    computed: Mapping[FeatureKey, FeatureValue] = field(default_factory=dict)
    #: The snapshot, for Tier B features only (Families 6-8). Reading it raises SnapshotIncomplete
    #: when the source is stale or absent, which the engine turns into DQ_FAILED.
    snapshot: MarketSnapshot | None = None
    #: Series-valued dependencies, filled by the engine on request (``VOL_OF_VOL`` needs the series
    #: of ``VOL_CC``, not its latest value).
    series_provider: Callable[[str, str, int], np.ndarray] | None = None
    #: Structured payload a feature wants carried alongside its number — swing levels, prior-period
    #: extremes, the ATR a threshold was scaled by. Merged into the FeatureValue's ``detail`` by
    #: :func:`evaluate`, so it reaches the audit record and the dashboard rather than being recomputed
    #: by every consumer.
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def bars(self) -> Window:
        """The bar window, for a Tier A feature.

        ``window`` is optional because Tier B features have none; this accessor is what bar-based
        features use, so a missing window is a clear error here instead of an ``AttributeError`` on
        ``None`` somewhere inside a formula.
        """
        if self.window is None:
            raise FeatureError(
                "a bar-based feature was computed without a window",
                component=COMPONENT, operation="bars",
                context={"asset": self.asset,
                         "timeframe": self.timeframe.value if self.timeframe else None},
                likely_cause="the spec is marked windowless but reads bars, or the engine took the "
                             "Tier B path for a Tier A feature",
                remediation="this is an engine or registration bug, not a data problem",
            )
        return self.window

    @property
    def tf(self) -> Timeframe:
        """The timeframe, for a bar-based feature. ``None`` only ever means Tier B."""
        if self.timeframe is None:
            raise FeatureError(
                "a bar-based feature was computed without a timeframe",
                component=COMPONENT, operation="tf",
                context={"asset": self.asset},
                likely_cause="a Tier B spec reached a Tier A code path",
                remediation="this is an engine or registration bug, not a data problem",
            )
        return self.timeframe

    def param(self, name: str) -> Any:
        if name in self.unset:
            from core.errors import ParameterError
            raise ParameterError(
                f"parameter {name!r} has never been calibrated (status: unset)",
                component=COMPONENT, operation="param",
                context={"parameter": name, "placeholder_value": self.params.get(name),
                         "asset": self.asset,
                         "timeframe": self.timeframe.value if self.timeframe else "point-in-time"},
                likely_cause="the calibration step that sets this parameter on training data has not "
                             "been run",
                remediation=f"calibrate {name} and set its status to fixed/provisional in "
                            "config/parameters.toml; features depending on it stay quiet until then",
            )
        if name not in self.params:
            raise FeatureError(
                f"parameter {name!r} is not registered but a feature asked for it",
                component=COMPONENT, operation="param",
                context={"feature_asset": self.asset, "requested": name,
                         "available": sorted(self.params)[:40]},
                likely_cause="a feature was written against a parameter that was never added to "
                             "config/parameters.toml",
                remediation="register the parameter (the cap is 30) or correct the name",
            )
        return self.params[name]

    def dep(self, name: str, *, timeframe: Timeframe | None = None, horizon: str = "-") -> float:
        """A dependency's numeric value, or :class:`DependencyNotOK` carrying its status."""
        key = FeatureKey(name, timeframe or self.timeframe, horizon)
        fv = self.computed.get(key)
        if fv is None:
            raise FeatureError(
                f"dependency {key} has not been computed yet",
                component=COMPONENT, operation="dep",
                context={"dependent_on": str(key), "asset": self.asset},
                likely_cause="the dependency is declared after its consumer in the registry, or not "
                             "registered for this timeframe",
                remediation="declare depends_on and order the registry so dependencies come first",
            )
        if not fv.status.is_ok or fv.value is None:
            raise DependencyNotOK(key, fv.status, fv.reason)
        return fv.value

    def dep_series(self, name: str, *, horizon: str = "-", length: int) -> np.ndarray:
        """The trailing series of a dependency, ``length`` values ending at the window's last bar."""
        if self.series_provider is None:
            raise FeatureError(
                f"{name} was asked for as a series but no provider is attached",
                component=COMPONENT, operation="dep_series",
                context={"name": name, "horizon": horizon},
                likely_cause="the engine did not attach a series provider for this pass",
                remediation="this is an engine bug, not a data problem",
            )
        return self.series_provider(name, horizon, length)

    def series_window(self, asset: str, timeframe: Timeframe, *, lookback: int,
                      extra_bars: int = 1) -> Window:
        """Another series' window, extracted at the depth asked for.

        A refusal carries the extraction's own status and reason — WARMING when that series is simply
        younger, GAP_IN_WINDOW when it has a hole in *this* window — so a cross-asset feature reports
        which series and which problem, not a generic unavailability.
        """
        key = SeriesKey(asset, self.product, timeframe)
        if self.window_provider is None:
            raise FeatureError(
                f"{key} was needed but no window provider is attached",
                component=COMPONENT, operation="series_window",
                context={"key": str(key)},
                likely_cause="the engine did not attach a window provider for this pass",
                remediation="this is an engine bug, not a data problem",
            )
        outcome = self.window_provider(key, lookback, extra_bars)
        if outcome.window is None:
            raise DependencyNotOK(FeatureKey(f"series:{asset}", timeframe, self.horizon),
                                  outcome.status, f"{asset} {timeframe.value}: {outcome.reason}")
        return outcome.window

    def other_window(self, asset: str, *, lookback: int | None = None,
                     extra_bars: int = 1) -> Window:
        """Another asset's window on the same timeframe and product (Family 5)."""
        return self.series_window(asset, self.tf,
                                  lookback=lookback if lookback is not None else self.lookback,
                                  extra_bars=extra_bars)

    def window_for(self, asset: str, timeframe: Timeframe, *, lookback: int = 2,
                   extra_bars: int = 0) -> Window:
        """A window on another timeframe — ``STR_DIST_PDH`` reads the stored 1d bars rather than
        resampling, so the daily boundary is the venue's own (F-012)."""
        return self.series_window(asset, timeframe, lookback=lookback, extra_bars=extra_bars)


#: A compute function returns a float, or a string for a categorical feature.
ComputeFn = Callable[[ComputeContext], "float | str"]
#: A vectorised function returns one value per bar of the window it is given (specs/12 §7).
VectorFn = Callable[[Window, int], np.ndarray]


@dataclass(frozen=True)
class FeatureSpec:
    """One feature, declared once. ``version`` bumps when the formula changes (specs/12 §8)."""

    name: str
    version: str
    family: int
    tier: str                                   # "A" backtestable, "B" no history
    #: ``(None,)`` for a Tier B feature — see :class:`features.value.FeatureKey`.
    timeframes: tuple[Timeframe | None, ...]
    compute: ComputeFn
    horizons: tuple[str, ...] = HORIZONS
    #: Bars needed *beyond* the lookback: N returns need N+1 closes, ATR needs a prior close.
    extra_bars: int = 0
    #: Bars needed beyond the lookback expressed as another horizon's lookback (``VOL_OF_VOL``).
    extra_horizon: str | None = None
    #: The shortest lookback at which this feature is *defined at all*. ``MR_VR`` at k=8 needs more
    #: than 8 one-period returns, so it is undefined on the 1d short lookback of 7 -- a fact about the
    #: formula, not a runtime failure. The engine skips such combinations and reports them once,
    #: instead of emitting a NUMERICAL_FAILURE every hour for something time can never fix.
    min_lookback: int = 0
    #: For a ``NO_HORIZON`` feature, which horizon's lookback sizes its window. Defaults to "long",
    #: so a feature depending on the long-horizon value has the bars to compute it.
    lookback_horizon: str = "long"
    depends_on: tuple[tuple[str, str], ...] = ()       # (name, horizon) on the same timeframe
    categorical: bool = False
    vectorised: VectorFn | None = None
    #: Tier B features read the snapshot rather than a bar window.
    windowless: bool = False
    #: Point-in-time sources this feature reads, declared so the startup check can verify the snapshot
    #: request covers them. A missing source is a configuration mistake, and it should surface when the
    #: app starts rather than as a wall of DQ_FAILED an hour later.
    tier_b_sources: tuple[str, ...] = ()
    #: True when the feature needs a trailing Tier B series (``FND_Z``, ``POS_ROC``, ``BAS_Z``), which
    #: only exists if the request set ``tier_b_history_ms``.
    needs_history: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.tier not in ("A", "B"):
            raise FeatureError(
                f"feature {self.name} declares tier {self.tier!r}",
                component=COMPONENT, operation="FeatureSpec",
                context={"name": self.name, "tier": self.tier},
                likely_cause="tier must be A (OHLCV-derived, backtestable) or B (no history)",
                remediation='use "A" or "B"; the distinction decides what may be backtested',
            )
        if not self.horizons:
            raise FeatureError(
                f"feature {self.name} declares no horizons",
                component=COMPONENT, operation="FeatureSpec",
                context={"name": self.name},
                likely_cause="an empty horizon tuple means the feature would never be computed",
                remediation='use HORIZONS, or NO_HORIZON for a feature with no lookback choice',
            )

    def keys(self, timeframes: Sequence[Timeframe | None] | None = None) -> list[FeatureKey]:
        tfs = [tf for tf in self.timeframes
               if timeframes is None or tf in timeframes or tf is None]
        return [FeatureKey(self.name, tf, h) for tf in tfs for h in self.horizons]


def evaluate(spec: FeatureSpec, ctx: ComputeContext, *, computed_at_ms: int) -> FeatureValue:
    """Run one compute function and wrap whatever happens in a :class:`FeatureValue`.

    This is the only place feature exceptions are turned into statuses, and each kind maps to a
    distinct one — a degenerate denominator, a stale Tier B source and an uncalibrated parameter are
    three different problems with three different fixes, and flattening them into "unavailable" is
    what makes an outage look like a quiet market.
    """
    from core.errors import CryptoTraderError, ParameterError, SnapshotError, SnapshotIncomplete

    w = None if spec.windowless else ctx.window

    def made(status: FeatureStatus, *, value: float | None = None, label: str | None = None,
             reason: str | None = None, detail: dict[str, Any] | None = None) -> FeatureValue:
        """One construction point, so every status carries the same provenance fields."""
        return FeatureValue(
            name=spec.name, version=spec.version, status=status, value=value, label=label,
            reason=reason, timeframe=ctx.timeframe, horizon=ctx.horizon, lookback=ctx.lookback,
            computed_at_ms=computed_at_ms,
            window_start_ms=w.window_start_ms if w else 0,
            window_end_ms=w.window_end_ms if w else 0,
            bars_used=w.size if w else 0,
            synthesised_bars=w.synthesised_bars if w else 0,
            detail={**ctx.detail, **(detail or {})},
        )

    try:
        out = spec.compute(ctx)
    except Degenerate as e:
        return made(FeatureStatus.NUMERICAL_FAILURE, reason=str(e), detail={"guard": e.reason})
    except DependencyNotOK as e:
        return made(e.status, reason=f"depends on {e.dep} which is {e.status.value}: {e.reason}",
                    detail={"dependency": str(e.dep)})
    except NotApplicable as e:
        return made(FeatureStatus.INSUFFICIENT_DATA, reason=e.reason)
    except ParameterError as e:
        return made(FeatureStatus.PARAMETER_UNSET, reason=e.message,
                    detail={"parameter": e.context.get("parameter")})
    except SnapshotIncomplete as e:
        # Data quality: the source exists but is stale, absent or unusable at this as_of.
        return made(FeatureStatus.DQ_FAILED, reason=e.message,
                    detail={"source": e.context.get("source")})
    except SnapshotError as e:
        # Configuration: the request never asked for what this feature reads. Reported per feature
        # rather than raised, so one misconfigured Tier B source cannot blank every other family --
        # the deterministic place to catch this is assert_request_sufficient at startup.
        return made(FeatureStatus.MISCONFIGURED, reason=e.message,
                    detail={"fix": e.remediation})
    except CryptoTraderError:
        raise                                  # a real bug: let it out rather than label it a status

    if spec.categorical:
        if not isinstance(out, str):
            raise FeatureError(
                f"categorical feature {spec.name} returned {type(out).__name__}, not a label",
                component=COMPONENT, operation="evaluate",
                context={"name": spec.name, "returned": repr(out)},
                likely_cause="the compute function returns a number but the spec says categorical",
                remediation="return a label string, or drop categorical=True",
            )
        return made(FeatureStatus.OK, label=out)
    if isinstance(out, str):
        raise FeatureError(
            f"numeric feature {spec.name} returned the string {out!r}",
            component=COMPONENT, operation="evaluate",
            context={"name": spec.name, "returned": out},
            likely_cause="a label was returned from a feature not declared categorical",
            remediation="declare categorical=True, or return a number",
        )
    return made(FeatureStatus.OK, value=float(out))
