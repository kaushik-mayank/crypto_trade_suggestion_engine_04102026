"""Normalization outcomes (specs/11 §4.1).

The governing rule is **reject, do not coerce**. A normalizer therefore never returns a
"best effort" record: it returns one of three outcomes, and a rejection always carries the reason
and a reference to the raw bytes so the actual payload can be inspected (specs/19 §8).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from core.errors import CryptoTraderError

T = TypeVar("T")


@dataclass(frozen=True)
class RejectedRow:
    """One row inside an otherwise usable payload that could not be accepted."""

    identifier: str                     # e.g. the pair, or an index
    reason: str                         # a stable reason code: SKW_NONZERO, STALE_ROW, SCHEMA_INCOMPLETE
    detail: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"identifier": self.identifier, "reason": self.reason, "detail": self.detail, **self.context}


@dataclass(frozen=True)
class Accepted(Generic[T]):
    records: tuple[T, ...]
    raw_ref: str | None = None
    warnings: tuple[str, ...] = ()

    ok = True

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class Partial(Generic[T]):
    """Some rows accepted, some rejected. Normal for ``current_prices/futures/rt`` (specs/11 §5.1)."""

    records: tuple[T, ...]
    rejected: tuple[RejectedRow, ...]
    raw_ref: str | None = None

    ok = True

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class Rejected:
    """The whole payload is unusable. ``error`` carries the classification and remediation."""

    error: CryptoTraderError
    raw_ref: str | None = None

    ok = False
    records: tuple[Any, ...] = ()

    def __len__(self) -> int:
        return 0

    @property
    def reason(self) -> str:
        return self.error.code


NormalizeOutcome = Accepted | Partial | Rejected
