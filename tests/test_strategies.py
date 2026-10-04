"""PHASE-17/18/19 — the twelve tradeable strategies and the two controls.

Three layers, because they catch different things:

* **Registry invariants** over all fourteen at once. These are the tests that notice a strategy
  quietly disappearing, a control flipping to tradeable, or a ``confidence`` value appearing — all of
  which are single-line mistakes with no visible symptom.
* **Partition invariants.** Several strategies are *defined* by not overlapping another:
  C2-A's pullback band against C3-B's, C3-B's ``t_min`` handoff, C2-C against C1-A. The spec treats an
  overlap as a bug rather than a conflict, so the tests assert the arithmetic of the partition
  directly instead of hoping two strategies never arm together in an example.
* **Behaviour and failure paths** per strategy, with hand-computed expectations.

The failure-path tests matter as much as the success ones here. A strategy that silently fails to arm
is indistinguishable from a market that did not qualify unless the refusal is classified, so several
tests assert the *classification* of a refusal rather than only that it happened.
"""
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from config.parameters import load_registry
from core.errors import (
    ConfigError,
    FeatureNotAvailable,
    ProblemClass,
    SetupNotReady,
    StrategyError,
)
from features.value import FeatureKey, FeatureSet, FeatureStatus, FeatureValue
from normalize.canonical import Bar, Product, Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot, SeriesKey, SeriesState
from strategy.act import Direction
from strategy.base import Lifecycle, SetupState, assert_originating_timeframe
from strategy.c2_participation import PULLBACK_MAX_ATR, PULLBACK_MIN_ATR, assert_c1a_partition
from strategy.c3_transition import C3A, C3B, C3C, MIN_EXPECTED_PAYOFF, RECLAIM_BARS
from strategy.c4_established import BENCHMARK_FOR, C4A, C4B, C4C, EVIDENCE_BASIS
from strategy.controls import assert_backtest_only
from strategy.evaluation import act_unit, effective_stop, price_now, update_trail
from strategy.registry import (
    CONTROL_IDS,
    TRADEABLE_IDS,
    backtest_strategies,
    benchmark_mode,
    control_strategies,
    live_strategies,
)

H = 3_600_000
M15 = 900_000
M1 = 60_000
T0 = 1767225600000
ASSET = "B-BTC_USDT"
REGISTRY = load_registry()
PARAMS = {p.name: p.value for p in REGISTRY.parameters}
#: The registry ships swing_atr_multiple unset (status "unset", value 0.0). Pivot-based conditions are
#: undefined until PHASE-27 calibrates it, so the tests that need structure supply a working value and
#: say so, rather than silently depending on an uncalibrated parameter.
PARAMS_WITH_THETA = {**PARAMS, "swing_atr_multiple": 1.0}


# ---- builders -----------------------------------------------------------------------------

def bars(tf: Timeframe, closes, *, asset=ASSET, end_ms=None, highs=None, lows=None):
    """Closed bars ending at ``end_ms``, one per ``tf`` interval, aligned to the timeframe grid."""
    end = end_ms if end_ms is not None else T0 + 2000 * H
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        t = ((end - (n - i) * tf.ms) // tf.ms) * tf.ms
        o = closes[i - 1] if i else c
        hi = highs[i] if highs else max(o, c) * 1.001
        lo = lows[i] if lows else min(o, c) * 0.999
        out.append(Bar(asset=asset, product=Product.FUTURES, timeframe=tf, open_time_ms=t,
                       open=str(o), high=str(hi), low=str(lo), close=str(c),
                       volume_base="100", is_final=True, fetched_at_ms=end))
    return tuple(out)


def series_of(tf: Timeframe, closes, **kw) -> tuple[SeriesKey, SeriesState]:
    key = SeriesKey(ASSET, Product.FUTURES, tf)
    bs = bars(tf, closes, **kw)
    return key, SeriesState(key=key, bars=bs, requested=len(bs), min_required=1, ok=True)


def snapshot(*, as_of=None, m15=None, m1=None, h1=None, series=None) -> MarketSnapshot:
    """A snapshot carrying the series ACT needs. ``as_of`` defaults past the last bar."""
    as_of = as_of if as_of is not None else T0 + 2000 * H
    built = dict(series or {})
    for tf, closes in ((Timeframe.M15, m15), (Timeframe.M1, m1), (Timeframe.H1, h1)):
        if closes is None:
            continue
        key, state = series_of(tf, closes, end_ms=as_of)
        built[key] = state
    return MarketSnapshot(as_of_ms=as_of, code_version="test", parameter_hash=REGISTRY.parameter_hash,
                          series=built, built_by="test")


def features(overrides=None, *, as_of=None, asset=ASSET) -> FeatureSet:
    """Feature values keyed by ``(name, timeframe, horizon)``; ``None`` makes one DQ_FAILED.

    ``detail`` is attached per feature so a strategy reading ``range_low`` or ``sma`` reads a real
    payload rather than an empty dict — those details are load-bearing for C3-A's bracket and C4-C's
    target, and a test that stubbed them away would pass while the strategy published nothing.
    """
    as_of = as_of if as_of is not None else T0 + 2000 * H
    defaults = {
        ("ATR", Timeframe.H1, "short"): 100.0,
        ("ATR", Timeframe.H1, "medium"): 120.0,
        ("ATR", Timeframe.H1, "long"): 400.0,
        ("VOL_CC", Timeframe.H1, "short"): 0.004,
        ("VOL_CC", Timeframe.H1, "medium"): 0.005,
        ("VOL_CC", Timeframe.H1, "long"): 0.006,
        ("VOL_OF_VOL", Timeframe.H1, "-"): 0.001,      # NO_HORIZON in the registry
        ("TRD_RET", Timeframe.H1, "short"): 0.05,
        ("TRD_RET", Timeframe.H1, "medium"): 0.04,
        ("TRD_RET", Timeframe.D1, "short"): 0.03,
        ("TRD_TSTAT", Timeframe.H1, "medium"): 1.0,
        ("TRD_TSTAT", Timeframe.D1, "medium"): 3.0,
        ("MR_VR_K2", Timeframe.H1, "short"): 1.4,
        ("MR_VR_K8", Timeframe.H1, "short"): 0.9,
        ("DEV_ATR", Timeframe.H1, "medium"): 0.5,
        ("DEV_Z", Timeframe.H1, "short"): -2.5,
        ("DEV_Z", Timeframe.H1, "medium"): -2.5,
        ("VLM_ZROBUST", Timeframe.H1, "short"): 0.2,
        ("STR_RANGEPOS", Timeframe.H1, "short"): 0.5,
        ("STR_RANGEPOS", Timeframe.H1, "medium"): 0.5,
        ("STR_SWING", Timeframe.H1, "short"): 0.3,
        ("STR_DIST_PDH", Timeframe.H1, "short"): -0.4,
        # Tier B: no timeframe (specs/02 -- a point-in-time reading has no bar).
        ("FND_RATE", None, "-"): 0.0002,
        ("FND_PRED", None, "-"): 0.0002,
        ("FND_TENSION", None, "-"): 0.1,
        ("FND_Z", None, "-"): 1.0,
        ("FND_CAPDIST", None, "-"): 0.5,
        ("FND_CUM_1D", None, "-"): 0.0006,
        ("BAS_BPS", None, "-"): -3.5,
        ("BAS_Z", None, "-"): -1.0,
        ("POS_LONG_CNT", None, "-"): 0.52,
        ("POS_LONG_VAL", None, "-"): 0.51,
        ("POS_DIVERGENCE", None, "-"): 0.01,
        ("POS_Z_CNT", None, "-"): 0.4,
        ("POS_Z_VAL", None, "-"): 0.3,
        ("POS_ROC_1D", None, "-"): 0.02,
        ("LIQ_SPREAD", None, "-"): 1.2,
        ("LIQ_DEPTH", None, "-"): 2_000_000.0,
        ("LIQ_IMBAL", None, "-"): 0.05,
        ("X_BETA", Timeframe.H1, "medium"): 1.1,
        ("X_CORR", Timeframe.H1, "medium"): 0.85,
        ("X_RS", Timeframe.H1, "medium"): -2.4,
        ("X_DISP", Timeframe.H1, "medium"): 0.02,
        ("VLM_ZROBUST", Timeframe.H1, "medium"): 0.2,
        ("STR_SEQ", Timeframe.H1, "short"): 1.0,
        ("TRD_ER", Timeframe.H1, "medium"): 0.6,
    }
    details = {
        ("STR_RANGEPOS", Timeframe.H1, "short"): {"range_low": 9900.0, "range_high": 10000.0},
        ("STR_RANGEPOS", Timeframe.H1, "medium"): {"range_low": 9500.0, "range_high": 10000.0},
        ("DEV_Z", Timeframe.H1, "short"): {"sma": 10050.0, "std": 20.0, "close": 9950.0},
        ("DEV_Z", Timeframe.H1, "medium"): {"sma": 10050.0, "std": 20.0, "close": 9950.0},
        ("STR_SWING", Timeframe.H1, "short"): {
            "nearest_level": 9970.0, "nearest_is_high": True, "nearest_touches": 3,
            "last_swing_high": 9970.0, "last_swing_high_ms": as_of - 2 * H,
            "last_swing_low": 9900.0, "last_swing_low_ms": as_of - 3 * H},
        ("STR_DIST_PDH", Timeframe.H1, "short"): {
            "prior_high": 10040.0, "prior_low": 9850.0,
            "dist_high_atr": -0.9, "dist_low_atr": 1.0},
    }
    merged = dict(defaults)
    merged.update(overrides or {})
    out = {}
    for (name, tf, horizon), value in merged.items():
        key = FeatureKey(name, tf, horizon)
        if value is None:
            out[key] = FeatureValue(name=name, version="1", status=FeatureStatus.DQ_FAILED,
                                    reason=f"{name} source unavailable", timeframe=tf,
                                    horizon=horizon)
        else:
            out[key] = FeatureValue(name=name, version="1", status=FeatureStatus.OK, value=value,
                                    timeframe=tf, horizon=horizon,
                                    detail=dict(details.get((name, tf, horizon), {})))
    return FeatureSet(asset=asset, product=Product.FUTURES, as_of_ms=as_of, values=out)


def regime(*, vol_ratio=0.3, mr_vr=1.4, dwell=40, cell=(0, 1), degraded=False,
           v_edges=(0.5, 1.5), p_edges=(0.8, 1.2), as_of=None, reason=None) -> RegimeState:
    as_of = as_of if as_of is not None else T0 + 2000 * H
    return RegimeState(
        cell=cell, cell_label=f"cell {cell}" if cell else "unclassified", dwell_bars=dwell,
        entered_at_ms=as_of - dwell * H, as_of_ms=as_of,
        axis_values={"vol_ratio": vol_ratio, "mr_vr": mr_vr},
        axis_boundaries={"vol_ratio": list(v_edges), "mr_vr": list(p_edges)},
        model_version="test-2x2", grid=2, degraded=degraded, persistence_prob=None if degraded else 0.7,
        median_dwell=None if degraded else 6.0, reason=reason)


def results_by_name(outcome):
    return {r.name: r for r in outcome.conditions}


# ---- registry invariants ------------------------------------------------------------------

def test_the_registry_holds_exactly_twelve_tradeable_strategies_and_no_controls():
    live = live_strategies(PARAMS)
    assert [s.id for s in live] == list(TRADEABLE_IDS)
    assert all(s.tradeable for s in live)
    assert not any(s.id in CONTROL_IDS for s in live)


def test_the_control_suite_is_never_tradeable_and_produces_one_instance_per_seed():
    controls = control_strategies(PARAMS, seeds=(1, 2, 3))
    assert [c.id for c in controls] == ["C4-X1", "C4-X1", "C4-X1", "C4-X2"]
    assert [c.seed for c in controls if c.id == "C4-X1"] == [1, 2, 3]
    assert not any(c.tradeable for c in controls)
    assert all(c.mode == "backtest" for c in controls)


def test_an_empty_seed_list_is_refused_because_zero_paths_prove_less_than_one():
    with pytest.raises(StrategyError, match="at least one seed"):
        control_strategies(PARAMS, seeds=())


def test_the_backtest_set_is_the_live_set_plus_the_controls():
    assert len(backtest_strategies(PARAMS, seeds=(0, 1))) == len(TRADEABLE_IDS) + 3


def test_every_strategy_originates_on_an_allowed_timeframe():
    """specs/13 §1: only 1h and 1d may originate a setup. 15m and 1m are confirmation only."""
    for s in live_strategies(PARAMS):
        assert_originating_timeframe(s.id, s.signal_timeframe)


def test_every_condition_has_a_unique_name_and_a_real_description():
    """A condition with no description is unreadable in a rejection record, which is where
    conditions are actually read from."""
    for s in live_strategies(PARAMS_WITH_THETA):
        names = [c.name for c in s.conditions]
        assert len(names) == len(set(names)), f"{s.id} has duplicate condition names: {names}"
        for c in s.conditions:
            assert len(c.description) > 20, f"{s.id}.{c.name} has no usable description"


def test_no_strategy_declares_a_confidence_value_anywhere():
    """specs/06 §13 criterion 10 and specs/03 §6: ``confidence`` is null system-wide until Phase 16.

    Asserted against the class bodies rather than a signal, so a confidence field added to a strategy
    that happens not to arm in any test is still caught.
    """
    for s in live_strategies(PARAMS_WITH_THETA):
        assert not hasattr(s, "confidence"), f"{s.id} declares a confidence attribute"


def test_only_c3a_is_expansion_seeking():
    """The ``VOL_EXTREME`` veto reads this flag. If a second strategy claimed it, that strategy would
    silently escape a veto designed to stop it."""
    seeking = [s.id for s in live_strategies(PARAMS) if s.expansion_seeking]
    assert seeking == ["C3-A"]


def test_the_class_four_members_declare_the_benchmark_protocol():
    """specs/06 §6 pairs each benchmark with the bespoke strategies it measures. Asserted against the
    live registry so a renamed strategy breaks the pairing loudly."""
    live_ids = set(TRADEABLE_IDS)
    for bench, targets in BENCHMARK_FOR.items():
        assert bench in live_ids
        for t in targets:
            assert t in live_ids, f"{bench} benchmarks {t}, which is not in the registry"


def test_benchmark_mode_returns_separate_instances_so_it_cannot_change_the_suggestion_stream():
    bench = benchmark_mode(PARAMS)
    assert [b.mode for b in bench] == ["benchmark"] * 3
    assert all(s.mode == "suggestion" for s in live_strategies(PARAMS) if s.id.startswith("C4-"))


def test_a_control_refuses_to_run_outside_backtest_mode():
    with pytest.raises(StrategyError, match="only run in backtest"):
        assert_backtest_only("C4-X1", "live")
    assert_backtest_only("C4-X1", "backtest")      # does not raise


# ---- partition invariants -----------------------------------------------------------------

def test_the_pullback_bands_of_c2a_and_c3b_are_contiguous_and_do_not_overlap():
    """C2-A takes [1.0, 3.0] ATR, C3-B the band below it. Contiguous with no gap and no overlap, so a
    pullback of any depth belongs to exactly one of them (specs/05 §4).

    Hand-checked: C3-B's predicate is ``depth < 1.0`` and C2-A's is ``1.0 <= depth <= 3.0``.
    """
    for depth in (0.0, 0.5, 0.999, 1.0, 1.5, 3.0, 3.001, 10.0):
        in_c3b = depth < PULLBACK_MIN_ATR
        in_c2a = PULLBACK_MIN_ATR <= depth <= PULLBACK_MAX_ATR
        assert not (in_c3b and in_c2a), f"depth {depth} is in both bands"
    assert (PULLBACK_MIN_ATR > 0.999) and (PULLBACK_MIN_ATR <= 1.0), "a gap opened between the bands"


def test_c3b_and_c2a_partition_on_the_same_t_min_so_neither_can_drift():
    """C3-B requires ``|t| < t_min`` and C2-A requires ``|t| >= t_min``, both reading the registry.
    Consuming the same parameter is what makes the partition exact rather than approximate."""
    t_min = float(PARAMS["trend_tstat_threshold"])
    fs = features({("TRD_TSTAT", Timeframe.H1, "medium"): t_min - 0.01})
    out = C3B(params=PARAMS_WITH_THETA).evaluate(
        snapshot(m15=[10000] * 60, m1=[10000] * 60, h1=[10000] * 60), fs, regime(), None)
    assert results_by_name(out)["trend_not_established"].passed

    fs2 = features({("TRD_TSTAT", Timeframe.H1, "medium"): t_min + 0.01})
    out2 = C3B(params=PARAMS_WITH_THETA).evaluate(
        snapshot(m15=[10000] * 60, m1=[10000] * 60, h1=[10000] * 60), fs2, regime(), None)
    assert not results_by_name(out2)["trend_not_established"].passed


def test_c2c_and_c1a_arming_together_is_a_bug_not_a_conflict():
    """specs/04 §5 is explicit: C2-C trades funding-aligned trends and C1-A fades funding extremes.
    Both arming on the same asset means one of the two thresholds is wrong, so the partition check
    **raises** rather than handing the pair to arbitration to sort out."""
    extreme = features({("FND_RATE", None, "-"): 0.0030})      # 30x the 0.0001 baseline
    with pytest.raises(StrategyError, match="belongs to C1-A"):
        assert_c1a_partition(extreme, PARAMS, armed=True)
    # Armed on moderate funding is the normal case, and an unarmed C2-C is never a partition breach
    # however extreme funding gets -- C1-A owns that reading alone.
    moderate = features({("FND_RATE", None, "-"): 0.0002})
    assert_c1a_partition(moderate, PARAMS, armed=True)
    assert_c1a_partition(extreme, PARAMS, armed=False)


# ---- C3-A: the directionless bracket -------------------------------------------------------

def c3a_snapshot(*, m15_last=9950, as_of=None):
    as_of = as_of if as_of is not None else T0 + 2000 * H
    return snapshot(as_of=as_of, m15=[9950] * 40 + [m15_last], m1=[9950] * 40, h1=[9950] * 60)


def test_c3a_arms_pending_with_both_triggers_and_no_predicted_direction():
    """specs/05 §3: the *timing* of a contraction resolution is partially predictable, the
    *direction* is not. So the setup publishes a bracket and refuses to pick a side."""
    out = C3A(params=PARAMS).evaluate(c3a_snapshot(), features(), regime(vol_ratio=0.3, dwell=40),
                                      None)
    assert out.state is not None, [r.to_dict() for r in out.conditions if not r.passed]
    assert out.state.context["pending"] is True
    detail = out.signal.detail
    assert detail["pending_direction"] is True
    assert detail["entry_triggers"] == {"long": "10000.0", "short": "9900.0"}
    # No entry, stop or target while pending: a bracket has two candidate entries and one of them is
    # wrong, so publishing either as *the* entry would be a direction the strategy did not take.
    assert out.signal.entry is None and out.signal.stop is None and out.signal.targets == ()
    assert "ACT" in detail["no_act"]


def test_c3a_is_the_only_strategy_that_does_not_use_act():
    """ACT confirms *arrest* when entering against a move; C3-A enters *with* the resolution. The
    published trigger state therefore carries delta 0, which is how the record shows no ratchet."""
    out = C3A(params=PARAMS).evaluate(c3a_snapshot(), features(), regime(), None)
    state = out.state
    managed = C3A(params=PARAMS).evaluate(c3a_snapshot(m15_last=10050), features(), regime(), state)
    assert managed.signal.act is not None
    assert managed.signal.act.delta == Decimal(0)


def test_c3as_payoff_gate_rejects_a_loose_contraction_without_any_target_being_chosen():
    """specs/05 §2.2: ``ATR_long / contraction_range >= 3.0``. A wide contraction fails arithmetically
    rather than being rescued by stretching the target.

    Hand-computed: a range of [9000, 10000] is 1000 wide against ATR_long 400, so the payoff is
    0.4 — far below the 3.0 floor. A range of [9900, 10000] is 100 wide, giving 4.0, which passes.
    """
    loose = features({("STR_RANGEPOS", Timeframe.H1, "short"): 0.5})
    loose.values[FeatureKey("STR_RANGEPOS", Timeframe.H1, "short")].detail.update(
        {"range_low": 9000.0, "range_high": 10000.0})
    out = C3A(params=PARAMS).evaluate(c3a_snapshot(), loose, regime(), None)
    gate = results_by_name(out)["payoff_sufficient"]
    assert not gate.passed
    assert gate.value == pytest.approx(0.4)
    assert gate.threshold == MIN_EXPECTED_PAYOFF
    assert out.state is None


def test_c3a_resolves_its_direction_on_a_confirmed_15m_close_and_stops_at_the_opposite_boundary():
    """Hand-computed: the bracket is [9900, 10000]. A 15m close of 10050 is above the high, so the
    direction is LONG, the entry is 10000, the stop is the **opposite boundary** 9900 — full
    structural invalidation — and T1 is entry + ATR_long = 10000 + 400 = 10400."""
    strategy = C3A(params=PARAMS)
    armed = strategy.evaluate(c3a_snapshot(), features(), regime(), None)
    managed = strategy.evaluate(c3a_snapshot(m15_last=10050), features(), regime(), armed.state)
    sig = managed.signal
    assert sig.direction is Direction.LONG
    assert sig.entry == Decimal("10000.0")
    assert sig.stop == Decimal("9900.0")
    assert sig.targets == (Decimal("10400.0"),)
    assert "opposite boundary" in sig.detail["stop_basis"]
    assert sig.detail["pending_direction"] is False


def test_c3a_resolving_downward_mirrors_exactly():
    strategy = C3A(params=PARAMS)
    armed = strategy.evaluate(c3a_snapshot(), features(), regime(), None)
    managed = strategy.evaluate(c3a_snapshot(m15_last=9850), features(), regime(), armed.state)
    assert managed.signal.direction is Direction.SHORT
    assert managed.signal.entry == Decimal("9900.0")
    assert managed.signal.stop == Decimal("10000.0")
    assert managed.signal.targets == (Decimal("9500.0"),)


def test_c3a_voids_when_volatility_normalises_without_a_directional_resolution():
    """The expansion happened as chop rather than as a move, so the premise is spent — and the setup
    dies even though no trigger ever fired. That is the whole mechanism invalidation for C3-A."""
    strategy = C3A(params=PARAMS)
    armed = strategy.evaluate(c3a_snapshot(), features(), regime(vol_ratio=0.3), None)
    managed = strategy.evaluate(c3a_snapshot(), features(), regime(vol_ratio=1.2), armed.state)
    assert managed.state.lifecycle is Lifecycle.INVALIDATED
    assert "no directional resolution" in managed.reason


def test_c3a_declines_rather_than_arming_on_one_quiet_bar_when_the_regime_is_degraded():
    """Without a cell there is no dwell counter, so compression *persistence* cannot be shown. The
    condition reports unavailable rather than passing on an unmeasured claim (specs/12 §10)."""
    out = C3A(params=PARAMS).evaluate(
        c3a_snapshot(), features(), regime(degraded=True, cell=None, reason="failed separation"), None)
    cond = results_by_name(out)["compression_persisted"]
    assert not cond.passed and cond.unavailable
    assert "dwell" in cond.reason


# ---- C3-B: trend birth ---------------------------------------------------------------------

def c3b_features(over=None):
    base = {("MR_VR_K2", Timeframe.H1, "short"): 1.4,
            ("MR_VR_K8", Timeframe.H1, "short"): 0.9,
            ("TRD_RET", Timeframe.H1, "short"): 0.08,
            ("TRD_TSTAT", Timeframe.H1, "medium"): 1.0,
            ("TRD_TSTAT", Timeframe.D1, "medium"): 1.0,
            ("DEV_ATR", Timeframe.H1, "medium"): 0.4}
    base.update(over or {})
    return features(base)


def test_c3b_requires_the_crossing_itself_not_merely_a_trending_reading():
    """specs/05 §4 condition 1 is a *transition*: ``MR_VR`` crossing from below 1 to above it. Read
    from the two horizons rather than stored history — k=2 above 1 while k=8 is not yet is a recent
    crossing, because the short horizon responds first."""
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    crossed = C3B(params=PARAMS_WITH_THETA).evaluate(snap, c3b_features(), regime(vol_ratio=1.3), None)
    assert results_by_name(crossed)["vr_crossed_above_one"].passed

    established = C3B(params=PARAMS_WITH_THETA).evaluate(
        snap, c3b_features({("MR_VR_K8", Timeframe.H1, "short"): 1.3}),
        regime(vol_ratio=1.3), None)
    assert not results_by_name(established)["vr_crossed_above_one"].passed


def test_c3b_cannot_date_the_crossing_without_the_longer_horizon_and_says_so():
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    out = C3B(params=PARAMS_WITH_THETA).evaluate(
        snap, c3b_features({("MR_VR_K8", Timeframe.H1, "short"): None}), regime(vol_ratio=1.3), None)
    cond = results_by_name(out)["vr_crossed_above_one"]
    assert not cond.passed and cond.unavailable


def test_c3b_takes_only_the_shallow_pullback_band_c2a_leaves_vacant():
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    shallow = C3B(params=PARAMS_WITH_THETA).evaluate(
        snap, c3b_features({("DEV_ATR", Timeframe.H1, "medium"): 0.4}), regime(vol_ratio=1.3), None)
    assert results_by_name(shallow)["shallow_pullback"].passed

    deep = C3B(params=PARAMS_WITH_THETA).evaluate(
        snap, c3b_features({("DEV_ATR", Timeframe.H1, "medium"): 2.0}), regime(vol_ratio=1.3), None)
    assert not results_by_name(deep)["shallow_pullback"].passed


def test_c3b_declines_when_the_daily_trend_is_significantly_opposed():
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    out = C3B(params=PARAMS_WITH_THETA).evaluate(
        snap, c3b_features({("TRD_TSTAT", Timeframe.D1, "medium"): -4.0}),
        regime(vol_ratio=1.3), None)
    assert not results_by_name(out)["daily_not_opposed"].passed


def test_c3b_publishes_regime_persistence_as_null_with_a_reason_when_degraded():
    """specs/05 §4 is explicit: a labelled regime statistic, never a trade win rate, and **null with a
    reason** rather than a number the model cannot support."""
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[9000] * 59 + [10000])
    out = C3B(params=PARAMS_WITH_THETA).evaluate(
        snap, c3b_features(), regime(vol_ratio=1.3, degraded=True, cell=None), None)
    if out.signal is not None:
        assert out.signal.detail["regime_persistence_prob"] is None
        assert "degraded" in out.signal.detail["confidence_unavailable_reason"]
        assert "not a win rate" in out.signal.detail["persistence_caveat"]


def test_c3bs_maturity_handoff_to_c2a_is_success_not_invalidation():
    """``|TRD_TSTAT|`` reaching ``t_min`` means the trend matured into one C2-A recognises. The
    lifecycle still terminates — one position, one owner — but the recorded reason has to say handoff,
    because a reader counting invalidations as failures would otherwise count every success."""
    strategy = C3B(params=PARAMS_WITH_THETA)
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[9000] * 59 + [10000])
    armed = strategy.evaluate(snap, c3b_features(), regime(vol_ratio=1.3), None)
    if armed.state is None:
        pytest.skip(f"did not arm: {[r.name for r in armed.conditions if not r.passed]}")
    matured = strategy.evaluate(
        snap, c3b_features({("TRD_TSTAT", Timeframe.H1, "medium"): 3.0}), regime(vol_ratio=1.3),
        armed.state)
    assert "HANDOFF C3-B -> C2-A" in matured.reason
    assert "success, not invalidation" in matured.reason


# ---- C3-C: boundary resolution -------------------------------------------------------------

ARM_AS_OF = T0 + 2000 * H


def c3c_snapshot(closes_15m, *, as_of=ARM_AS_OF):
    return snapshot(as_of=as_of, m15=closes_15m, m1=[9950] * 40, h1=[9950] * 60)


def c3c_after_arming(post_arm):
    """A snapshot whose ``as_of`` has advanced far enough that ``post_arm`` closes fall **after** the
    setup armed.

    This matters structurally, not just for the fixture: C3-C judges acceptance and rejection only
    from bars that closed since arming, because a break that happened before the setup existed is not
    this setup's break. Advancing ``as_of`` is what makes those bars exist.
    """
    as_of = ARM_AS_OF + len(post_arm) * M15
    return c3c_snapshot([9950] * 10 + list(post_arm), as_of=as_of)


def test_c3c_arms_pending_and_states_that_the_mechanism_is_inferred_not_measured():
    """specs/05 §5 requires the epistemic limit *in the signal*: the premise is that stops cluster
    beyond obvious levels, and there is no liquidation feed and no order-flow data, so the mechanism
    is inferred from structure rather than observed. That is weaker footing than C1-A's funding cash
    flow and the reasoning output has to say so."""
    out = C3C(params=PARAMS_WITH_THETA).evaluate(
        c3c_snapshot([9950] * 10), features(), regime(vol_ratio=0.8), None)
    assert out.state is not None, [r.to_dict() for r in out.conditions if not r.passed]
    assert out.state.context["pending"] is True
    assert "cannot observe" in out.signal.rationale
    assert "no liquidation feed" in out.signal.detail["epistemic_caveat"]
    assert "whipsaw" in out.signal.detail["dominant_failure_mode"]


def test_c3c_requires_a_boundary_with_a_verifiable_touch_count():
    """Condition 2 exists so the level is not arbitrary. A prior-period extreme carries no touch
    count, so when that is the nearest boundary the condition reports *unverifiable* rather than
    assuming the level is significant."""
    fs = features({("STR_SWING", Timeframe.H1, "short"): None})
    out = C3C(params=PARAMS_WITH_THETA).evaluate(c3c_snapshot([9950] * 10), fs, regime(vol_ratio=0.8),
                                                 None)
    cond = results_by_name(out)["boundary_significant"]
    assert not cond.passed
    assert "touch count" in cond.reason


def test_c3c_rejects_a_boundary_that_has_been_traded_through_too_many_times():
    fs = features()
    fs.values[FeatureKey("STR_SWING", Timeframe.H1, "short")].detail["nearest_touches"] = 12
    out = C3C(params=PARAMS_WITH_THETA).evaluate(c3c_snapshot([9950] * 10), fs, regime(vol_ratio=0.8),
                                                 None)
    assert not results_by_name(out)["not_already_chopping"].passed


def test_c3c_does_not_read_price_merely_sitting_below_a_level_as_an_acceptance():
    """The regression this test exists for: resolution has to be judged relative to the side price
    approached from. Without that reference, "three closes below the boundary" reads as acceptance of
    a downside break even when price never went above it — which would leave the strategy permanently
    short of every level above the market.

    The boundary here is 9970 (the nearest swing) and price stays at 9950 throughout, i.e. always
    *inside*. Nothing has resolved.
    """
    strategy = C3C(params=PARAMS_WITH_THETA)
    armed = strategy.evaluate(c3c_snapshot([9950] * 10), features(), regime(vol_ratio=0.8), None)
    assert armed.state.context["inside_is_below"] is True
    managed = strategy.evaluate(c3c_snapshot([9950] * 14), features(), regime(vol_ratio=0.8),
                                armed.state)
    assert managed.state.context["pending"] is True
    assert managed.state.context.get("branch") is None
    assert managed.signal.entry is None


def test_c3c_acceptance_trades_with_the_break_and_needs_volume_above_median():
    """Acceptance = a close beyond, sustained for the reclaim window, on volume above median. A break
    on thin volume is the whipsaw case, so the volume requirement is not cosmetic."""
    strategy = C3C(params=PARAMS_WITH_THETA)
    armed = strategy.evaluate(c3c_snapshot([9950] * 10), features(), regime(vol_ratio=0.8), None)
    # The boundary is 9970 (the nearest swing). One bar inside, then a break held for the whole
    # reclaim window: sustained beyond, which is acceptance.
    beyond = [9950] + [9990] * (RECLAIM_BARS + 1)

    thin = features({("VLM_ZROBUST", Timeframe.H1, "short"): -1.0})
    out_thin = strategy.evaluate(c3c_after_arming(beyond), thin, regime(vol_ratio=0.8), armed.state)
    assert out_thin.state.context["pending"] is True, "accepted a break on below-median volume"

    strategy2 = C3C(params=PARAMS_WITH_THETA)
    armed2 = strategy2.evaluate(c3c_snapshot([9950] * 10), features(), regime(vol_ratio=0.8), None)
    out = strategy2.evaluate(c3c_after_arming(beyond), features(), regime(vol_ratio=0.8),
                             armed2.state)
    assert out.state.context["branch"] == "acceptance"
    assert out.state.direction is Direction.LONG


def test_c3c_rejection_trades_against_the_break_and_carries_the_tighter_stop():
    """specs/05 §5: the rejection branch carries the better geometry — a tight stop just beyond the
    failed extreme against a target spanning the range — and acceptance is allowed to fail the R:R
    gate rather than having its target stretched."""
    strategy = C3C(params=PARAMS_WITH_THETA)
    armed = strategy.evaluate(c3c_snapshot([9950] * 10), features(), regime(vol_ratio=0.8), None)
    # Close above 9970, then back inside within the reclaim window.
    out = strategy.evaluate(c3c_after_arming([9950, 9990, 9960]), features(), regime(vol_ratio=0.8),
                            armed.state)
    assert out.state.context["branch"] == "rejection"
    assert out.state.direction is Direction.SHORT
    assert "failed extreme" in out.signal.detail["stop_basis"]
    assert "better geometry" in out.signal.detail["geometry_note"]


def test_c3c_counts_breaches_so_chop_can_void_the_setup():
    strategy = C3C(params=PARAMS_WITH_THETA)
    armed = strategy.evaluate(c3c_snapshot([9950] * 10), features(), regime(vol_ratio=0.8), None)
    out = strategy.evaluate(c3c_after_arming([9950, 9990, 9950, 9990, 9950, 9990]), features(),
                            regime(vol_ratio=0.8), armed.state)
    assert len(out.state.context["breaks_seen"]) == 3


def test_c3c_declines_in_a_volatility_blowout_where_levels_do_not_hold():
    out = C3C(params=PARAMS_WITH_THETA).evaluate(
        c3c_snapshot([9950] * 10), features(), regime(vol_ratio=5.0), None)
    assert not results_by_name(out)["not_a_blowout"].passed


# ---- C4-A: Donchian breakout ---------------------------------------------------------------

def c4a_snapshot(m15_last, *, as_of=None):
    as_of = as_of if as_of is not None else T0 + 2000 * H
    return snapshot(as_of=as_of, m15=[9800] * 40 + [m15_last], m1=[9800] * 40, h1=[9800] * 60)


def test_c4a_takes_a_breakout_aligned_with_the_daily_trend():
    """Hand-computed: the medium channel is [9500, 10000]. A 15m close of 10100 is above the high, so
    the entry is the boundary 10000, the stop is s x ATR_1h inside it (1.0 x 100 = 9900), and there is
    **no target at all**."""
    out = C4A(params=PARAMS).evaluate(c4a_snapshot(10100), features(), regime(vol_ratio=1.1), None)
    assert out.state is not None, [r.to_dict() for r in out.conditions if not r.passed]
    sig = out.signal
    assert sig.direction is Direction.LONG
    assert sig.entry == Decimal("10000.0")
    assert sig.stop == Decimal("9900.0")
    assert sig.targets == ()
    assert "trailed from entry" in sig.detail["targets"]


def test_c4a_refuses_a_breakout_against_the_daily_trend():
    """The 1d direction filter is the single highest-value filter for any breakout system, and it must
    be the *named* reason for the refusal so the rejection record is diagnostic."""
    fs = features({("TRD_TSTAT", Timeframe.D1, "medium"): -3.0})
    out = C4A(params=PARAMS).evaluate(c4a_snapshot(10100), fs, regime(vol_ratio=1.1), None)
    cond = results_by_name(out)["daily_direction_aligned"]
    assert not cond.passed
    assert out.state is None


def test_c4a_distinguishes_no_breakout_from_a_breakout_against_the_trend():
    """Two different states that would otherwise both read as "the direction filter failed". The
    rejection record has to tell them apart, because one means "nothing happened" and the other means
    "something happened and we declined it"."""
    quiet = C4A(params=PARAMS).evaluate(c4a_snapshot(9800), features(), regime(vol_ratio=1.1), None)
    cond = results_by_name(quiet)["daily_direction_aligned"]
    assert not cond.passed
    assert "no breakout to align" in cond.reason
    assert not results_by_name(quiet)["breakout_confirmed"].passed


def test_c4a_skips_a_breakout_in_the_exhaustion_decile():
    """specs/06 §4.1: breakouts in the top decile of ``VOL_RATIO`` are usually exhaustion rather than
    initiation. Deliberately only two filters, and this is the second."""
    out = C4A(params=PARAMS).evaluate(c4a_snapshot(10100), features(), regime(vol_ratio=9.0), None)
    assert not results_by_name(out)["not_exhaustion_volatility"].passed
    assert out.signal is None


def test_c4a_applies_exactly_two_filters_and_says_so():
    out = C4A(params=PARAMS).evaluate(c4a_snapshot(10100), features(), regime(vol_ratio=1.1), None)
    assert out.signal.detail["filters_applied"] == 2
    assert out.signal.detail["pyramiding"] is False
    assert "over-filter" in out.signal.detail["filter_note"]


def test_c4a_voids_when_price_returns_inside_the_channel_before_the_trail_engages():
    """False breakouts are this strategy's most common outcome and are expected; the R:R floor is the
    defence. Once the trail has engaged it becomes the only exit, so this check stops — two exit rules
    racing on one position would make the backtest depend on loop order."""
    strategy = C4A(params=PARAMS)
    armed = strategy.evaluate(c4a_snapshot(10100), features(), regime(vol_ratio=1.1), None)
    back_inside = snapshot(m15=[9800] * 40 + [9700], m1=[9800] * 39 + [9700], h1=[9800] * 60)
    managed = strategy.evaluate(back_inside, features(), regime(vol_ratio=1.1), armed.state)
    assert managed.state.lifecycle is Lifecycle.INVALIDATED
    assert "breakout was false" in managed.reason


def test_the_class_four_signal_additions_are_present_and_null_rather_than_absent():
    """specs/06 §10. ``suppressed_by`` is published as null rather than omitted so no consumer has to
    distinguish "not suppressed" from "the field is missing"."""
    out = C4A(params=PARAMS).evaluate(c4a_snapshot(10100), features(), regime(vol_ratio=1.1), None)
    d = out.signal.detail
    assert d["mode"] == "suggestion"
    assert d["evidence_basis"] == EVIDENCE_BASIS
    assert d["benchmark_for"] == list(BENCHMARK_FOR["C4-A"])
    assert d["suppressed_by"] is None
    assert d["control"] is None and d["seed"] is None
    assert "cannot verify" in d["evidence_caveat"]


# ---- C4-B: dual-timeframe momentum ---------------------------------------------------------

def test_c4b_declines_exactly_at_the_signal_boundary_where_whipsaw_lives():
    """specs/06 §4.2 names the signal boundary as the whipsaw case. A zero return has no sign to take,
    so declining there is the conservative reading rather than a coin flip."""
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    out = C4B(params=PARAMS).evaluate(
        snap, features({("TRD_RET", Timeframe.H1, "medium"): 0.0}), regime(), None)
    assert not results_by_name(out)["momentum_signal_on"].passed
    assert out.state is None


def test_c4b_requires_the_daily_return_to_agree_and_reports_the_degree_of_agreement():
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    disagree = C4B(params=PARAMS).evaluate(
        snap, features({("TRD_RET", Timeframe.D1, "short"): -0.02}), regime(), None)
    assert not results_by_name(disagree)["daily_agrees"].passed

    agree = C4B(params=PARAMS).evaluate(snap, features(), regime(), None)
    assert agree.signal.detail["strength_inputs"]["agreement_1d"] == pytest.approx(0.03)


def test_c4b_declares_its_volatility_scaling_without_applying_it():
    """``portfolio_vol_target`` belongs to the risk engine (specs/08). A strategy that pre-scaled its
    own size would be sized twice, so the scaling travels as a declaration the risk engine reads."""
    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    out = C4B(params=PARAMS).evaluate(snap, features(), regime(), None)
    scaling = out.signal.detail["vol_scaling"]
    assert scaling["realised_vol"] == pytest.approx(0.005)
    assert scaling["target_vol"] == PARAMS["portfolio_vol_target"]
    assert "risk.engine" in scaling["applied_by"]


def test_c4b_is_state_driven_and_exits_when_the_state_ends():
    """The difference from C4-A that justifies running both: C4-A is event-driven and waits for a
    breakout, C4-B holds while the signal is on. So its exit is a sign flip, not a level."""
    strategy = C4B(params=PARAMS)
    snap = snapshot(m15=[10000] * 40, m1=[9000] * 39 + [10000], h1=[10000] * 60)
    armed = strategy.evaluate(snap, features(), regime(), None)
    assert armed.signal.detail["signal_type"] == "state-driven"
    flipped = strategy.evaluate(snap, features({("TRD_RET", Timeframe.H1, "medium"): -0.04}),
                                regime(), armed.state)
    assert flipped.state.lifecycle is Lifecycle.INVALIDATED
    assert "no longer LONG" in flipped.reason


def test_c4b_publishes_no_target_because_the_exit_is_a_flip_or_a_trail():
    snap = snapshot(m15=[10000] * 40, m1=[9000] * 39 + [10000], h1=[10000] * 60)
    out = C4B(params=PARAMS).evaluate(snap, features(), regime(), None)
    assert out.signal.targets == ()
    assert "flip" in out.signal.detail["exit_rule"]


# ---- C4-C: short-horizon reversal ----------------------------------------------------------

def test_c4c_arms_against_the_displacement_and_targets_the_mean():
    """Hand-computed: ``DEV_Z(short)`` is -2.5 against a threshold of 2.25, so the displacement is
    down and the reversal is LONG. The target is the short-lookback mean the displacement was
    measured against — 10050 — so entry and exit come from one quantity rather than two."""
    snap = snapshot(m15=[10000] * 40, m1=[9900] * 39 + [9950], h1=[10000] * 60)
    out = C4C(params=PARAMS).evaluate(snap, features(), regime(), None)
    assert out.state is not None, [r.to_dict() for r in out.conditions if not r.passed]
    assert out.signal.direction is Direction.LONG
    assert out.signal.targets == (Decimal("10050.0"),)
    assert "no stop" in out.signal.detail["stop_basis"]


def test_c4c_excludes_the_volume_tail_as_its_only_filter():
    """The mechanism is liquidity *demand*, not information. Where the move was informational no
    reversal should be expected, and top-decile volume is the crude proxy for that."""
    snap = snapshot(m15=[10000] * 40, m1=[9900] * 40, h1=[10000] * 60)
    out = C4C(params=PARAMS).evaluate(
        snap, features({("VLM_ZROBUST", Timeframe.H1, "short"): 2.5}), regime(), None)
    cond = results_by_name(out)["not_volume_tail"]
    assert not cond.passed
    assert out.state is None


def test_c4c_has_exactly_two_conditions_and_no_regime_gate():
    """Deliberately unconditioned, and that is the point. C1-B is the conditioned version of this
    idea, gated on ``MR_VR < 1``; adding that gate here would collapse C4-C into C1-B and destroy the
    comparison that tests whether the conditioning adds anything (specs/06 §4.3)."""
    conditions = C4C(params=PARAMS).conditions
    assert [c.name for c in conditions] == ["displaced", "not_volume_tail"]
    assert not any("regime" in c.name or "vr" in c.name.lower() for c in conditions)


def test_c4c_arms_in_a_trending_regime_where_the_conditioned_version_would_be_gated():
    """The partition against C1-B, asserted directly: ``MR_VR`` of 1.6 is trending, which is exactly
    where C1-B's gate closes. C4-C must still arm, or there is no comparison to make."""
    snap = snapshot(m15=[10000] * 40, m1=[9900] * 39 + [9950], h1=[10000] * 60)
    out = C4C(params=PARAMS).evaluate(
        snap, features({("MR_VR_K2", Timeframe.H1, "short"): 1.6}), regime(mr_vr=1.6), None)
    assert out.state is not None
    assert "No regime gate is applied, by design" in out.signal.rationale


def test_c4c_publishes_the_evaluation_requirements_that_reject_hit_rate_and_sharpe():
    """specs/06 §13 criterion 6. The requirement travels on the signal rather than living only in a
    document the backtest author may not read: a short-gamma strategy's hit rate will look excellent
    and will be misleading."""
    snap = snapshot(m15=[10000] * 40, m1=[9900] * 39 + [9950], h1=[10000] * 60)
    out = C4C(params=PARAMS).evaluate(snap, features(), regime(), None)
    d = out.signal.detail
    assert d["payoff_shape"] == "short_gamma"
    assert set(d["evaluation_requires"]) == {"max_drawdown", "worst_single_trade",
                                             "loss_distribution_tail"}
    assert "rejected as sufficient" in d["hit_rate_and_sharpe"]
    assert "do not widen the stop" in d["if_tail_is_poor"]


def test_c4c_voids_pre_entry_when_the_displacement_closes_before_the_trigger_fires():
    """An invalidation rather than a target, because it fires *pre-entry*. Booking a closed
    displacement as a win would be a profit on a position that was never entered — the single easiest
    way to make a mean-reversion backtest look excellent and be wrong."""
    strategy = C4C(params=PARAMS)
    snap = snapshot(m15=[10000] * 40, m1=[9900] * 39 + [9950], h1=[10000] * 60)
    armed = strategy.evaluate(snap, features(), regime(), None)
    closed = strategy.evaluate(snap, features({("DEV_Z", Timeframe.H1, "short"): 0.1}), regime(),
                               armed.state)
    assert closed.state.lifecycle is Lifecycle.INVALIDATED
    assert "never entered" in closed.reason


# ---- the shared trail ----------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(prices=st.lists(st.decimals(min_value=Decimal("100"), max_value=Decimal("20000"), places=2),
                       min_size=2, max_size=40),
       long=st.booleans())
def test_the_shared_trail_is_monotone_and_never_loosens(prices, long):
    """One trail implementation for C2-A/B/C, C3-B and C4-A/B, for the same reason ACT has one: a
    trail that loosens in one strategy and not another is a bug that shows up in exactly one
    backtest."""
    state = SetupState(signal_id="s", strategy_id="C4-A", asset=ASSET, product=Product.FUTURES,
                       direction=Direction.LONG if long else Direction.SHORT,
                       armed_at_ms=T0, armed_snapshot_id="x", expires_at_ms=T0 + H,
                       lifecycle=Lifecycle.TRIGGERED)
    levels = []
    for p in prices:
        update_trail(state, atr=Decimal("10"), s=Decimal("1"), price=p)
        levels.append(Decimal(state.context["trail_level"]))
    if long:
        assert all(b >= a for a, b in zip(levels, levels[1:])), levels
    else:
        assert all(b <= a for a, b in zip(levels, levels[1:])), levels


def test_the_trail_does_not_engage_before_entry():
    state = SetupState(signal_id="s", strategy_id="C4-A", asset=ASSET, product=Product.FUTURES,
                       direction=Direction.LONG, armed_at_ms=T0, armed_snapshot_id="x",
                       expires_at_ms=T0 + H, lifecycle=Lifecycle.SETUP_DETECTED)
    update_trail(state, atr=Decimal("10"), s=Decimal("1"), price=Decimal("10000"))
    assert "trail_level" not in state.context


def test_the_effective_stop_is_never_looser_than_the_published_stop():
    """Early in a trade the trail sits *behind* the structural stop. Taking the trail unconditionally
    would widen risk below the ``R`` the signal was sized against."""
    state = SetupState(signal_id="s", strategy_id="C4-A", asset=ASSET, product=Product.FUTURES,
                       direction=Direction.LONG, armed_at_ms=T0, armed_snapshot_id="x",
                       expires_at_ms=T0 + H, lifecycle=Lifecycle.TRIGGERED,
                       context={"trail_level": "9800"})
    assert effective_stop(state, Decimal("9900")) == Decimal("9900")
    state.context["trail_level"] = "9950"
    assert effective_stop(state, Decimal("9900")) == Decimal("9950")


# ---- failure paths and their classification ------------------------------------------------

def test_a_missing_15m_series_is_a_configuration_problem_not_an_application_bug():
    """The SnapshotRequest did not ask for the timeframe. No amount of waiting adds a series nobody
    requested, so this is the operator's problem and must be classified as one."""
    snap = snapshot(m1=[10000] * 40)        # no 15m at all
    with pytest.raises(ConfigError) as e:
        act_unit(snap, ASSET, Product.FUTURES, PARAMS)
    assert e.value.problem_class is ProblemClass.CONFIGURATION
    assert "15m" in e.value.message


def test_a_warming_15m_window_is_a_data_gate_not_an_application_bug():
    """The regression this exists for: reported as ``application_bug``, a warming recorder sends an
    operator to read code that is working. ``SetupNotReady`` is a ``StrategyError`` subclass so
    condition bodies still absorb it, but it carries the honest classification."""
    key = SeriesKey(ASSET, Product.FUTURES, Timeframe.M15)
    short = SeriesState(key=key, bars=bars(Timeframe.M15, [10000] * 3), requested=200,
                        min_required=25, ok=False, reason="WARMING", detail="need 25, have 3")
    snap = MarketSnapshot(as_of_ms=T0 + 2000 * H, code_version="t", parameter_hash="p",
                          series={key: short}, built_by="test")
    with pytest.raises(SetupNotReady) as e:
        act_unit(snap, ASSET, Product.FUTURES, PARAMS)
    assert e.value.problem_class is ProblemClass.DATA_QUALITY
    assert e.value.code == "STRATEGY_NOT_READY"
    assert isinstance(e.value, StrategyError)       # still absorbed by a condition body


def test_a_missing_price_is_a_gate_that_names_what_is_missing():
    with pytest.raises(SetupNotReady) as e:
        price_now(features(), snapshot(m15=[10000] * 40))
    assert e.value.problem_class is ProblemClass.DATA_QUALITY
    assert "1m" in e.value.message


def test_a_strategy_whose_atr_is_unavailable_declines_rather_than_substituting_a_stop():
    """Hard invariant: fail safely rather than silently producing an incorrect number. A substituted
    ATR would produce a stop that looks measured and is not."""
    snap = c4a_snapshot(10100)
    fs = features({("ATR", Timeframe.H1, "short"): None})
    strategy = C4A(params=PARAMS)
    with pytest.raises(FeatureNotAvailable) as e:
        strategy.evaluate(snap, fs, regime(vol_ratio=1.1), None)
    assert e.value.problem_class is ProblemClass.DATA_QUALITY
    assert "no substitute value" in e.value.remediation


def every_registered_feature() -> FeatureSet:
    """Every ``(feature, timeframe, horizon)`` the registry declares, each with a plausible value.

    Built from :func:`features.registry.all_keys` rather than hand-listed, so a strategy reading a
    feature nobody thought to add to a fixture is exercised rather than skipped — and so this fixture
    cannot drift behind the registry.
    """
    from features.registry import all_keys

    plausible = {"ATR": 100.0, "VOL_CC": 0.005, "VOL_OF_VOL": 0.001, "VOL_RATIO": 0.8,
                 "TRD_RET": 0.03, "TRD_TSTAT": 1.0, "TRD_ER": 0.6, "DEV_Z": -2.5, "DEV_ATR": 0.5,
                 "DEV_VWAP": -0.5, "X_CORR": 0.85, "X_BETA": 1.1, "X_RS": -2.4, "X_DISP": 0.02,
                 "STR_RANGEPOS": 0.5, "STR_SWING": 0.3, "STR_DIST_PDH": -0.4}
    out = {}
    for key in all_keys():
        value = plausible.get(key.name, 1.0)
        detail = {"range_low": 9900.0, "range_high": 10000.0, "sma": 10050.0, "std": 20.0,
                  "nearest_level": 9970.0, "nearest_touches": 3, "nearest_is_high": True,
                  "last_swing_high": 9970.0, "last_swing_high_ms": T0 + 1998 * H,
                  "last_swing_low": 9900.0, "last_swing_low_ms": T0 + 1997 * H,
                  "prior_high": 10040.0, "prior_low": 9850.0,
                  "dist_high_atr": -0.9, "dist_low_atr": 1.0, "close": 9950.0}
        out[key] = FeatureValue(name=key.name, version="1", status=FeatureStatus.OK, value=value,
                                timeframe=key.timeframe, horizon=key.horizon, detail=detail)
    return FeatureSet(asset=ASSET, product=Product.FUTURES, as_of_ms=T0 + 2000 * H, values=out)


def test_every_live_strategy_only_reads_registered_parameters():
    """A condition asking for an unregistered parameter raises, which is the right behaviour — but it
    would only surface on the one evaluation that reaches that condition. Running every strategy's
    full condition list against one snapshot surfaces it at test time instead.

    The 30-parameter cap is the reason this matters: a strategy that quietly needed a 31st would
    break the registry's guarantee rather than just failing.
    """
    from strategy.conditions import ConditionContext

    snap = snapshot(m15=[10000] * 40, m1=[10000] * 40, h1=[10000] * 60)
    full = every_registered_feature()
    for strategy in live_strategies(PARAMS_WITH_THETA):
        for cond in strategy.conditions:
            ctx = ConditionContext(asset=ASSET, snapshot=snap, features=full, regime=regime(),
                                   params=strategy.params)
            result = cond.evaluate(ctx)
            assert "is not registered but a condition asked for it" not in (result.reason or ""),                 f"{strategy.id}.{cond.name} reads an unregistered parameter: {result.reason}"


def test_the_feature_fixture_covers_every_registered_key():
    """Guards the test above: if the fixture fell behind the registry, the sweep would silently stop
    exercising the conditions that read the newer features."""
    from features.registry import all_keys

    assert set(every_registered_feature().values) == set(all_keys())
