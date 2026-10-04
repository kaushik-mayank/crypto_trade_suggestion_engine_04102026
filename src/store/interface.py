"""Storage contract (specs/11 §8.4).

SQLite is the **source of truth**; Parquet is a derived artifact regenerated from it. No caller may
depend on SQL dialect or SQLite specifics, so swapping the backend touches only ``src/store/``.

The load-bearing guarantee here is the last method: ``get_point_in_time`` and ``get_bars`` must
**never** return data newer than ``as_of_ms``. That is the storage layer's contribution to the
look-ahead boundary, and it is asserted in tests rather than trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from normalize.canonical import (
    Bar,
    BookSnapshot,
    FuturesRT,
    InstrumentMeta,
    Positioning,
    Product,
    SpotTicker,
    Timeframe,
    Trade,
)


@dataclass(frozen=True)
class BarMutation:
    """A bar already marked final came back with different values (specs/11 §6.2).

    Rare and serious: every signal whose audit references that ``as_of`` will no longer replay.
    """

    asset: str
    product: Product
    timeframe: Timeframe
    open_time_ms: int
    old: dict[str, str]
    new: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset, "product": self.product.value, "timeframe": self.timeframe.value,
            "open_time_ms": self.open_time_ms, "old": self.old, "new": self.new,
        }


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    finalized: int = 0
    mutations: list[BarMutation] = field(default_factory=list)

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def to_dict(self) -> dict[str, Any]:
        return {"inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged,
                "finalized": self.finalized, "mutations": len(self.mutations)}


@dataclass(frozen=True)
class DQEvent:
    """One data-quality or operational event. The vocabulary the operator greps."""

    kind: str                     # BAR_MUTATION, ROW_REJECTED, GAP_DETECTED, POLL_DROPPED, ...
    component: str
    operation: str
    at_ms: int
    problem_class: str | None = None
    severity: str = "warning"
    asset: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunRecord:
    process: str                  # "recorder" | "app" | "backtest" | "probe"
    started_ms: int
    code_version: str
    parameter_hash: str
    settings_hash: str = ""
    notes: str = ""


class StorageInterface(Protocol):
    # ---- bars ------------------------------------------------------------------------------
    def upsert_bars(self, bars: list[Bar], *, finalize_on_repeat: bool = True) -> UpsertResult: ...

    def get_bars(self, asset: str, product: Product, timeframe: Timeframe, start_ms: int, end_ms: int,
                 *, final_only: bool = True, as_of_ms: int | None = None) -> list[Bar]: ...

    def latest_bar_time(self, asset: str, product: Product, timeframe: Timeframe,
                        *, final_only: bool = True) -> int | None: ...

    def bar_count(self, asset: str, product: Product, timeframe: Timeframe, *, final_only: bool = True) -> int: ...

    def non_final_bar_times(self, asset: str, product: Product, timeframe: Timeframe, *,
                            settled_before_ms: int, limit: int = 1000) -> list[int]: ...

    # ---- point-in-time series (Tier B) -----------------------------------------------------
    def get_point_in_time(self, asset: str, source: str, as_of_ms: int) -> Any | None: ...

    def get_point_in_time_history(self, asset: str, source: str, start_ms: int, end_ms: int,
                                  *, limit: int = 5000) -> list[Any]: ...

    def upsert_futures_rt(self, rows: list[FuturesRT]) -> int: ...

    def upsert_spot_ticker(self, rows: list[SpotTicker]) -> int: ...

    def upsert_positioning(self, rows: list[Positioning]) -> int: ...

    def upsert_books(self, rows: list[BookSnapshot]) -> int: ...

    def upsert_trades(self, rows: list[Trade]) -> int: ...

    def upsert_instrument(self, rows: list[InstrumentMeta]) -> int: ...

    def get_instrument(self, asset: str, product: Product, as_of_ms: int | None = None) -> InstrumentMeta | None: ...

    # ---- gaps and known-absent bars --------------------------------------------------------
    def confirm_absent_bar(self, asset: str, product: Product, timeframe: Timeframe, open_time_ms: int,
                           *, at_ms: int) -> int: ...

    def is_absent_confirmed(self, asset: str, product: Product, timeframe: Timeframe, open_time_ms: int,
                            *, required: int = 2) -> bool: ...

    def known_absent_bars(self, asset: str, product: Product, timeframe: Timeframe,
                          start_ms: int, end_ms: int, *, required: int = 2) -> set[int]: ...

    # ---- events and runs -------------------------------------------------------------------
    def record_dq_event(self, event: DQEvent) -> int: ...

    def dq_event_counts(self, since_ms: int) -> dict[str, int]: ...

    def record_run(self, run: RunRecord) -> int: ...

    def close_run(self, run_id: int, *, stopped_ms: int, reason: str) -> None: ...

    # ---- lifecycle -------------------------------------------------------------------------
    def health(self) -> Any: ...

    def close(self) -> None: ...
