"""The look-ahead boundary (specs/10 §2).

> The strategy engine is a pure function of an immutable ``MarketSnapshot``. It has no access to
> storage, no clock, and no knowledge of whether it is running live or in a backtest.

Two design points carry that:

**1. ``FormingBar`` is not a ``Bar``.** ``specs/10`` §2.1 requires the forming bar to be separated
from closed bars *in the type system, not by convention*, because only the trigger logic may read it.
So ``FormingBar`` does not subclass ``Bar`` and does not hold one — it exposes the handful of fields
a trigger needs and nothing that would satisfy a ``list[Bar]`` signature. A feature function cannot
accidentally accept it; the mistake becomes a type error rather than a silent look-ahead.

**2. Nothing is fetched on access.** A snapshot holds values, not handles. Everything the engine may
read was resolved at build time with ``as_of`` applied, so "read something newer" has no expression.
Reads of things that were *not* fit to use raise :class:`SnapshotIncomplete` rather than returning a
plausible number — conflating "0.3" with "could not be computed" is how an outage becomes a trade
(specs/12 §2).
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, NamedTuple

from core.errors import ProblemClass, SnapshotError, SnapshotIncomplete
from normalize.canonical import Bar, InstrumentMeta, Product, Timeframe

COMPONENT = "snapshot"


class SeriesKey(NamedTuple):
    """Identifies one bar series. Hashable, so it keys the snapshot's maps."""

    asset: str
    product: Product
    timeframe: Timeframe

    def __str__(self) -> str:
        return f"{self.asset}:{self.product.value}:{self.timeframe.value}"


class InstrumentKey(NamedTuple):
    asset: str
    product: Product

    def __str__(self) -> str:
        return f"{self.asset}:{self.product.value}"


@dataclass(frozen=True, slots=True)
class FormingBar:
    """The in-progress bar at ``as_of`` — deliberately **not** a :class:`Bar`.

    Built by aggregating *closed* finer bars up to ``as_of`` (see
    :meth:`snapshot.builder.SnapshotBuilder._forming`), never by reading a partial coarse bar, so it
    cannot contain anything that had not happened yet.
    """

    key: SeriesKey
    open_time_ms: int
    as_of_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    last: Decimal
    volume_base: Decimal
    constituents: int
    built_from: Timeframe

    @property
    def elapsed_fraction(self) -> float:
        """How far into the bar ``as_of`` is, in [0, 1). A trigger that wants "the bar is nearly
        over" asks this rather than comparing timestamps itself."""
        return (self.as_of_ms - self.open_time_ms) / self.key.timeframe.ms

    def to_dict(self) -> dict[str, Any]:
        return {"key": str(self.key), "open_time_ms": self.open_time_ms, "open": str(self.open),
                "high": str(self.high), "low": str(self.low), "last": str(self.last),
                "volume_base": str(self.volume_base), "constituents": self.constituents,
                "built_from": self.built_from.value,
                "elapsed_fraction": round(self.elapsed_fraction, 4)}


@dataclass(frozen=True, slots=True)
class SeriesState:
    """What the builder found for one requested series, and whether it is usable."""

    key: SeriesKey
    bars: tuple[Bar, ...]
    requested: int
    min_required: int
    ok: bool
    reason: str | None = None
    detail: str | None = None
    #: Bar open times confirmed absent (specs/11 §7.2). The *times* and not just a count, because
    #: specs/12 §6.2 applies a different rule per timeframe and needs to know which slots they are.
    absent_times: frozenset[int] = frozenset()
    unconfirmed_gaps: tuple[int, ...] = ()

    @property
    def known_absent(self) -> int:
        return len(self.absent_times)

    @property
    def newest_close_ms(self) -> int | None:
        return self.bars[-1].close_time_ms if self.bars else None

    def to_dict(self) -> dict[str, Any]:
        return {"key": str(self.key), "bars": len(self.bars), "requested": self.requested,
                "min_required": self.min_required, "ok": self.ok, "reason": self.reason,
                "detail": self.detail, "known_absent": self.known_absent,
                "unconfirmed_gaps": len(self.unconfirmed_gaps),
                "newest_close_ms": self.newest_close_ms}


@dataclass(frozen=True, slots=True)
class PointInTimeValue:
    """A Tier B value with its own source timestamp and a freshness verdict.

    Held even when stale, because the dashboard must be able to show *why* a family went quiet
    (specs/15 §9). ``MarketSnapshot.tier_b`` refuses to hand out a stale one.
    """

    source: str
    asset: str
    value: Any
    source_ts_ms: int | None
    age_ms: int | None
    max_age_ms: int
    ok: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "asset": self.asset, "present": self.value is not None,
                "source_ts_ms": self.source_ts_ms, "age_ms": self.age_ms,
                "max_age_ms": self.max_age_ms, "ok": self.ok, "reason": self.reason}


def _freeze(d: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return MappingProxyType(dict(d))


@dataclass(frozen=True)
class MarketSnapshot:
    """Immutable, self-contained, and the only thing the strategy engine sees."""

    as_of_ms: int
    code_version: str
    parameter_hash: str
    series: Mapping[SeriesKey, SeriesState] = field(default_factory=dict)
    forming: Mapping[SeriesKey, FormingBar | None] = field(default_factory=dict)
    point_in_time: Mapping[tuple[str, str], PointInTimeValue] = field(default_factory=dict)
    #: Trailing Tier B records, oldest first, all stamped at or before ``as_of``. Present only for
    #: the sources the request asked history for: a z-score of funding needs the series, and Tier B
    #: has no other source of history (specs/02 Family 6/7).
    point_in_time_history: Mapping[tuple[str, str], tuple[Any, ...]] = field(default_factory=dict)
    instruments: Mapping[InstrumentKey, InstrumentMeta] = field(default_factory=dict)
    dq: Mapping[str, Any] = field(default_factory=dict)
    built_by: str = "unknown"          # "live" | "backtest" — provenance only; never branched on
    build_ms: int = 0                  # wall-clock build duration, for the operator

    def __post_init__(self) -> None:
        object.__setattr__(self, "series", _freeze(self.series))
        object.__setattr__(self, "forming", _freeze(self.forming))
        object.__setattr__(self, "point_in_time", _freeze(self.point_in_time))
        object.__setattr__(self, "point_in_time_history", _freeze(self.point_in_time_history))
        object.__setattr__(self, "instruments", _freeze(self.instruments))
        object.__setattr__(self, "dq", _freeze(self.dq))

    # ---- identity ---------------------------------------------------------------------------

    @property
    def snapshot_id(self) -> str:
        """``sha256(as_of, code_version, parameter_hash)`` per specs/10 §2.1 — the three things a
        replay needs in order to rebuild this snapshot from the store."""
        h = hashlib.sha256(f"{self.as_of_ms}|{self.code_version}|{self.parameter_hash}".encode())
        return h.hexdigest()[:32]

    @property
    def content_digest(self) -> str:
        """A digest of the *data*, which ``snapshot_id`` deliberately excludes.

        A replay that rebuilds the same ``snapshot_id`` but a different ``content_digest`` has found
        a recorded bar mutation, not a code bug — which is exactly the distinction specs/13 §9 needs
        and could not otherwise make.
        """
        h = hashlib.sha256()
        for key in sorted(self.series, key=str):
            state = self.series[key]
            h.update(f"{key}|{state.ok}|{len(state.bars)}".encode())
            for b in state.bars:
                h.update(f"{b.open_time_ms}:{b.open}:{b.high}:{b.low}:{b.close}:{b.volume_base}".encode())
        for pk in sorted(self.point_in_time, key=str):
            pv = self.point_in_time[pk]
            h.update(f"{pk}|{pv.source_ts_ms}|{pv.ok}".encode())
        return h.hexdigest()[:32]

    # ---- reads: every one either returns usable data or says why it cannot ------------------

    def bars(self, asset: str, product: Product, timeframe: Timeframe) -> tuple[Bar, ...]:
        """Closed bars, ascending, all with ``close_time <= as_of``.

        Raises :class:`SnapshotIncomplete` when the series is not fit to compute on, so a caller
        cannot mistake a short or holed series for a complete one.
        """
        key = SeriesKey(asset, product, timeframe)
        state = self.series.get(key)
        if state is None:
            raise SnapshotError(
                f"series {key} was not requested for this snapshot",
                component=COMPONENT, operation="bars",
                context={"requested": sorted(str(k) for k in self.series)},
                likely_cause="a feature or strategy needs a series the snapshot request does not list",
                remediation="add the series to the SnapshotRequest; the engine cannot fetch it itself",
            )
        if not state.ok:
            raise SnapshotIncomplete(
                f"series {key} is not usable at this as_of: {state.detail}",
                component=COMPONENT, operation="bars",
                context={"key": str(key), "reason": state.reason, "have": len(state.bars),
                         "min_required": state.min_required, "as_of_ms": self.as_of_ms,
                         "unconfirmed_gaps": list(state.unconfirmed_gaps[:5])},
                likely_cause=state.detail,
                remediation="this asset must go quiet for features needing this series, not fall back "
                            "to a shorter window",
            )
        return state.bars

    def series_ok(self, asset: str, product: Product, timeframe: Timeframe) -> bool:
        """Non-raising form, for callers that degrade rather than fail."""
        state = self.series.get(SeriesKey(asset, product, timeframe))
        return bool(state and state.ok)

    def forming_bar(self, asset: str, product: Product, timeframe: Timeframe) -> FormingBar | None:
        """The in-progress bar. **Trigger logic only** (specs/02 §3); a feature function that takes
        this is a defect, and the type makes that visible at review."""
        key = SeriesKey(asset, product, timeframe)
        if key not in self.forming:
            raise SnapshotError(
                f"no forming bar was requested for {key}",
                component=COMPONENT, operation="forming_bar",
                context={"available": sorted(str(k) for k in self.forming)},
                likely_cause="trigger logic asked for a forming bar the request did not include",
                remediation="add the key to SnapshotRequest.forming",
            )
        return self.forming[key]

    def tier_b(self, source: str, asset: str) -> Any:
        """A Tier B value (funding, positioning, book, ticker) — or a refusal with the reason."""
        pv = self.point_in_time.get((source, asset))
        if pv is None:
            raise SnapshotError(
                f"point-in-time source {source!r} for {asset} was not requested",
                component=COMPONENT, operation="tier_b",
                context={"requested": sorted(f"{s}:{a}" for s, a in self.point_in_time)},
                likely_cause="a Tier B family needs a source the snapshot request does not list",
                remediation="add (source, asset) to SnapshotRequest.tier_b",
            )
        if not pv.ok:
            raise SnapshotIncomplete(
                f"{source} for {asset} is not usable: {pv.reason}",
                component=COMPONENT, operation="tier_b",
                context={"source": source, "asset": asset, "age_ms": pv.age_ms,
                         "max_age_ms": pv.max_age_ms, "as_of_ms": self.as_of_ms},
                likely_cause=pv.reason,
                remediation="Tier B has no history to fall back on; the family must go quiet "
                            "(specs/02 Tier B rule)",
            )
        return pv.value

    def tier_b_series(self, source: str, asset: str) -> tuple[Any, ...]:
        """The trailing records for a Tier B source, oldest first.

        Raises when the request did not ask for history, rather than returning an empty tuple: "no
        history was requested" and "this source has no history yet" lead to different fixes, and a
        z-score computed from an empty series would be the second mistaken for the first.
        """
        key = (source, asset)
        if key not in self.point_in_time_history:
            raise SnapshotError(
                f"no point-in-time history was requested for {source}:{asset}",
                component=COMPONENT, operation="tier_b_series",
                context={"requested": sorted(f"{s}:{a}" for s, a in self.point_in_time_history)},
                likely_cause="a trailing Tier B statistic needs history the request did not include",
                remediation="set tier_b_history_ms on the SnapshotRequest",
            )
        return self.point_in_time_history[key]

    def has_tier_b_series(self, source: str, asset: str) -> bool:
        return (source, asset) in self.point_in_time_history

    def tier_b_state(self, source: str, asset: str) -> PointInTimeValue | None:
        """The verdict without the refusal — for the dashboard's "why is this quiet" panel."""
        return self.point_in_time.get((source, asset))

    def instrument(self, asset: str, product: Product) -> InstrumentMeta:
        """Tick, increments and **fees**. Missing metadata is fatal to any cost-aware decision, so
        this raises rather than defaulting — a hardcoded fee is the 100x error of specs/10 §5."""
        key = InstrumentKey(asset, product)
        meta = self.instruments.get(key)
        if meta is None:
            raise SnapshotIncomplete(
                f"no instrument metadata for {key} at this as_of",
                component=COMPONENT, operation="instrument",
                context={"asset": asset, "product": product.value, "as_of_ms": self.as_of_ms,
                         "available": sorted(str(k) for k in self.instruments)},
                likely_cause="instrument metadata was never polled, or none predates as_of",
                remediation="poll /exchange/v1/markets_details before trading this asset; fees and "
                            "tick size must never be assumed (invariant 1)",
            )
        return meta

    # ---- reporting --------------------------------------------------------------------------

    @property
    def ok(self) -> bool:
        """True when every requested series and Tier B source is usable."""
        return (all(s.ok for s in self.series.values())
                and all(p.ok for p in self.point_in_time.values()))

    @property
    def problems(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, state in self.series.items():
            if not state.ok:
                out.append({"kind": "series", "key": str(key), "reason": state.reason,
                            "detail": state.detail, "problem_class": ProblemClass.DATA_QUALITY.value})
        for (source, asset), pv in self.point_in_time.items():
            if not pv.ok:
                out.append({"kind": "point_in_time", "key": f"{source}:{asset}", "reason": pv.reason,
                            "detail": pv.reason, "problem_class": ProblemClass.DATA_QUALITY.value})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_ms": self.as_of_ms,
            "snapshot_id": self.snapshot_id,
            "content_digest": self.content_digest,
            "code_version": self.code_version,
            "parameter_hash": self.parameter_hash,
            "built_by": self.built_by,
            "build_ms": self.build_ms,
            "ok": self.ok,
            "series": [s.to_dict() for s in self.series.values()],
            "forming": {str(k): (v.to_dict() if v else None) for k, v in self.forming.items()},
            "point_in_time": [p.to_dict() for p in self.point_in_time.values()],
            "point_in_time_history": {f"{s}:{a}": len(v)
                                      for (s, a), v in self.point_in_time_history.items()},
            "instruments": sorted(str(k) for k in self.instruments),
            "problems": self.problems,
            "dq": dict(self.dq),
        }
