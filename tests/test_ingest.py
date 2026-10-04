"""PHASE-06 — ingest client, adapters, scheduler and data quality.

Weighted heavily toward failure behaviour: what the system does when the venue misbehaves is the
part that decides whether it goes quiet or computes on garbage.
"""
import asyncio
from decimal import Decimal

import pytest

from cdx_http.client import HttpClient
from core.errors import (
    ConfigError,
    ExchangeMaintenance,
    HttpStatusError,
    RateLimited,
    TransportFailure,
)
from core.health import SystemStatus
from ingest import adapters
from ingest.client import IngestClient
from ingest.dq import DQMonitor, completeness
from ingest.scheduler import BACKOFF, PollTask, Priority, Scheduler, backoff_delay, classify
from normalize.canonical import Product

T0 = 1789725600000


class FakeResp:
    def __init__(self, status=200, content=b"{}", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class FakeSession:
    """Stands in for requests.Session: returns queued responses or raises queued exceptions."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append((method, url))
        item = self.queue.pop(0) if self.queue else FakeResp()
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses) -> IngestClient:
    return IngestClient(HttpClient(session=FakeSession(*responses), default_rate=10_000))


# ---- client: one error per failure mode, each with the right owner and retry stance -------

def test_transport_failure_is_external_and_retryable():
    import requests
    c = client(requests.ConnectionError("dns went away"))
    with pytest.raises(TransportFailure) as ei:
        c.get("futures_rt", "https://public.coindcx.com/x")
    assert ei.value.retryable is True
    assert ei.value.problem_class.value == "external_dependency"
    assert "connectivity" in ei.value.remediation


def test_429_is_rate_limited_and_says_the_rate_was_already_halved():
    with pytest.raises(RateLimited) as ei:
        client(FakeResp(429)).get("spot_ticker", "https://api.coindcx.com/x")
    assert ei.value.retryable is True
    assert "halved" in ei.value.remediation
    assert ei.value.severity.value == "warning"       # degraded, not a crash


def test_503_is_maintenance_and_distinct_from_a_server_error():
    with pytest.raises(ExchangeMaintenance) as ei:
        client(FakeResp(503)).get("spot_ticker", "https://api.coindcx.com/x")
    assert "documented exchange maintenance" in ei.value.likely_cause
    assert "goes quiet" in ei.value.remediation
    # and a 500 is a *different* error class
    with pytest.raises(HttpStatusError) as ei2:
        client(FakeResp(500)).get("spot_ticker", "https://api.coindcx.com/x")
    assert ei2.value.retryable is True and ei2.value.status == 500


def test_a_4xx_is_not_retryable_because_our_request_is_wrong():
    body = b'{"code":422,"errorCode":"BFF-SO-004","message":"Depth is not allowed","status":"error"}'
    with pytest.raises(HttpStatusError) as ei:
        client(FakeResp(422, body)).get("spot_book", "https://api.coindcx.com/x", {"Depth": 50})
    err = ei.value
    assert err.retryable is False
    assert "NOT retryable" in err.remediation
    assert "F-001" in err.likely_cause
    assert "Depth is not allowed" in err.context["body"]


def test_a_successful_response_returns_raw_bytes_and_timing():
    res = client(FakeResp(200, b'{"ok":1}', {"ratelimit": "limit=5000, remaining=4998"})).get(
        "spot_ticker", "https://api.coindcx.com/x")
    assert res.raw == b'{"ok":1}' and res.http_status == 200
    assert res.latency_ms >= 0
    c = client()
    assert c.rate_limit_headers(res) == {"ratelimit": "limit=5000, remaining=4998"}


# ---- adapters: the verified request-side traps are refused before the request ------------

def test_spot_book_rejects_an_unsupported_depth_before_calling_the_api():
    c = client()
    with pytest.raises(ConfigError) as ei:
        adapters.spot_book(c, pair="B-BTC_USDT", depth=37)
    assert ei.value.context["allowed"] == adapters.SPOT_DEPTHS
    assert c.http.session.calls == []                  # no request was made


def test_futures_book_rejects_the_depths_that_return_an_empty_book():
    for depth in (5, 100):
        with pytest.raises(ConfigError) as ei:
            adapters.futures_book(client(), pair="B-BTC_USDT", depth=depth)
        assert "EMPTY book" in ei.value.likely_cause    # F-014


def test_spot_book_sends_lowercase_depth():
    c = client(FakeResp(200, b'{"timestamp":1,"bids":{},"asks":{}}'))
    adapters.spot_book(c, pair="B-BTC_USDT", depth=50)
    url = c.http.session.calls[0][1]
    assert "depth=50" in url and "Depth=" not in url    # F-001


def test_futures_candles_converts_the_window_to_seconds():
    c = client(FakeResp(200, b'{"s":"ok","data":[]}'))
    adapters.futures_candles(c, pair="B-BTC_USDT", resolution="60", start_ms=T0, end_ms=T0 + 3_600_000)
    url = c.http.session.calls[0][1]
    assert f"from={T0 // 1000}" in url and "to=1789729200" in url


def test_inverted_windows_and_oversized_pages_are_refused():
    with pytest.raises(ConfigError, match="startTime"):
        adapters.spot_candles(client(), pair="a", interval="1h", start_ms=T0 + 1, end_ms=T0)
    with pytest.raises(ConfigError, match="limit"):
        adapters.spot_candles(client(), pair="a", interval="1h", start_ms=T0, end_ms=T0 + 1, limit=5000)


def test_candle_chunks_stay_under_the_futures_truncation_cap():
    chunks = adapters.candle_chunks(Product.FUTURES, 60_000, T0, T0 + 60_000 * 25_000)
    assert all((e - s) / 60_000 + 1 <= adapters.FUTURES_SAFE_CHUNK for s, e in chunks)
    assert chunks[0][0] == T0 and chunks[-1][1] == T0 + 60_000 * 25_000
    # consecutive windows share a boundary bar: a duplicate is possible, a hole is not
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))


# ---- scheduler: classification, backoff, isolation, stopping ------------------------------

def test_every_error_class_maps_to_a_backoff_schedule():
    assert classify(RateLimited("x", component="c", operation="o")) == "rate_limited"
    assert classify(ExchangeMaintenance("x", component="c", operation="o")) == "maintenance"
    assert classify(TransportFailure("x", component="c", operation="o")) == "transport"
    assert classify(HttpStatusError("x", component="c", operation="o", status=500, retryable=True)) == "server_error"
    assert classify(HttpStatusError("x", component="c", operation="o", status=422)) == "client_error"
    assert classify(RuntimeError("bug")) == "internal"
    for kind in ("rate_limited", "maintenance", "transport", "server_error", "internal"):
        assert kind in BACKOFF


def test_maintenance_backs_off_far_longer_than_a_rate_limit():
    # 503 is documented maintenance: wait minutes. 429 is our own rate: retry in seconds.
    assert backoff_delay("maintenance", 1) > backoff_delay("rate_limited", 1) * 10
    assert backoff_delay("maintenance", 10) <= BACKOFF["maintenance"][1] * 1.15   # capped (+ jitter)


def test_one_failing_task_does_not_stop_the_others():
    calls = {"good": 0}

    async def good():
        calls["good"] += 1

    async def bad():
        raise TransportFailure("down", component="ingest.client", operation="get")

    tasks = [PollTask("good", 1, good), PollTask("bad", 1, bad)]
    s = Scheduler(tasks)
    asyncio.run(s.tick())
    assert calls["good"] == 1
    assert tasks[1].fail_streak == 1 and tasks[1].failures == 1
    assert tasks[0].health().status is SystemStatus.HEALTHY
    assert tasks[1].health().status is SystemStatus.DEGRADED


def test_a_non_retryable_request_error_stops_the_task_instead_of_looping():
    async def bad():
        raise HttpStatusError("422", component="ingest.client", operation="get", status=422, retryable=False)

    t = PollTask("spot_book", 1, bad)
    s = Scheduler([t])
    asyncio.run(s.tick())
    assert t.stopped_reason and "non-retryable" in t.stopped_reason
    assert t.due_at == float("inf")                     # never rescheduled
    h = t.health()
    assert h.status is SystemStatus.DOWN and h.problem_class.value == "external_dependency"
    asyncio.run(s.tick())
    assert t.failures == 1                              # not retried


def test_persistent_failure_becomes_down_rather_than_an_endless_retry():
    async def bad():
        raise TransportFailure("down", component="ingest.client", operation="get")

    t = PollTask("futures_rt", 0, bad, dead_after=3)
    s = Scheduler([t])
    for _ in range(5):
        t.due_at = 0                                    # pretend the backoff elapsed
        asyncio.run(s.tick())
    assert t.fail_streak >= 3 and t.is_dead
    assert t.health().status is SystemStatus.DOWN
    assert t.failures == 3, "a dead task is not polled again until it is fixed and restarted"


def test_a_bug_in_a_task_is_reported_as_an_application_problem_not_an_external_one():
    async def buggy():
        raise KeyError("typo")

    t = PollTask("buggy", 1, buggy)
    asyncio.run(Scheduler([t]).tick())
    assert t.last_error.problem_class.value == "application_bug"
    assert "not an external failure" in t.last_error.likely_cause


def test_a_recovered_task_clears_its_failure_state():
    state = {"fail": True}

    async def flaky():
        if state["fail"]:
            raise TransportFailure("x", component="c", operation="o")

    t = PollTask("flaky", 0, flaky)
    s = Scheduler([t])
    asyncio.run(s.tick())
    assert t.fail_streak == 1
    state["fail"] = False
    t.due_at = 0
    asyncio.run(s.tick())
    assert t.fail_streak == 0 and t.last_error is None
    assert t.health().status is SystemStatus.HEALTHY


def test_low_priority_polls_are_dropped_first_under_a_rate_budget():
    ran = []

    async def note(name):
        ran.append(name)

    tasks = [PollTask("rt", 1, lambda: note("rt"), priority=Priority.P0),
             PollTask("book", 1, lambda: note("book"), priority=Priority.P3)]
    s = Scheduler(tasks, budget_per_sec=1)
    asyncio.run(s.tick())
    assert ran == ["rt"]                                # P0 ran, P3 was dropped
    assert tasks[1].dropped == 1


def test_dropped_polls_are_recorded_not_silently_skipped():
    events = []

    class Store:
        def record_dq_event(self, e):
            events.append(e)

    async def noop():
        pass

    tasks = [PollTask("rt", 1, noop, priority=Priority.P0), PollTask("book", 1, noop, priority=Priority.P3)]
    asyncio.run(Scheduler(tasks, store=Store(), budget_per_sec=1).tick())
    assert [e.kind for e in events] == ["POLL_DROPPED"]
    assert events[0].detail["task"] == "book"


def test_failures_are_recorded_as_dq_events_with_their_classification():
    events = []

    class Store:
        def record_dq_event(self, e):
            events.append(e)

    async def bad():
        raise ExchangeMaintenance("503", component="ingest.client", operation="get")

    asyncio.run(Scheduler([PollTask("rt", 1, bad)], store=Store()).tick())
    assert events[0].kind == "POLL_FAILED"
    assert events[0].problem_class == "external_dependency"
    assert events[0].detail["code"] == "EXTERNAL_MAINTENANCE"


def test_scheduler_stops_when_every_task_is_dead_rather_than_spinning():
    async def bad():
        raise HttpStatusError("422", component="c", operation="o", status=422)

    t = PollTask("x", 0, bad)
    s = Scheduler([t])
    asyncio.run(asyncio.wait_for(s.run_forever(idle_sleep_s=0.01), timeout=5))
    assert t.is_dead


def test_a_task_that_stops_polling_reports_stale_before_it_reports_healthy():
    t = PollTask("rt", 1, lambda: None)
    t.last_success_ms = 1                               # long ago
    h = t.health()
    assert h.status is SystemStatus.STALE
    assert h.problem_class.value == "infrastructure"    # the loop is blocked, not the venue


# ---- data quality -----------------------------------------------------------------------

def test_freshness_per_source_and_its_health():
    dq = DQMonitor({"futures_rt": 60_000})
    assert dq.source("futures_rt").health(T0).status is SystemStatus.WARMING     # nothing seen yet
    dq.source("futures_rt").observe(source_ts_ms=T0 - 1_000, sent_at_ms=T0 - 100, received_at_ms=T0)
    assert dq.is_fresh("futures_rt", T0) is True
    assert dq.source("futures_rt").health(T0).status is SystemStatus.HEALTHY
    assert dq.is_fresh("futures_rt", T0 + 120_000) is False
    stale = dq.source("futures_rt").health(T0 + 120_000)
    assert stale.status is SystemStatus.STALE and stale.problem_class.value == "external_dependency"


def test_a_source_without_its_own_timestamp_is_degraded_not_silently_fresh():
    dq = DQMonitor({"positioning": 2_700_000})
    dq.source("positioning").observe(source_ts_ms=None, sent_at_ms=T0, received_at_ms=T0)
    h = dq.source("positioning").health(T0)
    assert h.status is SystemStatus.DEGRADED
    assert "no timestamp of its own" in h.detail


def test_basis_comparison_discards_unaligned_pairs_instead_of_approximating():
    dq = DQMonitor({}, disagree_tolerance_ms=2000)
    assert dq.disagree.check(spot_last=Decimal(100), spot_ts_ms=T0,
                             futures_mark=Decimal(100), futures_ts_ms=T0 + 10_000) is None
    assert dq.disagree.discarded_unaligned == 1 and dq.disagree.compared == 0
    bps = dq.disagree.check(spot_last=Decimal(100), spot_ts_ms=T0,
                            futures_mark=Decimal("99.96"), futures_ts_ms=T0 + 500)
    assert bps == Decimal(-4)                            # hand-computed: -0.04% = -4 bps
    assert dq.disagree.discard_fraction == 0.5


def test_implausible_disagreement_degrades_health_with_a_measured_reference():
    dq = DQMonitor({})
    dq.disagree.check(spot_last=Decimal(100), spot_ts_ms=T0, futures_mark=Decimal(105), futures_ts_ms=T0)
    h = dq.disagree.health()
    assert h.status is SystemStatus.DEGRADED
    assert "F-023" in h.remediation


def test_clock_skew_beyond_tolerance_is_an_infrastructure_problem():
    dq = DQMonitor({"futures_rt": 60_000}, clock_tolerance_ms=3000)
    for _ in range(5):
        dq.source("futures_rt").observe(source_ts_ms=T0 + 8_000, sent_at_ms=T0 - 50, received_at_ms=T0)
    h = dq.clock_health()
    assert h.status is SystemStatus.DEGRADED
    assert h.problem_class.value == "infrastructure"
    assert "NTP/chrony" in h.remediation


def test_completeness_treats_a_confirmed_absent_bar_as_real_information():
    expected = {T0, T0 + 60_000, T0 + 120_000}
    present = {T0, T0 + 120_000}
    assert completeness(expected, present, known_absent=set())["missing"] == 1
    confirmed = completeness(expected, present, known_absent={T0 + 60_000})
    assert confirmed["missing"] == 0 and confirmed["completeness"] == 1.0
    assert confirmed["known_absent"] == 1


# ---- the finalization sweep (F-036) -------------------------------------------------------

def _futures_candle_payload(start_ms: int, count: int, *, close="105"):
    """A futures candles response shaped like the venue's: ``{"s": "ok", "data": [...]}``, ascending,
    with ``time`` in **milliseconds** even though the request's ``from``/``to`` are seconds (F-009)."""
    import json

    rows = [{"open": 100, "high": 110, "low": 90, "close": float(close), "volume": 1.5,
             "time": start_ms + i * 3_600_000} for i in range(count)]
    return json.dumps({"s": "ok", "data": rows}).encode()


def _pipeline(store, session_responses):
    from ingest.pipeline import IngestPipeline

    class Settings:
        class polling:
            futures_rt_s = 15.0
            spot_ticker_s = 30.0
            candles_1m_s = 60.0
            positioning_s = 900.0
            orderbook_s = 300.0
            instrument_meta_s = 21600.0
            refetch_bars = 3

        class freshness:
            book_max_age_s = 600.0

    return IngestPipeline(client=client(*session_responses), store=store,
                          dq=DQMonitor({"futures_candles_1h": 7_200_000}),
                          assets=["B-BTC_USDT"], spot_markets={"BTCUSDT": "B-BTC_USDT"},
                          settings=Settings())


@pytest.fixture
def store(tmp_path):
    from store import open_store

    s = open_store(tmp_path / "ingest.db")
    yield s
    s.close()


def test_the_sweep_finalizes_history_that_a_single_fetch_left_invisible(store):
    """F-036 end to end: bars pulled once are stored, correct and unusable until re-fetched.

    Hand-computed: five 1h bars are written once, so all five are non-final and
    ``get_bars(final_only=True)`` returns nothing. One sweep re-requests the window, the venue returns
    the same values, and the two-fetch rule settles all five.
    """
    from normalize.canonical import Bar, Timeframe

    bars = [Bar(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=Timeframe.H1,
                open_time_ms=T0 + i * 3_600_000, open="100", high="110", low="90", close="105",
                volume_base="1.5", is_final=False, fetched_at_ms=T0) for i in range(5)]
    store.upsert_bars(bars)
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=True) == 0

    pipeline = _pipeline(store, [FakeResp(content=_futures_candle_payload(T0, 5))])
    settled = asyncio.run(pipeline.poll_settle("B-BTC_USDT", Product.FUTURES, Timeframe.H1, "60"))
    assert settled > 0
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=True) == 5


def test_the_sweep_makes_no_request_when_there_is_nothing_to_settle(store):
    """A no-op pass must cost nothing: the sweep runs on a brisk cadence precisely because an empty
    one is free, and a request per empty pass would waste a third of the budget on nothing."""
    from normalize.canonical import Timeframe

    pipeline = _pipeline(store, [])
    assert asyncio.run(pipeline.poll_settle("B-BTC_USDT", Product.FUTURES, Timeframe.H1, "60")) == 0
    assert pipeline.client.http.session.calls == []


def test_a_bar_the_venue_no_longer_returns_stays_non_final_rather_than_being_promoted(store):
    """An unconfirmable bar is exactly what the two-fetch rule exists to exclude. Promoting it
    because a re-fetch *tried* would defeat the rule while appearing to satisfy it."""
    from normalize.canonical import Bar, Timeframe

    store.upsert_bars([Bar(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=Timeframe.H1,
                           open_time_ms=T0, open="100", high="110", low="90", close="105",
                           volume_base="1.5", is_final=False, fetched_at_ms=T0)])
    # The venue returns a *different* window: the bar in question never comes back.
    pipeline = _pipeline(store, [FakeResp(content=_futures_candle_payload(T0 + 50 * 3_600_000, 2))])
    asyncio.run(pipeline.poll_settle("B-BTC_USDT", Product.FUTURES, Timeframe.H1, "60"))
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=True) == 0
    still_pending = store.non_final_bar_times("B-BTC_USDT", Product.FUTURES, Timeframe.H1,
                                              settled_before_ms=T0 + 100 * 3_600_000)
    assert T0 in still_pending


def test_every_series_gets_a_sweep_task_below_the_live_polls_in_priority(store):
    """Settling old history matters, and it never matters more than knowing the current price."""
    from normalize.canonical import Timeframe

    pipeline = _pipeline(store, [])
    tasks = pipeline.build_tasks(candle_timeframes=[(Product.FUTURES, Timeframe.H1, "60"),
                                                    (Product.FUTURES, Timeframe.M1, "1")],
                                 enable_books=False, enable_positioning=False)
    sweeps = [t for t in tasks if t.name.startswith("settle:")]
    assert {t.name for t in sweeps} == {"settle:futures:B-BTC_USDT:1h", "settle:futures:B-BTC_USDT:1m"}
    live = [t for t in tasks if t.name.startswith("candles:")]
    assert all(s.priority > min(t.priority for t in live) for s in sweeps)
