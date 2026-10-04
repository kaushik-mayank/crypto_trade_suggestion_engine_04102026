"""PHASE-08 — the look-ahead boundary, and the drivers either side of it.

The tests that matter are the ones that would pass if the boundary leaked: a forming bar built from
the completed coarse bar (look-ahead), and a backtest snapshot richer than a live one at the same
``as_of`` (parity).
"""
import dataclasses
from decimal import Decimal

import pytest

from core.errors import ClockError, ProtocolViolation, SnapshotError, SnapshotIncomplete
from core.version import code_version
from normalize.canonical import Bar, Fee, InstrumentMeta, Positioning, Product, Timeframe
from pipeline.drivers import BacktestDriver, LiveDriver
from snapshot import (
    MarketSnapshot,
    SeriesKey,
    SeriesRequest,
    SnapshotBuilder,
    SnapshotRequest,
)
from store import open_store

H = 3_600_000
M = 60_000
DAY0 = 1789689600000          # 2026-09-18T00:00:00Z
ASSET = "B-BTC_USDT"
PH = "testhash"


def bar(t, *, tf=Timeframe.H1, close="100", high="110", low="90", vol="2", asset=ASSET,
        product=Product.FUTURES, final=True):
    return Bar(asset=asset, product=product, timeframe=tf, open_time_ms=t, open="100",
               high=high, low=low, close=close, volume_base=vol, is_final=final,
               fetched_at_ms=t + tf.ms, source="test")


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "s.db")
    yield s
    s.close()


@pytest.fixture
def builder(store):
    return SnapshotBuilder(store=store, parameter_hash=PH)


def seed_hours(store, n, *, start=DAY0, asset=ASSET, tf=Timeframe.H1):
    store.upsert_bars([bar(start + i * tf.ms, tf=tf, asset=asset) for i in range(n)])
    return start + n * tf.ms          # the ms just after the last bar closed


def req(bars=4, min_bars=1, tf=Timeframe.H1, **kw):
    return SnapshotRequest(series=(SeriesRequest(key=SeriesKey(ASSET, Product.FUTURES, tf),
                                                 bars=bars, min_bars=min_bars),), **kw)


# ---- the request is validated before anything is read ------------------------------------

def test_a_request_that_can_never_be_warm_is_refused():
    with pytest.raises(SnapshotError, match="can never be warm"):
        SeriesRequest(key=SeriesKey(ASSET, Product.FUTURES, Timeframe.H1), bars=10, min_bars=50)


def test_zero_bars_and_empty_requests_are_refused():
    with pytest.raises(SnapshotError, match="at least one bar"):
        SeriesRequest(key=SeriesKey(ASSET, Product.FUTURES, Timeframe.H1), bars=0)
    with pytest.raises(SnapshotError, match="at least one bar series"):
        SnapshotRequest(series=())


def test_an_unknown_tier_b_source_is_refused_with_the_known_ones_listed():
    with pytest.raises(SnapshotError) as ei:
        SnapshotRequest(series=(SeriesRequest(SeriesKey(ASSET, Product.FUTURES, Timeframe.H1), 5),),
                        tier_b=(("open_interest", ASSET),))
    assert "futures_rt" in str(ei.value)


def test_a_forming_bar_may_not_be_built_from_an_equal_or_coarser_timeframe():
    """This is the look-ahead guard in the request itself: a forming 1h bar 'from 1h' would be the
    completed 1h bar."""
    with pytest.raises(SnapshotError, match="strictly finer"):
        SnapshotRequest(series=(SeriesRequest(SeriesKey(ASSET, Product.FUTURES, Timeframe.H1), 5),),
                        forming=(SeriesKey(ASSET, Product.FUTURES, Timeframe.H1),),
                        forming_from=Timeframe.H1)


def test_the_builder_refuses_to_exist_without_a_parameter_hash(store):
    with pytest.raises(SnapshotError, match="parameter hash"):
        SnapshotBuilder(store=store, parameter_hash="")


# ---- the boundary holds ------------------------------------------------------------------

def test_a_snapshot_contains_only_bars_that_closed_by_as_of(builder, store):
    seed_hours(store, 6)
    as_of = DAY0 + 4 * H + 100          # 04:00:00.1 — four bars have closed
    snap = builder.build(req(bars=10), as_of)
    bars = snap.bars(ASSET, Product.FUTURES, Timeframe.H1)
    assert [b.open_time_ms for b in bars] == [DAY0, DAY0 + H, DAY0 + 2 * H, DAY0 + 3 * H]
    assert all(b.close_time_ms <= as_of for b in bars)


def test_a_snapshot_is_immutable(builder, store):
    seed_hours(store, 3)
    snap = builder.build(req(bars=3), DAY0 + 3 * H)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.as_of_ms = 0                               # type: ignore[misc]
    with pytest.raises(TypeError):                      # MappingProxyType
        snap.series[SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)] = None  # type: ignore[index]


def test_the_snapshot_holds_no_store_handle(builder, store):
    """specs/10 §2: the engine has no storage handle. If a snapshot carried one, every other
    guarantee here would be decoration."""
    seed_hours(store, 3)
    snap = builder.build(req(bars=3), DAY0 + 3 * H)
    for name in vars(snap):
        assert not hasattr(getattr(snap, name), "get_bars"), f"{name} exposes a store"
    assert not hasattr(snap, "store")


def test_reading_a_series_that_was_not_requested_names_what_is_available(builder, store):
    seed_hours(store, 3)
    snap = builder.build(req(bars=3), DAY0 + 3 * H)
    with pytest.raises(SnapshotError) as ei:
        snap.bars(ASSET, Product.FUTURES, Timeframe.D1)
    assert "was not requested" in ei.value.message
    assert "cannot fetch it itself" in ei.value.remediation


# ---- warm-up, edges and gaps -------------------------------------------------------------

def test_a_short_series_refuses_rather_than_computing_on_what_it_has(builder, store):
    seed_hours(store, 5)
    snap = builder.build(req(bars=100, min_bars=50), DAY0 + 5 * H)
    with pytest.raises(SnapshotIncomplete) as ei:
        snap.bars(ASSET, Product.FUTURES, Timeframe.H1)
    assert ei.value.context["reason"] == "WARMING"
    assert "need 50" in ei.value.message and "have 5" in ei.value.message
    assert snap.ok is False
    assert snap.series_ok(ASSET, Product.FUTURES, Timeframe.H1) is False


def test_an_empty_series_says_so_distinctly_from_a_short_one(builder, store):
    snap = builder.build(req(bars=5), DAY0 + 5 * H)
    state = snap.series[SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)]
    assert state.reason == "EMPTY" and "never ingested" in state.detail


def test_ingestion_falling_behind_is_reported_as_edge_behind_not_as_healthy(builder, store):
    seed_hours(store, 4)                       # newest bar closed at 04:00
    snap = builder.build(req(bars=10), DAY0 + 8 * H)    # as_of is 08:00 — four hours behind
    state = snap.series[SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)]
    assert state.ok is False and state.reason == "EDGE_BEHIND"
    assert "describes the past, not now" in state.detail


def test_a_window_that_is_both_short_and_stale_reports_the_stale_edge_not_the_warm_up(builder, store):
    """The regression this exists for (F-035): ingestion had been stopped for a fortnight, and the
    only thing reported was ``WARMING`` with "335 more hours to warm".

    Both statements were true, but they read as opposite instructions. ``WARMING`` says "the system is
    fine, give it time"; ``EDGE_BEHIND`` says "ingestion is dead, go restart it". Reporting the
    reassuring one for a dead feed is how a stopped recorder goes unnoticed, so staleness is checked
    **first** and the shortfall is named as a consequence of it rather than as a separate problem.
    """
    seed_hours(store, 4)                                 # newest bar closed at 04:00
    # as_of is 46 hours past the newest stored bar, and the window still reaches back far enough to
    # include it -- so the series is short *and* stale at the same time.
    snap = builder.build(req(bars=100, min_bars=50), DAY0 + 50 * H)
    state = snap.series[SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)]
    assert state.reason == "EDGE_BEHIND", "a dead feed was reported as merely warming"
    assert "describes the past, not now" in state.detail
    assert "consequence of the stale edge" in state.detail


def test_a_short_but_current_window_still_reports_warming(builder, store):
    """The other half of the ordering: when the edge *is* current, "give it time" is the correct and
    actionable message, and it must not be crowded out."""
    seed_hours(store, 5)
    snap = builder.build(req(bars=100, min_bars=50), DAY0 + 5 * H)
    state = snap.series[SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)]
    assert state.reason == "WARMING"
    assert "more to warm" in state.detail


def test_one_bar_of_edge_lag_is_tolerated_because_candles_are_polled_after_the_close(builder, store):
    seed_hours(store, 4)
    snap = builder.build(req(bars=4), DAY0 + 5 * H + 10_000)   # the 04:00 bar has closed, not stored
    assert snap.series_ok(ASSET, Product.FUTURES, Timeframe.H1) is True


def test_unconfirmed_gaps_are_recorded_for_the_feature_layer_not_hidden(builder, store):
    """specs/12 §6 gates gaps per computation window, so the snapshot reports them rather than
    deciding; what it must never do is hand over a holed series as if it were whole."""
    store.upsert_bars([bar(DAY0), bar(DAY0 + H), bar(DAY0 + 3 * H)])
    snap = builder.build(req(bars=4), DAY0 + 4 * H)
    state = snap.series[SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)]
    assert state.unconfirmed_gaps == (DAY0 + 2 * H,)
    store.confirm_absent_bar(ASSET, Product.FUTURES, Timeframe.H1, DAY0 + 2 * H, at_ms=DAY0)
    store.confirm_absent_bar(ASSET, Product.FUTURES, Timeframe.H1, DAY0 + 2 * H, at_ms=DAY0 + H)
    state2 = builder.build(req(bars=4), DAY0 + 4 * H).series[SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)]
    assert state2.unconfirmed_gaps == () and state2.known_absent == 1


# ---- the forming bar ---------------------------------------------------------------------

def test_the_forming_bar_is_built_from_closed_finer_bars_and_stops_at_as_of(builder, store):
    seed_hours(store, 2)
    # 1m bars for the third hour: only the first 30 have closed by as_of
    minutes = [bar(DAY0 + 2 * H + i * M, tf=Timeframe.M1, high=str(100 + i), low=str(100 - i),
                   close=str(100 + i), vol="1") for i in range(60)]
    store.upsert_bars(minutes)
    as_of = DAY0 + 2 * H + 30 * M
    key = SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)
    snap = builder.build(req(bars=2, forming=(key,)), as_of)
    fb = snap.forming_bar(ASSET, Product.FUTURES, Timeframe.H1)
    assert fb is not None
    assert fb.constituents == 30                     # not 60 — the rest had not happened
    assert fb.high == Decimal("129") and fb.low == Decimal("71")
    assert fb.volume_base == Decimal("30")
    assert fb.open_time_ms == DAY0 + 2 * H
    assert fb.elapsed_fraction == pytest.approx(0.5)
    assert fb.built_from is Timeframe.M1


def test_a_forming_bar_cannot_be_passed_where_a_bar_is_expected(builder, store):
    """The type separation specs/10 §2.1 asks for: FormingBar is not a Bar and holds no Bar, so a
    feature function's `list[Bar]` signature cannot be satisfied by it."""
    seed_hours(store, 1)
    store.upsert_bars([bar(DAY0 + H + i * M, tf=Timeframe.M1) for i in range(10)])
    key = SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)
    fb = builder.build(req(bars=1, forming=(key,)), DAY0 + H + 10 * M).forming_bar(
        ASSET, Product.FUTURES, Timeframe.H1)
    assert not isinstance(fb, Bar)
    assert not any(isinstance(getattr(fb, f.name), Bar) for f in dataclasses.fields(fb))
    assert not hasattr(fb, "close")          # a Bar has `close`; a forming bar has `last`


def test_no_finer_bars_yet_gives_no_forming_bar_rather_than_a_guess(builder, store):
    seed_hours(store, 2)
    key = SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)
    snap = builder.build(req(bars=2, forming=(key,)), DAY0 + 2 * H + 5_000)
    assert snap.forming_bar(ASSET, Product.FUTURES, Timeframe.H1) is None


def test_asking_for_an_unrequested_forming_bar_is_an_error_not_none(builder, store):
    seed_hours(store, 2)
    snap = builder.build(req(bars=2), DAY0 + 2 * H)
    with pytest.raises(SnapshotError, match="no forming bar was requested"):
        snap.forming_bar(ASSET, Product.FUTURES, Timeframe.H1)


# ---- Tier B ------------------------------------------------------------------------------

def pos(ts):
    return Positioning(asset=ASSET, ts_ms=ts, long_count_pct="60", short_count_pct="40",
                       long_value_pct="55", short_value_pct="45")


def test_a_fresh_tier_b_value_is_returned_with_its_own_timestamp(builder, store):
    seed_hours(store, 2)
    store.upsert_positioning([pos(DAY0 + 2 * H - 60_000)])
    snap = builder.build(req(bars=2, tier_b=(("positioning", ASSET),)), DAY0 + 2 * H)
    assert snap.tier_b("positioning", ASSET).long_count_pct == Decimal("60")
    assert snap.tier_b_state("positioning", ASSET).age_ms == 60_000


def test_a_stale_tier_b_value_refuses_and_says_how_stale(builder, store):
    seed_hours(store, 2)
    store.upsert_positioning([pos(DAY0)])                    # two hours before as_of; window is 45 min
    snap = builder.build(req(bars=2, tier_b=(("positioning", ASSET),)), DAY0 + 2 * H)
    with pytest.raises(SnapshotIncomplete) as ei:
        snap.tier_b("positioning", ASSET)
    assert "7200.0s old" in ei.value.message and "window is 2700s" in ei.value.message
    assert "no history to fall back on" in ei.value.remediation
    assert snap.tier_b_state("positioning", ASSET).value is not None   # still shown on the dashboard


def test_a_missing_tier_b_source_refuses_rather_than_returning_none(builder, store):
    seed_hours(store, 2)
    snap = builder.build(req(bars=2, tier_b=(("futures_rt", ASSET),)), DAY0 + 2 * H)
    with pytest.raises(SnapshotIncomplete, match="no futures_rt record"):
        snap.tier_b("futures_rt", ASSET)


def test_missing_instrument_metadata_refuses_because_fees_must_never_be_assumed(builder, store):
    seed_hours(store, 2)
    snap = builder.build(req(bars=2), DAY0 + 2 * H)
    with pytest.raises(SnapshotIncomplete) as ei:
        snap.instrument(ASSET, Product.FUTURES)
    assert "invariant 1" in ei.value.remediation


def test_instrument_metadata_carries_real_fees(builder, store):
    seed_hours(store, 2)
    store.upsert_instrument([InstrumentMeta(
        asset=ASSET, product=Product.FUTURES, tick="0.1", qty_increment="0.001", min_notional="5",
        maker_fee=Fee.from_api_percent("0.05"), taker_fee=Fee.from_api_percent("0.05"),
        fetched_at_ms=DAY0)])
    snap = builder.build(
        SnapshotRequest(series=(SeriesRequest(SeriesKey(ASSET, Product.FUTURES, Timeframe.H1), 2, 1),),
                        instruments=((ASSET, Product.FUTURES),)),
        DAY0 + 2 * H)
    assert snap.instrument(ASSET, Product.FUTURES).taker_fee.fraction == Decimal("0.0005")


# ---- identity ----------------------------------------------------------------------------

def test_snapshot_id_is_the_three_things_a_replay_needs(builder, store):
    seed_hours(store, 3)
    a = builder.build(req(bars=3), DAY0 + 3 * H)
    b = builder.build(req(bars=3), DAY0 + 3 * H)
    assert a.snapshot_id == b.snapshot_id
    assert builder.build(req(bars=3), DAY0 + 3 * H + 1).snapshot_id != a.snapshot_id
    other = SnapshotBuilder(store=store, parameter_hash="different")
    assert other.build(req(bars=3), DAY0 + 3 * H).snapshot_id != a.snapshot_id
    assert a.code_version == code_version()


def test_a_mutated_bar_changes_the_content_digest_but_not_the_snapshot_id(builder, store):
    """This is what lets a replay mismatch be attributed: same id, different content means the
    recorded history changed underneath, not that the code is wrong (specs/13 §9)."""
    seed_hours(store, 3)
    before = builder.build(req(bars=3), DAY0 + 3 * H)
    store.upsert_bars([bar(DAY0 + H, close="101")])       # a late revision of a closed bar
    after = builder.build(req(bars=3), DAY0 + 3 * H)
    assert after.snapshot_id == before.snapshot_id
    assert after.content_digest != before.content_digest


# ---- drivers -----------------------------------------------------------------------------

def test_live_and_backtest_snapshots_are_identical_at_the_same_as_of(builder, store):
    """specs/10 §2.2 parity, asserted rather than assumed."""
    seed_hours(store, 8)
    store.upsert_bars([bar(DAY0 + 8 * H + i * M, tf=Timeframe.M1) for i in range(20)])
    as_of = DAY0 + 8 * H + 20 * M
    key = SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)
    request = req(bars=8, forming=(key,))

    live = LiveDriver(builder=builder, request=request, clock=lambda: as_of).snapshot()
    back = next(BacktestDriver(builder=builder, request=request, start_ms=as_of - 20 * M,
                               end_ms=as_of, step=Timeframe.H1, offset_ms=20 * M).snapshots())

    assert live.as_of_ms == back.as_of_ms
    assert live.snapshot_id == back.snapshot_id
    assert live.content_digest == back.content_digest
    assert (live.forming_bar(ASSET, Product.FUTURES, Timeframe.H1)
            == back.forming_bar(ASSET, Product.FUTURES, Timeframe.H1))
    assert live.to_dict()["series"] == back.to_dict()["series"]
    assert live.built_by == "live" and back.built_by == "backtest"   # provenance only


def test_a_clock_behind_the_recorded_data_fails_as_infrastructure(builder, store):
    seed_hours(store, 4)                                  # data up to 04:00
    driver = LiveDriver(builder=builder, request=req(bars=4), clock=lambda: DAY0 + 2 * H)
    with pytest.raises(ClockError) as ei:
        driver.snapshot()
    assert ei.value.problem_class.value == "infrastructure"
    assert "w32tm /resync" in ei.value.remediation
    assert driver.snapshots_built == 0


def test_a_backtest_window_inside_the_holdout_is_a_protocol_violation(builder):
    with pytest.raises(ProtocolViolation) as ei:
        BacktestDriver(builder=builder, request=req(), start_ms=DAY0, end_ms=DAY0 + 40 * 86_400_000,
                       step=Timeframe.H1, max_as_of_ms=DAY0 + 10 * 86_400_000)
    assert "days inside" in ei.value.message
    assert "unlocked once" in ei.value.remediation


def test_a_misaligned_or_inverted_backtest_window_is_refused(builder):
    with pytest.raises(SnapshotError, match="not aligned"):
        BacktestDriver(builder=builder, request=req(), start_ms=DAY0 + 1, end_ms=DAY0 + H,
                       step=Timeframe.H1)
    with pytest.raises(SnapshotError, match="ends at or before it starts"):
        BacktestDriver(builder=builder, request=req(), start_ms=DAY0 + H, end_ms=DAY0,
                       step=Timeframe.H1)
    with pytest.raises(SnapshotError, match="inside one 1h step"):
        BacktestDriver(builder=builder, request=req(), start_ms=DAY0, end_ms=DAY0 + 4 * H,
                       step=Timeframe.H1, offset_ms=H + 1)


def test_the_backtest_walk_reports_coverage_instead_of_averaging_it_away(builder, store):
    seed_hours(store, 6, start=DAY0 + 4 * H)      # nothing before 04:00
    driver = BacktestDriver(builder=builder, request=req(bars=4, min_bars=4), start_ms=DAY0,
                            end_ms=DAY0 + 9 * H, step=Timeframe.H1, offset_ms=1000)
    snaps = list(driver.snapshots())
    cov = driver.coverage()
    assert cov["steps"] == len(snaps) == 9      # 00:00..08:00 inclusive, one per hour
    assert 0 < cov["usable"] < cov["steps"]
    assert cov["usable_fraction"] == cov["usable"] / cov["steps"]
    assert set(cov["tier_a_reasons"]) <= {"EMPTY", "WARMING", "EDGE_BEHIND"}


def test_coverage_separates_tier_a_holes_from_the_permanent_absence_of_tier_b(builder, store):
    """One number cannot answer "is this window usable", and reporting one is actively misleading.

    **Tier B has zero history**: funding, positioning and the book are point-in-time with no archive,
    so every step of a historical window is "incomplete" for Tier B — permanently and by design. A
    single ``usable_fraction`` reads 0% and makes a perfectly sound Tier A backtest look impossible,
    which is exactly what happened on the first real run (F-039: 1,246 steps, 0% usable, 98% of them
    fine for every strategy that reads OHLCV).

    So the Tier A fraction is reported separately, and the Tier A *reasons* are reported in full —
    there are at most three of them and they are the ones a reader can act on, whereas a single
    ranked list truncated for readability shows only the hundreds of Tier B entries.
    """
    seed_hours(store, 6, start=DAY0 + 4 * H)
    driver = BacktestDriver(builder=builder, request=req(bars=4, min_bars=4), start_ms=DAY0,
                            end_ms=DAY0 + 9 * H, step=Timeframe.H1, offset_ms=1000)
    list(driver.snapshots())
    cov = driver.coverage()
    assert cov["tier_a_usable"] + driver.tier_a_incomplete == cov["steps"]
    assert cov["tier_a_usable_fraction"] == cov["tier_a_usable"] / cov["steps"]
    # This request asks for no Tier B, so nothing is Tier-B-only incomplete and the two readings agree.
    assert cov["tier_b_only_incomplete"] == 0
    assert cov["tier_a_usable"] == cov["usable"]
    assert "tier_a_usable_fraction" in cov["reading"]


def test_the_backtest_grid_is_reproducible(builder):
    kw = {"builder": builder, "request": req(), "start_ms": DAY0, "end_ms": DAY0 + 5 * H,
          "step": Timeframe.H1}
    assert BacktestDriver(**kw).as_of_grid() == BacktestDriver(**kw).as_of_grid()
    assert BacktestDriver(**kw).as_of_grid()[0] == DAY0


def test_to_dict_is_serialisable_and_names_the_problems(builder, store):
    seed_hours(store, 2)
    snap = builder.build(req(bars=50, min_bars=50, tier_b=(("futures_rt", ASSET),)), DAY0 + 2 * H)
    d = snap.to_dict()
    import json
    json.dumps(d)                                   # must not raise: the API returns this
    assert d["ok"] is False
    kinds = {p["kind"] for p in d["problems"]}
    assert kinds == {"series", "point_in_time"}
    assert isinstance(snap, MarketSnapshot)
