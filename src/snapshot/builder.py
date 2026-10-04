"""Builds a :class:`MarketSnapshot` — the one place look-ahead could still enter.

Everything here applies ``as_of`` and nothing else reads the store on the engine's behalf. Three
rules earn their keep:

**Bars are asked for with ``as_of_ms``**, so the store itself re-checks that nothing unclosed is
returned (:class:`store.sqlite_store.SqliteStore._assert_no_lookahead`). Belt and braces: the
builder is the belt, that assertion is the braces.

**The forming bar is aggregated from closed finer bars, never read as a partial coarse bar.** In live
those are the same thing; in a backtest they are emphatically not — the stored 1h bar at ``as_of``
is *already complete*, so reading it as "the bar so far" would hand the strategy the rest of the
hour. Building it from 1m bars that closed at or before ``as_of`` makes that impossible, and makes
live and backtest snapshots structurally identical (specs/10 §2.2).

**Gaps are recorded, not adjudicated here.** specs/12 §6 gates gaps *per computation window*, so the
builder reports which slots are missing and which are confirmed absent and lets the feature layer
apply the timeframe rule. What the builder does judge is warm-up depth and the trailing edge: too
few bars, or a series whose newest bar is more than ``edge_tolerance_bars`` behind, is not usable.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.errors import SnapshotError
from core.logging import get_logger
from core.version import code_version as _code_version
from normalize.canonical import Product, Timeframe, validate_ms
from pipeline.gaps import expected_bar_times
from snapshot.models import (
    COMPONENT,
    FormingBar,
    InstrumentKey,
    MarketSnapshot,
    PointInTimeValue,
    SeriesKey,
    SeriesState,
)
from store.sqlite_store import PIT_SOURCES

log = get_logger("snapshot.builder")

#: Warm-up depth per timeframe (specs/02 §9, specs/12 §5). 1h/4h/1d are the spec's own numbers;
#: 1m/15m carry a nominal depth because specs/02 §3 confines them to trigger evaluation, never alpha.
WARMUP_BARS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 120,
    Timeframe.M15: 120,
    Timeframe.M30: 120,
    Timeframe.H1: 1440,
    Timeframe.H4: 360,
    Timeframe.H8: 180,
    Timeframe.D1: 180,
}

#: Freshness windows per point-in-time source, in ms (specs/11 §9 defaults; overridden from settings).
DEFAULT_FRESHNESS_MS: dict[str, int] = {
    "futures_rt": 60_000,
    "spot_ticker": 120_000,
    "spot_book": 600_000,
    "futures_book": 600_000,
    "positioning": 2_700_000,
}


@dataclass(frozen=True)
class SeriesRequest:
    """One series the engine will need. ``bars`` is how many to load; ``min_bars`` is the warm-up
    depth below which the series is refused rather than used short."""

    key: SeriesKey
    bars: int
    min_bars: int | None = None

    def __post_init__(self) -> None:
        if self.bars <= 0:
            raise SnapshotError(
                f"series {self.key} requested {self.bars} bars",
                component=COMPONENT, operation="SeriesRequest",
                context={"key": str(self.key), "bars": self.bars},
                likely_cause="a caller computed a window size from an empty or zero-valued parameter",
                remediation="request at least one bar",
            )
        if self.min_bars is not None and self.min_bars > self.bars:
            raise SnapshotError(
                f"series {self.key} needs {self.min_bars} bars to be warm but only loads {self.bars}",
                component=COMPONENT, operation="SeriesRequest",
                context={"key": str(self.key), "bars": self.bars, "min_bars": self.min_bars},
                likely_cause="the warm-up depth exceeds the load window, so the series can never be warm",
                remediation="load at least min_bars bars",
            )

    @property
    def required(self) -> int:
        return self.min_bars if self.min_bars is not None else min(self.bars, WARMUP_BARS[self.key.timeframe])


@dataclass(frozen=True)
class SnapshotRequest:
    """Everything the engine is permitted to see. Declared up front, so a strategy cannot widen it
    at runtime — that is what makes the boundary structural rather than advisory."""

    series: tuple[SeriesRequest, ...]
    tier_b: tuple[tuple[str, str], ...] = ()          # (source, asset)
    instruments: tuple[InstrumentKey, ...] = ()
    forming: tuple[SeriesKey, ...] = ()
    forming_from: Timeframe = Timeframe.M1
    #: How far back to load Tier B history for every source in ``tier_b``. Zero means none, and a
    #: feature that needs a trailing series then refuses rather than computing one from a single point.
    tier_b_history_ms: int = 0

    def __post_init__(self) -> None:
        if not self.series:
            raise SnapshotError(
                "a snapshot request must name at least one bar series",
                component=COMPONENT, operation="SnapshotRequest",
                likely_cause="the request was assembled from an empty asset or timeframe list",
                remediation="name the assets and timeframes the engine needs",
            )
        # Accept plain (asset, product) tuples — they are the natural thing to write — but normalise
        # them here, so a mistyped key fails with this message rather than an AttributeError deep in
        # the builder.
        keys: list[InstrumentKey] = []
        for raw in self.instruments:
            try:
                keys.append(InstrumentKey(*raw))
            except TypeError as e:
                raise SnapshotError(
                    f"instrument key {raw!r} is not (asset, product)",
                    component=COMPONENT, operation="SnapshotRequest",
                    context={"given": repr(raw)},
                    likely_cause="a bare asset string or a three-element tuple was passed",
                    remediation='pass InstrumentKey(asset, product) or ("B-BTC_USDT", Product.FUTURES)',
                    cause=e,
                ) from e
        object.__setattr__(self, "instruments", tuple(keys))

        seen: set[SeriesKey] = set()
        for s in self.series:
            if s.key in seen:
                raise SnapshotError(
                    f"series {s.key} requested twice with different windows",
                    component=COMPONENT, operation="SnapshotRequest",
                    context={"key": str(s.key)},
                    likely_cause="two callers added the same series; the snapshot cannot hold both windows",
                    remediation="merge them and request the longer window",
                )
            seen.add(s.key)
        for source, asset in self.tier_b:
            if source not in PIT_SOURCES:
                raise SnapshotError(
                    f"unknown point-in-time source {source!r}",
                    component=COMPONENT, operation="SnapshotRequest",
                    context={"source": source, "asset": asset, "known": sorted(PIT_SOURCES)},
                    likely_cause="a typo, or a source added to the store without registering it",
                    remediation=f"use one of {sorted(PIT_SOURCES)}",
                )
        for key in self.forming:
            if key.timeframe.ms <= self.forming_from.ms:
                raise SnapshotError(
                    f"cannot build a forming {key.timeframe.value} bar from {self.forming_from.value} bars",
                    component=COMPONENT, operation="SnapshotRequest",
                    context={"key": str(key), "forming_from": self.forming_from.value},
                    likely_cause="the forming bar must be aggregated from a strictly finer timeframe; "
                                 "reading the partial coarse bar instead would leak the rest of the bar "
                                 "in a backtest",
                    remediation="request forming bars for coarser timeframes only",
                )

    @classmethod
    def standard(cls, assets: list[str], *, product: Product = Product.FUTURES,
                 timeframes: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4, Timeframe.D1),
                 bars: Mapping[Timeframe, int] | None = None,
                 tier_b_sources: tuple[str, ...] = ("futures_rt", "spot_ticker", "positioning",
                                                    "futures_book", "spot_book"),
                 forming_timeframes: tuple[Timeframe, ...] = (Timeframe.H1,),
                 tier_b_history_ms: int = 7 * 86_400_000) -> SnapshotRequest:
        """The request the live app uses: alpha timeframes at warm-up depth, Tier B per asset,
        instrument metadata for fees, and a forming 1h bar for the trigger."""
        depth = dict(bars or {})
        series = tuple(
            SeriesRequest(key=SeriesKey(a, product, tf), bars=depth.get(tf, WARMUP_BARS[tf]))
            for a in assets for tf in timeframes
        )
        return cls(
            series=series,
            tier_b=tuple((s, a) for a in assets for s in tier_b_sources),
            instruments=tuple(InstrumentKey(a, product) for a in assets),
            forming=tuple(SeriesKey(a, product, tf) for a in assets for tf in forming_timeframes),
            tier_b_history_ms=tier_b_history_ms,
        )

    @property
    def keys(self) -> tuple[SeriesKey, ...]:
        return tuple(s.key for s in self.series)


@dataclass
class SnapshotBuilder:
    """Turns ``(request, as_of)`` into an immutable snapshot. Holds the store; the snapshot does not."""

    store: Any
    parameter_hash: str
    freshness_ms: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_FRESHNESS_MS))
    #: How far behind the newest expected bar the store may be and the series still count as usable.
    #: One bar, because candles are polled shortly *after* the close (settings.polling offsets), so a
    #: just-closed bar is legitimately not stored yet. Two would mean ingestion is genuinely behind.
    edge_tolerance_bars: int = 1
    code_version: str = ""

    def __post_init__(self) -> None:
        if not self.code_version:
            self.code_version = _code_version()
        if not self.parameter_hash:
            raise SnapshotError(
                "snapshot builder created without a parameter hash",
                component=COMPONENT, operation="init",
                likely_cause="the parameter registry was not loaded before the builder",
                remediation="pass load_registry().parameter_hash; a signal without it cannot be replayed",
            )

    @classmethod
    def from_settings(cls, store: Any, settings: Any, parameter_hash: str) -> SnapshotBuilder:
        f = settings.freshness
        return cls(store=store, parameter_hash=parameter_hash, freshness_ms={
            "futures_rt": int(f.futures_rt_max_age_s * 1000),
            "spot_ticker": int(f.spot_ticker_max_age_s * 1000),
            "spot_book": int(f.book_max_age_s * 1000),
            "futures_book": int(f.book_max_age_s * 1000),
            "positioning": int(f.positioning_max_age_s * 1000),
        })

    # ---- build ------------------------------------------------------------------------------

    def build(self, request: SnapshotRequest, as_of_ms: int, *, built_by: str = "live",
              dq: Mapping[str, Any] | None = None) -> MarketSnapshot:
        started = time.perf_counter()
        validate_ms(as_of_ms, field="as_of_ms", component=COMPONENT, operation="build")

        series = {s.key: self._series(s, as_of_ms) for s in request.series}
        forming = {k: self._forming(k, request.forming_from, as_of_ms) for k in request.forming}
        pit = {(src, asset): self._point_in_time(src, asset, as_of_ms) for src, asset in request.tier_b}
        history: dict[tuple[str, str], tuple[Any, ...]] = {}
        if request.tier_b_history_ms > 0:
            start = as_of_ms - request.tier_b_history_ms
            for src, asset in request.tier_b:
                history[(src, asset)] = tuple(
                    self.store.get_point_in_time_history(asset, src, start, as_of_ms))
        instruments = {}
        for ik in request.instruments:
            meta = self.store.get_instrument(ik.asset, ik.product, as_of_ms)
            if meta is not None:
                instruments[ik] = meta

        snap = MarketSnapshot(
            as_of_ms=as_of_ms, code_version=self.code_version, parameter_hash=self.parameter_hash,
            series=series, forming=forming, point_in_time=pit, point_in_time_history=history,
            instruments=instruments,
            dq=dict(dq or {}), built_by=built_by,
            build_ms=int((time.perf_counter() - started) * 1000),
        )
        if not snap.ok:
            log.info("snapshot_incomplete", as_of_ms=as_of_ms, built_by=built_by,
                     snapshot_id=snap.snapshot_id, problems=snap.problems[:6])
        return snap

    # ---- one series -------------------------------------------------------------------------

    def _series(self, req: SeriesRequest, as_of_ms: int) -> SeriesState:
        key = req.key
        tf = key.timeframe
        latest_expected = ((as_of_ms - tf.ms) // tf.ms) * tf.ms
        window_start = latest_expected - (req.bars - 1) * tf.ms

        bars = tuple(self.store.get_bars(key.asset, key.product, tf, window_start, as_of_ms,
                                         final_only=True, as_of_ms=as_of_ms))
        absent = self.store.known_absent_bars(key.asset, key.product, tf, window_start, as_of_ms)
        present = {b.open_time_ms for b in bars}
        expected = expected_bar_times(tf, window_start, latest_expected)
        missing = tuple(t for t in expected if t not in present and t not in absent)

        def state(ok: bool, reason: str | None = None, detail: str | None = None) -> SeriesState:
            return SeriesState(key=key, bars=bars, requested=req.bars, min_required=req.required,
                               ok=ok, reason=reason, detail=detail,
                               absent_times=frozenset(absent & set(expected)), unconfirmed_gaps=missing)

        if not bars:
            return state(False, "EMPTY",
                         f"no closed {tf.value} bars stored for {key.asset} at or before as_of; "
                         "either this asset was never ingested or the window predates the history")
        # Staleness is checked **before** depth, and the order is load-bearing. Both can be true at
        # once -- a recorder stopped two weeks ago leaves a window that is short *and* whose newest bar
        # is ancient -- but they read as opposite instructions. WARMING says "the system is fine, give
        # it time"; EDGE_BEHIND says "ingestion is dead, go restart it". Reporting the reassuring one
        # for a dead feed is how a stopped recorder goes unnoticed for a fortnight, which is exactly
        # what happened here (F-035).
        lag_bars = (latest_expected - bars[-1].open_time_ms) // tf.ms
        if lag_bars > self.edge_tolerance_bars:
            short_by = max(0, req.required - len(bars))
            return state(False, "EDGE_BEHIND",
                         f"newest stored {tf.value} bar is {lag_bars} bars behind the one that should "
                         f"have closed by as_of (tolerance {self.edge_tolerance_bars}); ingestion is "
                         "behind, so this series describes the past, not now"
                         + (f". The window is also {short_by} bars short, but that is a consequence of "
                            "the stale edge, not a separate problem" if short_by else ""))
        if len(bars) < req.required:
            return state(False, "WARMING",
                         f"need {req.required} {tf.value} bars, have {len(bars)} "
                         f"({(req.required - len(bars)) * tf.ms / 3_600_000:.1f}h more to warm)")
        return state(True)

    # ---- the forming bar --------------------------------------------------------------------

    def _forming(self, key: SeriesKey, fine: Timeframe, as_of_ms: int) -> FormingBar | None:
        """Aggregate closed ``fine`` bars from the start of the current ``key.timeframe`` window.

        Returns ``None`` when nothing has closed yet in this window — normal in the first minute of
        an hour, and honest: there is no "bar so far" to describe.
        """
        tf = key.timeframe
        window_start = (as_of_ms // tf.ms) * tf.ms
        bars = self.store.get_bars(key.asset, key.product, fine, window_start, as_of_ms,
                                   final_only=True, as_of_ms=as_of_ms)
        if not bars:
            return None
        return FormingBar(
            key=key, open_time_ms=window_start, as_of_ms=as_of_ms,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            last=bars[-1].close,
            volume_base=sum((b.volume_base for b in bars), Decimal(0)),
            constituents=len(bars), built_from=fine,
        )

    # ---- Tier B ------------------------------------------------------------------------------

    def _point_in_time(self, source: str, asset: str, as_of_ms: int) -> PointInTimeValue:
        max_age = self.freshness_ms.get(source, 120_000)
        value = self.store.get_point_in_time(asset, source, as_of_ms)
        if value is None:
            return PointInTimeValue(source=source, asset=asset, value=None, source_ts_ms=None,
                                    age_ms=None, max_age_ms=max_age, ok=False,
                                    reason=f"no {source} record for {asset} at or before as_of "
                                           "(Tier B has zero history, so there is nothing to fall back on)")
        ts = getattr(value, "source_ts_ms", None) or getattr(value, "ts_ms", None)
        if ts is None:
            return PointInTimeValue(source=source, asset=asset, value=value, source_ts_ms=None,
                                    age_ms=None, max_age_ms=max_age, ok=False,
                                    reason=f"the {source} record carries no timestamp of its own, so its "
                                           "freshness cannot be judged")
        age = as_of_ms - int(ts)
        if age > max_age:
            return PointInTimeValue(source=source, asset=asset, value=value, source_ts_ms=int(ts),
                                    age_ms=age, max_age_ms=max_age, ok=False,
                                    reason=f"{age / 1000:.1f}s old, window is {max_age / 1000:.0f}s")
        if age < 0:
            return PointInTimeValue(source=source, asset=asset, value=value, source_ts_ms=int(ts),
                                    age_ms=age, max_age_ms=max_age, ok=False,
                                    reason=f"the record is stamped {-age / 1000:.1f}s after as_of; either "
                                           "this host's clock is wrong or the source's is")
        return PointInTimeValue(source=source, asset=asset, value=value, source_ts_ms=int(ts),
                                age_ms=age, max_age_ms=max_age, ok=True)
