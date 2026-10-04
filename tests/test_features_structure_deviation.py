"""PHASE-11 — Families 3 (structure) and 4 (deviation / mean reversion).

Family 3 ships **dormant**: ``swing_atr_multiple`` has never been calibrated, so ``STR_SWING`` and
``STR_SEQ`` report ``PARAMETER_UNSET`` rather than using the placeholder 0.0, which would make every
local extreme a pivot. These tests cover both states — dormant with the registry's real parameters,
and live with a calibrated theta — because the strategies in specs/03-06 depend on the live one.
"""
import numpy as np
import pytest

from config.parameters import load_registry
from features.engine import FeatureEngine
from features.f3_structure import find_pivots, str_rangepos
from features.f4_deviation import PRIMARY_VR, dev_atr, dev_vwap, dev_z, mr_ac1
from features.numerics import Degenerate
from features.spec import ComputeContext, NotApplicable
from features.value import FeatureStatus
from features.windows import extract
from normalize.canonical import Bar, Product, Timeframe
from snapshot.models import MarketSnapshot, SeriesKey, SeriesState

H = 3_600_000
DAY = 86_400_000
T0 = 1767225600000          # 2026-01-01T00:00:00Z
ASSET = "B-BTC_USDT"
REG = load_registry()
PARAMS = {p.name: p.value for p in REG.parameters}
#: The same parameters with theta calibrated, for the tests that need live structure.
CALIBRATED = {**PARAMS, "swing_atr_multiple": 0.5}


def mkbar(i, o, h, low, c, v=10, tf=Timeframe.H1, asset=ASSET):
    return Bar(asset=asset, product=Product.FUTURES, timeframe=tf, open_time_ms=T0 + i * tf.ms,
               open=str(o), high=str(h), low=str(low), close=str(c), volume_base=str(v),
               is_final=True, fetched_at_ms=T0)


def series_state(bars, tf=Timeframe.H1, asset=ASSET):
    key = SeriesKey(asset, Product.FUTURES, tf)
    return SeriesState(key=key, bars=tuple(bars), requested=len(bars), min_required=1, ok=True)


def window(bars, *, lookback, extra_bars=0, tf=Timeframe.H1):
    out = extract(series_state(bars, tf), lookback=lookback, extra_bars=extra_bars)
    assert out.ok, out.reason
    return out.window


def ctx(w, *, lookback, horizon="short", params=CALIBRATED, computed=None, windows=None,
        tf=Timeframe.H1):
    from features.windows import WindowOutcome

    def provide(key, lb, extra):
        other = (windows or {}).get(key)
        if other is None:
            return WindowOutcome(None, FeatureStatus.DQ_FAILED, f"series {key} is absent")
        return WindowOutcome(other, FeatureStatus.OK, bars_available=other.size)

    return ComputeContext(asset=ASSET, product=Product.FUTURES, timeframe=tf, horizon=horizon,
                          lookback=lookback, window=w, params=params,
                          computed=computed or {}, window_provider=provide)


def with_atr(value, horizon="short", tf=Timeframe.H1):
    """A computed-values map containing just an OK ATR, for features that depend on it."""
    from features.value import FeatureKey, FeatureValue
    fv = FeatureValue(name="ATR", version="1.0.0", status=FeatureStatus.OK, value=value,
                      timeframe=tf, horizon=horizon)
    return {FeatureKey("ATR", tf, horizon): fv}


# ---- pivots ------------------------------------------------------------------------------

def zigzag(peaks=(100, 120, 105, 130, 110), *, legs=6, start=0):
    """A clean zigzag of flat bars, each at one price.

    Flat bars on purpose: if a bar's high were carried from the previous close, the bar *after* a peak
    would share the peak's high and the strict-inequality pivot test would never fire. Real bars do
    overlap that way, which is why the confirmation window `k` exists -- but a fixture meant to prove
    pivot detection should isolate it.
    """
    prices = []
    for a, b in zip(peaks, peaks[1:]):
        prices.extend(np.linspace(a, b, legs + 1)[:-1])
    prices.append(peaks[-1])
    return [mkbar(start + i, p, p, p, p) for i, p in enumerate(prices)]


def test_a_zigzag_produces_alternating_confirmed_pivots():
    bars = zigzag()
    w = window(bars, lookback=len(bars))
    pivots = find_pivots(w, k=2, theta=0.5, atr=3.0)
    kinds = [p.is_high for p in pivots]
    assert len(pivots) >= 3
    assert kinds == sorted(kinds, key=lambda _: 0)          # no assertion on order beyond alternation
    assert all(kinds[i] != kinds[i + 1] for i in range(len(kinds) - 1)), kinds
    highs = [p.price for p in pivots if p.is_high]
    lows = [p.price for p in pivots if not p.is_high]
    assert max(highs) == pytest.approx(120, abs=5) or max(highs) == pytest.approx(130, abs=5)
    assert min(lows) == pytest.approx(105, abs=6)


def test_the_last_k_bars_can_never_contain_a_pivot_because_structure_lags():
    bars = zigzag()
    w = window(bars, lookback=len(bars))
    k = 3
    pivots = find_pivots(w, k=k, theta=0.5, atr=3.0)
    assert all(p.index <= w.size - 1 - k for p in pivots)


def test_a_higher_theta_admits_fewer_pivots_and_zero_admits_noise():
    bars = zigzag()
    w = window(bars, lookback=len(bars))
    loose = find_pivots(w, k=2, theta=0.0, atr=3.0)
    strict = find_pivots(w, k=2, theta=4.0, atr=3.0)
    assert len(loose) >= len(strict)
    assert len(strict) < len(loose) or len(loose) == 0


def test_pivots_cannot_be_found_without_a_positive_atr():
    bars = zigzag()
    w = window(bars, lookback=len(bars))
    with pytest.raises(Degenerate, match="ZERO_VARIANCE"):
        find_pivots(w, k=2, theta=0.5, atr=0.0)


def test_touch_counts_need_no_tolerance_parameter():
    """A bar touches a level when its own range contains it — unambiguous, and no 31st parameter."""
    base = zigzag()
    bars = base + [mkbar(len(base) + i, 120, 121, 119, 120) for i in range(4)]   # revisit ~120
    w = window(bars, lookback=len(bars))
    pivots = find_pivots(w, k=2, theta=0.3, atr=3.0)
    assert any(p.touches > 0 for p in pivots)


# ---- Family 3 through the engine ---------------------------------------------------------

def make_snapshot(n=400, *, daily=0, asset=ASSET, seed=4):
    """``daily`` is capped to the span the hourly series covers: a hand-built SeriesState has no
    ``as_of`` filtering, so a longer daily series would place bars after as_of -- which the real
    snapshot builder makes impossible."""
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    # Independent per-bar excursions, not a fixed multiple of max(open, close): with a fixed multiple
    # the bar after a local maximum inherits the same high, no bar is ever strictly higher than its
    # neighbours, and pivot detection finds nothing -- an artefact of the fixture, not the detector
    # (verified against real BTC history, which yields 31 pivots over 168 bars at theta=0.5).
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.004)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.004)))
        bars.append(mkbar(i, o, hi, lo, c, asset=asset))
    series = {SeriesKey(asset, Product.FUTURES, Timeframe.H1): series_state(bars, asset=asset)}
    if daily:
        daily = min(daily, max(3, n * H // DAY))
        dc = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, daily)))
        dbars = [Bar(asset=asset, product=Product.FUTURES, timeframe=Timeframe.D1,
                     open_time_ms=T0 + i * DAY, open=str(dc[i - 1] if i else c),
                     high=str(max(dc[i - 1] if i else c, c) * 1.01),
                     low=str(min(dc[i - 1] if i else c, c) * 0.99), close=str(c),
                     volume_base="1000", is_final=True, fetched_at_ms=T0)
                 for i, c in enumerate(dc)]
        series[SeriesKey(asset, Product.FUTURES, Timeframe.D1)] = series_state(dbars, Timeframe.D1,
                                                                              asset=asset)
    as_of = T0 + n * H
    return MarketSnapshot(as_of_ms=as_of, code_version="test", parameter_hash="ph", series=series)


def test_the_structure_family_is_dormant_until_theta_is_calibrated():
    engine = FeatureEngine(PARAMS, parameter_hash="ph",
                           unset_params=frozenset({"swing_atr_multiple"}))
    fs = engine.compute(make_snapshot(), ASSET, Product.FUTURES, timeframes=[Timeframe.H1])
    for name in ("STR_SWING", "STR_SEQ"):
        fv = fs.get(name, Timeframe.H1, "short")
        assert fv.status is FeatureStatus.PARAMETER_UNSET
        assert "never been calibrated" in fv.reason
        assert fv.detail["parameter"] == "swing_atr_multiple"
        assert fv.value is None and fv.label is None
    # a feature that does not need theta is unaffected
    assert fs.is_ok("STR_RANGEPOS", Timeframe.H1, "short")


def test_with_theta_calibrated_the_swing_levels_are_published_in_detail():
    engine = FeatureEngine(CALIBRATED, parameter_hash="ph")
    fs = engine.compute(make_snapshot(), ASSET, Product.FUTURES, timeframes=[Timeframe.H1])
    fv = fs.get("STR_SWING", Timeframe.H1, "medium")
    assert fv.status is FeatureStatus.OK, fv.reason
    d = fv.detail
    assert d["pivots_confirmed"] >= 1
    assert d["confirmation_lag_bars"] == 3                 # k, from the registry
    assert d["atr_used"] > 0
    assert fv.value == pytest.approx(abs(d["close"] - d["nearest_level"]) / d["atr_used"])
    assert (d["last_swing_high"] is not None) or (d["last_swing_low"] is not None)


def test_str_seq_is_one_of_the_declared_labels_or_explicitly_undefined():
    from features.f3_structure import SEQ_LABELS
    engine = FeatureEngine(CALIBRATED, parameter_hash="ph")
    fs = engine.compute(make_snapshot(), ASSET, Product.FUTURES, timeframes=[Timeframe.H1])
    fv = fs.get("STR_SEQ", Timeframe.H1, "medium")
    assert fv.status is FeatureStatus.OK, fv.reason
    assert fv.label in SEQ_LABELS and fv.value is None
    assert len(fv.detail["highs"]) == 2 and len(fv.detail["lows"]) == 2


def test_str_seq_labels_trend_structure_from_hand_built_pivots():
    from features.f3_structure import str_seq
    # rising peaks (120, 135) and rising troughs (110, 125): HH-HL
    bars = zigzag(peaks=(100, 120, 110, 135, 125, 150), legs=5)
    w = window(bars, lookback=len(bars))
    assert str_seq(ctx(w, lookback=w.size, computed=with_atr(4.0))) == "HH-HL"
    # falling peaks and falling troughs: LH-LL
    bars2 = zigzag(peaks=(150, 125, 135, 110, 120, 100), legs=5)
    w2 = window(bars2, lookback=len(bars2))
    assert str_seq(ctx(w2, lookback=w2.size, computed=with_atr(4.0))) == "LH-LL"
    # rising peaks (130, 150) with falling troughs (95, 80): broadening. The trailing leg back up
    # is what confirms the second low -- a pivot at the window edge is never confirmed.
    bars3 = zigzag(peaks=(100, 130, 95, 150, 80, 120), legs=5)
    w3 = window(bars3, lookback=len(bars3))
    assert str_seq(ctx(w3, lookback=w3.size, computed=with_atr(4.0))) == "HH-LL"


def test_no_qualifying_pivot_is_not_applicable_rather_than_a_failure():
    from features.f3_structure import str_swing
    quiet = [mkbar(i, 100, 100.5, 99.5, 100) for i in range(40)]     # nothing stands out
    w = window(quiet, lookback=40)
    with pytest.raises(NotApplicable) as ei:
        str_swing(ctx(w, lookback=40, computed=with_atr(1.0)))
    assert "no confirmed structure" in ei.value.reason


def test_str_rangepos_is_hand_computable_and_bounded():
    bars = [mkbar(i, 100, 110, 90, c) for i, c in enumerate((100, 105, 95, 100))]
    w = window(bars, lookback=4)
    assert str_rangepos(ctx(w, lookback=4)) == pytest.approx((100 - 90) / (110 - 90))
    flat = [mkbar(i, 100, 100, 100, 100) for i in range(5)]
    with pytest.raises(Degenerate, match="ZERO_VARIANCE"):
        str_rangepos(ctx(window(flat, lookback=5), lookback=5))


def test_str_dist_pdh_uses_the_stored_daily_bar_not_a_resampled_one():
    engine = FeatureEngine(CALIBRATED, parameter_hash="ph")
    snap = make_snapshot(n=400, daily=20)
    fs = engine.compute(snap, ASSET, Product.FUTURES, timeframes=[Timeframe.H1])
    fv = fs.get("STR_DIST_PDH", Timeframe.H1, "short")
    assert fv.status is FeatureStatus.OK
    d = fv.detail
    daily = snap.series[SeriesKey(ASSET, Product.FUTURES, Timeframe.D1)]
    stored = {b.open_time_ms: (float(b.high), float(b.low)) for b in daily.bars}
    assert d["prior_day_ms"] in stored
    assert (d["prior_high"], d["prior_low"]) == stored[d["prior_day_ms"]]
    assert d["prior_day_ms"] < (snap.as_of_ms // DAY) * DAY      # strictly a prior day
    assert fv.value == pytest.approx((d["close"] - d["prior_high"]) / d["atr_used"])
    assert d["dist_low_atr"] > d["dist_high_atr"]                # the low is below the high


def test_str_dist_pdh_without_a_daily_series_says_which_series_is_missing():
    engine = FeatureEngine(CALIBRATED, parameter_hash="ph")
    fs = engine.compute(make_snapshot(n=400, daily=0), ASSET, Product.FUTURES,
                        timeframes=[Timeframe.H1])
    fv = fs.get("STR_DIST_PDH", Timeframe.H1, "short")
    assert fv.status is FeatureStatus.DQ_FAILED
    assert "1d" in fv.reason and ASSET in fv.reason


# ---- Family 4 ----------------------------------------------------------------------------

def test_dev_z_is_hand_computable():
    closes = [100, 102, 98, 104, 106]
    bars = [mkbar(i, c, c, c, c) for i, c in enumerate(closes)]
    w = window(bars, lookback=5)
    arr = np.array(closes, dtype=float)
    expected = (arr[-1] - arr.mean()) / arr.std(ddof=1)
    assert dev_z(ctx(w, lookback=5)) == pytest.approx(expected)


def test_dev_atr_expresses_the_same_deviation_in_atr_units():
    closes = [100, 102, 98, 104, 106]
    bars = [mkbar(i, c, c + 1, c - 1, c) for i, c in enumerate(closes)]
    w = window(bars, lookback=4, extra_bars=1)          # 5 bars: 4-bar window plus the prior close
    arr = np.array(closes[-4:], dtype=float)
    got = dev_atr(ctx(w, lookback=4, computed=with_atr(2.0)))
    assert got == pytest.approx((arr[-1] - arr.mean()) / 2.0)


def test_dev_vwap_weights_by_volume_and_refuses_a_zero_volume_window():
    bars = [mkbar(0, 100, 110, 90, 100, v=1), mkbar(1, 100, 110, 90, 100, v=99)]
    w = window(bars, lookback=2)
    tp = (110 + 90 + 100) / 3
    got = dev_vwap(ctx(w, lookback=2, computed=with_atr(5.0)))
    assert got == pytest.approx((100 - tp) / 5.0)           # both bars share a typical price

    zero = [mkbar(i, 100, 110, 90, 100, v=0) for i in range(3)]
    with pytest.raises(Degenerate) as ei:
        dev_vwap(ctx(window(zero, lookback=3), lookback=3, computed=with_atr(5.0)))
    assert "no weights" in ei.value.detail


def test_mr_ac1_recovers_the_sign_of_a_known_autocorrelation():
    rng = np.random.default_rng(2)
    for phi, expect_negative in ((-0.5, True), (0.5, False)):
        r = np.zeros(400)
        for i in range(1, r.size):
            r[i] = phi * r[i - 1] + rng.normal(0, 0.01)
        closes = 100 * np.exp(np.cumsum(r))
        bars = [mkbar(i, c, c * 1.001, c * 0.999, c) for i, c in enumerate(closes)]
        w = window(bars, lookback=399, extra_bars=1)
        got = mr_ac1(ctx(w, lookback=399))
        assert (got < -0.2) is expect_negative, (phi, got)


def test_the_variance_ratio_carries_its_k_and_interpretation():
    engine = FeatureEngine(CALIBRATED, parameter_hash="ph")
    fs = engine.compute(make_snapshot(n=400), ASSET, Product.FUTURES, timeframes=[Timeframe.H1])
    fv = fs.get("MR_VR_K2", Timeframe.H1, "medium")
    assert fv.status is FeatureStatus.OK
    assert fv.detail["k"] == 2
    assert fv.detail["interpretation"] in ("trending", "mean-reverting", "random walk")
    assert (fv.value > 1) == (fv.detail["interpretation"] == "trending")


def test_a_combination_the_formula_cannot_support_is_skipped_not_failed_forever():
    """Reported once at startup and never computed, because time cannot fix it.

    Three formulas declare a floor the short lookbacks cannot clear:

    * ``MR_VR`` at k=8 needs more than 8 returns; the 1d short lookback is 7.
    * ``STR_SWING`` and ``STR_SEQ`` need ``2k + 2 = 8`` bars for a pivot to exist at all; the 4h and
      1d short lookbacks are 6 and 7. Before that floor was declared, these were computed on every
      evaluation and could never return a pivot for **any** θ — an impossibility reported as a
      per-bar data-quality failure, indistinguishable from a real one (F-038).

    The distinction being asserted is *skipped* versus *failed*: a skipped combination says "this can
    never work and here is the arithmetic", while a failed one says "try again later".
    """
    from features.value import FeatureKey

    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    assert ("MR_VR_K8@1d/short", 7, 9) in engine.undefined
    assert ("STR_SWING@4h/short", 6, 8) in engine.undefined
    assert ("STR_SWING@1d/short", 7, 8) in engine.undefined
    assert ("STR_SEQ@4h/short", 6, 8) in engine.undefined
    assert ("STR_SEQ@1d/short", 7, 8) in engine.undefined
    assert engine.stats()["undefined_combinations"] == 5

    fs = engine.compute(make_snapshot(n=400, daily=200), ASSET, Product.FUTURES)
    for name, tf in (("MR_VR_K8", Timeframe.D1), ("STR_SWING", Timeframe.H4),
                     ("STR_SWING", Timeframe.D1)):
        assert FeatureKey(name, tf, "short") not in fs
    # The longer horizons on the same timeframes are unaffected: the floor is per combination.
    assert FeatureKey("MR_VR_K8", Timeframe.D1, "medium") in fs
    assert FeatureKey("STR_SWING", Timeframe.D1, "medium") in fs


def test_the_primary_variance_ratio_is_named_so_conditions_can_be_replayed():
    assert PRIMARY_VR == "MR_VR_K2"
    from features.registry import BY_NAME
    assert PRIMARY_VR in BY_NAME


def test_every_family_3_and_4_value_records_the_window_it_used():
    engine = FeatureEngine(CALIBRATED, parameter_hash="ph")
    fs = engine.compute(make_snapshot(n=400, daily=200), ASSET, Product.FUTURES,
                        timeframes=[Timeframe.H1])
    for fv in fs:
        if fv.status is FeatureStatus.OK:
            assert fv.bars_used > 0
            assert fv.window_end_ms == fs.as_of_ms
            assert fv.window_start_ms < fv.window_end_ms
            assert fv.lookback > 0
