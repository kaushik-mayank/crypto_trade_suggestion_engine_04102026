"""System health and state reporting (specs/14 §3–§5, specs/17 §8).

The question this answers is the one that matters when something is wrong:

> **Is this an application bug, a configuration mistake, local infrastructure, or CoinDCX?**

Each component reports a ``ComponentHealth`` with a state and, when unhealthy, the
``ProblemClass`` that owns it. The overall ``SystemStatus`` is the worst of them, and the report
lists the specific reasons — so a dashboard banner and a future Azure alert both say *what* broke
and *who* fixes it, rather than "degraded".

Deliberate distinctions, because collapsing any of them misleads (specs/14 §4):

* ``HEALTHY`` — working.
* ``WARMING`` — working, but features are still accumulating bars. Normal for a new install; it is
  **progress, not failure**, and must never render as empty.
* ``DEGRADED`` — some sources failing, others fine. Panels that depend on healthy sources stay live.
* ``STALE`` — data present but too old to trust. A triggered signal still shows its stop.
* ``DOWN`` — ingestion or storage is broken. **Never the same as "no setups found".**
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import CryptoTraderError, ProblemClass


class SystemStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARMING = "WARMING"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DOWN = "DOWN"

    @property
    def rank(self) -> int:
        return {"HEALTHY": 0, "WARMING": 1, "DEGRADED": 2, "STALE": 3, "DOWN": 4}[self.value]

    def worst_of(self, other: SystemStatus) -> SystemStatus:
        return self if self.rank >= other.rank else other


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class ComponentHealth:
    """One named component's state. ``problem_class`` is required unless HEALTHY/WARMING."""

    name: str
    status: SystemStatus
    detail: str = ""
    problem_class: ProblemClass | None = None
    last_success_ms: int | None = None
    consecutive_failures: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    def __post_init__(self) -> None:
        if self.status in (SystemStatus.DEGRADED, SystemStatus.STALE, SystemStatus.DOWN) and self.problem_class is None:
            # Enforced: an unhealthy component with no owner is an unactionable alert.
            raise ValueError(f"component {self.name!r} is {self.status.value} but declares no problem_class")

    @property
    def age_seconds(self) -> float | None:
        return None if self.last_success_ms is None else (now_ms() - self.last_success_ms) / 1000

    @classmethod
    def healthy(cls, name: str, *, last_success_ms: int | None = None, detail: str = "", **ctx: Any) -> ComponentHealth:
        return cls(name=name, status=SystemStatus.HEALTHY, detail=detail, last_success_ms=last_success_ms, context=ctx)

    @classmethod
    def from_error(cls, name: str, err: CryptoTraderError, *, status: SystemStatus = SystemStatus.DOWN,
                   last_success_ms: int | None = None, consecutive_failures: int = 0) -> ComponentHealth:
        """Build health straight from a raised error, so the classification cannot drift from it."""
        return cls(
            name=name,
            status=status,
            detail=err.message,
            problem_class=err.problem_class,
            last_success_ms=last_success_ms,
            consecutive_failures=consecutive_failures,
            context={"code": err.code, **err.context},
            remediation=err.remediation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "problem_class": self.problem_class.value if self.problem_class else None,
            "last_success_ms": self.last_success_ms,
            "age_seconds": round(self.age_seconds, 1) if self.age_seconds is not None else None,
            "consecutive_failures": self.consecutive_failures,
            "context": self.context,
            "remediation": self.remediation,
        }


@dataclass
class HealthReport:
    components: list[ComponentHealth] = field(default_factory=list)
    served_at_ms: int = field(default_factory=now_ms)
    data_as_of_ms: int | None = None
    warmup_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    def add(self, c: ComponentHealth) -> HealthReport:
        self.components.append(c)
        return self

    @property
    def status(self) -> SystemStatus:
        status = SystemStatus.HEALTHY
        for c in self.components:
            status = status.worst_of(c.status)
        return status

    @property
    def problem_classes(self) -> list[str]:
        """Distinct owners of every current problem — the triage summary."""
        seen: list[str] = []
        for c in self.components:
            if c.problem_class and c.problem_class.value not in seen:
                seen.append(c.problem_class.value)
        return seen

    def unhealthy(self) -> list[ComponentHealth]:
        return [c for c in self.components if c.status is not SystemStatus.HEALTHY]

    @property
    def degraded_reasons(self) -> list[str]:
        """Human-readable, specific, and never apologetic (specs/15 §11)."""
        out = []
        for c in self.unhealthy():
            age = f", last ok {c.age_seconds:.0f}s ago" if c.age_seconds is not None else ""
            owner = f" [{c.problem_class.value}]" if c.problem_class else ""
            out.append(f"{c.name}: {c.status.value.lower()} — {c.detail}{age}{owner}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "served_at_ms": self.served_at_ms,
            "data_as_of_ms": self.data_as_of_ms,
            "age_seconds": round((self.served_at_ms - self.data_as_of_ms) / 1000, 1)
            if self.data_as_of_ms is not None else None,
            "problem_classes": self.problem_classes,
            "degraded_reasons": self.degraded_reasons,
            "warmup_pct": self.warmup_pct,
            "notes": self.notes,
            "components": [c.to_dict() for c in self.components],
        }
