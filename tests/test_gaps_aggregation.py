"""PHASE-07 — gaps, absent-bar confirmation and aggregation refusal.

The refusal tests matter most: `specs/02` §1 branches the whole timeframe stack on P12 precisely
because aggregating across a hole produces a bar that is wrong and looks fine.
"""
from decimal import Decimal

import pytest

from core.errors import GapError
from normalize.canonical import Bar, Product, Timeframe
from pipeline.gaps import (
    REQUIRED_CONFIRMATIONS,
    aggregate_bars,
    aggregate_series,
    confirm_absence,
    detect_gaps,
    expected_bar_times,
)
from store import open_store

H = 3_600_000
T0 = 1789725600000            # 2026-09-18T10:00:00Z
T4 = 1789718400000            # 2026-09-18T08:00:00Z — aligned to 4h


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "s.db")
    yield s
    s.close()


def hour(t, close="100", asset="B-BTC_USDT", product=Product.FUTURES, final=True, volume="2"):
    return Bar(asset=asset, product=product, timeframe=Timeframe.H1, open_time_ms=t,
               open="100", high="110", low="90", close=close, volume_base=volume,
               is_final=final, fetched_at_ms=t + H)


# ---- expected times ----------------------------------------------------------------------

def test_expected_times_are_aligned_to_the_timeframe_grid():
    assert expected_bar_times(Timeframe.H1, T0, T0 + 2 * H) == [T0, T0 + H, T0 + 2 * H]
    # a start mid-bar rounds forward to the next boundary
    assert expected_bar_times(Timeframe.H1, T0 + 1, T0 + 2 * H) == [T0 + H, T0 + 2 * H]


# ---- detection ---------------------------------------------------------------------------

def test_detect_gaps_separates_missing_from_confirmed_absent(store):
    store.upsert_bars([hour(T0), hour(T0 + 2 * H)])          # 11:00 missing
    r = detect_gaps(store, "B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0, T0 + 2 * H)
    assert r.missing == [T0 + H] and r.has_unconfirmed_gap
    assert r.completeness == pytest.approx(2 / 3)

    for _ in range(REQUIRED_CONFIRMATIONS):
        store.confirm_absent_bar("B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0 + H, at_ms=T0)
    r2 = detect_gaps(store, "B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0, T0 + 2 * H)
    assert r2.missing == [] and r2.known_absent == 1
    assert r2.completeness == 1.0                            # a no-trade bar is not missing data


# ---- confirmation ------------------------------------------------------------------------

def test_a_returning_bar_proves_it_was_a_fetch_gap_not_an_absent_bar(store):
    def fetch(start, end):
        return [hour(t) for t in range(start, end + 1, H)]    # everything comes back

    settled = confirm_absence(store, fetch, "B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0 + H,
                              now_ms=T0 + 5 * H)
    assert settled is False
    assert store.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0 + H, T0 + H, final_only=False)
    assert store.is_absent_confirmed("B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0 + H) is False


def test_absence_needs_two_sightings_with_neighbours_present(store):
    def fetch(start, end):
        return [hour(t) for t in range(start, end + 1, H) if t != T0 + H]

    args = (store, fetch, "B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0 + H)
    assert confirm_absence(*args, now_ms=T0) is False        # one sighting is not evidence
    assert confirm_absence(*args, now_ms=T0 + H) is True
    assert store.is_absent_confirmed("B-BTC_USDT", Product.FUTURES, Timeframe.H1, T0 + H) is True


def test_an_empty_bracketing_window_is_inconclusive_not_a_confirmation(store):
    """If the fetch itself failed, absence cannot be concluded — that is how a real outage would
    otherwise be recorded as 'these bars do not exist'."""
    settled = confirm_absence(store, lambda s, e: [], "B-BTC_USDT", Product.FUTURES, Timeframe.H1,
                              T0 + H, now_ms=T0)
    assert settled is False
    assert store.known_absent_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, 0, T0 + 9 * H) == set()


# ---- aggregation: hand-computed, then the refusals ---------------------------------------

def test_four_hourly_bars_aggregate_to_one_exact_four_hour_bar():
    bars = [
        Bar(asset="a", product=Product.SPOT, timeframe=Timeframe.H1, open_time_ms=T4,
            open="100", high="105", low="99", close="104", volume_base="1", is_final=True, fetched_at_ms=T4),
        Bar(asset="a", product=Product.SPOT, timeframe=Timeframe.H1, open_time_ms=T4 + H,
            open="104", high="112", low="103", close="110", volume_base="2", is_final=True, fetched_at_ms=T4),
        Bar(asset="a", product=Product.SPOT, timeframe=Timeframe.H1, open_time_ms=T4 + 2 * H,
            open="110", high="111", low="95", close="97", volume_base="3", is_final=True, fetched_at_ms=T4),
        Bar(asset="a", product=Product.SPOT, timeframe=Timeframe.H1, open_time_ms=T4 + 3 * H,
            open="97", high="101", low="96", close="100", volume_base="4", is_final=True, fetched_at_ms=T4),
    ]
    agg = aggregate_bars(bars, factor=4, target=Timeframe.H4)
    assert agg.open == Decimal("100")        # first open
    assert agg.high == Decimal("112")        # max high
    assert agg.low == Decimal("95")          # min low
    assert agg.close == Decimal("100")       # last close
    assert agg.volume_base == Decimal("10")  # 1+2+3+4
    assert agg.open_time_ms == T4 and agg.timeframe is Timeframe.H4
    assert agg.source == "aggregated:4x1h" and agg.is_final is True


def test_aggregation_refuses_across_an_unconfirmed_gap():
    bars = [hour(T4, product=Product.SPOT), hour(T4 + H, product=Product.SPOT),
            hour(T4 + 3 * H, product=Product.SPOT)]          # the third hour is missing
    with pytest.raises(GapError) as ei:
        aggregate_bars(bars, factor=4, target=Timeframe.H4, window_start_ms=T4)
    err = ei.value
    assert "refusing to aggregate" in err.message
    assert err.context["missing"] == [T4 + 2 * H] and err.context["have"] == 3
    assert "silently wrong bar" in err.remediation


def test_aggregation_proceeds_when_the_hole_is_confirmed_absent():
    bars = [hour(T4, product=Product.SPOT), hour(T4 + H, product=Product.SPOT),
            hour(T4 + 3 * H, product=Product.SPOT)]
    agg = aggregate_bars(bars, factor=4, target=Timeframe.H4, window_start_ms=T4,
                         known_absent={T4 + 2 * H})
    assert agg.volume_base == Decimal("6")               # 2+2+2 from the three real bars
    assert agg.open_time_ms == T4


def test_a_wrong_factor_is_refused_with_the_right_one_named():
    bars = [Bar(asset="a", product=Product.FUTURES, timeframe=Timeframe.M5, open_time_ms=T4 + i * 300_000,
                open="1", high="1", low="1", close="1", volume_base="1", is_final=True, fetched_at_ms=T4)
            for i in range(12)]
    with pytest.raises(GapError) as ei:
        aggregate_bars(bars, factor=12, target=Timeframe.M15)    # the specs/01 §P12 error
    assert "12 x 5m is an hour" in ei.value.likely_cause
    assert "factor=3" in ei.value.remediation


def test_a_misaligned_window_is_refused():
    """`Bar` itself refuses a misaligned open, so the only way to get here is a window derived from
    the first available bar rather than from the grid — which is exactly the mistake guarded."""
    bars = [hour(T4 + (i + 1) * H) for i in range(4)]      # 09:00..12:00 — each bar is legal
    with pytest.raises(GapError, match="not aligned"):
        aggregate_bars(bars, factor=4, target=Timeframe.H4, window_start_ms=T4 + H)


def test_mixed_timeframes_and_empty_input_are_refused():
    with pytest.raises(GapError, match="empty set"):
        aggregate_bars([], factor=4, target=Timeframe.H4)
    mixed = [hour(T4), Bar(asset="a", product=Product.FUTURES, timeframe=Timeframe.M15,
                           open_time_ms=T4, open="1", high="1", low="1", close="1",
                           volume_base="1", is_final=True, fetched_at_ms=T4)]
    with pytest.raises(GapError, match="mixed timeframes"):
        aggregate_bars(mixed, factor=4, target=Timeframe.H4)


def test_a_fully_absent_window_is_skipped_rather_than_invented():
    with pytest.raises(GapError, match="confirmed absent"):
        aggregate_bars([hour(T4 - H)], factor=4, target=Timeframe.H4, window_start_ms=T4,
                       known_absent={T4, T4 + H, T4 + 2 * H, T4 + 3 * H})


def test_aggregate_series_reports_refusals_without_discarding_good_windows():
    bars = [hour(T4 + i * H, product=Product.SPOT) for i in range(8) if i != 5]   # one hole in window 2
    out, refusals = aggregate_series(bars, factor=4, target=Timeframe.H4)
    assert [b.open_time_ms for b in out] == [T4]              # first window complete
    assert len(refusals) == 1 and refusals[0]["window_start_ms"] == T4 + 4 * H
    assert refusals[0]["reason"] == "DQ_GAP"


def test_aggregation_of_real_history_matches_the_verified_exactness():
    """F-013 measured futures 4x1h -> native 240 as exact; re-verify against the imported data."""
    import pathlib
    db = pathlib.Path("data/state.db")
    if not db.exists():
        pytest.skip("no local store with imported history")
    s = open_store(db, migrate=False)
    try:
        bars = s.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, T4, T4 + 4 * H - 1)
        native = s.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H4, T4, T4)
        if len(bars) != 4 or not native:
            pytest.skip("window not present in the local store")
        agg = aggregate_bars(bars, factor=4, target=Timeframe.H4, window_start_ms=T4)
        assert (agg.open, agg.high, agg.low, agg.close) == (native[0].open, native[0].high,
                                                           native[0].low, native[0].close)
    finally:
        s.close()
