"""Live and backtest drivers (specs/10 §2.2).

Both own a clock and a store handle; the engine downstream owns neither. That is the whole
difference between them:

* :class:`LiveDriver` — ``as_of = now``, one snapshot at a time.
* :class:`BacktestDriver` — ``as_of`` steps along a grid, snapshots yielded in order.

They share one :class:`snapshot.builder.SnapshotBuilder` and one ``SnapshotRequest``, so "the
backtest saw something live cannot" is not a bug that can be introduced in a driver — it would have
to be introduced in the builder, which both use identically. ``test_snapshot.py`` asserts parity on
the same ``as_of`` to keep it that way.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.errors import ClockError, ProtocolViolation, SnapshotError
from core.health import ComponentHealth, SystemStatus
from core.logging import get_logger
from normalize.canonical import Timeframe
from snapshot.builder import SnapshotBuilder, SnapshotRequest
from snapshot.models import MarketSnapshot

log = get_logger("pipeline.drivers")

COMPONENT = "pipeline.drivers"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class LiveDriver:
    """Builds snapshots at wall-clock now.

    The clock is injected so tests can control it, and checked against the store on every build: if
    recorded bars claim to have closed in the future, the host clock is wrong, and every freshness
    verdict downstream would be wrong with it. That is infrastructure, not a bug or a venue problem,
    and :class:`ClockError` says so.
    """

    builder: SnapshotBuilder
    request: SnapshotRequest
    clock: Callable[[], int] = _now_ms
    #: How far ahead of ``now`` a stored bar's close may be before the clock is called wrong.
    #: Wider than the measured +/-2 s venue skew (F-026) so ordinary jitter is not an alarm.
    clock_tolerance_ms: int = 5_000
    snapshots_built: int = 0
    last_as_of_ms: int | None = None
    last_ok: bool | None = None

    def snapshot(self, *, dq: Mapping[str, Any] | None = None) -> MarketSnapshot:
        as_of = self.clock()
        self._assert_clock_sane(as_of)
        snap = self.builder.build(self.request, as_of, built_by="live", dq=dq)
        self.snapshots_built += 1
        self.last_as_of_ms = as_of
        self.last_ok = snap.ok
        return snap

    def _assert_clock_sane(self, as_of_ms: int) -> None:
        for key in self.request.keys:
            newest = self.builder.store.latest_bar_time(key.asset, key.product, key.timeframe)
            if newest is None:
                continue
            close_ms = newest + key.timeframe.ms
            if close_ms > as_of_ms + self.clock_tolerance_ms:
                raise ClockError(
                    f"a stored {key.timeframe.value} bar for {key.asset} closed "
                    f"{(close_ms - as_of_ms) / 1000:.1f}s in the future",
                    component=COMPONENT, operation="snapshot",
                    context={"key": str(key), "bar_close_ms": close_ms, "now_ms": as_of_ms,
                             "tolerance_ms": self.clock_tolerance_ms},
                    likely_cause="this host's clock is behind (a suspend/resume without NTP resync will "
                                 "do it), or the machine that recorded these bars was ahead",
                    remediation="resync the clock (w32tm /resync on Windows, chronyc makestep on Linux) "
                                "before starting the app; as_of and every freshness window depend on it",
                )

    def health(self) -> ComponentHealth:
        if self.snapshots_built == 0:
            return ComponentHealth(name="driver:live", status=SystemStatus.WARMING,
                                   detail="no snapshot built yet")
        return ComponentHealth.healthy(
            "driver:live", detail=f"{self.snapshots_built} snapshots, last complete={self.last_ok}",
            last_success_ms=self.last_as_of_ms)


@dataclass
class BacktestDriver:
    """Steps ``as_of`` across a grid, yielding one snapshot per step.

    ``max_as_of_ms`` is the holdout wall: if the caller passes the boundary from
    :class:`store.holdout.HoldoutGuard`, stepping past it raises :class:`ProtocolViolation` rather
    than quietly producing results on data that was promised to stay untouched (specs/09 §1).
    """

    builder: SnapshotBuilder
    request: SnapshotRequest
    start_ms: int
    end_ms: int
    step: Timeframe
    max_as_of_ms: int | None = None
    #: Offset added to each boundary, so ``as_of`` sits just inside the bar that has closed rather
    #: than exactly on its close. Without it, floor arithmetic at the boundary is ambiguous.
    offset_ms: int = 0
    steps_yielded: int = 0
    incomplete_steps: int = 0
    #: Steps where an OHLCV **series** was unusable — the kind that invalidates a Tier A evaluation.
    tier_a_incomplete: int = 0
    #: Steps whose only problem was absent point-in-time data, which is permanent over history.
    tier_b_only_incomplete: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    #: Series-level reasons only — the Tier A problems, which are few and actionable.
    series_reasons: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms:
            raise SnapshotError(
                f"backtest window ends at or before it starts ({self.start_ms} -> {self.end_ms})",
                component=COMPONENT, operation="BacktestDriver",
                context={"start_ms": self.start_ms, "end_ms": self.end_ms},
                likely_cause="the dates were swapped, or one was parsed as seconds instead of ms",
                remediation="pass UTC milliseconds with start < end",
            )
        if self.start_ms % self.step.ms:
            raise SnapshotError(
                f"backtest start {self.start_ms} is not aligned to a {self.step.value} boundary",
                component=COMPONENT, operation="BacktestDriver",
                context={"start_ms": self.start_ms, "step": self.step.value,
                         "aligned": (self.start_ms // self.step.ms) * self.step.ms},
                likely_cause="an arbitrary timestamp was used as the grid origin",
                remediation="align the start to the step's UTC boundary so every run steps the same grid",
            )
        if not 0 <= self.offset_ms < self.step.ms:
            raise SnapshotError(
                f"offset {self.offset_ms} ms does not lie inside one {self.step.value} step",
                component=COMPONENT, operation="BacktestDriver",
                context={"offset_ms": self.offset_ms, "step_ms": self.step.ms},
                likely_cause="an offset larger than the step would silently skip or repeat a bar",
                remediation=f"use 0 <= offset_ms < {self.step.ms}",
            )
        if self.max_as_of_ms is not None and self.end_ms > self.max_as_of_ms:
            raise ProtocolViolation(
                f"backtest window ends {(self.end_ms - self.max_as_of_ms) / 86_400_000:.1f} days inside "
                "the holdout period",
                component=COMPONENT, operation="BacktestDriver",
                context={"end_ms": self.end_ms, "holdout_start_ms": self.max_as_of_ms},
                likely_cause="the window was extended to 'use all the data' without checking the wall",
                remediation="end the window at the holdout boundary; the holdout is unlocked once, "
                            "deliberately, through HoldoutGuard.unlock()",
            )

    def as_of_grid(self) -> list[int]:
        return list(range(self.start_ms + self.offset_ms, self.end_ms + 1, self.step.ms))

    def snapshots(self) -> Iterator[MarketSnapshot]:
        for as_of in self.as_of_grid():
            snap = self.builder.build(self.request, as_of, built_by="backtest")
            self.steps_yielded += 1
            if not snap.ok:
                self.incomplete_steps += 1
                kinds = set()
                for p in snap.problems:
                    reason = str(p.get("reason"))
                    self.reasons[reason] = self.reasons.get(reason, 0) + 1
                    kinds.add(str(p.get("kind")))
                    # Series reasons are counted separately because they are the actionable ones and
                    # there are only a handful of them (EMPTY / WARMING / EDGE_BEHIND). Tier B
                    # reasons are one per source per asset and vastly outnumber them, so a single
                    # ranked list truncated for readability shows only Tier B and hides the problems
                    # worth fixing.
                    if p.get("kind") == "series":
                        self.series_reasons[reason] = self.series_reasons.get(reason, 0) + 1
                # Tracked separately because the two mean entirely different things over history.
                if "series" in kinds:
                    self.tier_a_incomplete += 1
                if kinds == {"point_in_time"}:
                    self.tier_b_only_incomplete += 1
            yield snap
        log.info("backtest_walk_complete", steps=self.steps_yielded,
                 incomplete=self.incomplete_steps, tier_a_incomplete=self.tier_a_incomplete,
                 tier_b_only=self.tier_b_only_incomplete, reasons=self.reasons,
                 start_ms=self.start_ms, end_ms=self.end_ms, step=self.step.value)

    def coverage(self) -> dict[str, Any]:
        """What fraction of the walk is usable — split by *what* was missing.

        One number cannot answer this, and reporting one would be actively misleading over history.
        **Tier B has zero history** before the recorder started (specs/02: funding, positioning, book
        and tape are point-in-time with no archive), so every step in a 2022-2025 window is
        "incomplete" for Tier B — permanently and by design. A single `usable_fraction` would read
        0% and make a perfectly sound Tier A backtest look impossible.

        So three numbers:

        * ``usable_fraction`` — steps with no problem of any kind. The strict reading, and the only
          one a Tier B strategy can be evaluated on.
        * ``tier_a_usable_fraction`` — steps whose **OHLCV series** were all complete. This is the
          number that matters for a Tier A backtest, because a missing funding rate cannot affect a
          strategy that never reads one.
        * ``tier_b_only_incomplete`` — steps whose *only* problem was absent point-in-time data.
          Expected to be ~100% of a historical window, and if it is **not**, something is wrong: it
          would mean Tier B history exists where it should not.
        """
        total = self.steps_yielded
        usable = total - self.incomplete_steps
        tier_a_usable = total - self.tier_a_incomplete
        return {
            "steps": total, "usable": usable, "incomplete": self.incomplete_steps,
            "usable_fraction": (usable / total) if total else None,
            "tier_a_usable": tier_a_usable,
            "tier_a_usable_fraction": (tier_a_usable / total) if total else None,
            "tier_b_only_incomplete": self.tier_b_only_incomplete,
            "tier_b_only_fraction": (self.tier_b_only_incomplete / total) if total else None,
            "reading": "judge a Tier A backtest on tier_a_usable_fraction. Tier B has zero history "
                       "before the recorder started, so tier_b_only_incomplete near 100% over a "
                       "historical window is expected, not a defect (specs/09 §13)",
            # Shown in full: there are at most three distinct series reasons and they are the ones
            # a reader can act on.
            "tier_a_reasons": dict(sorted(self.series_reasons.items(), key=lambda kv: -kv[1])),
            "all_reasons_top": dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])[:8]),
        }
