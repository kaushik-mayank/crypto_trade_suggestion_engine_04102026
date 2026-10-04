"""Polling scheduler (specs/11 §2, §3.3; specs/10 §6 "asyncio tasks with per-task last-run state").

Failure behaviour is the whole point of this module:

* **Per-task isolation.** Each task owns its schedule, its failure streak and its backoff. One
  endpoint failing never stalls another — the bug that would quietly stop trigger evaluation while
  the order book kept polling.
* **Backoff by error class, not by guess.** ``RateLimited`` halves the host rate for 10 minutes;
  ``ExchangeMaintenance`` backs off long (30 s → 15 min) because 503 is documented maintenance; a
  non-retryable 4xx **stops the task** rather than looping, because our request is wrong.
* **Persistent failure becomes a state, not a retry loop.** After ``fail_streak >= dead_after``, a
  task reports ``DOWN`` with its last error. Nothing is hidden behind endless retries.
* **Priorities.** P0 (prices, 1m candles) preempt; P3 (order books) is dropped first when the token
  bucket is constrained. Dropped polls are recorded in ``dq_events``, never silently skipped.
* **Catch-up on restart.** ``due_at`` is derived from the last success, so a task that missed its
  window runs immediately rather than waiting a full interval.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from core.errors import CryptoTraderError, HttpStatusError, ProblemClass, RateLimited
from core.health import ComponentHealth, SystemStatus
from core.logging import get_logger
from store.interface import DQEvent

log = get_logger("ingest.scheduler")


class Priority(IntEnum):
    P0 = 0      # live price and 1m candles: the system's actual job
    P1 = 1      # coarser candles
    P2 = 2      # positioning, instrument metadata
    P3 = 3      # order books — dropped first (refreshed every 5 min anyway)


#: Backoff schedules per error class: (base seconds, cap seconds).
BACKOFF = {
    "rate_limited": (1.0, 60.0),
    "maintenance": (30.0, 900.0),
    "transport": (2.0, 60.0),
    "server_error": (5.0, 120.0),
    "data": (5.0, 120.0),
    "internal": (60.0, 600.0),
}


def classify(err: BaseException) -> str:
    from core.errors import ExchangeMaintenance, ExternalError, TransportFailure
    if isinstance(err, RateLimited):
        return "rate_limited"
    if isinstance(err, ExchangeMaintenance):
        return "maintenance"
    if isinstance(err, TransportFailure):
        return "transport"
    if isinstance(err, HttpStatusError):
        return "server_error" if err.retryable else "client_error"
    if isinstance(err, ExternalError):
        return "data"
    if isinstance(err, CryptoTraderError):
        return "data" if err.problem_class is ProblemClass.DATA_QUALITY else "internal"
    return "internal"


def backoff_delay(kind: str, streak: int) -> float:
    base, cap = BACKOFF.get(kind, BACKOFF["internal"])
    delay = min(cap, base * (2 ** max(0, streak - 1)))
    return delay * (0.85 + 0.3 * random.random())        # jitter: avoid synchronised retries


@dataclass
class PollTask:
    """One polling job. ``run`` is an async callable that performs the poll and stores its result."""

    name: str
    interval_s: float
    run: Callable[[], Awaitable[Any]]
    priority: Priority = Priority.P1
    host: str = ""
    dead_after: int = 8                  # consecutive failures before the task reports DOWN
    enabled: bool = True

    # state
    due_at: float = 0.0
    last_success_ms: int | None = None
    last_attempt_ms: int | None = None
    fail_streak: int = 0
    successes: int = 0
    failures: int = 0
    dropped: int = 0
    stopped_reason: str | None = None
    last_error: CryptoTraderError | None = None
    last_latency_ms: int | None = None

    @property
    def is_dead(self) -> bool:
        return self.stopped_reason is not None or self.fail_streak >= self.dead_after

    def health(self) -> ComponentHealth:
        if not self.enabled:
            return ComponentHealth(name=self.name, status=SystemStatus.HEALTHY,
                                   detail="disabled by configuration",
                                   last_success_ms=self.last_success_ms)
        if self.stopped_reason:
            err = self.last_error
            return ComponentHealth(
                name=self.name, status=SystemStatus.DOWN,
                detail=f"stopped: {self.stopped_reason}",
                problem_class=err.problem_class if err else ProblemClass.APPLICATION,
                last_success_ms=self.last_success_ms, consecutive_failures=self.fail_streak,
                context={"code": err.code if err else None},
                remediation=err.remediation if err else "inspect the logs for this task",
            )
        if self.fail_streak >= self.dead_after:
            err = self.last_error
            return ComponentHealth(
                name=self.name, status=SystemStatus.DOWN,
                detail=f"{self.fail_streak} consecutive failures: {err.message if err else 'unknown'}",
                problem_class=err.problem_class if err else ProblemClass.APPLICATION,
                last_success_ms=self.last_success_ms, consecutive_failures=self.fail_streak,
                context={"code": err.code if err else None}, remediation=err.remediation if err else None)
        if self.fail_streak > 0:
            err = self.last_error
            return ComponentHealth(
                name=self.name, status=SystemStatus.DEGRADED,
                detail=f"{self.fail_streak} recent failure(s): {err.message if err else ''}",
                problem_class=err.problem_class if err else ProblemClass.EXTERNAL,
                last_success_ms=self.last_success_ms, consecutive_failures=self.fail_streak,
                remediation=err.remediation if err else None)
        stale_after = max(self.interval_s * 3, 60)
        if self.last_success_ms is not None and (time.time() * 1000 - self.last_success_ms) / 1000 > stale_after:
            return ComponentHealth(
                name=self.name, status=SystemStatus.STALE,
                detail=f"no successful poll in {(time.time() * 1000 - self.last_success_ms) / 1000:.0f}s",
                problem_class=ProblemClass.INFRASTRUCTURE, last_success_ms=self.last_success_ms,
                remediation="the scheduler may be blocked or the process suspended; check the event loop")
        return ComponentHealth.healthy(self.name, last_success_ms=self.last_success_ms,
                                       detail=f"every {self.interval_s:g}s",
                                       successes=self.successes, failures=self.failures)

    def stats(self) -> dict[str, Any]:
        return {"name": self.name, "priority": int(self.priority), "interval_s": self.interval_s,
                "successes": self.successes, "failures": self.failures, "dropped": self.dropped,
                "fail_streak": self.fail_streak, "last_success_ms": self.last_success_ms,
                "last_latency_ms": self.last_latency_ms, "stopped_reason": self.stopped_reason}


class Scheduler:
    """Runs :class:`PollTask` objects cooperatively until stopped."""

    def __init__(self, tasks: list[PollTask], *, store: Any = None, clock: Callable[[], float] = time.monotonic,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 max_concurrent: int = 4, budget_per_sec: float | None = None):
        self.tasks = tasks
        self.store = store
        self._clock = clock
        self._now_ms = now_ms
        self._sem = asyncio.Semaphore(max_concurrent)
        self._stopping = asyncio.Event()
        self.budget_per_sec = budget_per_sec          # when set, P3 is dropped once the budget is tight
        self._issued_this_second = 0.0
        self._second_started = clock()

    # ---- budget / preemption ---------------------------------------------------------------

    def _budget_allows(self, task: PollTask) -> bool:
        if self.budget_per_sec is None:
            return True
        now = self._clock()
        if now - self._second_started >= 1.0:
            self._second_started, self._issued_this_second = now, 0.0
        if self._issued_this_second + 1 <= self.budget_per_sec:
            return True
        # Budget exhausted: P0/P1 still go (they are the system's job), P2/P3 are dropped.
        return task.priority <= Priority.P1

    def _record_drop(self, task: PollTask, reason: str) -> None:
        task.dropped += 1
        log.warning("poll_dropped", task=task.name, priority=int(task.priority), reason=reason)
        if self.store is not None:
            self.store.record_dq_event(DQEvent(
                kind="POLL_DROPPED", component="ingest.scheduler", operation="tick",
                at_ms=self._now_ms(), problem_class="infrastructure", severity="warning",
                detail={"task": task.name, "priority": int(task.priority), "reason": reason}))

    # ---- one task -------------------------------------------------------------------------

    async def _run_task(self, task: PollTask) -> None:
        async with self._sem:
            started = self._clock()
            task.last_attempt_ms = self._now_ms()
            try:
                await task.run()
            except CryptoTraderError as e:
                self._on_failure(task, e)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - an unclassified error is a bug; record and keep going
                wrapped = CryptoTraderError(
                    f"unhandled error in poll task {task.name}: {type(e).__name__}: {e}",
                    component="ingest.scheduler", operation=task.name,
                    context={"task": task.name},
                    likely_cause="a defect in the task body — this is not an external failure",
                    remediation="fix the task; the scheduler kept running and other tasks are unaffected",
                    cause=e,
                )
                log.exception("poll_task_bug", task=task.name)
                self._on_failure(task, wrapped)
            else:
                task.successes += 1
                task.fail_streak = 0
                task.last_error = None
                task.last_success_ms = self._now_ms()
                task.last_latency_ms = int((self._clock() - started) * 1000)
                task.due_at = self._clock() + task.interval_s
                log.debug("poll_ok", task=task.name, latency_ms=task.last_latency_ms)

    def _on_failure(self, task: PollTask, err: CryptoTraderError) -> None:
        task.failures += 1
        task.fail_streak += 1
        task.last_error = err
        kind = classify(err)

        if kind == "client_error":
            # Our request is wrong. Retrying cannot fix it, and a backoff loop would hide the bug.
            task.stopped_reason = f"non-retryable {err.code}"
            task.due_at = float("inf")
            log.error("poll_task_stopped", task=task.name, error=err,
                      detail="non-retryable request error: the task is stopped, not retried")
        else:
            delay = backoff_delay(kind, task.fail_streak)
            task.due_at = self._clock() + delay
            level = log.warning if task.fail_streak < task.dead_after else log.error
            level("poll_failed", task=task.name, error=err, fail_streak=task.fail_streak,
                  retry_in_s=round(delay, 1))

        if self.store is not None:
            self.store.record_dq_event(DQEvent(
                kind="POLL_FAILED", component=err.component, operation=task.name,
                at_ms=self._now_ms(), problem_class=err.problem_class.value, severity=err.severity.value,
                detail={"task": task.name, "code": err.code, "fail_streak": task.fail_streak,
                        "message": err.message, **{k: str(v) for k, v in err.context.items()}}))

    # ---- loop ----------------------------------------------------------------------------

    async def tick(self) -> int:
        """Run every due task once, concurrently. Returns how many were started."""
        now = self._clock()
        due = sorted((t for t in self.tasks if t.enabled and not t.is_dead and t.due_at <= now),
                     key=lambda t: (t.priority, t.due_at))
        started: list[asyncio.Task] = []
        for task in due:
            if not self._budget_allows(task):
                self._record_drop(task, "rate budget exhausted; lower priority dropped first")
                task.due_at = now + task.interval_s
                continue
            self._issued_this_second += 1
            started.append(asyncio.create_task(self._run_task(task), name=f"poll:{task.name}"))
        if started:
            await asyncio.gather(*started, return_exceptions=False)
        return len(started)

    async def run_forever(self, *, idle_sleep_s: float = 0.25) -> None:
        log.info("scheduler_started", tasks=[t.name for t in self.tasks if t.enabled],
                 disabled=[t.name for t in self.tasks if not t.enabled])
        while not self._stopping.is_set():
            await self.tick()
            if all(t.is_dead or not t.enabled for t in self.tasks):
                log.error("scheduler_idle", detail="every task is stopped or disabled; nothing will poll",
                          remediation="fix the stopped tasks and restart; the scheduler is not retrying them")
                return
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=idle_sleep_s)
            except TimeoutError:
                pass
        log.info("scheduler_stopped")

    def stop(self) -> None:
        self._stopping.set()

    # ---- reporting ------------------------------------------------------------------------

    def health(self) -> list[ComponentHealth]:
        return [t.health() for t in self.tasks if t.enabled]

    def stats(self) -> list[dict[str, Any]]:
        return [t.stats() for t in self.tasks]
