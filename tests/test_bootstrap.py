"""The history bootstrap — the one command a fresh host depends on.

Local data is not deployable (the development database holds the holdout and is gitignored for that
reason), so a new host rebuilds from the API. If this command is wrong, the host either has no data
or — much worse — has data that looks present and is invisible to the feature layer, which is the
failure already recorded as F-036.

The tests that matter most here are about **idempotence and ordering**, because those are what make
the command safe to run on a machine nobody is watching: safe to re-run, safe to interrupt, and
leaving the *recent* history complete if it is interrupted.
"""

import pytest

from bootstrap.fetch import (
    CHUNK_BARS,
    EMPTY_CHUNKS_BEFORE_STOP,
    SETTLE_AFTER_BARS,
    _mark_settled,
    chunks,
    run_job,
    verify_chunk,
)
from bootstrap.plan import (
    FUTURES_RESOLUTION,
    LIVE_MARGIN,
    Job,
    Profile,
    live_depth,
    plan,
)
from core.errors import BootstrapError
from normalize.canonical import Bar, Product, Timeframe
from snapshot.builder import SeriesRequest, SnapshotRequest
from snapshot.models import SeriesKey
from store import open_store

T0 = 1767225600000
H = 3_600_000
ASSET = "B-BTC_USDT"


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "bootstrap.db")
    yield s
    s.close()


def request_for(*specs) -> SnapshotRequest:
    return SnapshotRequest(series=tuple(
        SeriesRequest(SeriesKey(ASSET, Product.FUTURES, tf), bars=bars) for tf, bars in specs))


def job_for(tf=Timeframe.H1, *, start, end) -> Job:
    return Job(asset=ASSET, product=Product.FUTURES, timeframe=tf,
               api_resolution=FUTURES_RESOLUTION[tf], start_ms=start, end_ms=end, missing=1)


def bars_for(tf: Timeframe, times, *, close="100") -> list[Bar]:
    return [Bar(asset=ASSET, product=Product.FUTURES, timeframe=tf, open_time_ms=t,
                open=close, high=close, low=close, close=close, volume_base="1",
                is_final=False, fetched_at_ms=t + tf.ms) for t in times]


# ---- the plan is derived, not maintained ---------------------------------------------------

def test_the_live_depth_comes_from_what_the_app_actually_asks_for():
    """Hand-computed: 1440 × 2 = 2880, and a shallow 240-bar series gets 240 + 500 rather than 480.

    Derived rather than listed, so changing ``app.build_request`` cannot leave the bootstrap fetching
    the wrong depth — which would show up as a series permanently ``WARMING`` on a host nobody is
    watching.
    """
    assert live_depth(1440) == 1440 * LIVE_MARGIN
    assert live_depth(240) == 740, "a shallow series needs a floor, not just a multiplier"


def test_the_plan_follows_the_snapshot_request_rather_than_a_hardcoded_list(store):
    request = request_for((Timeframe.H1, 1440), (Timeframe.D1, 180))
    p = plan(store, request, now_ms=T0 + 5000 * H)
    assert {j.timeframe for j in p.jobs} == {Timeframe.H1, Timeframe.D1}
    # Depth per series, from that series' own request.
    h1 = next(j for j in p.jobs if j.timeframe is Timeframe.H1)
    assert (h1.end_ms - h1.start_ms) // H == pytest.approx(live_depth(1440), abs=1)


def test_an_empty_database_reports_everything_missing(store):
    p = plan(store, request_for((Timeframe.H1, 100)), now_ms=T0 + 5000 * H)
    job = p.jobs[0]
    assert job.present == 0
    assert job.missing == job.expected > 0
    assert not p.complete


def test_a_complete_database_yields_an_empty_plan(store):
    """Idempotence, and it is a property of the design rather than a feature.

    The plan is recomputed from the database every time, so there is no "already done" marker that
    could go stale, be lost, or be wrong.
    """
    now = T0 + 300 * H
    depth = live_depth(100)
    times = list(range(now - depth * H, now, H))
    store.upsert_bars(bars_for(Timeframe.H1, times))
    store.upsert_bars(bars_for(Timeframe.H1, times))      # second fetch finalizes them

    p = plan(store, request_for((Timeframe.H1, 100)), now_ms=now)
    assert p.complete
    assert p.outstanding == []
    assert "no completion marker" in p.to_dict()["idempotence_note"]


def test_a_confirmed_absent_bar_is_not_counted_as_missing(store):
    """Otherwise the bootstrap would re-fetch a hole in the venue's own history forever.

    This is the difference between a command that terminates and one that does not, and it comes free
    from going through ``detect_gaps`` rather than counting rows.
    """
    now = T0 + 300 * H
    depth = live_depth(100)
    times = list(range(now - depth * H, now, H))
    hole = times[len(times) // 2]
    present = [t for t in times if t != hole]
    store.upsert_bars(bars_for(Timeframe.H1, present))
    store.upsert_bars(bars_for(Timeframe.H1, present))

    before = plan(store, request_for((Timeframe.H1, 100)), now_ms=now).jobs[0]
    assert before.missing == 1

    # The venue confirms twice that it has no such bar.
    store.confirm_absent_bar(ASSET, Product.FUTURES, Timeframe.H1, hole, at_ms=now)
    store.confirm_absent_bar(ASSET, Product.FUTURES, Timeframe.H1, hole, at_ms=now + 1000)

    after = plan(store, request_for((Timeframe.H1, 100)), now_ms=now).jobs[0]
    assert after.missing == 0, "a confirmed-absent bar must not drive an endless re-fetch"


def test_the_full_profile_reaches_back_to_the_known_floor(store):
    now = T0 + 5000 * H
    live = plan(store, request_for((Timeframe.H1, 100)), now_ms=now, profile=Profile.LIVE).jobs[0]
    full = plan(store, request_for((Timeframe.H1, 100)), now_ms=now, profile=Profile.FULL).jobs[0]
    assert full.start_ms < live.start_ms


def test_only_futures_are_planned_because_that_is_all_the_app_requests(store):
    """Fetching spot candles nobody reads would be an hour of a host's time for nothing."""
    request = SnapshotRequest(series=(
        SeriesRequest(SeriesKey(ASSET, Product.FUTURES, Timeframe.H1), bars=100),
        SeriesRequest(SeriesKey(ASSET, Product.SPOT, Timeframe.H1), bars=100),
    ))
    p = plan(store, request, now_ms=T0 + 500 * H)
    assert [j.product for j in p.jobs] == [Product.FUTURES]


# ---- chunk ordering is what makes an interruption survivable -------------------------------

def test_chunks_are_requested_newest_first():
    """An interrupted bootstrap must leave the *recent* history complete, because that is the half
    the live system needs to run at all. Oldest-first would leave a host with four years of 2021 and
    nothing usable."""
    job = job_for(start=T0, end=T0 + 25_000 * H)
    windows = list(chunks(job))
    assert len(windows) > 1
    assert windows[0][1] == job.end_ms, "the first request must cover the most recent window"
    assert all(a[0] >= b[0] for a, b in zip(windows, windows[1:])), "windows must walk backwards"


def test_chunks_cover_the_whole_span_with_no_hole_at_the_seams():
    job = job_for(start=T0, end=T0 + 25_000 * H)
    windows = list(chunks(job))
    assert windows[-1][0] == job.start_ms
    # Consecutive windows overlap rather than abut, so a boundary bar is never the first or last row
    # of a page and cannot be lost to an off-by-one.
    for newer, older in zip(windows, windows[1:]):
        assert older[1] >= newer[0], f"gap between {older} and {newer}"


def test_a_chunk_never_exceeds_the_venues_silent_truncation_cap():
    """The futures endpoint truncates past ~30,000 bars without saying so (F-009), and a truncated
    page read as complete is a hole nobody notices."""
    job = job_for(tf=Timeframe.M1, start=T0, end=T0 + 500_000 * 60_000)
    for start, end in list(chunks(job))[:5]:
        assert (end - start) // 60_000 <= CHUNK_BARS


# ---- finalization: the F-036 shortcut, and its limits --------------------------------------

def test_settled_history_is_written_final_so_it_is_not_invisible():
    """F-036: a bar becomes final only on a second fetch, and ``get_bars(final_only=True)`` — what
    the snapshot builder uses — cannot see a non-final bar. Writing four years of correct history
    that nothing can read is the failure this avoids.

    Hand-computed: with ``SETTLE_AFTER_BARS = 3``, a bar closing more than 3 intervals ago is
    settled. The 4 most recent are left pending.
    """
    now = T0 + 100 * H
    job = job_for(start=T0, end=now)
    times = [now - i * H for i in range(10)]
    prepared, final_count, pending = _mark_settled(tuple(bars_for(Timeframe.H1, times)), job,
                                                   now - SETTLE_AFTER_BARS * H)
    assert final_count + pending == 10
    assert pending == SETTLE_AFTER_BARS + 1, "only the recent tail is left for the live sweep"
    assert all(b.is_final for b in prepared if b.open_time_ms + H <= now - SETTLE_AFTER_BARS * H)


def test_the_recent_tail_is_left_for_the_live_sweep_not_forced_final():
    """Close to now the venue may still legitimately revise a bar, which is exactly what the
    two-fetch rule is for. The shortcut applies only where it cannot."""
    now = T0 + 100 * H
    job = job_for(start=T0, end=now)
    just_closed = bars_for(Timeframe.H1, [now - H])
    prepared, final_count, pending = _mark_settled(tuple(just_closed), job,
                                                   now - SETTLE_AFTER_BARS * H)
    assert final_count == 0 and pending == 1
    assert not prepared[0].is_final


# ---- fetching, against a fake venue -------------------------------------------------------

class FakeClient:
    """Serves candles from a dict of bar times, and records what was asked for."""

    def __init__(self, available: set[int], tf: Timeframe = Timeframe.H1, *, close="100",
                 second_close: str | None = None):
        self.available = available
        self.tf = tf
        self.close = close
        self.second_close = second_close
        self.calls: list[tuple[int, int]] = []
        self._seen: set[tuple[int, int]] = set()

    def get(self, endpoint, url, params=None):
        import json
        from types import SimpleNamespace

        params = params or {}
        start = int(params["from"]) * 1000
        end = int(params["to"]) * 1000
        self.calls.append((start, end))
        repeat = (start, end) in self._seen
        self._seen.add((start, end))
        close = self.second_close if (repeat and self.second_close) else self.close
        rows = [{"time": t, "open": close, "high": close, "low": close, "close": close,
                 "volume": "1"}
                for t in sorted(self.available) if start <= t <= end]
        body = json.dumps({"s": "ok", "data": rows}).encode()
        return SimpleNamespace(raw=body, url=url, sent_at_ms=T0, received_at_ms=T0 + 10,
                               status=200, headers={})


def test_a_job_writes_what_it_fetches_and_the_plan_then_reports_complete(store):
    now = T0 + 300 * H
    depth = live_depth(100)
    times = set(range(now - depth * H, now, H))
    client = FakeClient(times)

    request = request_for((Timeframe.H1, 100))
    p = plan(store, request, now_ms=now)
    assert not p.complete

    result = run_job(store, client, p.jobs[0], now_ms=now, verify_fraction=0.0)
    assert result.rows_written > 0
    assert result.written_final > 0, "settled history must be written final (F-036)"

    after = plan(store, request, now_ms=now)
    assert after.complete, "re-planning after a successful fetch must report nothing missing"


def test_rerunning_a_finished_job_makes_no_requests(store):
    """The property that makes this safe to put in a cron job or run by hand twice."""
    now = T0 + 300 * H
    depth = live_depth(100)
    times = set(range(now - depth * H, now, H))
    client = FakeClient(times)
    request = request_for((Timeframe.H1, 100))

    run_job(store, client, plan(store, request, now_ms=now).jobs[0], now_ms=now,
            verify_fraction=0.0)
    before = len(client.calls)

    second = plan(store, request, now_ms=now)
    assert second.complete
    assert second.outstanding == []
    assert len(client.calls) == before, "planning must not touch the network"


def test_walking_past_the_start_of_history_stops_instead_of_requesting_emptiness(store):
    """A series' history has a floor. Continuing past it would walk backwards through years of empty
    pages one request at a time."""
    now = T0 + 100_000 * H
    client = FakeClient(set())          # the venue has nothing at all
    job = job_for(start=T0, end=now)
    result = run_job(store, client, job, now_ms=now, verify_fraction=0.0)
    assert result.stopped_early is not None
    assert result.empty_chunks == EMPTY_CHUNKS_BEFORE_STOP
    assert "no history" in result.stopped_early


# ---- verification is what keeps the finalization shortcut honest ---------------------------

def test_a_matching_refetch_reports_no_mismatch(store):
    now = T0 + 300 * H
    times = set(range(now - 100 * H, now, H))
    client = FakeClient(times)
    job = job_for(start=now - 100 * H, end=now)
    written = bars_for(Timeframe.H1, sorted(times))
    assert verify_chunk(client, job, job.start_ms, job.end_ms, written) is None


def test_a_differing_refetch_is_reported_with_the_field_that_differs(store):
    """Writing settled history final on one fetch is only defensible if a second fetch agrees, so a
    disagreement has to be loud and specific rather than counted as drift."""
    now = T0 + 300 * H
    times = set(range(now - 100 * H, now, H))
    client = FakeClient(times, close="100", second_close="101")
    job = job_for(start=now - 100 * H, end=now)
    written = bars_for(Timeframe.H1, sorted(times), close="100")

    # First call primes the "seen" set; the second serves the different price.
    verify_chunk(client, job, job.start_ms, job.end_ms, written)
    mismatch = verify_chunk(client, job, job.start_ms, job.end_ms, written)
    assert mismatch is not None
    assert mismatch["kind"] == "value_differs"
    assert mismatch["field"] in ("open", "high", "low", "close")


def test_a_precision_difference_is_not_reported_as_a_mismatch(store):
    """F-037: ``100`` and ``100.000`` are the same price. Comparing the decimal *strings* would make
    the verifier cry wolf on every chunk the venue formatted differently."""
    now = T0 + 300 * H
    times = set(range(now - 50 * H, now, H))
    client = FakeClient(times, close="100", second_close="100.000")
    job = job_for(start=now - 50 * H, end=now)
    written = bars_for(Timeframe.H1, sorted(times), close="100")
    verify_chunk(client, job, job.start_ms, job.end_ms, written)
    assert verify_chunk(client, job, job.start_ms, job.end_ms, written) is None


def test_a_venue_failure_mid_fetch_names_the_window_and_says_how_to_resume(store):
    """A bootstrap that silently skipped a window would leave a hole indistinguishable from one the
    venue does not have."""
    from core.errors import TransportFailure

    class Failing:
        def get(self, *a, **kw):
            raise TransportFailure("no response", component="test", operation="get")

    now = T0 + 300 * H
    with pytest.raises(BootstrapError) as e:
        run_job(store, Failing(), job_for(start=now - 100 * H, end=now), now_ms=now,
                verify_fraction=0.0)
    assert "re-run the bootstrap" in e.value.remediation
    assert e.value.retryable is True
    assert "start_ms" in e.value.context
