"""Data quality, computed at ingest (specs/11 §9, specs/02 Family 10).

Family 10 lives here rather than in the feature layer because it describes **ingestion health**, not
market state. It gates everything downstream and is never alpha.

| metric | computation | verified basis |
|---|---|---|
| ``DQ_AGE`` | ``now − source_timestamp`` per source | per-source windows in settings |
| ``DQ_LATENCY`` | ``received_at_ms − btST`` | the measurable TPE relay time |
| ``DQ_CLOCK`` | source timestamp vs local midpoint | ±2 s measured (F-026) → tolerance ≥ 3 s |
| ``DQ_COMPLETE`` | expected vs received bars, consulting ``known_absent_bars`` | — |
| ``DQ_DISAGREE`` | spot last vs futures mark, **only within 2 s** | an unsynchronised comparison measures elapsed time, not disagreement (F-023) |

``DQ_SKW`` is deliberately **not** implemented as a gate: F-004 showed ``skw`` is one value shared by
every fresh row, so rejecting on it would discard live data. Its range is recorded instead.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.errors import ProblemClass
from core.health import ComponentHealth, SystemStatus
from core.logging import get_logger

log = get_logger("ingest.dq")


@dataclass
class SourceState:
    """Rolling observations for one source, used for freshness, skew and health."""

    name: str
    max_age_ms: int
    last_source_ts_ms: int | None = None
    last_received_ms: int | None = None
    last_latency_ms: int | None = None
    skew_samples: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    rejections: dict[str, int] = field(default_factory=dict)
    accepted: int = 0

    def observe(self, *, source_ts_ms: int | None, sent_at_ms: int, received_at_ms: int,
                accepted: int = 1) -> None:
        self.last_received_ms = received_at_ms
        self.accepted += accepted
        if source_ts_ms is None:
            return
        self.last_source_ts_ms = source_ts_ms
        self.last_latency_ms = received_at_ms - source_ts_ms
        midpoint = (sent_at_ms + received_at_ms) / 2
        self.skew_samples.append(source_ts_ms - midpoint)

    def record_rejection(self, reason: str, n: int = 1) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + n

    def age_ms(self, now_ms: int) -> int | None:
        return None if self.last_source_ts_ms is None else now_ms - self.last_source_ts_ms

    def is_fresh(self, now_ms: int) -> bool:
        age = self.age_ms(now_ms)
        return age is not None and age <= self.max_age_ms

    @property
    def skew_ms(self) -> float | None:
        return statistics.median(self.skew_samples) if self.skew_samples else None

    def health(self, now_ms: int) -> ComponentHealth:
        if self.last_received_ms is None:
            return ComponentHealth(name=f"dq:{self.name}", status=SystemStatus.WARMING,
                                   detail="no observation yet")
        age = self.age_ms(now_ms)
        if age is None:
            return ComponentHealth(
                name=f"dq:{self.name}", status=SystemStatus.DEGRADED,
                detail="source carries no timestamp of its own, so freshness cannot be judged",
                problem_class=ProblemClass.DATA_QUALITY,
                last_success_ms=self.last_received_ms,
                remediation="treat this series' age as poll-cadence-bounded (positioning does this)")
        if age > self.max_age_ms:
            return ComponentHealth(
                name=f"dq:{self.name}", status=SystemStatus.STALE,
                detail=f"{age / 1000:.1f}s old, window is {self.max_age_ms / 1000:.0f}s",
                problem_class=ProblemClass.EXTERNAL, last_success_ms=self.last_received_ms,
                context={"age_ms": age, "max_age_ms": self.max_age_ms},
                remediation="the venue or the link is behind; the system must go quiet rather than "
                            "compute on stale inputs")
        return ComponentHealth.healthy(f"dq:{self.name}", last_success_ms=self.last_received_ms,
                                       detail=f"{age / 1000:.1f}s old",   # age is not None here
                                       latency_ms=self.last_latency_ms,
                                       skew_ms=round(self.skew_ms, 1) if self.skew_ms is not None else None)


@dataclass
class DisagreementCheck:
    """``DQ_DISAGREE`` with the hard 2-second gate (specs/02 Family 6)."""

    tolerance_ms: int = 2000
    max_bps: Decimal = Decimal(50)
    compared: int = 0
    discarded_unaligned: int = 0
    last_bps: Decimal | None = None
    breaches: int = 0

    def check(self, *, spot_last: Decimal, spot_ts_ms: int, futures_mark: Decimal,
              futures_ts_ms: int) -> Decimal | None:
        """Basis in bps, or ``None`` when the pair is not timestamp-aligned.

        An unaligned pair is **discarded, not approximated** — the Phase-1 finding was that a
        ~120-point apparent gap on BTC was entirely explained by a 6.5-minute fetch gap.
        """
        if abs(futures_ts_ms - spot_ts_ms) > self.tolerance_ms:
            self.discarded_unaligned += 1
            return None
        self.compared += 1
        bps = (futures_mark / spot_last - 1) * Decimal(10_000)
        self.last_bps = bps
        if abs(bps) > self.max_bps:
            self.breaches += 1
            log.warning("dq_disagree", spot_last=str(spot_last), futures_mark=str(futures_mark),
                        bps=float(bps), tolerance_ms=self.tolerance_ms,
                        detail="spot and futures disagree beyond the plausible bound")
        return bps

    @property
    def discard_fraction(self) -> float | None:
        total = self.compared + self.discarded_unaligned
        return None if total == 0 else self.discarded_unaligned / total

    def health(self) -> ComponentHealth:
        if self.compared == 0 and self.discarded_unaligned == 0:
            return ComponentHealth(name="dq:disagree", status=SystemStatus.WARMING, detail="no pairs compared yet")
        if self.breaches:
            return ComponentHealth(
                name="dq:disagree", status=SystemStatus.DEGRADED,
                detail=f"{self.breaches} comparison(s) beyond {self.max_bps} bps (last {self.last_bps:.2f})",
                problem_class=ProblemClass.DATA_QUALITY,
                context={"compared": self.compared, "discarded_unaligned": self.discarded_unaligned},
                remediation="check whether one feed is stale; basis beyond a few bps is implausible "
                            "at this venue (measured −4 bps mean, F-023)")
        return ComponentHealth.healthy(
            "dq:disagree", detail=f"{self.compared} aligned comparisons, last {self.last_bps:.2f} bps"
            if self.last_bps is not None else "no aligned comparison yet",
            discard_fraction=round(self.discard_fraction, 4) if self.discard_fraction is not None else None)


class DQMonitor:
    """Aggregates per-source state and produces the data-quality half of the health report."""

    def __init__(self, windows: dict[str, int], *, clock_tolerance_ms: int = 3000,
                 disagree_tolerance_ms: int = 2000):
        self.sources = {name: SourceState(name=name, max_age_ms=w) for name, w in windows.items()}
        self.disagree = DisagreementCheck(tolerance_ms=disagree_tolerance_ms)
        self.clock_tolerance_ms = clock_tolerance_ms

    def source(self, name: str) -> SourceState:
        if name not in self.sources:
            self.sources[name] = SourceState(name=name, max_age_ms=120_000)
        return self.sources[name]

    def is_fresh(self, name: str, now_ms: int) -> bool:
        return self.source(name).is_fresh(now_ms)

    def clock_health(self) -> ComponentHealth:
        """``DQ_CLOCK``: the median skew across sources that carry their own timestamp."""
        samples = [s.skew_ms for s in self.sources.values() if s.skew_ms is not None]
        if not samples:
            return ComponentHealth(name="dq:clock", status=SystemStatus.WARMING, detail="no skew samples yet")
        worst = max(samples, key=abs)
        if abs(worst) > self.clock_tolerance_ms:
            return ComponentHealth(
                name="dq:clock", status=SystemStatus.DEGRADED,
                detail=f"median skew {worst:.0f} ms exceeds the {self.clock_tolerance_ms} ms tolerance",
                problem_class=ProblemClass.INFRASTRUCTURE,
                context={"worst_median_skew_ms": round(worst, 1)},
                remediation="check NTP/chrony on this host; skew invalidates freshness gating and the "
                            "2-second basis tolerance")
        return ComponentHealth.healthy("dq:clock", detail=f"worst median skew {worst:.0f} ms")

    def health(self, now_ms: int) -> list[ComponentHealth]:
        return [*(s.health(now_ms) for s in self.sources.values()), self.clock_health(), self.disagree.health()]

    def summary(self, now_ms: int) -> dict[str, Any]:
        return {
            "sources": {name: {"age_s": _age_s(s, now_ms),
                               "latency_ms": s.last_latency_ms, "skew_ms": s.skew_ms,
                               "accepted": s.accepted, "rejections": dict(s.rejections)}
                        for name, s in self.sources.items()},
            "disagree": {"compared": self.disagree.compared,
                         "discarded_unaligned": self.disagree.discarded_unaligned,
                         "discard_fraction": self.disagree.discard_fraction,
                         "last_bps": float(self.disagree.last_bps) if self.disagree.last_bps is not None else None},
        }


def _age_s(state: SourceState, now_ms: int) -> float | None:
    age = state.age_ms(now_ms)
    return None if age is None else age / 1000


def completeness(expected_times: set[int], present_times: set[int], known_absent: set[int]) -> dict[str, Any]:
    """``DQ_COMPLETE``. A bar confirmed absent is **not** missing — it is a no-trade bar."""
    missing = expected_times - present_times - known_absent
    denominator = len(expected_times - known_absent)
    return {
        "expected": len(expected_times),
        "present": len(present_times),
        "known_absent": len(known_absent & expected_times),
        "missing": len(missing),
        "missing_times": sorted(missing)[:20],
        "completeness": (1 - len(missing) / denominator) if denominator else 1.0,
    }
