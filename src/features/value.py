"""The feature contract (specs/12 §2).

> A strategy must be able to distinguish *"this feature is 0.3"* from *"this feature could not be
> computed because the window contained a gap."* Conflating them is how a data outage becomes a
> trade.

So a feature never returns a bare float. It returns a :class:`FeatureValue` carrying a status and a
reason, and :class:`FeatureSet` **raises** when a caller reads a non-OK value. A strategy has to
handle the status explicitly, which makes the handling visible at review — and a strategy that
forgets fails loudly instead of trading on a number that was never computed.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, NamedTuple

from core.errors import FeatureError, FeatureNotAvailable
from normalize.canonical import Product, Timeframe

COMPONENT = "features"


class FeatureStatus(str, Enum):
    """Why a feature does or does not have a value.

    The statuses of specs/12 §2, plus two the spec's list cannot express:

    ``PARAMETER_UNSET`` — ``swing_atr_multiple`` ships uncalibrated, so ``STR_SWING`` has no threshold
    until it is set on training data. Reporting that as INSUFFICIENT_DATA would blame the data for a
    calibration nobody ran, and an operator would look in the wrong place.

    ``MISCONFIGURED`` — the snapshot request does not provide something the feature reads (a Tier B
    source, or history for a trailing statistic). ``FeatureEngine.assert_request_sufficient`` catches
    this at startup; if it is ever seen at runtime it means the app skipped that check, and it must not
    be confused with the venue being down.
    """

    OK = "OK"
    WARMING = "WARMING"                      # not enough bars yet; time fixes it
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # the window cannot be formed at all
    GAP_IN_WINDOW = "GAP_IN_WINDOW"          # a hole, confirmed absent or not (specs/12 §6)
    NUMERICAL_FAILURE = "NUMERICAL_FAILURE"  # a degenerate case, per specs/12 §4
    DQ_FAILED = "DQ_FAILED"                  # an upstream data-quality gate refused
    PARAMETER_UNSET = "PARAMETER_UNSET"      # a required parameter has never been calibrated
    MISCONFIGURED = "MISCONFIGURED"          # the snapshot does not carry what this feature needs

    @property
    def is_ok(self) -> bool:
        return self is FeatureStatus.OK


class FeatureKey(NamedTuple):
    """A feature is identified by name, timeframe and which shared lookback it used (specs/02 §4).

    ``horizon`` is ``"short"``, ``"medium"``, ``"long"`` or ``"-"`` for features with no lookback
    choice, so ``TRD_TSTAT`` at 1h/168 and at 1h/720 are distinct values rather than one overwriting
    the other.

    ``timeframe`` is ``None`` for Tier B features, which are **point-in-time**: funding, positioning
    and the book are single observations with their own timestamps, not statistics over bars. Giving
    them a nominal 1h would imply an hourly derivation that does not exist.
    """

    name: str
    timeframe: Timeframe | None
    horizon: str = "-"

    def __str__(self) -> str:
        where = self.timeframe.value if self.timeframe is not None else "pit"
        return f"{self.name}@{where}" + (f"/{self.horizon}" if self.horizon != "-" else "")


@dataclass(frozen=True, slots=True)
class FeatureValue:
    name: str
    version: str
    status: FeatureStatus
    value: float | None = None
    reason: str | None = None
    timeframe: Timeframe | None = None
    horizon: str = "-"
    window_start_ms: int = 0
    window_end_ms: int = 0
    bars_used: int = 0
    computed_at_ms: int = 0
    lookback: int = 0
    #: Count of synthesised no-trade bars in the window (specs/12 §6.3). Non-zero only on 1m, where
    #: synthesis is permitted, and carried so the dependency is visible on anything 1m-derived.
    synthesised_bars: int = 0
    #: Categorical features (``STR_SEQ``) have a label rather than a number; ``value`` stays None.
    label: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status.is_ok and self.value is None and self.label is None:
            raise FeatureError(
                f"feature {self.name} reported OK with no value and no label",
                component=COMPONENT, operation="FeatureValue",
                context={"name": self.name, "timeframe": getattr(self.timeframe, "value", None)},
                likely_cause="a compute function returned None on a success path",
                remediation="return a non-OK status with a reason instead; OK must mean a usable value",
            )
        if not self.status.is_ok and self.value is not None:
            raise FeatureError(
                f"feature {self.name} reported {self.status.value} but still carries a value",
                component=COMPONENT, operation="FeatureValue",
                context={"name": self.name, "status": self.status.value, "value": self.value},
                likely_cause="a partially computed number was attached to a failure",
                remediation="a non-OK feature must carry no value at all, so it cannot be used by "
                            "accident",
            )
        if not self.status.is_ok and not self.reason:
            raise FeatureError(
                f"feature {self.name} reported {self.status.value} without a reason",
                component=COMPONENT, operation="FeatureValue",
                context={"name": self.name, "status": self.status.value},
                likely_cause="a failure path was added without a message",
                remediation='every non-OK status needs a reason, e.g. "need 720 bars, have 431"',
            )

    @property
    def key(self) -> FeatureKey:
        return FeatureKey(self.name, self.timeframe, self.horizon)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "status": self.status.value,
                "value": self.value, "label": self.label, "reason": self.reason,
                "timeframe": self.timeframe.value if self.timeframe else None,   # None = point-in-time
                "horizon": self.horizon, "lookback": self.lookback,
                "window_start_ms": self.window_start_ms, "window_end_ms": self.window_end_ms,
                "bars_used": self.bars_used, "computed_at_ms": self.computed_at_ms,
                "synthesised_bars": self.synthesised_bars, "detail": dict(self.detail)}


@dataclass(frozen=True)
class FeatureSet:
    """Immutable container handed to strategies. Reading a non-OK feature raises."""

    asset: str
    product: Product
    as_of_ms: int
    values: Mapping[FeatureKey, FeatureValue] = field(default_factory=dict)
    parameter_hash: str = ""
    code_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def __iter__(self) -> Iterator[FeatureValue]:
        return iter(self.values.values())

    def __len__(self) -> int:
        return len(self.values)

    def __contains__(self, key: object) -> bool:
        return key in self.values

    def get(self, name: str, timeframe: Timeframe | None = None, horizon: str = "-") -> FeatureValue:
        """The :class:`FeatureValue` whatever its status — for the dashboard and for a strategy that
        wants to record *why* a condition could not be evaluated."""
        key = FeatureKey(name, timeframe, horizon)
        value = self.values.get(key)
        if value is None:
            raise FeatureError(
                f"feature {key} was never computed for {self.asset}",
                component=COMPONENT, operation="get",
                context={"asset": self.asset, "requested": str(key),
                         "available": sorted(str(k) for k in self.values)[:40],
                         "computed": len(self.values)},
                likely_cause="a strategy needs a feature the engine does not register for this "
                             "timeframe, or the horizon label is wrong",
                remediation="register the feature for this timeframe, or correct the name/horizon",
            )
        return value

    def value(self, name: str, timeframe: Timeframe | None = None, horizon: str = "-") -> float:
        """The number. Raises :class:`FeatureNotAvailable` unless the status is OK."""
        fv = self.get(name, timeframe, horizon)
        if not fv.status.is_ok:
            raise FeatureNotAvailable(
                f"{fv.key} is {fv.status.value}: {fv.reason}",
                component=COMPONENT, operation="value",
                context={"asset": self.asset, "feature": str(fv.key), "status": fv.status.value,
                         "bars_used": fv.bars_used, "lookback": fv.lookback,
                         "as_of_ms": self.as_of_ms},
                likely_cause=fv.reason,
                remediation="a strategy must record this as the failing condition and decline; "
                            "there is no substitute value",
            )
        if fv.value is None:                      # a categorical feature read as a number
            raise FeatureError(
                f"{fv.name} is categorical and has no numeric value (label={fv.label!r})",
                component=COMPONENT, operation="value",
                context={"feature": str(fv.key), "label": fv.label},
                likely_cause="a numeric comparison was applied to a categorical feature",
                remediation="read it with label() instead",
            )
        return fv.value

    def label(self, name: str, timeframe: Timeframe | None = None, horizon: str = "-") -> str:
        fv = self.get(name, timeframe, horizon)
        if not fv.status.is_ok or fv.label is None:
            raise FeatureNotAvailable(
                f"{fv.key} has no label: {fv.status.value} ({fv.reason})",
                component=COMPONENT, operation="label",
                context={"feature": str(fv.key), "status": fv.status.value},
                likely_cause=fv.reason or "the feature is numeric, not categorical",
                remediation="check status before reading a label",
            )
        return fv.label

    def status(self, name: str, timeframe: Timeframe | None = None,
               horizon: str = "-") -> FeatureStatus:
        return self.get(name, timeframe, horizon).status

    def is_ok(self, name: str, timeframe: Timeframe | None = None, horizon: str = "-") -> bool:
        key = FeatureKey(name, timeframe, horizon)
        fv = self.values.get(key)
        return bool(fv and fv.status.is_ok)

    @property
    def ok_count(self) -> int:
        return sum(1 for v in self.values.values() if v.status.is_ok)

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.values.values():
            counts[v.status.value] = counts.get(v.status.value, 0) + 1
        return counts

    def not_ok(self) -> list[FeatureValue]:
        return [v for v in self.values.values() if not v.status.is_ok]

    def to_dict(self) -> dict[str, Any]:
        return {"asset": self.asset, "product": self.product.value, "as_of_ms": self.as_of_ms,
                "parameter_hash": self.parameter_hash, "code_version": self.code_version,
                "computed": len(self.values), "ok": self.ok_count,
                "status_counts": self.status_counts(),
                "values": [v.to_dict() for v in self.values.values()]}
