"""PHASE-12 — Families 5 (cross-asset) and 9 (volume).

The cross-asset tests matter because their failure mode is invisible: a correlation between two
series that cover *different bars* is a well-formed number that measures the offset instead of the
relationship. That is the same error F-023 found in the spot/futures basis, one layer up.
"""
import numpy as np
import pytest

from config.parameters import load_registry
from features.engine import FeatureEngine
from features.f5_cross import BENCHMARK, UNIVERSE, x_disp
from features.f9_volume import MAD_TO_SIGMA, vlm_trend, vlm_zrobust
from features.numerics import Degenerate
from features.spec import ComputeContext
from features.value import FeatureStatus
from features.windows import extract
from normalize.canonical import Bar, Product, Timeframe
from snapshot.models import MarketSnapshot, SeriesKey, SeriesState

H = 3_600_000
T0 = 1767225600000
PARAMS = {p.name: p.value for p in load_registry().parameters}
ETH, SOL = "B-ETH_USDT", "B-SOL_USDT"


def bars_for(asset, closes, *, skip=(), volumes=None, tf=Timeframe.H1):
    out = []
    for i, c in enumerate(closes):
        if i in skip:
            continue
        v = volumes[i] if volumes is not None else 100.0
        out.append(Bar(asset=asset, product=Product.FUTURES, timeframe=tf, open_time_ms=T0 + i * tf.ms,
                       open=str(c), high=str(c * 1.001), low=str(c * 0.999), close=str(c),
                       volume_base=str(v), is_final=True, fetched_at_ms=T0))
    return out


def state_for(asset, bars, tf=Timeframe.H1):
    key = SeriesKey(asset, Product.FUTURES, tf)
    return key, SeriesState(key=key, bars=tuple(bars), requested=len(bars), min_required=1, ok=True)


def snapshot_of(series_bars, *, n):
    series = dict(state_for(a, b) for a, b in series_bars.items())
    return MarketSnapshot(as_of_ms=T0 + n * H, code_version="test", parameter_hash="ph",
                          series=series)


def window_of(asset, bars, *, lookback, extra_bars=1):
    _, st = state_for(asset, bars)
    out = extract(st, lookback=lookback, extra_bars=extra_bars)
    assert out.ok, out.reason
    return out.window


def provider_for(windows):
    """A window provider over a fixed {SeriesKey: Window} map, for direct-call tests."""
    from features.value import FeatureStatus as _S
    from features.windows import WindowOutcome

    def provide(key, lookback, extra_bars):
        w = windows.get(key)
        if w is None:
            return WindowOutcome(None, _S.DQ_FAILED, f"series {key} is not in this snapshot")
        need = lookback + extra_bars
        if w.size < need:
            return WindowOutcome(None, _S.WARMING,
                                 f"need {need} bars, have {w.size}", bars_available=w.size)
        return WindowOutcome(w.tail(need), _S.OK, bars_available=w.size)

    return provide


def ctx_for(asset, w, *, lookback, others=None, computed=None, horizon="short"):
    return ComputeContext(asset=asset, product=Product.FUTURES, timeframe=Timeframe.H1,
                          horizon=horizon, lookback=lookback, window=w, params=PARAMS,
                          window_provider=provider_for(others or {}), computed=computed or {})


def geometric(n, *, mu=0.0, sigma=0.01, seed=1, start=100.0):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(mu, sigma, n)))


# ---- Family 5 ----------------------------------------------------------------------------

def test_beta_recovers_a_constructed_relationship():
    """ETH returns built as 1.5x BTC's plus small noise must produce beta near 1.5."""
    rng = np.random.default_rng(3)
    btc_r = rng.normal(0, 0.01, 400)
    eth_r = 1.5 * btc_r + rng.normal(0, 0.0005, 400)
    btc = 100 * np.exp(np.cumsum(btc_r))
    eth = 200 * np.exp(np.cumsum(eth_r))

    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    snap = snapshot_of({BENCHMARK: bars_for(BENCHMARK, btc), ETH: bars_for(ETH, eth)}, n=400)
    fs = engine.compute(snap, ETH, Product.FUTURES, timeframes=[Timeframe.H1])
    assert fs.value("X_BETA", Timeframe.H1, "medium") == pytest.approx(1.5, abs=0.05)
    assert fs.value("X_CORR", Timeframe.H1, "medium") > 0.95


def test_the_benchmark_correlates_one_with_itself_by_definition_not_by_measurement():
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    snap = snapshot_of({BENCHMARK: bars_for(BENCHMARK, geometric(400))}, n=400)
    fs = engine.compute(snap, BENCHMARK, Product.FUTURES, timeframes=[Timeframe.H1])
    assert fs.value("X_CORR", Timeframe.H1, "medium") == 1.0
    assert fs.value("X_BETA", Timeframe.H1, "medium") == 1.0
    assert fs.get("X_CORR", Timeframe.H1, "medium").detail["self"] is True
    assert fs.value("X_RS", Timeframe.H1, "medium") == 0.0


def test_a_flat_benchmark_fails_rather_than_reporting_a_correlation():
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    snap = snapshot_of({BENCHMARK: bars_for(BENCHMARK, np.full(400, 100.0)),
                        ETH: bars_for(ETH, geometric(400))}, n=400)
    fs = engine.compute(snap, ETH, Product.FUTURES, timeframes=[Timeframe.H1])
    fv = fs.get("X_CORR", Timeframe.H1, "medium")
    assert fv.status is FeatureStatus.NUMERICAL_FAILURE
    assert fv.detail["guard"] in ("ZERO_STDDEV", "ZERO_VARIANCE")


def test_series_covering_different_bars_are_refused_not_correlated_by_position():
    """The trap: both windows hold 169 bars, but ETH's are shifted by the hole. Correlating them by
    position would measure the offset. Alignment is by timestamp, so it refuses."""
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    btc = geometric(400, seed=2)
    eth = geometric(400, seed=9)
    # ETH is missing a long contiguous stretch in the middle of the medium window
    holes = set(range(230, 260))
    snap = snapshot_of({BENCHMARK: bars_for(BENCHMARK, btc),
                        ETH: bars_for(ETH, eth, skip=holes)}, n=400)
    fs = engine.compute(snap, ETH, Product.FUTURES, timeframes=[Timeframe.H1])
    fv = fs.get("X_CORR", Timeframe.H1, "medium")
    assert fv.status is FeatureStatus.GAP_IN_WINDOW        # the hole is caught before arithmetic
    assert fv.value is None


def test_an_absent_benchmark_names_the_series_it_needed():
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    snap = snapshot_of({ETH: bars_for(ETH, geometric(400))}, n=400)
    fs = engine.compute(snap, ETH, Product.FUTURES, timeframes=[Timeframe.H1])
    for name in ("X_CORR", "X_BETA", "X_DISP"):
        fv = fs.get(name, Timeframe.H1, "medium")
        assert fv.status is FeatureStatus.DQ_FAILED
        assert BENCHMARK in fv.reason or "B-BTC_USDT" in str(fv.detail)


def test_relative_strength_is_zero_when_the_asset_moves_exactly_its_beta():
    """The point of beta-adjusting: a high-beta asset that moved exactly its beta is not 'strong'."""
    rng = np.random.default_rng(11)
    btc_r = rng.normal(0.001, 0.01, 400)
    eth_r = 2.0 * btc_r                                   # exactly twice, no idiosyncratic move
    snap = snapshot_of({BENCHMARK: bars_for(BENCHMARK, 100 * np.exp(np.cumsum(btc_r))),
                        ETH: bars_for(ETH, 100 * np.exp(np.cumsum(eth_r)))}, n=400)
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    fs = engine.compute(snap, ETH, Product.FUTURES, timeframes=[Timeframe.H1])
    assert fs.value("X_BETA", Timeframe.H1, "medium") == pytest.approx(2.0, abs=1e-6)
    assert fs.value("X_RS", Timeframe.H1, "medium") == pytest.approx(0.0, abs=1e-9)
    detail = fs.get("X_RS", Timeframe.H1, "medium").detail
    assert detail["own_return"] == pytest.approx(2 * detail["benchmark_return"], rel=1e-9)


def test_relative_strength_is_positive_for_a_genuine_outperformance():
    rng = np.random.default_rng(12)
    btc_r = rng.normal(0.0, 0.01, 400)
    eth_r = 1.0 * btc_r + 0.002                           # a steady idiosyncratic drift
    snap = snapshot_of({BENCHMARK: bars_for(BENCHMARK, 100 * np.exp(np.cumsum(btc_r))),
                        ETH: bars_for(ETH, 100 * np.exp(np.cumsum(eth_r)))}, n=400)
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    fs = engine.compute(snap, ETH, Product.FUTURES, timeframes=[Timeframe.H1])
    assert fs.value("X_RS", Timeframe.H1, "medium") > 0.1


def test_dispersion_is_hand_computable_and_identical_across_the_universe():
    n = 200
    closes = {BENCHMARK: np.full(n, 100.0) * np.exp(np.linspace(0, 0.10, n)),
              ETH: np.full(n, 100.0) * np.exp(np.linspace(0, 0.20, n)),
              SOL: np.full(n, 100.0) * np.exp(np.linspace(0, 0.30, n))}
    snap = snapshot_of({a: bars_for(a, c) for a, c in closes.items()}, n=n)
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    values = [engine.compute(snap, a, Product.FUTURES, timeframes=[Timeframe.H1])
              .value("X_DISP", Timeframe.H1, "short") for a in UNIVERSE]
    assert values[0] == pytest.approx(values[1]) == pytest.approx(values[2])

    lookback = 24
    rets = [float(np.log(c[-1] / c[-(lookback + 1)])) for c in closes.values()]
    assert values[0] == pytest.approx(float(np.std(np.array(rets), ddof=1)))


def test_dispersion_of_three_identical_series_fails_rather_than_returning_zero():
    n = 200
    same = geometric(n, seed=5)
    snap = snapshot_of({a: bars_for(a, same) for a in UNIVERSE}, n=n)
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    fv = engine.compute(snap, ETH, Product.FUTURES, timeframes=[Timeframe.H1]) \
        .get("X_DISP", Timeframe.H1, "short")
    assert fv.status is FeatureStatus.NUMERICAL_FAILURE and fv.detail["guard"] == "ZERO_STDDEV"


def test_dispersion_refuses_when_one_universe_member_is_too_short():
    """The refusal names which series was too short and by how much, rather than reporting the
    dispersion of the members that happened to be long enough."""
    from features.spec import DependencyNotOK
    w = window_of(ETH, bars_for(ETH, geometric(60)), lookback=24)
    others = {SeriesKey(BENCHMARK, Product.FUTURES, Timeframe.H1):
              window_of(BENCHMARK, bars_for(BENCHMARK, geometric(10, seed=3)), lookback=8)}
    with pytest.raises(DependencyNotOK) as ei:
        x_disp(ctx_for(ETH, w, lookback=24, others=others))
    assert ei.value.status is FeatureStatus.WARMING
    assert BENCHMARK in str(ei.value) and "need 25 bars, have 9" in str(ei.value)


# ---- Family 9 ----------------------------------------------------------------------------

def test_vlm_zrobust_is_hand_computable():
    vols = [10.0, 12.0, 11.0, 50.0, 13.0]
    w = window_of(BENCHMARK, bars_for(BENCHMARK, [100] * 5, volumes=vols), lookback=5, extra_bars=0)
    arr = np.array(vols)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    expected = (vols[-1] - median) / (MAD_TO_SIGMA * mad)
    got = vlm_zrobust(ctx_for(BENCHMARK, w, lookback=5))
    assert got == pytest.approx(expected)
    assert MAD_TO_SIGMA == 1.4826


def test_a_volume_spike_is_large_under_the_robust_score_and_not_swamped_by_itself():
    """The reason specs/02 chose median/MAD: a mean/std score is dominated by the spike it is meant
    to measure, and reports it as barely two sigma."""
    rng = np.random.default_rng(21)
    vols = list(10.0 + rng.normal(0, 1.0, 43)) + [1000.0]     # ordinary volume, then a 100x spike
    w = window_of(BENCHMARK, bars_for(BENCHMARK, [100] * len(vols), volumes=vols),
                  lookback=len(vols), extra_bars=0)
    robust = vlm_zrobust(ctx_for(BENCHMARK, w, lookback=len(vols)))
    arr = np.array(vols)
    classical = (arr[-1] - arr.mean()) / arr.std(ddof=1)
    assert robust > 100, robust          # the spike is enormous on the robust scale
    assert classical < 7, classical      # and barely notable on the classical one


def test_vlm_zrobust_refuses_when_half_the_window_is_identical():
    vols = [10.0] * 30
    w = window_of(BENCHMARK, bars_for(BENCHMARK, [100] * 30, volumes=vols), lookback=30, extra_bars=0)
    with pytest.raises(Degenerate) as ei:
        vlm_zrobust(ctx_for(BENCHMARK, w, lookback=30))
    assert ei.value.reason == "ZERO_MAD"
    assert "half the window is identical" in ei.value.detail


def test_vlm_trend_recovers_the_sign_of_a_volume_trend():
    # noise on purpose: a perfectly exponential ramp is a straight line in logs, which the
    # ZERO_RESIDUAL_VARIANCE guard correctly refuses
    rng = np.random.default_rng(31)
    rising = list(np.exp(np.linspace(np.log(10), np.log(1000), 50) + rng.normal(0, 0.05, 50)))
    w = window_of(BENCHMARK, bars_for(BENCHMARK, [100] * 50, volumes=rising), lookback=50,
                  extra_bars=0)
    assert vlm_trend(ctx_for(BENCHMARK, w, lookback=50)) > 0
    falling = rising[::-1]
    w2 = window_of(BENCHMARK, bars_for(BENCHMARK, [100] * 50, volumes=falling), lookback=50,
                   extra_bars=0)
    assert vlm_trend(ctx_for(BENCHMARK, w2, lookback=50)) < 0


def test_vlm_trend_refuses_a_zero_volume_bar_rather_than_adding_an_epsilon():
    """A synthesised 1m no-trade bar has volume 0 (specs/12 §6.2); ln(0) is undefined, and an epsilon
    would make the slope an artefact of the epsilon."""
    vols = [10.0] * 20 + [0.0] + [10.0] * 9
    w = window_of(BENCHMARK, bars_for(BENCHMARK, [100] * 30, volumes=vols), lookback=30, extra_bars=0)
    with pytest.raises(Degenerate) as ei:
        vlm_trend(ctx_for(BENCHMARK, w, lookback=30))
    assert ei.value.reason == "NON_POSITIVE_VOLUME"
    assert "artefact of the epsilon" in ei.value.detail


def test_the_aggressor_feature_is_deliberately_not_registered():
    """VLM_AGGR needs the full tape; F-011 measured a fixed 30 rows per request regardless of limit,
    so a per-bar buy/sell split would be a reconstruction presented as a measurement."""
    from features.registry import BY_NAME
    assert "VLM_AGGR" not in BY_NAME
    import features.f9_volume as f9
    assert "VLM_AGGR" in f9.__doc__ and "not registered" in f9.__doc__


def test_volume_features_carry_the_synthesised_bar_count():
    vols = [10.0, 12.0, 14.0, 11.0, 13.0]
    w = window_of(BENCHMARK, bars_for(BENCHMARK, [100] * 5, volumes=vols), lookback=5, extra_bars=0)
    c = ctx_for(BENCHMARK, w, lookback=5)
    vlm_zrobust(c)
    assert "synthesised_bars" in c.detail
