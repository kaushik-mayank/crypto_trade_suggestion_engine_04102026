"""PHASE-04 — the SQLite store, with the failure paths given equal weight.

The look-ahead guard and the sqlite error translation are the two things here that protect the rest
of the system, so both are exercised deliberately rather than assumed.
"""
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from core.errors import IntegrityError, MigrationError, PointInTimeViolation, StoreError
from core.health import SystemStatus
from normalize.canonical import (
    Bar,
    BookSnapshot,
    Fee,
    FuturesRT,
    InstrumentMeta,
    MarginTier,
    Positioning,
    Product,
    SpotTicker,
    Timeframe,
)
from store import DQEvent, RunRecord, SqliteStore, open_store
from store.migrations import latest_version

H = 3_600_000
T0 = 1789725600000            # 2026-09-18T10:00:00Z


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "state.db")
    yield s
    s.close()


def bar(t=T0, close="105", tf=Timeframe.H1, final=False, fetched=None, asset="B-BTC_USDT"):
    return Bar(asset=asset, product=Product.FUTURES, timeframe=tf, open_time_ms=t,
               open="100", high="110", low="90", close=close, volume_base="1.5",
               is_final=final, fetched_at_ms=fetched or (t + tf.ms))


# ---- migrations ---------------------------------------------------------------------------

def test_migrations_are_idempotent_and_reach_the_current_version(tmp_path):
    p = tmp_path / "s.db"
    with SqliteStore(p) as s:
        assert s.migrate() == latest_version()
        assert s.migrate() == latest_version()        # second call changes nothing
        s.require_schema()


def test_a_database_from_a_newer_build_is_refused_not_downgraded(tmp_path):
    p = tmp_path / "s.db"
    with SqliteStore(p) as s:
        s.migrate()
        s.conn.execute("INSERT INTO schema_version (version, applied_ms, description) VALUES (99, 0, 'future')")
    with SqliteStore(p) as s, pytest.raises(MigrationError) as ei:
        s.migrate()
    assert "NEWER build" in ei.value.likely_cause
    assert ei.value.context["applied"] == [1, 2, 99]


def test_require_schema_fails_fast_when_migrations_were_never_run(tmp_path):
    with SqliteStore(tmp_path / "s.db") as s, pytest.raises(MigrationError) as ei:
        s.require_schema()
    assert ei.value.context["have"] == 0 and ei.value.context["want"] == latest_version()
    assert "run migrate" in ei.value.remediation


def test_a_failing_migration_leaves_no_partial_schema(tmp_path, monkeypatch):
    # patch where it is USED: sqlite_store imported the list by value
    monkeypatch.setattr("store.sqlite_store.MIGRATIONS",
                        [(1, "broken", "CREATE TABLE ok (a INT); CREATE TABLE ;")])
    with SqliteStore(tmp_path / "s.db") as s:
        with pytest.raises(MigrationError) as ei:
            s.migrate()
        assert "rolled back" in ei.value.message
        assert s.schema_version() == 0
        tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "ok" not in tables, "the transaction must have rolled the whole script back"


# ---- bars: write semantics ---------------------------------------------------------------

def test_first_write_inserts_and_an_identical_refetch_finalises(store):
    assert store.upsert_bars([bar()]).to_dict()["inserted"] == 1
    r = store.upsert_bars([bar()])
    assert (r.finalized, r.updated, r.mutations) == (1, 0, [])
    assert store.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, 0, T0 + H)[0].is_final is True


def test_a_changed_final_bar_is_reported_as_a_mutation_with_both_versions(store):
    store.upsert_bars([bar()])
    store.upsert_bars([bar()])                       # now final
    r = store.upsert_bars([bar(close="106")])        # the exchange revised it
    assert len(r.mutations) == 1
    m = r.mutations[0]
    assert m.old["close"] == "105" and m.new["close"] == "106"
    assert m.open_time_ms == T0 and m.timeframe is Timeframe.H1
    assert store.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, 0, T0 + H)[0].close == Decimal("106")


def test_a_changed_non_final_bar_is_an_update_not_a_mutation(store):
    store.upsert_bars([bar()])                       # in progress, not final
    r = store.upsert_bars([bar(close="107")])
    assert r.updated == 1 and r.mutations == []


def test_decimal_precision_survives_the_round_trip_exactly(store):
    store.upsert_bars([Bar(asset="a", product=Product.SPOT, timeframe=Timeframe.H1, open_time_ms=T0,
                           open="78062.01000000", high="78124.99", low="77982.88", close="78094.01000000",
                           volume_base="351.67586000", fetched_at_ms=T0 + H)])
    got = store.get_bars("a", Product.SPOT, Timeframe.H1, 0, T0 + H, final_only=False)[0]
    assert str(got.open) == "78062.01000000"          # trailing zeros preserved: TEXT, never REAL
    assert got.volume_base == Decimal("351.67586000")


def test_bar_counts_and_latest_time_respect_finality(store):
    store.upsert_bars([bar(T0), bar(T0 + H)])
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1) == 0        # none final yet
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=False) == 2
    store.upsert_bars([bar(T0), bar(T0 + H)])
    assert store.latest_bar_time("B-BTC_USDT", Product.FUTURES, Timeframe.H1) == T0 + H


# ---- the look-ahead boundary -------------------------------------------------------------

def test_as_of_excludes_a_bar_that_had_not_closed_yet(store):
    store.upsert_bars([bar(T0, final=True), bar(T0 + H, final=True)])
    # at 10:30 the 10:00 bar is still open, so nothing has closed
    assert store.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, 0, T0 + 2 * H,
                          as_of_ms=T0 + 1_800_000) == []
    # at 11:00 exactly, the 10:00 bar has closed and the 11:00 bar has not
    got = store.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, 0, T0 + 2 * H, as_of_ms=T0 + H)
    assert [b.open_time_ms for b in got] == [T0]


def test_the_lookahead_guard_raises_if_a_future_bar_ever_slips_through(store):
    """Defence in depth: the guard is what catches a query built without the predicate."""
    bars = [bar(T0, final=True), bar(T0 + H, final=True)]
    with pytest.raises(PointInTimeViolation) as ei:
        store._assert_no_lookahead(bars, T0 + H, asset="B-BTC_USDT", timeframe=Timeframe.H1)
    assert ei.value.problem_class.value == "application_bug"      # look-ahead is OUR bug
    assert ei.value.context["offending_open_times"] == [T0 + H]
    assert "as_of_ms" in ei.value.remediation


def test_point_in_time_never_returns_a_record_newer_than_as_of(store):
    store.upsert_futures_rt([
        FuturesRT(asset="B-BTC_USDT", ts_ms=T0, funding_rate="0.0001", predicted_funding_rate="0.0001",
                  mark_price="78000", last_price="78001", tpe_tick_send_ms=T0, received_at_ms=T0),
        FuturesRT(asset="B-BTC_USDT", ts_ms=T0 + 60_000, funding_rate="0.0002", predicted_funding_rate="0.0002",
                  mark_price="78100", last_price="78101", tpe_tick_send_ms=T0 + 60_000, received_at_ms=T0 + 60_000),
    ])
    got = store.get_point_in_time("B-BTC_USDT", "futures_rt", T0 + 30_000)
    assert got.mark_price == Decimal("78000")        # the older row, not the newer one
    assert store.get_point_in_time("B-BTC_USDT", "futures_rt", T0 - 1) is None


def test_an_unknown_point_in_time_source_lists_the_known_ones(store):
    with pytest.raises(StoreError) as ei:
        store.get_point_in_time("a", "orderflow", T0)
    assert "futures_rt" in ei.value.context["known"]


# ---- tier B writes ----------------------------------------------------------------------

def test_futures_rt_without_its_own_timestamp_is_an_integrity_error(store):
    row = FuturesRT(asset="a", ts_ms=T0, funding_rate="0", predicted_funding_rate="0",
                    mark_price="1", last_price="1")           # no btST/bmST
    with pytest.raises(IntegrityError) as ei:
        store.upsert_futures_rt([row])
    assert "NO_ROW_TIMESTAMP" in ei.value.remediation


def test_tier_b_writes_are_idempotent(store):
    t = SpotTicker(asset="a", ts_ms=T0, last_price="100", received_at_ms=T0)
    store.upsert_spot_ticker([t]), store.upsert_spot_ticker([t])
    assert store.conn.execute("SELECT COUNT(*) FROM spot_ticker").fetchone()[0] == 1

    p = Positioning(asset="a", ts_ms=T0, long_count_pct="54.92", short_count_pct="45.08",
                    long_value_pct="31.65", short_value_pct="68.35")
    store.upsert_positioning([p]), store.upsert_positioning([p])
    assert store.get_point_in_time("a", "positioning", T0).divergence == Decimal("23.27")

    b = BookSnapshot(asset="a", product=Product.SPOT, ts_ms=T0,
                     bids=((Decimal("99.9"), Decimal("2")),), asks=((Decimal("100.1"), Decimal("1")),),
                     fetched_at_ms=T0)
    store.upsert_books([b]), store.upsert_books([b])
    back = store.get_point_in_time("a", "spot_book", T0)
    assert back.bids[0] == (Decimal("99.9"), Decimal("2")) and back.mid == Decimal("100.0")


def test_instrument_round_trips_fees_as_fractions_and_tiers(store):
    inst = InstrumentMeta(asset="B-BTC_USDT", product=Product.FUTURES, tick="0.1", qty_increment="0.001",
                          min_notional="60", maker_fee=Fee.from_api_percent("0.0236"),
                          taker_fee=Fee.from_api_percent("0.059"),
                          margin_tiers=(MarginTier(max_notional=Decimal(20000),
                                                   maintenance_margin_rate=Decimal("0.005")),),
                          status="active", fetched_at_ms=T0)
    store.upsert_instrument([inst])
    back = store.get_instrument("B-BTC_USDT", Product.FUTURES)
    assert back.taker_fee.fraction == Decimal("0.00059")
    assert back.maintenance_margin_for(Decimal(1000)) == Decimal("0.005")
    assert store.get_instrument("B-BTC_USDT", Product.FUTURES, as_of_ms=T0 - 1) is None


# ---- known-absent bars ------------------------------------------------------------------

def test_absence_needs_two_confirmations_before_it_counts(store):
    args = ("a", Product.SPOT, Timeframe.M1, T0)
    assert store.confirm_absent_bar(*args, at_ms=T0) == 1
    assert store.is_absent_confirmed(*args) is False          # one sighting is not evidence
    assert store.confirm_absent_bar(*args, at_ms=T0 + 60_000) == 2
    assert store.is_absent_confirmed(*args) is True
    assert store.known_absent_bars("a", Product.SPOT, Timeframe.M1, 0, T0 + H) == {T0}


# ---- events and runs --------------------------------------------------------------------

def test_dq_events_are_counted_by_kind(store):
    for kind in ("ROW_REJECTED", "ROW_REJECTED", "BAR_MUTATION"):
        store.record_dq_event(DQEvent(kind=kind, component="c", operation="o", at_ms=T0,
                                      problem_class="data_quality", detail={"x": 1}))
    assert store.dq_event_counts(since_ms=0) == {"ROW_REJECTED": 2, "BAR_MUTATION": 1}
    assert store.dq_event_counts(since_ms=T0 + 1) == {}


def test_runs_record_code_and_parameter_provenance(store):
    rid = store.record_run(RunRecord(process="app", started_ms=T0, code_version="abc123",
                                     parameter_hash="deadbeef"))
    store.close_run(rid, stopped_ms=T0 + 1000, reason="clean shutdown")
    row = store.conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    assert row["code_version"] == "abc123" and row["stop_reason"] == "clean shutdown"


# ---- sqlite failures, each translated into an actionable error --------------------------

def test_a_corrupt_database_file_names_the_recovery_path(tmp_path):
    p = tmp_path / "corrupt.db"
    p.write_bytes(b"this is definitely not a sqlite database" * 10)
    with pytest.raises(StoreError) as ei:
        open_store(p)
    assert ei.value.problem_class.value == "infrastructure"
    assert "rebuild" in ei.value.remediation and "raw JSONL" in ei.value.remediation


def test_a_read_only_database_rejects_writes_with_a_clear_reason(tmp_path):
    p = tmp_path / "ro.db"
    open_store(p).close()
    s = SqliteStore(p, read_only=True)
    with pytest.raises(StoreError) as ei:
        s.upsert_bars([bar()])
    assert "read-only" in ei.value.message or "readonly" in str(ei.value).lower()
    s.close()


def test_a_locked_database_says_so_and_says_why(tmp_path):
    p = tmp_path / "locked.db"
    open_store(p).close()
    blocker = sqlite3.connect(p, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    s = SqliteStore(p, timeout_s=0.1)
    try:
        with pytest.raises(StoreError) as ei:
            s.upsert_bars([bar()])
        assert "locked" in ei.value.message
        assert "one writer per database" in ei.value.remediation
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        s.close()


def test_a_missing_table_is_reported_as_a_migration_problem(tmp_path):
    p = tmp_path / "s.db"
    with SqliteStore(p) as s:          # deliberately un-migrated
        with pytest.raises(MigrationError) as ei:
            s.get_bars("a", Product.SPOT, Timeframe.H1, 0, 1)
        assert "migrations were not run" in ei.value.likely_cause


def test_using_a_closed_store_fails_clearly_rather_than_crashing(tmp_path):
    s = open_store(tmp_path / "s.db")
    s.close()
    with pytest.raises(StoreError) as ei:
        s.bar_count("a", Product.SPOT, Timeframe.H1)
    assert "closed" in ei.value.message


# ---- health ----------------------------------------------------------------------------

def test_health_is_healthy_when_migrated_and_reports_schema(store):
    h = store.health()
    assert h.status is SystemStatus.HEALTHY
    assert h.context["schema_version"] == latest_version()


def test_health_reports_down_with_an_owner_when_the_schema_is_stale(tmp_path, monkeypatch):
    with SqliteStore(tmp_path / "s.db") as s:
        s.migrate()
        monkeypatch.setattr("store.sqlite_store.latest_version", lambda: 99)
        h = s.health()
        assert h.status is SystemStatus.DOWN
        assert h.problem_class.value == "infrastructure"
        assert "schema" in h.detail


def test_health_warns_before_the_disk_fills(store, monkeypatch):
    fake = shutil.disk_usage(Path.cwd())._replace(free=100 * 10**6)      # 100 MB left
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: fake)
    h = store.health()
    assert h.status is SystemStatus.DEGRADED
    assert "free" in h.detail and h.remediation


# ---- finding bars that never finalized ----------------------------------------------------

def test_non_final_bars_are_findable_so_a_single_fetch_cannot_hide_history_forever(store):
    """The query behind F-036.

    A bar becomes final only when a second fetch returns it unchanged. History pulled in one sweep —
    a catch-up after an outage, an import, a first run — is therefore stored, correct, and
    **invisible** to ``get_bars(final_only=True)``, which is what the snapshot builder uses. The
    symptom is a series reporting WARMING indefinitely while the row count says the data is there.
    Nothing can repair that without first being able to find the bars.
    """
    now = T0 + 100 * Timeframe.H1.ms
    store.upsert_bars([bar(t=T0 + i * Timeframe.H1.ms, final=False) for i in range(5)])
    store.upsert_bars([bar(t=T0 + 10 * Timeframe.H1.ms, final=True)])

    pending = store.non_final_bar_times("B-BTC_USDT", Product.FUTURES, Timeframe.H1,
                                        settled_before_ms=now)
    assert pending == [T0 + i * Timeframe.H1.ms for i in range(5)]      # oldest first
    assert T0 + 10 * Timeframe.H1.ms not in pending                     # already final

    # The count confirms the asymmetry that makes this worth a query of its own.
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=True) == 1
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=False) == 6


def test_a_bar_that_has_only_just_closed_is_not_offered_for_settling(store):
    """Close to now the venue may still be revising a bar legitimately, and re-requesting those
    churns requests without finalizing anything."""
    store.upsert_bars([bar(t=T0, final=False)])
    just_closed = T0 + Timeframe.H1.ms          # the bar closed exactly now
    assert store.non_final_bar_times("B-BTC_USDT", Product.FUTURES, Timeframe.H1,
                                     settled_before_ms=just_closed - 1) == []
    assert store.non_final_bar_times("B-BTC_USDT", Product.FUTURES, Timeframe.H1,
                                     settled_before_ms=just_closed) == [T0]


def test_the_settling_query_is_bounded_so_one_sweep_cannot_monopolise_the_budget(store):
    store.upsert_bars([bar(t=T0 + i * Timeframe.H1.ms, final=False) for i in range(50)])
    now = T0 + 100 * Timeframe.H1.ms
    pending = store.non_final_bar_times("B-BTC_USDT", Product.FUTURES, Timeframe.H1,
                                        settled_before_ms=now, limit=10)
    assert len(pending) == 10
    assert pending[0] == T0                     # oldest first: the oldest hole blocks the longest warm-up


def test_the_same_price_at_a_different_precision_is_not_a_mutation(store):
    """A false ``BAR_MUTATION`` is the loudest possible false alarm: ERROR-level, recorded as an
    external dependency fault, and it marks signals non-replayable.

    The venue is already known to send inconsistent precision (hard invariant 6, verified on order
    books: ``"11570.67000000"`` beside ``"2001"``), so ``105`` arriving as ``105.0`` on the next fetch
    is a live hazard. Hashing the Decimal *strings* would report it as a revision of history; the
    store compares values.
    """
    store.upsert_bars([bar(close="105", final=True)])
    res = store.upsert_bars([bar(close="105.000", final=True)])
    assert res.mutations == [], "the same price at a different precision was reported as a mutation"
    assert res.unchanged == 1


def test_a_genuinely_changed_price_is_still_reported_as_a_mutation(store):
    """The other half: the guard above must not make the store blind to real revisions, which are the
    thing that breaks replay."""
    store.upsert_bars([bar(close="105", final=True)])
    res = store.upsert_bars([bar(close="106", final=True)])
    assert len(res.mutations) == 1


def test_a_precision_difference_still_finalizes_a_bar_on_the_second_fetch(store):
    """The two-fetch rule has to fire on a *value* repeat. If precision defeated it, the finalization
    sweep would re-request the same window forever and settle nothing (F-036)."""
    store.upsert_bars([bar(close="105.0", final=False)])
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=True) == 0
    res = store.upsert_bars([bar(close="105", final=False)])
    assert res.finalized == 1
    assert store.bar_count("B-BTC_USDT", Product.FUTURES, Timeframe.H1, final_only=True) == 1


def test_canonical_decimal_maps_equal_values_to_equal_strings():
    """Hand-checked, including the exponent form ``normalize()`` produces for round numbers."""
    from store.sqlite_store import canonical_decimal

    for a, b in (("105", "105.0"), ("105", "105.000"), ("100", "1E+2"), ("0.5", "0.5000"),
                 ("0", "0.00"), ("11570.67", "11570.67000000")):
        assert canonical_decimal(a) == canonical_decimal(b), (a, b)
    assert canonical_decimal("100") == "100"            # plain, never exponent form
    assert canonical_decimal("105") != canonical_decimal("106")
