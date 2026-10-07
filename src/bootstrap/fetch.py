"""Execute a fetch job: chunked requests → the existing normalizer → SQLite.

**Nothing here parses a payload or computes a price.** The request goes through
:mod:`ingest.adapters` (already rate-limited, already correct about the futures endpoint taking
*seconds* while returning *milliseconds* — hazard #1), the body goes through
:func:`normalize.candles.normalize_futures` (hazard-aware, verified in Phase 1), and the bars go to
``store.upsert_bars``. This module only decides *which windows to ask for and in what order*.

That split is deliberate. Every correctness risk in historical fetching — unit confusion, ordering,
duplicate bar times, OHLC validity — already has a tested implementation, and a second one written
for the bootstrap would be a second place for those bugs to live. What is genuinely new here is
orchestration, which is what this file contains.

**Finalization, and why the two-fetch rule is applied differently to settled history.**

A bar becomes ``is_final`` only when a second fetch returns it unchanged (specs/11 §6.2), and
``get_bars(final_only=True)`` — what the snapshot builder uses — cannot see a non-final bar. A naive
bootstrap therefore writes four years of correct history that is **completely invisible**, which is
exactly the failure recorded as F-036.

The rule exists to catch a bar that is still forming or being revised. A bar that closed six months
ago is neither, so requiring a second observation of it is ceremony rather than safety. So:

* bars whose close is older than :data:`SETTLE_AFTER_BARS` intervals are written **final on first
  fetch**, and the run record says how many and why;
* the recent tail is written non-final and left to the live ``settle:`` sweep, which is the right
  mechanism for bars the venue may still touch;
* :func:`verify_sample` re-requests a random sample and compares it bar for bar. That is what keeps
  the shortcut honest — it buys statistical confidence for a few percent of the requests, and any
  mismatch fails the run loudly rather than being counted as drift.
"""
from __future__ import annotations

import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from bootstrap.plan import Job, Plan
from core.errors import ALL_ERRORS, BootstrapError, ExternalError
from core.logging import get_logger
from ingest import adapters
from ingest.client import IngestClient
from normalize import candles as candle_norm
from normalize.base import PayloadContext
from normalize.canonical import Bar
from normalize.errors import Accepted, Partial

log = get_logger("bootstrap.fetch")
COMPONENT = "bootstrap.fetch"

#: Bars per request. The futures endpoint truncates silently past ~30,000 (F-009), so this stays
#: well below it — the same 10,000 the verified Phase-1 backfill uses.
CHUNK_BARS = 10_000

#: Consecutive chunks overlap by one bar, so a boundary bar is never the first or last row of a page
#: and cannot be lost to an off-by-one at the seam.
CHUNK_OVERLAP_BARS = 1

#: How far past its close a bar must be before a single fetch is treated as sufficient evidence.
#: Matches ``ingest.pipeline.SETTLE_AFTER_BARS``, so the bootstrap and the live sweep agree about
#: where "settled" begins rather than each having its own idea.
SETTLE_AFTER_BARS = 3

#: Fraction of fetched chunks re-requested and compared, to keep the finalization shortcut honest.
VERIFY_FRACTION = 0.02

#: A chunk that returns nothing is normal near the floor of a series' history. This many empty
#: chunks in a row ends the job: the series has no more history to give, and continuing would walk
#: backwards through years of emptiness one request at a time.
EMPTY_CHUNKS_BEFORE_STOP = 3


@dataclass
class JobResult:
    key: str
    requests: int = 0
    rows_received: int = 0
    rows_written: int = 0
    rows_finalized: int = 0
    written_final: int = 0
    written_pending: int = 0
    empty_chunks: int = 0
    rejected_payloads: int = 0
    mutations: int = 0
    verified_chunks: int = 0
    verify_mismatches: list[dict[str, Any]] = field(default_factory=list)
    stopped_early: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "requests": self.requests, "rows_received": self.rows_received,
            "rows_written": self.rows_written, "rows_finalized": self.rows_finalized,
            "written_final_on_first_fetch": self.written_final,
            "written_pending_for_the_live_sweep": self.written_pending,
            "empty_chunks": self.empty_chunks, "rejected_payloads": self.rejected_payloads,
            "bar_mutations": self.mutations,
            "verified_chunks": self.verified_chunks,
            "verify_mismatches": list(self.verify_mismatches),
            "stopped_early": self.stopped_early, "elapsed_ms": self.elapsed_ms,
        }


def chunks(job: Job) -> Iterator[tuple[int, int]]:
    """Windows to request, **newest first**.

    Newest first so that a bootstrap interrupted halfway leaves the *recent* history complete — which
    is the half the app needs to run at all. Oldest-first would mean an interrupted run has four years
    of 2021 data and nothing the live system can use.
    """
    span = job.timeframe.ms
    end = job.end_ms
    while end > job.start_ms:
        start = max(job.start_ms, end - (CHUNK_BARS - CHUNK_OVERLAP_BARS) * span)
        yield start, end
        if start <= job.start_ms:
            return
        end = start + CHUNK_OVERLAP_BARS * span


def _settle_before(now_ms: int, job: Job) -> int:
    return now_ms - SETTLE_AFTER_BARS * job.timeframe.ms


def _mark_settled(bars: tuple[Bar, ...], job: Job, settle_before_ms: int) -> tuple[list[Bar], int, int]:
    """Split bars into settled (written final) and recent (left for the live sweep)."""
    out: list[Bar] = []
    final_count = 0
    pending_count = 0
    for bar in bars:
        closed_at = bar.open_time_ms + job.timeframe.ms
        if closed_at <= settle_before_ms:
            out.append(bar.model_copy(update={"is_final": True}))
            final_count += 1
        else:
            out.append(bar)
            pending_count += 1
    return out, final_count, pending_count


def _request(client: IngestClient, job: Job, start_ms: int, end_ms: int) -> tuple[Any, Any]:
    result = adapters.futures_candles(client, pair=job.asset, resolution=job.api_resolution,
                                      start_ms=start_ms, end_ms=end_ms)
    ctx = PayloadContext(endpoint="futures_candles", received_at_ms=result.received_at_ms,
                         sent_at_ms=result.sent_at_ms, request_url=result.url,
                         asset=job.asset, product=job.product, timeframe=job.timeframe)
    return result, candle_norm.normalize_futures(result.raw, ctx)


def run_job(store: Any, client: IngestClient, job: Job, *, now_ms: int,
            verify_fraction: float = VERIFY_FRACTION, rng: random.Random | None = None,
            sleep_s: float = 0.0) -> JobResult:
    """Fetch one series' window, newest chunk first, writing as it goes.

    Writes per chunk rather than accumulating, so an interruption loses at most one chunk and a
    re-run picks up from the database. "Resumable" here is not a feature that had to be built — it
    falls out of the plan being recomputed from the database each time.
    """
    started = time.perf_counter()
    out = JobResult(key=job.key)
    rng = rng or random.Random(0)
    settle_before = _settle_before(now_ms, job)
    consecutive_empty = 0

    for start_ms, end_ms in chunks(job):
        try:
            result, outcome = _request(client, job, start_ms, end_ms)
        except ExternalError as e:
            # The venue failed on this chunk. Recorded and re-raised: a bootstrap that silently
            # skipped a window would leave a hole indistinguishable from one the venue does not have.
            raise BootstrapError(
                f"fetching {job.key} over [{start_ms}, {end_ms}] failed: {e.message}",
                component=COMPONENT, operation="run_job",
                context={"key": job.key, "start_ms": start_ms, "end_ms": end_ms,
                         "requests_so_far": out.requests, "rows_so_far": out.rows_written},
                likely_cause=e.likely_cause or "the venue rejected or dropped the request",
                remediation="re-run the bootstrap; it recomputes what is missing from the database, "
                            "so the work already written is kept and only the hole is retried",
                cause=e,
            ) from e
        out.requests += 1

        if not isinstance(outcome, (Accepted, Partial)):
            out.rejected_payloads += 1
            log.warning("bootstrap_chunk_rejected", key=job.key, start_ms=start_ms, end_ms=end_ms,
                        reason=getattr(getattr(outcome, "error", None), "message", "unknown"))
            continue
        if isinstance(outcome, Partial):
            out.rejected_payloads += 1

        bars = tuple(outcome.records)
        out.rows_received += len(bars)
        if not bars:
            out.empty_chunks += 1
            consecutive_empty += 1
            if consecutive_empty >= EMPTY_CHUNKS_BEFORE_STOP:
                out.stopped_early = (
                    f"{consecutive_empty} consecutive empty chunks: this series has no history "
                    f"before {end_ms}. Continuing would walk backwards through emptiness one "
                    f"request at a time")
                log.info("bootstrap_series_floor_reached", key=job.key, at_ms=end_ms,
                         detail=out.stopped_early)
                break
            continue
        consecutive_empty = 0

        prepared, final_count, pending_count = _mark_settled(bars, job, settle_before)
        res = store.upsert_bars(prepared)
        out.rows_written += res.written
        out.rows_finalized += res.finalized
        out.written_final += final_count
        out.written_pending += pending_count
        if res.mutations:
            out.mutations += len(res.mutations)
            for m in res.mutations:
                log.error("bar_mutation", **m.to_dict(),
                          detail="a bar already marked final came back different during the "
                                 "bootstrap; the stored history and the venue's disagree")

        if verify_fraction > 0 and rng.random() < verify_fraction:
            mismatch = verify_chunk(client, job, start_ms, end_ms, prepared)
            out.verified_chunks += 1
            if mismatch:
                out.verify_mismatches.append(mismatch)
        if sleep_s:
            time.sleep(sleep_s)

    out.elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info("bootstrap_job_done", **out.to_dict())
    return out


def verify_chunk(client: IngestClient, job: Job, start_ms: int, end_ms: int,
                 written: list[Bar]) -> dict[str, Any] | None:
    """Re-request a chunk and compare it bar for bar. ``None`` when it matches.

    This is what makes writing settled history final-on-first-fetch defensible rather than merely
    convenient: a sampled second observation, compared by **value** so a different decimal precision
    is not reported as a difference (F-037).
    """
    from store.sqlite_store import canonical_decimal

    try:
        _, outcome = _request(client, job, start_ms, end_ms)
    except ALL_ERRORS as e:
        return {"key": job.key, "window": [start_ms, end_ms], "kind": "verify_request_failed",
                "detail": e.message if hasattr(e, "message") else str(e)}
    if not isinstance(outcome, (Accepted, Partial)):
        return {"key": job.key, "window": [start_ms, end_ms], "kind": "verify_payload_rejected"}

    again = {b.open_time_ms: b for b in outcome.records}
    for bar in written:
        other = again.get(bar.open_time_ms)
        if other is None:
            return {"key": job.key, "window": [start_ms, end_ms], "kind": "bar_absent_on_refetch",
                    "open_time_ms": bar.open_time_ms}
        for field_name in ("open", "high", "low", "close", "volume_base"):
            a = canonical_decimal(getattr(bar, field_name))
            b = canonical_decimal(getattr(other, field_name))
            if a != b:
                return {"key": job.key, "window": [start_ms, end_ms], "kind": "value_differs",
                        "open_time_ms": bar.open_time_ms, "field": field_name,
                        "first_fetch": a, "second_fetch": b}
    return None


def run_plan(store: Any, client: IngestClient, plan_obj: Plan, *, now_ms: int,
             verify_fraction: float = VERIFY_FRACTION, seed: int = 0,
             sleep_s: float = 0.0) -> dict[str, Any]:
    """Run every outstanding job. Raises if any sampled verification found a real difference."""
    rng = random.Random(seed)
    results = [run_job(store, client, job, now_ms=now_ms, verify_fraction=verify_fraction,
                       rng=rng, sleep_s=sleep_s)
               for job in plan_obj.outstanding]
    mismatches = [m for r in results for m in r.verify_mismatches]
    summary = {
        "jobs": [r.to_dict() for r in results],
        "requests": sum(r.requests for r in results),
        "rows_written": sum(r.rows_written for r in results),
        "rows_finalized": sum(r.rows_finalized for r in results),
        "written_final_on_first_fetch": sum(r.written_final for r in results),
        "written_pending_for_the_live_sweep": sum(r.written_pending for r in results),
        "verified_chunks": sum(r.verified_chunks for r in results),
        "bar_mutations": sum(r.mutations for r in results),
        "verify_mismatches": mismatches,
        "finalization_note": (
            "bars whose close is older than "
            f"{SETTLE_AFTER_BARS} intervals were written final on their first fetch. The two-fetch "
            "rule exists to catch a bar still forming or being revised; a bar that closed long ago "
            "is neither, and requiring a second observation would leave correct history invisible "
            "to the feature layer (F-036). A sampled re-fetch checks the shortcut"),
    }
    if mismatches:
        raise BootstrapError(
            f"{len(mismatches)} sampled chunk(s) did not match on re-fetch",
            component=COMPONENT, operation="run_plan",
            context={"mismatches": mismatches[:5], "verified": summary["verified_chunks"]},
            likely_cause="the venue served different values for the same historical window, or a "
                         "normalization path is not deterministic",
            remediation="do not treat this database as a basis for a backtest. Inspect the named "
                        "windows against the raw payloads before continuing; the whole reason the "
                        "sample exists is that writing history final on one fetch is only safe if "
                        "a second fetch agrees",
        )
    return summary
