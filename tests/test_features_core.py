"""PHASE-09 — the feature layer's contract, guards, conversions and window gating.

Weighted towards failure paths, because that is where this layer earns its keep: every one of these
degenerate cases has a plausible-looking wrong answer available, and returning it is how a data
outage becomes a trade.
"""
import math
from decimal import Decimal

import numpy as np
import pytest

from core.errors import FeatureError, FeatureNotAvailable, InvalidOHLC, UnitError
from features.convert import RoundMode, to_float, to_price, to_quantity
from features.numerics import (
    Degenerate,
    autocorr1,
    efficiency_ratio,
    gk_variance,
    log_returns,
    ols_slope_tstat,
    pearson,
    safe_mad,
    safe_std,
    variance_ratio,
)
from features.value import FeatureSet, FeatureStatus, FeatureValue
from features.windows import extract
from normalize.canonical import Bar, Product, Timeframe
from snapshot.models import SeriesKey, SeriesState

H = 3_600_000
MIN = 60_000
T0 = 1789689600000
KEY = SeriesKey("B-BTC_USDT", Product.FUTURES, Timeframe.H1)
KEY_1M = SeriesKey("B-BTC_USDT", Product.FUTURES, Timeframe.M1)


def bar(t, *, tf=Timeframe.H1, o="100", h="110", low="90", c="105", v="10"):
    return Bar(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=tf, open_time_ms=t,
               open=o, high=h, low=low, close=c, volume_base=v, is_final=True, fetched_at_ms=t)


def state(bars, *, key=KEY, ok=True, reason=None, absent=frozenset(), gaps=()):
    return SeriesState(key=key, bars=tuple(bars), requested=len(bars), min_required=1, ok=ok,
                       reason=reason, detail=reason, absent_times=absent, unconfirmed_gaps=gaps)


# ---- the value contract ------------------------------------------------------------------

def test_an_ok_feature_must_carry_a_value():
    with pytest.raises(FeatureError, match="OK with no value"):
        FeatureValue(name="X", version="1", status=FeatureStatus.OK)


def test_a_failed_feature_may_not_carry_a_value():
    """Otherwise a caller could read the number off a NUMERICAL_FAILURE and never know."""
    with pytest.raises(FeatureError, match="still carries a value"):
        FeatureValue(name="X", version="1", status=FeatureStatus.GAP_IN_WINDOW, value=0.3,
                     reason="gap")


def test_a_failed_feature_must_carry_a_reason():
    with pytest.raises(FeatureError, match="without a reason"):
        FeatureValue(name="X", version="1", status=FeatureStatus.WARMING)


def test_reading_a_non_ok_feature_raises_with_the_reason_attached():
    fv = FeatureValue(name="TRD_ER", version="1.0.0", status=FeatureStatus.GAP_IN_WINDOW,
                      reason="3 of 24 bars missing", timeframe=Timeframe.H1, horizon="short")
    fs = FeatureSet(asset="B-BTC_USDT", product=Product.FUTURES, as_of_ms=T0, values={fv.key: fv})
    with pytest.raises(FeatureNotAvailable) as ei:
        fs.value("TRD_ER", Timeframe.H1, "short")
    assert "GAP_IN_WINDOW" in ei.value.message and "3 of 24" in ei.value.message
    assert "no substitute value" in ei.value.remediation
    assert fs.status("TRD_ER", Timeframe.H1, "short") is FeatureStatus.GAP_IN_WINDOW
    assert fs.is_ok("TRD_ER", Timeframe.H1, "short") is False


def test_asking_for_a_feature_that_was_never_computed_lists_what_exists():
    fs = FeatureSet(asset="B-BTC_USDT", product=Product.FUTURES, as_of_ms=T0)
    with pytest.raises(FeatureError) as ei:
        fs.value("NOPE", Timeframe.H1)
    assert "never computed" in ei.value.message
    assert "available" in ei.value.context


def test_a_categorical_feature_cannot_be_read_as_a_number():
    fv = FeatureValue(name="STR_SEQ", version="1.0.0", status=FeatureStatus.OK, label="HH-HL",
                      timeframe=Timeframe.H1)
    fs = FeatureSet(asset="a", product=Product.SPOT, as_of_ms=T0, values={fv.key: fv})
    assert fs.label("STR_SEQ", Timeframe.H1) == "HH-HL"
    with pytest.raises(FeatureError, match="categorical"):
        fs.value("STR_SEQ", Timeframe.H1)


# ---- the Decimal/float boundary ----------------------------------------------------------

def test_a_non_finite_input_never_enters_computation():
    with pytest.raises(FeatureError, match="non-finite"):
        to_float(Decimal("NaN"))
    with pytest.raises(FeatureError, match="non-finite"):
        to_float(float("inf"))
    with pytest.raises(FeatureError, match="cannot convert"):
        to_float(None)                                  # type: ignore[arg-type]


def test_stops_round_away_from_entry_and_targets_toward_it():
    """specs/08 §10: tick rounding may only ever make a level more conservative."""
    tick = Decimal("0.5")
    assert to_price(100.3, tick=tick, mode=RoundMode.STOP_LONG) == Decimal("100.0")     # down/away
    assert to_price(100.3, tick=tick, mode=RoundMode.STOP_SHORT) == Decimal("100.5")    # up/away
    assert to_price(100.8, tick=tick, mode=RoundMode.TARGET_LONG) == Decimal("100.5")   # down/nearer
    assert to_price(100.2, tick=tick, mode=RoundMode.TARGET_SHORT) == Decimal("100.5")  # up/nearer


def test_a_non_finite_or_untickable_price_is_never_published():
    with pytest.raises(FeatureError, match="non-finite price"):
        to_price(float("nan"), tick=Decimal("0.5"), mode=RoundMode.NEAREST)
    with pytest.raises(UnitError, match="tick size must be positive"):
        to_price(100.0, tick=Decimal(0), mode=RoundMode.NEAREST)


def test_quantity_always_rounds_down_so_the_risk_budget_is_never_exceeded():
    assert to_quantity(1.9999, increment=Decimal("0.001")) == Decimal("1.999")
    with pytest.raises(FeatureError, match="invalid quantity"):
        to_quantity(-1.0, increment=Decimal("0.001"))


# ---- the numerical guards (specs/12 §4) --------------------------------------------------

def test_a_straight_line_fails_rather_than_reporting_an_enormous_t_statistic():
    with pytest.raises(Degenerate) as ei:
        ols_slope_tstat(np.arange(100, dtype=np.float64))
    assert ei.value.reason == "ZERO_RESIDUAL_VARIANCE"
    assert "unbounded rather than large" in ei.value.detail


def test_a_flat_window_fails_each_guard_with_its_own_name():
    flat = np.full(50, 100.0)
    for fn, expected in ((lambda: safe_std(flat), "ZERO_STDDEV"),
                         (lambda: safe_mad(flat), "ZERO_MAD"),
                         (lambda: efficiency_ratio(flat), "NO_MOVEMENT"),
                         (lambda: autocorr1(np.zeros(50)), "ZERO_VARIANCE"),
                         (lambda: variance_ratio(np.log(flat), 2), "ZERO_VARIANCE")):
        with pytest.raises(Degenerate) as ei:
            fn()
        assert ei.value.reason == expected


def test_a_non_positive_price_cannot_produce_a_log_return():
    with pytest.raises(Degenerate, match="NON_POSITIVE_PRICE"):
        log_returns(np.array([100.0, 0.0, 100.0]))


def test_correlation_of_a_constant_series_fails_rather_than_returning_zero():
    with pytest.raises(Degenerate, match="ZERO_STDDEV"):
        pearson(np.arange(30, dtype=float), np.full(30, 5.0))


def test_garman_klass_is_positive_on_valid_ohlc_and_alarms_on_invalid():
    """specs/12 §4.1: negativity is impossible on valid OHLC, so it is a data alarm, never clamped."""
    assert gk_variance(100, 110, 90, 105) > 0
    r = math.log(110 / 90)
    assert gk_variance(100, 110, 90, 105) == pytest.approx(0.5 * r**2 - (2 * math.log(2) - 1) * math.log(1.05)**2)
    with pytest.raises(InvalidOHLC) as ei:
        gk_variance(100, 100.001, 99.999, 130)     # C far outside [L, H]: impossible on a real bar
    assert "do not clamp" in ei.value.remediation
    assert ei.value.problem_class.value == "application_bug"


def test_the_variance_ratio_reads_one_on_a_random_walk_and_orders_correctly():
    """The test that caught F-031: the spec's literal formula gives ~1/k here, not ~1."""
    rng = np.random.default_rng(7)
    walk = np.log(np.cumprod(np.r_[100.0, 1 + rng.normal(0, 0.01, 4000)]))
    for k in (2, 4, 8):
        assert variance_ratio(walk, k) == pytest.approx(1.0, abs=0.2)

    mr = np.zeros(4000)                                  # AR(1) with negative phi: mean-reverting
    for i in range(1, mr.size):
        mr[i] = -0.4 * mr[i - 1] + rng.normal(0, 0.01)
    assert variance_ratio(np.log(100 * np.exp(np.cumsum(mr))), 2) < 0.8

    tr = np.zeros(4000)                                  # positive autocorrelation: trending
    for i in range(1, tr.size):
        tr[i] = 0.4 * tr[i - 1] + rng.normal(0, 0.01)
    assert variance_ratio(np.log(100 * np.exp(np.cumsum(tr))), 2) > 1.2


def test_the_variance_ratio_refuses_windows_it_cannot_support():
    with pytest.raises(Degenerate, match="INVALID_HORIZON"):
        variance_ratio(np.log(np.arange(1, 50, dtype=float)), 1)
    with pytest.raises(Degenerate, match="INSUFFICIENT_POINTS"):
        variance_ratio(np.log(np.arange(1, 6, dtype=float)), 8)


# ---- window extraction and the gap rules (specs/12 §5, §6) -------------------------------

def test_a_short_series_is_warming_with_the_time_still_needed():
    out = extract(state([bar(T0 + i * H) for i in range(5)]), lookback=24)
    assert out.status is FeatureStatus.WARMING and out.window is None
    assert out.reason == "need 24 1h bars, have 5"
    assert out.detail["hours_to_warm"] == 19.0


def test_extra_bars_are_counted_in_the_warm_up_requirement():
    """A 24-period return needs 25 closes; asking for 24 and silently using 23 returns would be a
    quietly different feature."""
    bars = [bar(T0 + i * H) for i in range(24)]
    assert extract(state(bars), lookback=24, extra_bars=1).status is FeatureStatus.WARMING
    bars.append(bar(T0 + 24 * H))
    out = extract(state(bars), lookback=24, extra_bars=1)
    assert out.ok and out.window.size == 25 and out.window.lookback == 24


def test_an_hourly_gap_refuses_even_when_the_bar_is_confirmed_absent():
    """specs/12 §6.2: at 1h/4h/1d an absent bar means something is wrong, not a quiet market."""
    bars = [bar(T0 + i * H) for i in range(30) if i != 10]
    out = extract(state(bars, absent=frozenset({T0 + 10 * H})), lookback=24)
    assert out.status is FeatureStatus.GAP_IN_WINDOW
    assert "confirmed absent" in out.reason and "something is wrong" in out.reason
    assert out.detail["confirmed_absent"] == 1 and out.detail["unconfirmed"] == 0


def test_an_unconfirmed_hourly_gap_says_it_is_unknown_which_it_is():
    bars = [bar(T0 + i * H) for i in range(30) if i != 10]
    out = extract(state(bars), lookback=24)
    assert out.status is FeatureStatus.GAP_IN_WINDOW
    assert out.detail["unconfirmed"] == 1


def test_a_confirmed_absent_minute_bar_is_synthesised_flat_and_counted():
    """specs/12 §6.2/§6.3: permitted on 1m only, because 1m never feeds alpha."""
    bars = [bar(T0 + i * MIN, tf=Timeframe.M1, c="101") for i in range(30) if i != 10]
    out = extract(state(bars, key=KEY_1M, absent=frozenset({T0 + 10 * MIN})), lookback=20)
    assert out.ok
    w = out.window
    assert w.synthesised_bars == 1
    hole = int(np.where(w.open_time_ms == T0 + 10 * MIN)[0][0])
    assert w.open[hole] == w.high[hole] == w.low[hole] == w.close[hole] == 101.0
    assert w.volume[hole] == 0.0


def test_an_unconfirmed_minute_gap_is_still_refused():
    bars = [bar(T0 + i * MIN, tf=Timeframe.M1) for i in range(30) if i != 10]
    out = extract(state(bars, key=KEY_1M), lookback=20)
    assert out.status is FeatureStatus.GAP_IN_WINDOW
    assert "not confirmed absent" in out.reason


def test_a_series_the_snapshot_already_rejected_is_not_re_diagnosed():
    out = extract(state([bar(T0 + i * H) for i in range(30)], ok=False, reason="EDGE_BEHIND"),
                  lookback=24)
    assert out.status is FeatureStatus.DQ_FAILED and "not usable" in out.reason


def test_window_arrays_are_float64_and_carry_their_own_bounds():
    out = extract(state([bar(T0 + i * H) for i in range(30)]), lookback=24)
    w = out.window
    assert w.close.dtype == np.float64 and w.size == 24
    assert w.window_start_ms == T0 + 6 * H
    assert w.window_end_ms == T0 + 30 * H
    assert w.typical_price[0] == pytest.approx((110 + 90 + 105) / 3)
