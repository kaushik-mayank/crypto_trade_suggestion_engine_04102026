"""PHASE-10 — Families 1 and 2, hand-computed, plus the specs/12 §7 parity requirement.

> **Required test, per feature: `compute_series` and `compute_at` agree on every bar of a fixture,
> to floating-point tolerance.** A feature without this test passing is not considered implemented.

So the parity test is table-driven over the registry: adding a feature with a vectorised path
automatically puts it under that requirement, and a feature whose two paths drift fails here rather
than producing a backtest that disagrees with live for reasons nobody can find.
"""
import math

import numpy as np
import pytest

from config.parameters import load_registry
from features.engine import FeatureEngine, engine_from_registry
from features.f1_trend import trd_er, trd_ret, trd_tstat
from features.f2_volatility import atr, true_range, vol_cc, vol_gk, vol_park
from features.numerics import Degenerate
from features.registry import BY_NAME, FEATURES, lookback_for
from features.spec import ComputeContext, FeatureSpec
from features.value import FeatureStatus
from features.windows import extract
from normalize.canonical import Bar, Product, Timeframe
from snapshot.models import SeriesKey, SeriesState

H = 3_600_000
T0 = 1767225600000          # 2026-01-01T00:00:00Z: far enough back that an 800-bar
                            # fixture still ends in the past (Bar refuses future times)
KEY = SeriesKey("B-BTC_USDT", Product.FUTURES, Timeframe.H1)
PARAMS = {p.name: p.value for p in load_registry().parameters}


def bars_from(rows, tf=Timeframe.H1):
    """rows: (open, high, low, close, volume) per bar, oldest first."""
    return [Bar(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=tf, open_time_ms=T0 + i * tf.ms,
                open=str(o), high=str(h), low=str(low), close=str(c), volume_base=str(v),
                is_final=True, fetched_at_ms=T0)
            for i, (o, h, low, c, v) in enumerate(rows)]


def window_of(rows, *, lookback, extra_bars=0, tf=Timeframe.H1):
    bars = bars_from(rows, tf)
    st = SeriesState(key=SeriesKey("B-BTC_USDT", Product.FUTURES, tf), bars=tuple(bars),
                     requested=len(bars), min_required=1, ok=True)
    out = extract(st, lookback=lookback, extra_bars=extra_bars)
    assert out.ok, out.reason
    return out.window


def ctx_for(window, *, lookback, horizon="short", tf=Timeframe.H1):
    return ComputeContext(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=tf,
                          horizon=horizon, lookback=lookback, window=window, params=PARAMS)


# ---- Family 1, by hand -------------------------------------------------------------------

def test_trd_ret_is_the_log_return_over_exactly_n_bars():
    rows = [(100, 100 + i, 100, 100 + i, 1) for i in range(5)]      # closes 100..104
    w = window_of(rows, lookback=4, extra_bars=1)
    assert trd_ret(ctx_for(w, lookback=4)) == pytest.approx(math.log(104 / 100))


def test_trd_er_is_net_move_over_path_length():
    # closes: 100 -> 102 -> 101 -> 103. net = 3, path = 2 + 1 + 2 = 5
    rows = [(100, 103, 99, c, 1) for c in (100, 102, 101, 103)]
    w = window_of(rows, lookback=3, extra_bars=1)
    assert trd_er(ctx_for(w, lookback=3)) == pytest.approx(3 / 5)


def test_trd_er_is_one_for_a_monotonic_path_and_zero_for_a_round_trip():
    up = [(100, 110, 99, c, 1) for c in (100, 101, 102, 103)]
    assert trd_er(ctx_for(window_of(up, lookback=3, extra_bars=1), lookback=3)) == pytest.approx(1.0)
    rt = [(100, 110, 99, c, 1) for c in (100, 102, 104, 100)]
    assert trd_er(ctx_for(window_of(rt, lookback=3, extra_bars=1), lookback=3)) == pytest.approx(0.0)


def test_trd_tstat_matches_an_independent_ols_computation():
    rng = np.random.default_rng(3)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 40)))
    rows = [(c, c * 1.01, c * 0.99, c, 1) for c in closes]
    w = window_of(rows, lookback=40)
    got = trd_tstat(ctx_for(w, lookback=40))

    y = np.log(closes)                                   # independent OLS with numpy.polyfit
    x = np.arange(y.size, dtype=float)
    beta, alpha = np.polyfit(x, y, 1)
    resid = y - (alpha + beta * x)
    se = math.sqrt(resid @ resid / (y.size - 2) / float(((x - x.mean()) ** 2).sum()))
    assert got == pytest.approx(beta / se, rel=1e-9)


# ---- Family 2, by hand -------------------------------------------------------------------

def test_vol_cc_is_the_sample_std_of_log_returns():
    closes = [100, 101, 100, 102, 101]
    rows = [(c, c, c, c, 1) for c in closes]
    w = window_of(rows, lookback=4, extra_bars=1)
    expected = float(np.std(np.diff(np.log(np.array(closes, dtype=float))), ddof=1))
    assert vol_cc(ctx_for(w, lookback=4)) == pytest.approx(expected)


def test_vol_park_matches_the_stated_formula():
    rows = [(100, 110, 90, 105, 1)] * 5
    w = window_of(rows, lookback=5)
    expected = math.sqrt(5 * math.log(110 / 90) ** 2 / (4 * 5 * math.log(2)))
    assert vol_park(ctx_for(w, lookback=5)) == pytest.approx(expected)


def test_vol_gk_matches_the_stated_formula():
    rows = [(100, 110, 90, 105, 1)] * 4
    w = window_of(rows, lookback=4)
    per_bar = 0.5 * math.log(110 / 90) ** 2 - (2 * math.log(2) - 1) * math.log(105 / 100) ** 2
    assert vol_gk(ctx_for(w, lookback=4)) == pytest.approx(math.sqrt(per_bar))


def test_true_range_uses_the_prior_close_not_the_current_open():
    # prior close 100; this bar H=105 L=99 -> TR = max(6, |105-100|, |99-100|) = 6
    rows = [(100, 100, 100, 100, 1), (104, 105, 99, 101, 1)]
    w = window_of(rows, lookback=1, extra_bars=1)
    assert true_range(w)[0] == pytest.approx(6.0)
    # a gap up: prior close 100, bar is 110-112 -> TR = |112 - 100| = 12, wider than H-L
    rows2 = [(100, 100, 100, 100, 1), (110, 112, 110, 111, 1)]
    w2 = window_of(rows2, lookback=1, extra_bars=1)
    assert true_range(w2)[0] == pytest.approx(12.0)


def test_atr_is_a_simple_mean_of_true_range_not_a_wilder_average():
    """specs/02 Family 2: SMA, because Wilder's seeding makes live and backtest differ."""
    rows = [(100, 100, 100, 100, 1), (100, 102, 98, 100, 1), (100, 104, 96, 100, 1),
            (100, 101, 99, 100, 1)]
    w = window_of(rows, lookback=3, extra_bars=1)
    trs = [4.0, 8.0, 2.0]
    assert atr(ctx_for(w, lookback=3)) == pytest.approx(sum(trs) / 3)


def test_atr_refuses_a_window_with_no_range_rather_than_returning_zero():
    rows = [(100, 100, 100, 100, 1)] * 6
    w = window_of(rows, lookback=5, extra_bars=1)
    with pytest.raises(Degenerate, match="ZERO_VARIANCE"):
        atr(ctx_for(w, lookback=5))


# ---- specs/12 §7: the two paths must agree ----------------------------------------------

def _fixture_window(n=300, tf=Timeframe.H1, seed=11):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.008, n)))
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.003)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.003)))
        rows.append((o, hi, lo, c, abs(rng.normal(100, 20))))
    return window_of(rows, lookback=n, tf=tf)


VECTORISED = [s for s in FEATURES if s.vectorised is not None]


@pytest.mark.parametrize("spec", VECTORISED, ids=[s.name for s in VECTORISED])
def test_vectorised_and_scalar_paths_agree_on_every_bar(spec: FeatureSpec):
    lookback = 24
    w = _fixture_window()
    series = spec.vectorised(w, lookback)
    assert series.size == w.size

    checked = 0
    for i in range(w.size):
        sub = w.upto(i)
        if sub.size < lookback + spec.extra_bars:
            assert not np.isfinite(series[i]), f"{spec.name}: series has a value at bar {i} but the " \
                                               "scalar path could not form a window there"
            continue
        try:
            scalar = spec.compute(ctx_for(sub, lookback=lookback))
        except Degenerate:
            assert not np.isfinite(series[i]), f"{spec.name}: scalar failed at {i}, series did not"
            continue
        assert np.isfinite(series[i]), f"{spec.name}: scalar produced {scalar} at bar {i}, series NaN"
        assert series[i] == pytest.approx(float(scalar), rel=1e-9, abs=1e-12), \
            f"{spec.name} disagrees at bar {i}: series={series[i]!r} scalar={scalar!r}"
        checked += 1
    assert checked > 200, f"{spec.name}: only {checked} bars actually compared"


def test_every_registered_feature_declares_a_version_and_a_family():
    for s in FEATURES:
        assert s.version.count(".") == 2, f"{s.name} version {s.version} is not semver"
        assert s.family >= 1 and s.tier in ("A", "B")
        assert s.description, f"{s.name} has no description"


# ---- the engine --------------------------------------------------------------------------

def make_snapshot(n_1h=800, *, asset="B-BTC_USDT", seed=5, holes=(), flat=False):
    """A snapshot carrying one 1h series — enough to exercise the engine.

    ``flat`` makes every bar a single price, which is the degenerate case the volatility guards
    exist for.
    """
    from snapshot.models import MarketSnapshot
    rng = np.random.default_rng(seed)
    closes = (np.full(n_1h, 100.0) if flat
              else 100 * np.exp(np.cumsum(rng.normal(0.0001, 0.006, n_1h))))
    bars = []
    for i, c in enumerate(closes):
        if i in holes:
            continue
        o = closes[i - 1] if i else c
        hi, lo = (c, c) if flat else (max(o, c) * 1.002, min(o, c) * 0.998)
        bars.append(Bar(asset=asset, product=Product.FUTURES, timeframe=Timeframe.H1,
                        open_time_ms=T0 + i * H, open=str(o), high=str(hi),
                        low=str(lo), close=str(c), volume_base="100",
                        is_final=True, fetched_at_ms=T0))
    key = SeriesKey(asset, Product.FUTURES, Timeframe.H1)
    st = SeriesState(key=key, bars=tuple(bars), requested=n_1h, min_required=1, ok=True)
    return MarketSnapshot(as_of_ms=T0 + n_1h * H, code_version="test", parameter_hash="ph",
                          series={key: st})


def test_the_engine_computes_the_registered_1h_features_and_reports_statuses():
    engine = engine_from_registry(load_registry())
    fs = engine.compute(make_snapshot(n_1h=700), "B-BTC_USDT", Product.FUTURES,
                        timeframes=[Timeframe.H1])
    assert len(fs) == len([k for s in FEATURES for k in s.keys([Timeframe.H1])])
    # 700 bars: short (24) and medium (168) are warm; long needs 721, so it is still WARMING and
    # says how much longer -- a state the dashboard shows rather than an error
    assert fs.is_ok("VOL_CC", Timeframe.H1, "short")
    assert fs.is_ok("VOL_CC", Timeframe.H1, "medium")
    warming = fs.get("TRD_RET", Timeframe.H1, "long")
    assert warming.status is FeatureStatus.WARMING
    assert warming.reason == "need 721 1h bars, have 700"
    assert warming.detail["hours_to_warm"] == 21.0
    assert fs.ok_count < len(fs) and fs.status_counts()["WARMING"] > 0

    # every OK value carries the window it used, so an audit can reconstruct it
    fv = fs.get("VOL_CC", Timeframe.H1, "short")
    assert fv.bars_used == 25 and fv.lookback == 24
    assert fv.window_end_ms == fs.as_of_ms
    assert fv.version == "1.0.0" and fv.computed_at_ms == fs.as_of_ms


def test_a_failing_dependency_is_propagated_by_name_not_recomputed():
    """A flat series makes VOL_CC degenerate; VOL_RATIO must say *which* input failed and why,
    rather than reporting its own division as the problem."""
    engine = engine_from_registry(load_registry())
    fs = engine.compute(make_snapshot(n_1h=800, flat=True), "B-BTC_USDT", Product.FUTURES,
                        timeframes=[Timeframe.H1])
    assert fs.status("VOL_CC", Timeframe.H1, "short") is FeatureStatus.NUMERICAL_FAILURE
    fv = fs.get("VOL_RATIO", Timeframe.H1)
    assert fv.status is FeatureStatus.NUMERICAL_FAILURE
    assert "VOL_CC@1h/short" in fv.reason
    assert fv.detail["dependency"] == "VOL_CC@1h/short"
    assert fv.value is None


def test_a_warming_dependency_keeps_the_warming_status_rather_than_becoming_an_error():
    engine = engine_from_registry(load_registry())
    fs = engine.compute(make_snapshot(n_1h=300), "B-BTC_USDT", Product.FUTURES,
                        timeframes=[Timeframe.H1])
    fv = fs.get("VOL_RATIO", Timeframe.H1)
    assert fv.status is FeatureStatus.WARMING       # time will fix this; nothing is broken
    assert "721" in fv.reason


def test_a_gap_disables_only_the_windows_that_contain_it():
    engine = engine_from_registry(load_registry())
    fs = engine.compute(make_snapshot(n_1h=800, holes=(700,)), "B-BTC_USDT", Product.FUTURES,
                        timeframes=[Timeframe.H1])
    # the hole is 100 bars back: inside the 168-bar medium window, outside the 24-bar short one
    assert fs.status("VOL_CC", Timeframe.H1, "medium") is FeatureStatus.GAP_IN_WINDOW
    assert fs.is_ok("VOL_CC", Timeframe.H1, "short")


def test_the_cache_is_keyed_by_version_and_parameter_hash():
    engine = engine_from_registry(load_registry())
    snap = make_snapshot()
    engine.compute(snap, "B-BTC_USDT", Product.FUTURES, timeframes=[Timeframe.H1])
    first = engine.stats()
    engine.compute(snap, "B-BTC_USDT", Product.FUTURES, timeframes=[Timeframe.H1])
    second = engine.stats()
    assert second["hits"] > first["hits"] and second["misses"] == first["misses"]

    other = FeatureEngine(engine.params, parameter_hash="different-hash")
    other.compute(snap, "B-BTC_USDT", Product.FUTURES, timeframes=[Timeframe.H1])
    assert other.stats()["hits"] == 0            # a different parameter hash shares nothing


def test_the_engine_refuses_a_snapshot_request_too_shallow_for_its_features():
    from core.errors import StartupError
    from snapshot import SeriesRequest, SnapshotRequest

    engine = engine_from_registry(load_registry())
    shallow = SnapshotRequest(series=(SeriesRequest(KEY, bars=100),))
    with pytest.raises(StartupError) as ei:
        engine.assert_request_sufficient(shallow)
    assert "cannot support the registered features" in ei.value.message
    assert any("needs 721" in p or "needs 181" in p or "needs 91" in p
               for p in ei.value.context["problems"])
    assert "deepen the SnapshotRequest" in ei.value.remediation


def test_a_missing_series_is_reported_per_feature_rather_than_crashing_the_pass():
    engine = engine_from_registry(load_registry())
    fs = engine.compute(make_snapshot(), "B-BTC_USDT", Product.FUTURES, timeframes=[Timeframe.D1])
    assert len(fs) > 0
    fv = fs.get("VOL_CC", Timeframe.D1, "short")
    assert fv.status is FeatureStatus.DQ_FAILED and "not in the snapshot" in fv.reason


def test_the_lookbacks_come_from_the_registry_not_from_constants():
    assert lookback_for(PARAMS, Timeframe.H1, "short") == 24
    assert lookback_for(PARAMS, Timeframe.H1, "long") == 720
    assert lookback_for(PARAMS, Timeframe.D1, "medium") == 30
    with pytest.raises(Exception, match="no registered lookback"):
        lookback_for(PARAMS, Timeframe.M5, "short")


def test_an_uncalibrated_parameter_is_reported_as_unset_not_as_a_data_problem():
    """``PARAMETER_UNSET`` is its own status, and that distinction is the point.

    "nobody has calibrated this yet" and "the data is bad" lead to opposite actions, so conflating
    them would send someone to check the feed over a configuration gap. The engine is given an
    explicit ``unset_params`` here rather than reading the live registry, which now has none —
    PHASE-27 calibrated the last one — because the behaviour under test is the engine's, not the
    registry's current contents.
    """
    engine = FeatureEngine(PARAMS, parameter_hash="ph",
                           unset_params=frozenset({"swing_atr_multiple"}))
    assert "swing_atr_multiple" in engine.unset_params
    features = engine.compute(make_snapshot(), "B-BTC_USDT", Product.FUTURES)
    swing = features.get("STR_SWING", Timeframe.H1, "short")
    assert swing.status is FeatureStatus.PARAMETER_UNSET
    assert swing.value is None
    assert "swing_atr_multiple" in (swing.reason or "")
    # Not a data-quality failure: a reader triaging this must not be sent to check the feed.
    assert swing.status is not FeatureStatus.DQ_FAILED


def test_the_registry_is_internally_consistent():
    names = [s.name for s in FEATURES]
    assert len(names) == len(set(names))
    for spec in FEATURES:
        for dep_name, dep_horizon in spec.depends_on:
            dep = BY_NAME[dep_name]
            assert dep_horizon in dep.horizons
            assert FEATURES.index(dep) < FEATURES.index(spec)
