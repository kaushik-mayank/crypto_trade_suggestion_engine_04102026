"""Ingest pipeline: adapter → normalizer → store, wired into :class:`PollTask` objects.

Outcome handling is deliberate, because "what happens when a payload is wrong" is the difference
between a system that goes quiet and one that computes on garbage:

* ``Accepted`` → write, observe freshness, done.
* ``Partial`` → write the good rows, record one ``dq_events`` row per rejection reason, and **stay
  healthy**. A stale ``B-NKN_USDT`` row must not stop BTC from updating.
* ``Rejected`` → write nothing and **raise**, so the scheduler backs off and the task's health turns
  DEGRADED then DOWN. A persistent rejection is a real problem (a schema change at the venue) and
  must be visible, not absorbed.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from functools import partial
from typing import Any

from core.logging import get_logger
from ingest import adapters
from ingest.client import IngestClient, IngestResult
from ingest.dq import DQMonitor
from ingest.scheduler import PollTask, Priority
from normalize import book as book_norm
from normalize import candles as candle_norm
from normalize import futures_rt as rt_norm
from normalize import instrument as instrument_norm
from normalize import positioning as pos_norm
from normalize import ticker as ticker_norm
from normalize.base import PayloadContext
from normalize.canonical import Product, Timeframe
from normalize.errors import Partial, Rejected
from store.interface import DQEvent

log = get_logger("ingest.pipeline")

#: How many bars of a finalization sweep to request per pass. Bounded because the sweep shares a
#: request budget with the live polls, and a sweep that starved them would trade a current price for
#: old history — the wrong way round for a system whose job is now.
SETTLE_BATCH_BARS = 300
#: How far past its close a bar must be before a re-fetch can settle it. Close to now the venue may
#: still be revising a bar legitimately, and re-requesting those churns without finalizing anything.
SETTLE_AFTER_BARS = 3


def _ctx(result: IngestResult, *, endpoint: str, asset: str | None = None,
         product: Product | None = None, timeframe: Timeframe | None = None) -> PayloadContext:
    return PayloadContext(endpoint=endpoint, received_at_ms=result.received_at_ms,
                          sent_at_ms=result.sent_at_ms, request_url=result.url,
                          asset=asset, product=product, timeframe=timeframe)


class IngestPipeline:
    """Builds and owns the poll tasks. One instance per process."""

    def __init__(self, *, client: IngestClient, store: Any, dq: DQMonitor,
                 assets: Sequence[str], spot_markets: dict[str, str], settings: Any):
        self.client = client
        self.store = store
        self.dq = dq
        self.assets = list(assets)
        self.spot_markets = dict(spot_markets)
        self.settings = settings

    # ---- shared outcome handling -----------------------------------------------------------

    def _handle(self, outcome: Any, *, source: str, result: IngestResult, write) -> int:
        if isinstance(outcome, Rejected):
            self.dq.source(source).record_rejection(outcome.error.code)
            self.store.record_dq_event(DQEvent(
                kind="PAYLOAD_REJECTED", component=outcome.error.component, operation=source,
                at_ms=result.received_at_ms, problem_class=outcome.error.problem_class.value,
                severity=outcome.error.severity.value,
                detail={"source": source, "code": outcome.error.code, "message": outcome.error.message,
                        "url": result.url}))
            raise outcome.error                      # the scheduler backs off; health turns DEGRADED/DOWN
        if isinstance(outcome, Partial):
            for row in outcome.rejected:
                self.dq.source(source).record_rejection(row.reason)
            reasons: dict[str, int] = {}
            for row in outcome.rejected:
                reasons[row.reason] = reasons.get(row.reason, 0) + 1
            self.store.record_dq_event(DQEvent(
                kind="ROWS_REJECTED", component=f"normalize.{source}", operation=source,
                at_ms=result.received_at_ms, problem_class="data_quality", severity="warning",
                detail={"source": source, "reasons": reasons,
                        "identifiers": [r.identifier for r in outcome.rejected][:10]}))
            log.info("rows_rejected", source=source, reasons=reasons)
        return write(list(outcome.records))

    # ---- task bodies -----------------------------------------------------------------------

    async def poll_futures_rt(self) -> int:
        result = await asyncio.to_thread(adapters.futures_rt, self.client)
        outcome = rt_norm.normalize(result.raw, _ctx(result, endpoint="futures_rt"),
                                    assets=tuple(self.assets),
                                    max_age_ms=int(self.settings.freshness.futures_rt_max_age_s * 1000))
        n = self._handle(outcome, source="futures_rt", result=result,
                         write=self.store.upsert_futures_rt)
        for row in outcome.records:
            self.dq.source("futures_rt").observe(source_ts_ms=row.source_ts_ms,
                                                 sent_at_ms=result.sent_at_ms,
                                                 received_at_ms=result.received_at_ms)
            self._maybe_compare_basis(row.asset, row)
        return n

    async def poll_spot_ticker(self) -> int:
        result = await asyncio.to_thread(adapters.spot_ticker, self.client)
        outcome = ticker_norm.normalize(result.raw, _ctx(result, endpoint="spot_ticker"),
                                        markets=self.spot_markets)
        n = self._handle(outcome, source="spot_ticker", result=result, write=self.store.upsert_spot_ticker)
        for row in outcome.records:
            self.dq.source("spot_ticker").observe(source_ts_ms=row.ts_ms, sent_at_ms=result.sent_at_ms,
                                                 received_at_ms=result.received_at_ms)
        return n

    def _maybe_compare_basis(self, asset: str, rt_row: Any) -> None:
        """``DQ_DISAGREE``: only on a timestamp-aligned pair (specs/11 §9)."""
        spot = self.store.get_point_in_time(asset, "spot_ticker", rt_row.received_at_ms)
        if spot is None or rt_row.source_ts_ms is None:
            return
        self.dq.disagree.check(spot_last=spot.last_price, spot_ts_ms=spot.ts_ms,
                               futures_mark=rt_row.mark_price, futures_ts_ms=rt_row.source_ts_ms)

    async def poll_candles(self, asset: str, product: Product, timeframe: Timeframe,
                           api_resolution: str) -> int:
        """Re-fetch the most recent bars so the two-fetch finalization rule can settle them."""
        refetch = max(2, int(self.settings.polling.refetch_bars))
        latest = self.store.latest_bar_time(asset, product, timeframe, final_only=False)
        now_ms = int(time.time() * 1000)
        start = (latest - refetch * timeframe.ms) if latest else (now_ms - refetch * timeframe.ms)
        if product is Product.SPOT:
            result = await asyncio.to_thread(adapters.spot_candles, self.client, pair=asset,
                                             interval=api_resolution, start_ms=start, end_ms=now_ms,
                                             limit=min(1000, refetch * 4 + 10))
            outcome = candle_norm.normalize_spot(result.raw, _ctx(result, endpoint="spot_candles", asset=asset,
                                                                 product=product, timeframe=timeframe))
        else:
            result = await asyncio.to_thread(adapters.futures_candles, self.client, pair=asset,
                                             resolution=api_resolution, start_ms=start, end_ms=now_ms)
            outcome = candle_norm.normalize_futures(result.raw, _ctx(result, endpoint="futures_candles",
                                                                    asset=asset, product=product,
                                                                    timeframe=timeframe))
        source = f"{product.value}_candles_{timeframe.value}"

        def write(bars: list) -> int:
            res = self.store.upsert_bars(bars)
            for m in res.mutations:
                # specs/11 §6.2: log at ERROR, record the event, and flag affected signals.
                log.error("bar_mutation", **m.to_dict(),
                          detail="a bar already marked final came back different; signals referencing "
                                 "that as_of will no longer replay")
                self.store.record_dq_event(DQEvent(
                    kind="BAR_MUTATION", component="ingest.pipeline", operation=source,
                    at_ms=result.received_at_ms, problem_class="external_dependency", severity="error",
                    asset=asset, detail=m.to_dict()))
            return res.written + res.finalized

        n = self._handle(outcome, source=source, result=result, write=write)
        if outcome.records:
            newest = outcome.records[-1]
            self.dq.source(source).observe(source_ts_ms=newest.close_time_ms,
                                           sent_at_ms=result.sent_at_ms,
                                           received_at_ms=result.received_at_ms)
        return n

    async def poll_settle(self, asset: str, product: Product, timeframe: Timeframe,
                          api_resolution: str) -> int:
        """Re-fetch the oldest **non-final** bars so the two-fetch finalization rule can settle them.

        Without this, any history pulled in a single sweep — a catch-up after an outage, an import, a
        first run — stays non-final forever. It is present and correct in the database and
        **completely invisible** to the feature layer, because ``get_bars(final_only=True)`` is what
        the snapshot builder uses. The symptom is a series that reports ``WARMING`` indefinitely while
        the row count says the data is there, which is how 332 of 1,440 1h bars sat unusable here
        (F-036).

        Deliberately narrow and bounded: one contiguous window per pass, oldest first, capped at
        :data:`SETTLE_BATCH_BARS`. Bounded because an unbounded repair would starve the live polls it
        shares a request budget with; oldest-first because the oldest hole is the one blocking the
        longest warm-up.

        A bar that comes back **different** is not quietly accepted — ``upsert_bars`` raises it as a
        mutation, which is handled identically to the live path. A bar that never comes back is left
        non-final rather than being marked final: an unconfirmable bar is exactly the thing the rule
        exists to exclude.
        """
        settle_after = SETTLE_AFTER_BARS * timeframe.ms
        now_ms = int(time.time() * 1000)
        pending = self.store.non_final_bar_times(
            asset, product, timeframe, settled_before_ms=now_ms - settle_after,
            limit=SETTLE_BATCH_BARS)
        if not pending:
            return 0

        # One contiguous window: from the oldest pending bar, spanning at most the batch size. The
        # venue serves a range, so asking for the range is one request rather than one per bar.
        start = pending[0]
        end = min(pending[-1], start + (SETTLE_BATCH_BARS - 1) * timeframe.ms) + timeframe.ms
        if product is Product.SPOT:
            result = await asyncio.to_thread(adapters.spot_candles, self.client, pair=asset,
                                             interval=api_resolution, start_ms=start, end_ms=end,
                                             limit=min(1000, SETTLE_BATCH_BARS + 10))
            outcome = candle_norm.normalize_spot(
                result.raw, _ctx(result, endpoint="spot_candles", asset=asset, product=product,
                                 timeframe=timeframe))
        else:
            result = await asyncio.to_thread(adapters.futures_candles, self.client, pair=asset,
                                             resolution=api_resolution, start_ms=start, end_ms=end)
            outcome = candle_norm.normalize_futures(
                result.raw, _ctx(result, endpoint="futures_candles", asset=asset, product=product,
                                 timeframe=timeframe))
        source = f"{product.value}_candles_{timeframe.value}"

        def write(bars: list) -> int:
            res = self.store.upsert_bars(bars)
            for m in res.mutations:
                log.error("bar_mutation", **m.to_dict(),
                          detail="a bar already marked final came back different during a "
                                 "finalization sweep; signals referencing that as_of will no longer "
                                 "replay")
                self.store.record_dq_event(DQEvent(
                    kind="BAR_MUTATION", component="ingest.pipeline", operation=f"settle:{source}",
                    at_ms=result.received_at_ms, problem_class="external_dependency",
                    severity="error", asset=asset, detail=m.to_dict()))
            return res.written + res.finalized

        settled = self._handle(outcome, source=source, result=result, write=write)
        remaining = len(self.store.non_final_bar_times(
            asset, product, timeframe, settled_before_ms=now_ms - settle_after, limit=100_000))
        log.info("bars_settled", asset=asset, product=product.value, timeframe=timeframe.value,
                 requested=len(pending), returned=len(outcome.records), remaining_non_final=remaining,
                 window=[start, end])
        return settled

    async def poll_book(self, asset: str, product: Product, depth: int) -> int:
        if product is Product.SPOT:
            result = await asyncio.to_thread(adapters.spot_book, self.client, pair=asset, depth=depth)
            outcome = book_norm.normalize_spot(result.raw, _ctx(result, endpoint="spot_book", asset=asset),
                                               max_age_ms=int(self.settings.freshness.book_max_age_s * 1000))
        else:
            result = await asyncio.to_thread(adapters.futures_book, self.client, pair=asset, depth=depth)
            outcome = book_norm.normalize_futures(result.raw, _ctx(result, endpoint="futures_book", asset=asset),
                                                  max_age_ms=int(self.settings.freshness.book_max_age_s * 1000))
        source = f"{product.value}_book"
        n = self._handle(outcome, source=source, result=result, write=self.store.upsert_books)
        for snap in outcome.records:
            self.dq.source(source).observe(source_ts_ms=snap.ts_ms, sent_at_ms=result.sent_at_ms,
                                           received_at_ms=result.received_at_ms)
        return n

    async def poll_instrument(self, asset: str) -> int:
        result = await asyncio.to_thread(adapters.instrument, self.client, pair=asset)
        outcome = instrument_norm.normalize(result.raw, _ctx(result, endpoint="instrument", asset=asset,
                                                            product=Product.FUTURES))
        for w in getattr(outcome, "warnings", ()):        # e.g. missing margin tiers -> DQ_META later
            log.warning("instrument_warning", asset=asset, detail=w)
        n = self._handle(outcome, source="instrument", result=result, write=self.store.upsert_instrument)
        self.dq.source("instrument").observe(source_ts_ms=result.received_at_ms,
                                             sent_at_ms=result.sent_at_ms,
                                             received_at_ms=result.received_at_ms)
        return n

    async def poll_positioning(self, asset: str) -> int:
        result = await asyncio.to_thread(adapters.positioning, self.client, pair=asset)
        outcome = pos_norm.normalize(result.raw, _ctx(result, endpoint="positioning", asset=asset))
        n = self._handle(outcome, source="positioning", result=result, write=self.store.upsert_positioning)
        for row in outcome.records:
            # This endpoint carries no timestamp: freshness is poll-cadence-bounded, and the DQ
            # source records the receive time so that is visible rather than implied.
            self.dq.source("positioning").observe(source_ts_ms=row.ts_ms, sent_at_ms=result.sent_at_ms,
                                                  received_at_ms=result.received_at_ms)
        return n

    # ---- task construction -----------------------------------------------------------------

    def build_tasks(self, *, candle_timeframes: Sequence[tuple[Product, Timeframe, str]] | None = None,
                    enable_books: bool = True, enable_positioning: bool = True) -> list[PollTask]:
        p = self.settings.polling
        tasks: list[PollTask] = [
            PollTask(name="futures_rt", interval_s=p.futures_rt_s, run=self.poll_futures_rt,
                     priority=Priority.P0, host="public.coindcx.com"),
            PollTask(name="spot_ticker", interval_s=p.spot_ticker_s, run=self.poll_spot_ticker,
                     priority=Priority.P0, host="api.coindcx.com"),
        ]
        combos = candle_timeframes if candle_timeframes is not None else [
            (Product.SPOT, Timeframe.M1, "1m"), (Product.FUTURES, Timeframe.M1, "1"),
            (Product.SPOT, Timeframe.M15, "15m"), (Product.FUTURES, Timeframe.M15, "15"),
            (Product.SPOT, Timeframe.H1, "1h"), (Product.FUTURES, Timeframe.H1, "60"),
            (Product.FUTURES, Timeframe.H4, "240"),
            (Product.SPOT, Timeframe.D1, "1d"), (Product.FUTURES, Timeframe.D1, "1D"),
        ]
        for asset in self.assets:
            for product, tf, res in combos:
                interval = p.candles_1m_s if tf is Timeframe.M1 else max(60.0, tf.ms / 1000 / 4)
                tasks.append(PollTask(
                    name=f"candles:{product.value}:{asset}:{tf.value}",
                    interval_s=interval,
                    run=partial(self.poll_candles, asset, product, tf, res),
                    priority=Priority.P0 if tf is Timeframe.M1 else Priority.P1,
                    host="api.coindcx.com" if product is Product.SPOT else "public.coindcx.com"))
            # One finalization sweep per series, at P2. Below the live polls deliberately: settling
            # old history matters, and it never matters more than knowing the current price. The
            # cadence is brisk while there is a backlog because each pass settles at most
            # SETTLE_BATCH_BARS, and a no-op pass costs nothing — poll_settle returns without a
            # request when there is nothing non-final.
            for product, tf, res in combos:
                tasks.append(PollTask(
                    name=f"settle:{product.value}:{asset}:{tf.value}",
                    interval_s=max(30.0, tf.ms / 1000 / 8),
                    run=partial(self.poll_settle, asset, product, tf, res),
                    priority=Priority.P2,
                    host="api.coindcx.com" if product is Product.SPOT else "public.coindcx.com"))
            tasks.append(PollTask(name=f"instrument:{asset}", interval_s=p.instrument_meta_s,
                                  run=partial(self.poll_instrument, asset), priority=Priority.P2,
                                  host="api.coindcx.com"))
            if enable_positioning:
                tasks.append(PollTask(name=f"positioning:{asset}", interval_s=p.positioning_s,
                                      run=partial(self.poll_positioning, asset), priority=Priority.P2,
                                      host="api.coindcx.com"))
            if enable_books:
                for product, depth in ((Product.SPOT, 50), (Product.FUTURES, 50)):
                    tasks.append(PollTask(
                        name=f"book:{product.value}:{asset}", interval_s=p.orderbook_s,
                        run=partial(self.poll_book, asset, product, depth),
                        priority=Priority.P3,        # dropped first under budget pressure
                        host="api.coindcx.com" if product is Product.SPOT else "public.coindcx.com"))
        return tasks
