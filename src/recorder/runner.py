"""Scheduler loop: per-task schedule and error state, gaps, health, quality rollups.

Single-threaded by design. Scheduling uses the wall clock so a laptop that slept for
hours simply finds every task overdue on wake, runs each once, and records the gap.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from cdx_http.backoff import Kind, backoff_delay, parse_retry_after
from cdx_http.client import HttpClient, HttpResult, TransportError, now_ms

from .storage import JsonlStore, iter_records, utc_day
from .tasks import CandleTask, Task

log = logging.getLogger("recorder")

HOST_SLOWDOWN_S = 600       # after a 429: halve the poll rate for that host for 10 min
ERROR_BODY_MAX = 64 * 1024


class PollError(Exception):
    def __init__(self, result: HttpResult):
        super().__init__(f"HTTP {result.status} from {result.url}")
        self.result = result


class MalformedPayload(Exception):
    def __init__(self, result: HttpResult, reason: str):
        super().__init__(f"malformed payload from {result.url}: {reason}")
        self.result = result
        self.reason = reason


@dataclass
class TaskState:
    next_due_ms: int = 0
    last_success_ms: int | None = None
    fail_streak: int = 0
    succeeded_since_start: bool = False
    stopped: bool = False
    last_kind: Kind | None = None


@dataclass
class StreamStats:
    """Counters for one stream over one UTC hour (quality) or one health window."""
    attempts: int = 0
    successes: int = 0
    failures: dict = field(default_factory=lambda: defaultdict(int))
    records: int = 0
    dedup_skipped: int = 0
    gaps: int = 0
    latency_ms_sum: int = 0
    latency_ms_max: int = 0
    requests: int = 0
    skew_ms: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "attempts": self.attempts, "successes": self.successes, "failures": dict(self.failures),
            "records": self.records, "dedup_skipped": self.dedup_skipped, "gaps": self.gaps,
            "requests": self.requests,
            "latency_ms_mean": round(self.latency_ms_sum / self.requests, 1) if self.requests else None,
            "latency_ms_max": self.latency_ms_max if self.requests else None,
        }
        if self.skew_ms:
            d["skew_ms_mean"] = round(sum(self.skew_ms) / len(self.skew_ms), 1)
            d["skew_ms_min"] = min(self.skew_ms)
            d["skew_ms_max"] = max(self.skew_ms)
        d.update(self.extra)
        return d


class TaskContext:
    """What a task may do: fetch, write records, report errors and quality counters."""

    def __init__(self, rec: Recorder, task: Task):
        self._rec = rec
        self._task = task

    def now_ms(self) -> int:
        return self._rec.clock_ms()

    def get(self, url, params=None, method="GET", headers=None, body=None) -> tuple[HttpResult, str, object]:
        """Fetch and require 2xx + valid JSON. Returns (result, decoded text, parsed JSON)."""
        res = self._rec.http.request(method, url, params=params, headers=headers, body=body)
        st = self._rec._stats(self._task.stream)
        st.requests += 1
        st.latency_ms_sum += res.latency_ms
        st.latency_ms_max = max(st.latency_ms_max, res.latency_ms)
        log.debug("poll %s status=%d latency=%dms bytes=%d", self._task.key, res.status, res.latency_ms, len(res.body))
        if not res.ok:
            raise PollError(res)
        try:
            text = res.body.decode("utf-8")
            obj = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise MalformedPayload(res, f"{type(e).__name__}: {e}") from None
        return res, text, obj

    def write(self, stream: str, pair: str | None, res: HttpResult, source_ts_ms: int | None, payload_raw: str) -> None:
        self._rec._write_record(stream, pair, res, source_ts_ms, payload_raw)

    def error(self, kind: str, res: HttpResult | None, detail: str = "", body: bytes | None = None) -> None:
        self._rec._log_error(self._task, kind, res, detail, body)

    def observe(self, name: str, value: int, mode: str = "sum") -> None:
        for st in (self._rec._stats(self._task.stream), self._rec._health_stats(self._task.stream)):
            if mode == "max":
                st.extra[name] = max(st.extra.get(name, value), value)
            elif mode == "min":
                st.extra[name] = min(st.extra.get(name, value), value)
            else:
                st.extra[name] = st.extra.get(name, 0) + value

    def meta_gap_event(self, event: str, task: Task, start_ms: int, floor_ms: int) -> None:
        self._rec.store.append_meta("gaps", {
            "event": event, "stream": task.stream, "pair": task.pair,
            "gap_start_ms": start_ms, "gap_end_ms": floor_ms, "duration_s": (floor_ms - start_ms) / 1000,
            "recoverable": False, "note": "older than the gap-fill window; re-run the one-time backfill",
        })
        log.warning("%s: candle gap %.1fh exceeds gap-fill window - run the backfill script",
                    task.key, (floor_ms - start_ms) / 3_600_000)


class Recorder:
    def __init__(
        self,
        tasks: list[Task],
        http: HttpClient,
        store: JsonlStore,
        health_interval_s: float = 900,
        secrets: tuple[str, ...] = (),
        clock_ms: Callable[[], int] = now_ms,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.tasks = tasks
        self.http = http
        self.store = store
        self.health_interval_ms = int(health_interval_s * 1000)
        self._secrets = tuple(s for s in secrets if s)
        self.clock_ms = clock_ms
        self._sleep = sleep
        self.state: dict[str, TaskState] = {t.key: TaskState() for t in tasks}
        self._last_key: dict[tuple[str, str | None], int] = {}
        self._host_slow_until: dict[str, int] = {}
        self._quality: dict[str, StreamStats] = defaultdict(StreamStats)
        self._health: dict[str, StreamStats] = defaultdict(StreamStats)
        self._quality_hour: int | None = None
        self._day: str | None = None
        self._next_health_ms = 0
        self.prior_shutdown_clean: bool | None = None
        self._stopping = False

    # ---- startup / shutdown --------------------------------------------------------

    def startup(self) -> None:
        now = self.clock_ms()
        self._compress(utc_day(now))
        self.prior_shutdown_clean = self._read_prior_shutdown()
        stale = self.store.root / "_meta" / "STOP"
        if stale.exists():                     # a stop request for a previous run, not this one
            stale.unlink()
            log.info("removed stale stop file from a previous run")
        self.store.append_meta("gaps", {"event": "startup", "at_ms": now, "prior_shutdown_clean": self.prior_shutdown_clean})
        for path in self.store.repaired:
            self._log_error_raw({"kind": "torn_line_repaired", "file": str(path)})
        self.store.repaired.clear()

        by_stream: dict[str, list[Task]] = defaultdict(list)
        for t in self.tasks:
            by_stream[t.stream].append(t)
        for stream, tasks in by_stream.items():
            candle_tasks = [t for t in tasks if isinstance(t, CandleTask)]
            # The two newest day files (not "today and yesterday": after a long outage the
            # newest file may be days old) give last-success. Candle coverage may need to
            # look further back, bounded by the gap-fill window.
            limit = 2
            if candle_tasks:
                limit = max(limit, int(max(t.max_gap_fill_days for t in candle_tasks)) + 2)
            last_by_pair: dict[str | None, int] = {}
            pending = {t.key for t in candle_tasks}
            for i, path in enumerate(self.store.day_files(stream)[:limit]):
                recs = list(iter_records(path))
                for r in recs:
                    ts = r.get("received_at_ms")
                    if isinstance(ts, int):
                        for key in (r.get("pair"), None):
                            if ts > last_by_pair.get(key, -1):
                                last_by_pair[key] = ts
                for t in candle_tasks:
                    if t.key in pending:
                        t.load_coverage(recs)
                        if t.coverage_ms is not None:
                            pending.discard(t.key)
                if i >= 1 and not pending:
                    break
            for t in tasks:
                last = last_by_pair.get(t.pair) if t.pair else last_by_pair.get(None)
                st = self.state[t.key]
                st.last_success_ms = last
                st.next_due_ms = now
                if last is not None:
                    log.info("%s: last record %.1f min ago", t.key, (now - last) / 60000)
        log.info("recorder started: %d tasks across %d streams (prior shutdown clean: %s)",
                 len(self.tasks), len(by_stream), self.prior_shutdown_clean)

    def _compress(self, today: str) -> None:
        """Gzip completed day files. A failure is logged and retried at the next day change
        or restart; it must never stop recording."""
        self._day = today
        try:
            for info in self.store.compress_completed(today):
                log.info("compressed %s: %s bytes -> %s", info["file"], info.get("raw_bytes"), info.get("gz_bytes", "-"))
        except Exception as e:
            log.exception("compression of completed day files failed")
            self._log_error_raw({"kind": "compression_failed", "error": f"{type(e).__name__}: {e}"})

    def _read_prior_shutdown(self) -> bool | None:
        path = self.store.meta_path("gaps")
        if not path.exists():
            return None
        last_event = None
        for r in iter_records(path):
            if r.get("event") in ("startup", "shutdown"):
                last_event = r["event"]
        return None if last_event is None else last_event == "shutdown"

    def shutdown(self) -> None:
        now = self.clock_ms()
        try:
            self._flush_quality(partial=True)
            self.store.append_meta("gaps", {"event": "shutdown", "at_ms": now, "reason": "clean"})
        finally:
            self.store.close()
        log.info("recorder stopped cleanly")

    def stop(self) -> None:
        self._stopping = True

    def _stop_requested(self) -> bool:
        """Graceful stop for a detached process (no console, so no Ctrl-C): the file
        ``<data_dir>/_meta/STOP`` is consumed and triggers a clean shutdown."""
        f = self.store.root / "_meta" / "STOP"
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass
            log.info("stop file found - shutting down")
            self._stopping = True
            return True
        return False

    # ---- main loop -------------------------------------------------------------------

    def run_forever(self) -> None:
        self.startup()
        try:
            while not self._stopping:
                self.tick()
                if self._stop_requested():
                    break
                wait = (min((s.next_due_ms for s in self.state.values() if not s.stopped), default=self.clock_ms() + 60_000)
                        - self.clock_ms()) / 1000
                self._sleep(max(0.05, min(wait, 1.0)))
        finally:
            self.shutdown()

    def tick(self) -> None:
        now = self.clock_ms()
        self._roll_quality(now)
        if utc_day(now) != self._day:
            self._compress(utc_day(now))
        due = sorted((t for t in self.tasks if not self.state[t.key].stopped and self.state[t.key].next_due_ms <= now),
                     key=lambda t: self.state[t.key].next_due_ms)
        for t in due:
            if self._stopping:
                break
            self._run_task(t)
        if self.clock_ms() >= self._next_health_ms:
            if self._next_health_ms:
                self._health_summary()
            self._next_health_ms = self.clock_ms() + self.health_interval_ms

    def _effective_interval_ms(self, t: Task, now: int) -> int:
        ms = int(t.interval_s * 1000)
        return ms * 2 if self._host_slow_until.get(t.host, 0) > now else ms

    def _run_task(self, t: Task) -> None:
        st = self.state[t.key]
        started = self.clock_ms()
        for s in (self._stats(t.stream), self._health_stats(t.stream)):
            s.attempts += 1
        kind: Kind | None = None
        retry_after = None
        try:
            t.run(TaskContext(self, t))
        except PollError as e:
            kind = e.result.kind
            retry_after = parse_retry_after(e.result.headers)
            self._log_error(t, kind.value, e.result, "", e.result.body)
        except MalformedPayload as e:
            kind = Kind.MALFORMED
            self._log_error(t, "malformed_json", e.result, e.reason, e.result.body, keep_raw=True)
        except TransportError as e:
            kind = Kind.TRANSPORT
            self._log_error_raw({"kind": "transport", "stream": t.stream, "pair": t.pair, "url": e.url,
                                 "sent_at_ms": e.sent_at_ms, "failed_at_ms": e.failed_at_ms, "error": e.cause_name})
            log.debug("transport error on %s: %s", t.key, e.cause_name)
        except Exception as e:  # never let one stream's bug kill the loop
            kind = Kind.INTERNAL
            log.exception("internal error in task %s", t.key)
            self._log_error_raw({"kind": "internal", "stream": t.stream, "pair": t.pair,
                                 "error": self._scrub(f"{type(e).__name__}: {e}")})

        now = self.clock_ms()
        if kind is None:
            self._on_success(t, st, now)
            st.next_due_ms = started + self._effective_interval_ms(t, now)
            return

        st.fail_streak += 1
        st.last_kind = kind
        for s in (self._stats(t.stream), self._health_stats(t.stream)):
            s.failures[kind.value] += 1
        if kind is Kind.UNAUTHORIZED:
            st.stopped = True
            log.critical("401 UNAUTHORIZED on %s - stream STOPPED. Check the API key in .env; restart to retry.", t.key)
            return
        if kind is Kind.RATE_LIMITED:
            self._host_slow_until[t.host] = now + HOST_SLOWDOWN_S * 1000
        delay = backoff_delay(kind, st.fail_streak, retry_after)
        if kind is Kind.MAINTENANCE:
            log.warning("503 MAINTENANCE (documented exchange maintenance) on %s - backing off %.0fs", t.key, delay)
        else:
            log.info("%s failed (%s, streak %d) - retry in %.0fs", t.key, kind.value, st.fail_streak, delay)
        st.next_due_ms = now + int(delay * 1000)

    def _on_success(self, t: Task, st: TaskState, now: int) -> None:
        for s in (self._stats(t.stream), self._health_stats(t.stream)):
            s.successes += 1
        prev = st.last_success_ms
        if prev is not None and now - prev > 2 * t.interval_s * 1000:
            if not st.succeeded_since_start:
                reason = "startup"
            elif st.fail_streak > 0:
                reason = "error_streak"
            else:
                reason = "unknown"   # typically laptop sleep
            self.store.append_meta("gaps", {
                "stream": t.stream, "pair": t.pair, "gap_start_ms": prev, "gap_end_ms": now,
                "duration_s": (now - prev) / 1000, "reason": reason, "recoverable": t.recoverable,
            })
            for s in (self._stats(t.stream), self._health_stats(t.stream)):
                s.gaps += 1
            log.info("GAP %s: %.1f min (%s)%s", t.key, (now - prev) / 60000, reason,
                     " - candle gap-fill will cover it" if t.recoverable else " - unrecoverable, left visible")
            if not t.recoverable:
                for c in self.tasks:
                    if isinstance(c, CandleTask) and not self.state[c.key].stopped:
                        self.state[c.key].next_due_ms = min(self.state[c.key].next_due_ms, now)
        if st.fail_streak:
            log.info("%s recovered after %d failures", t.key, st.fail_streak)
        st.last_success_ms = now
        st.fail_streak = 0
        st.last_kind = None
        st.succeeded_since_start = True

    # ---- writing -----------------------------------------------------------------------

    def _write_record(self, stream, pair, res: HttpResult, source_ts_ms, payload_raw) -> None:
        from .storage import envelope_line

        if source_ts_ms is not None:
            k = (stream, pair)
            if self._last_key.get(k) == source_ts_ms:
                self._stats(stream).dedup_skipped += 1
                return
            self._last_key[k] = source_ts_ms
            mid = (res.sent_at_ms + res.received_at_ms) // 2
            self._stats(stream).skew_ms.append(source_ts_ms - mid)
        line = envelope_line(stream, pair, res.sent_at_ms, res.received_at_ms, res.status, source_ts_ms, res.url, payload_raw)
        self.store.append(stream, res.received_at_ms, line)
        for s in (self._stats(stream), self._health_stats(stream)):
            s.records += 1

    def _scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "<redacted>")
        return text

    def _log_error(self, t: Task, kind: str, res: HttpResult | None, detail: str, body: bytes | None, keep_raw=False):
        rec = {"kind": kind, "stream": t.stream, "pair": t.pair}
        if res is not None:
            rec.update({"url": res.url, "http_status": res.status, "sent_at_ms": res.sent_at_ms,
                        "received_at_ms": res.received_at_ms})
        if detail:
            rec["detail"] = detail
        if body is not None:
            rec["body"] = self._scrub(body[:ERROR_BODY_MAX].decode("utf-8", errors="replace"))
            rec["body_truncated"] = len(body) > ERROR_BODY_MAX
            if keep_raw:
                raw = body
                for s in self._secrets:
                    raw = raw.replace(s.encode(), b"<redacted>")
                rec["body_b64"] = base64.b64encode(raw).decode("ascii")
        self._log_error_raw(rec)
        if kind not in ("maintenance",):
            log.info("error %s on %s: %s", kind, t.key, (res.status if res else "") or detail)

    def _log_error_raw(self, rec: dict) -> None:
        rec = {"at_ms": self.clock_ms(), **rec}
        self.store.append_meta("errors", rec)

    # ---- stats -------------------------------------------------------------------------

    def _stats(self, stream: str) -> StreamStats:
        return self._quality[stream]

    def _health_stats(self, stream: str) -> StreamStats:
        return self._health[stream]

    def _roll_quality(self, now: int) -> None:
        hour = now // 3_600_000
        if self._quality_hour is None:
            self._quality_hour = hour
        elif hour != self._quality_hour:
            self._flush_quality(partial=False)
            self._quality_hour = hour

    def _flush_quality(self, partial: bool) -> None:
        if self._quality_hour is None:
            return
        hour_start = self._quality_hour * 3_600_000
        for stream, st in sorted(self._quality.items()):
            self.store.append_meta("quality", {"hour_start_ms": hour_start, "stream": stream, "partial": partial,
                                               **st.to_dict()})
        self._quality.clear()

    def _health_summary(self) -> None:
        now = self.clock_ms()
        ages: dict[str, float] = {}
        stopped: set[str] = set()
        for t in self.tasks:
            st = self.state[t.key]
            if st.stopped:
                stopped.add(t.stream)
            age = (now - st.last_success_ms) / 1000 if st.last_success_ms else float("inf")
            ages[t.stream] = max(ages.get(t.stream, 0), age)
        parts = []
        for stream in sorted(ages):
            h = self._health.get(stream, StreamStats())
            errs = sum(h.failures.values())
            age = ages[stream]
            age_s = "never" if age == float("inf") else f"{age:.0f}s"
            parts.append(f"{stream} rec={h.records} err={errs} age={age_s}{' STOPPED' if stream in stopped else ''}")
        if not any(t.stream == "positioning" for t in self.tasks):
            parts.append("positioning NOT RECORDING")
        log.info("health | %s", " | ".join(parts))
        self._health.clear()
