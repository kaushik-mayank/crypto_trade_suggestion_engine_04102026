"""PHASE-15 — ACT, conditions and the lifecycle.

The five ACT properties specs/13 §3 lists are tested with Hypothesis, because they are statements
about *all* series rather than about an example. The ratchet and no-knife-catching properties are the
ones that make the primitive safe to use: if either broke, the system would chase a falling market and
nothing in a normal example-based test would notice.
"""
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from core.errors import StrategyError
from features.value import FeatureKey, FeatureSet, FeatureStatus, FeatureValue
from normalize.canonical import Product, Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot
from strategy.act import ActState, Direction, act
from strategy.base import (
    Lifecycle,
    RawSignal,
    SetupState,
    assert_originating_timeframe,
    assert_transition,
    no_setup,
    setup,
)
from strategy.conditions import (
    Condition,
    ConditionContext,
    ConditionResult,
    dwell_stable,
    evaluate_all,
    regime_axis,
    summarise,
    threshold,
)

T0 = 1767225600000
ASSET = "B-BTC_USDT"
DELTA = Decimal("0.5")
UNIT = Decimal("100")


def prices(*values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


# ---- ACT: the five properties (specs/13 §3) ----------------------------------------------

decimals = st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2,
                       allow_nan=False, allow_infinity=False)


@settings(max_examples=200, deadline=None)
@given(series=st.lists(decimals, min_size=2, max_size=60))
def test_the_long_trigger_ratchets_down_and_never_rises(series):
    """Property 1: E(t) is monotone non-increasing for a LONG, so the system never chases."""
    levels = [act(Direction.LONG, series[:i + 1], 0, UNIT, DELTA).e_t for i in range(len(series))]
    assert all(b <= a for a, b in zip(levels, levels[1:])), levels


@settings(max_examples=200, deadline=None)
@given(series=st.lists(decimals, min_size=2, max_size=60))
def test_the_short_trigger_ratchets_up_and_never_falls(series):
    """Property 2: the mirror."""
    levels = [act(Direction.SHORT, series[:i + 1], 0, UNIT, DELTA).e_t for i in range(len(series))]
    assert all(b >= a for a, b in zip(levels, levels[1:])), levels


@settings(max_examples=200, deadline=None)
@given(start=st.decimals(min_value=Decimal("100"), max_value=Decimal("50000"), places=2),
       steps=st.lists(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("50"), places=2),
                      min_size=2, max_size=40))
def test_a_long_never_triggers_on_a_strictly_monotone_decline(start, steps):
    """Property 3: no knife-catching. This is the property the primitive exists for."""
    series = [start]
    for step in steps:
        series.append(series[-1] - step)
    assume(series[-1] > 0)
    for i in range(1, len(series)):
        state = act(Direction.LONG, series[:i + 1], 0, UNIT, DELTA)
        assert not state.triggered, f"triggered mid-decline at {i}: {state}"


@settings(max_examples=200, deadline=None)
@given(series=st.lists(decimals, min_size=2, max_size=40))
def test_long_and_short_are_mirror_images_under_negation(series):
    """Property 4: act(LONG, s) is act(SHORT, -s) negated."""
    negated = [-v for v in series]
    long_state = act(Direction.LONG, series, 0, UNIT, DELTA)
    short_state = act(Direction.SHORT, negated, 0, UNIT, DELTA)
    assert short_state.x_star == -long_state.x_star
    assert short_state.e_t == -long_state.e_t
    assert short_state.triggered == long_state.triggered


@settings(max_examples=100, deadline=None)
@given(series=st.lists(decimals, min_size=2, max_size=30),
       unit=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000"), places=2))
def test_doubling_the_unit_doubles_the_gap(series, unit):
    """Property 5: the gap E(t) - X* scales linearly with the unit, and equals delta * unit."""
    one = act(Direction.LONG, series, 0, unit, DELTA)
    two = act(Direction.LONG, series, 0, unit * 2, DELTA)
    assert one.gap == DELTA * unit
    assert two.gap == DELTA * unit * 2


# ---- ACT: behaviour and refusals ---------------------------------------------------------

def test_the_trigger_sits_delta_units_above_the_running_low():
    series = prices(1000, 980, 950, 960)
    state = act(Direction.LONG, series, 0, UNIT, DELTA)
    assert state.x_star == Decimal("950")
    assert state.e_t == Decimal("1000")            # 950 + 0.5 * 100
    assert state.current == Decimal("960")
    assert not state.triggered
    assert state.distance_units == Decimal("0.4")  # (1000 - 960) / 100


def test_it_triggers_only_once_price_retraces_the_full_delta():
    series = prices(1000, 980, 950)
    assert not act(Direction.LONG, series, 0, UNIT, DELTA).triggered
    assert not act(Direction.LONG, series + prices(999), 0, UNIT, DELTA).triggered
    fired = act(Direction.LONG, series + prices(1000), 0, UNIT, DELTA)
    assert fired.triggered and fired.distance_units <= 0


def test_the_extreme_is_taken_from_the_adverse_series_when_supplied():
    """X* comes from closed 1m *lows* for a LONG; using closes only would understate the extreme and
    place the trigger too low."""
    closes = prices(1000, 980, 990)
    lows = prices(995, 960, 985)
    with_lows = act(Direction.LONG, closes, 0, UNIT, DELTA, adverse=lows)
    without = act(Direction.LONG, closes, 0, UNIT, DELTA)
    assert with_lows.x_star == Decimal("960") and without.x_star == Decimal("980")
    assert with_lows.e_t < without.e_t


def test_the_window_starts_at_arming_not_at_the_series_start():
    series = prices(1000, 800, 1200, 1150, 1180)     # the 800 predates arming
    state = act(Direction.LONG, series, 2, UNIT, DELTA)
    assert state.x_star == Decimal("1150")
    assert state.bars_since_armed == 3


def test_a_zero_unit_is_refused_because_the_trigger_would_be_the_extreme_itself():
    with pytest.raises(StrategyError) as ei:
        act(Direction.LONG, prices(100, 99), 0, Decimal(0), DELTA)
    assert "E(t) == X*" in ei.value.remediation
    assert "must wait or be invalidated, not entered" in ei.value.remediation


def test_a_zero_delta_and_an_empty_series_are_refused():
    with pytest.raises(StrategyError, match="delta must be positive"):
        act(Direction.LONG, prices(100, 99), 0, UNIT, Decimal(0))
    with pytest.raises(StrategyError, match="empty series"):
        act(Direction.LONG, [], 0, UNIT, DELTA)


def test_an_arming_index_outside_the_window_is_refused_with_both_numbers():
    with pytest.raises(StrategyError) as ei:
        act(Direction.LONG, prices(100, 99, 98), 7, UNIT, DELTA)
    assert "outside a series of 3" in ei.value.message
    assert "load the window from armed_at_ms" in ei.value.remediation


def test_a_misaligned_adverse_series_is_refused():
    with pytest.raises(StrategyError, match="adverse series has"):
        act(Direction.LONG, prices(100, 99, 98), 0, UNIT, DELTA, adverse=prices(100, 99))


def test_the_state_serialises_for_the_dashboard():
    d = act(Direction.LONG, prices(1000, 950, 960), 0, UNIT, DELTA).to_dict()
    import json
    json.dumps(d)
    assert d["x_star"] == "950" and d["e_t"] == "1000.0"
    assert isinstance(d["triggered"], bool)


# ---- conditions --------------------------------------------------------------------------

def context(*, values=None, regime=None, params=None) -> ConditionContext:
    fs = FeatureSet(asset=ASSET, product=Product.FUTURES, as_of_ms=T0, values=values or {})
    snap = MarketSnapshot(as_of_ms=T0, code_version="test", parameter_hash="ph")
    return ConditionContext(asset=ASSET, snapshot=snap, features=fs,
                            regime=regime or unclassified_regime(),
                            params=params or {"trend_tstat_threshold": 2.0})


def ok_value(name, value, tf=Timeframe.H1, horizon="-"):
    fv = FeatureValue(name=name, version="1.0.0", status=FeatureStatus.OK, value=value,
                      timeframe=tf, horizon=horizon)
    return {FeatureKey(name, tf, horizon): fv}


def warming_value(name, tf=Timeframe.H1, horizon="-"):
    fv = FeatureValue(name=name, version="1.0.0", status=FeatureStatus.WARMING,
                      reason="need 721 1h bars, have 300", timeframe=tf, horizon=horizon)
    return {FeatureKey(name, tf, horizon): fv}


def unclassified_regime(**kw):
    base = {"cell": None, "cell_label": "unclassified", "dwell_bars": 0, "entered_at_ms": 0,
            "as_of_ms": T0, "axis_values": {}, "axis_boundaries": {}, "model_version": "test",
            "grid": 2, "reason": "axis VOL_RATIO is WARMING"}
    return RegimeState(**{**base, **kw})


def test_a_threshold_condition_records_what_it_measured_and_compared_to():
    cond = threshold("trend_significant", "the 1h trend is distinguishable from noise",
                     "TRD_TSTAT", timeframe=Timeframe.H1, horizon="medium", op=">",
                     limit_param="trend_tstat_threshold")
    ctx = context(values=ok_value("TRD_TSTAT", 3.1, horizon="medium"))
    r = cond.evaluate(ctx)
    assert r.passed and r.value == 3.1 and r.threshold == 2.0
    assert r.description.startswith("the 1h trend")
    assert not r.unavailable


def test_an_unavailable_feature_fails_with_the_status_never_a_silent_false():
    cond = threshold("trend_significant", "d", "TRD_TSTAT", timeframe=Timeframe.H1,
                     horizon="medium", op=">", limit_param="trend_tstat_threshold")
    r = cond.evaluate(context(values=warming_value("TRD_TSTAT", horizon="medium")))
    assert r.passed is False
    assert r.unavailable is True                      # distinguishable from "measured and failed"
    assert "WARMING" in r.reason and "need 721" in r.reason
    assert r.value is None


def test_a_condition_must_name_exactly_one_kind_of_limit():
    with pytest.raises(StrategyError) as ei:
        threshold("x", "d", "F", timeframe=None, op=">", limit_param="p", limit=1.0)
    assert "exactly one of limit_param or limit" in ei.value.message
    with pytest.raises(StrategyError, match="exactly one"):
        threshold("x", "d", "F", timeframe=None, op=">")
    with pytest.raises(StrategyError, match="unknown comparison"):
        threshold("x", "d", "F", timeframe=None, op="~", limit=1.0)


def test_every_condition_is_evaluated_with_no_short_circuiting():
    """specs/13 §4: failing one condition is diagnostically different from failing five."""
    calls: list[str] = []

    def make(name, passes):
        def body(ctx):
            calls.append(name)
            return passes, 1.0, 0.0
        return Condition(name=name, description=name, body=body)

    conds = [make("a", False), make("b", True), make("c", False)]
    results = evaluate_all(conds, context())
    assert calls == ["a", "b", "c"]                   # all three ran despite 'a' failing
    s = summarise(results)
    assert s == {"total": 3, "passed": 1, "failed": ["a", "c"], "unavailable": [],
                 "all_passed": False, "first_failure": "a"}


def test_a_programming_error_in_a_condition_is_not_absorbed_as_a_failure():
    """Turning every exception into passed=False would make a bug indistinguishable from a market
    that did not qualify."""
    def body(ctx):
        raise ZeroDivisionError("a real bug")

    with pytest.raises(ZeroDivisionError):
        Condition(name="broken", description="d", body=body).evaluate(context())


def test_a_raw_axis_regime_condition_survives_degraded_mode():
    """specs/12 §10: MR_VR < 1 depends on the axis, not the cell, so it still evaluates."""
    degraded = unclassified_regime(cell=None, cell_label="degraded (raw axes only)",
                                  axis_values={"mr_vr": 0.8, "vol_ratio": 1.2}, degraded=True,
                                  reason="the discretised regime model failed its validity tests")
    cond = regime_axis("mean_reverting", "the variance ratio is below a random walk",
                       "mr_vr", op="<", limit=1.0)
    r = cond.evaluate(context(regime=degraded))
    assert r.passed and r.value == 0.8 and r.threshold == 1.0


def test_the_dwell_stability_condition_fails_explicitly_when_degraded():
    """It must not silently pass: a stability filter that defaults to 'stable' looks like a filter
    and is not one."""
    degraded = unclassified_regime(degraded=True, axis_values={"mr_vr": 0.8},
                                   cell_label="degraded (raw axes only)",
                                   reason="failed validity tests")
    r = dwell_stable().evaluate(context(regime=degraded))
    assert r.passed is False
    assert "specs/12 §10 drops this condition" in r.reason


def test_the_dwell_stability_condition_compares_dwell_to_the_cells_median():
    stable = RegimeState(cell=(1, 1), cell_label="steady / trending", dwell_bars=7,
                         entered_at_ms=T0, as_of_ms=T0, axis_values={"mr_vr": 1.1},
                         axis_boundaries={}, model_version="v", grid=3, median_dwell=5.0)
    r = dwell_stable().evaluate(context(regime=stable))
    assert r.passed and r.value == 7 and r.threshold == 5.0

    fresh = RegimeState(cell=(1, 1), cell_label="steady / trending", dwell_bars=2,
                        entered_at_ms=T0, as_of_ms=T0, axis_values={"mr_vr": 1.1},
                        axis_boundaries={}, model_version="v", grid=3, median_dwell=5.0)
    assert dwell_stable().evaluate(context(regime=fresh)).passed is False


def test_an_unclassified_regime_does_not_open_the_gate():
    r = dwell_stable().evaluate(context(regime=unclassified_regime()))
    assert r.passed is False
    assert "must not default to open" in r.reason        # the rule reaches the audit record


def test_a_missing_axis_is_reported_rather_than_treated_as_zero():
    cond = regime_axis("mean_reverting", "d", "mr_vr", op="<", limit=1.0)
    r = cond.evaluate(context(regime=unclassified_regime(axis_values={})))
    assert r.passed is False and "unavailable" in r.reason


# ---- lifecycle ---------------------------------------------------------------------------

def make_state(lifecycle=Lifecycle.SETUP_DETECTED):
    return SetupState(signal_id="sig-1", strategy_id="C1-B", asset=ASSET, product=Product.FUTURES,
                      direction=Direction.LONG, armed_at_ms=T0, armed_snapshot_id="snap",
                      expires_at_ms=T0 + 24 * 3_600_000, lifecycle=lifecycle)


def test_the_documented_lifecycle_path_is_walkable():
    s = make_state()
    s.transition(Lifecycle.ENTER_NOW, at_ms=T0 + 60_000, reason="price crossed E(t)")
    s.transition(Lifecycle.TRIGGERED, at_ms=T0 + 120_000, reason="price moved past")
    s.transition(Lifecycle.TARGET_HIT, at_ms=T0 + 3_600_000, reason="T1 reached")
    assert s.lifecycle is Lifecycle.TARGET_HIT and s.lifecycle.terminal
    assert [t["to"] for t in s.context["transitions"]] == ["ENTER_NOW", "TRIGGERED", "TARGET_HIT"]


def test_an_illegal_transition_is_refused_with_the_legal_ones_listed():
    s = make_state()
    with pytest.raises(StrategyError) as ei:
        s.transition(Lifecycle.TARGET_HIT, at_ms=T0)          # cannot skip ENTER_NOW/TRIGGERED
    assert "illegal lifecycle transition SETUP_DETECTED -> TARGET_HIT" in ei.value.message
    assert "ENTER_NOW" in ei.value.context["allowed"]
    assert s.lifecycle is Lifecycle.SETUP_DETECTED            # unchanged


def test_a_terminal_state_cannot_be_revived():
    s = make_state(Lifecycle.EXPIRED)
    with pytest.raises(StrategyError, match="illegal lifecycle transition"):
        s.transition(Lifecycle.SETUP_DETECTED, at_ms=T0)
    assert Lifecycle.EXPIRED.terminal and not Lifecycle.EXPIRED.active
    assert Lifecycle.SETUP_DETECTED.active


def test_only_1h_and_1d_may_originate_setups():
    assert_originating_timeframe("C1-B", Timeframe.H1)
    assert_originating_timeframe("C4-A", Timeframe.D1)
    with pytest.raises(StrategyError) as ei:
        assert_originating_timeframe("C1-B", Timeframe.M15)
    assert "15m and 1m are for triggering" in ei.value.remediation


def test_assert_transition_is_usable_standalone():
    assert_transition(Lifecycle.ENTER_NOW, Lifecycle.MISSED, signal_id="s")
    with pytest.raises(StrategyError):
        assert_transition(Lifecycle.CANDIDATE, Lifecycle.TRIGGERED, signal_id="s")


# ---- signals and outcomes ----------------------------------------------------------------

def make_signal(**kw):
    base = {"signal_id": "sig-1", "strategy_id": "C1-B", "strategy_version": "1.0.0",
            "asset": ASSET, "product": Product.FUTURES, "direction": Direction.LONG,
            "lifecycle": Lifecycle.SETUP_DETECTED, "as_of_ms": T0,
            "signal_timeframe": Timeframe.H1, "entry": Decimal("1000"), "stop": Decimal("950"),
            "targets": (Decimal("1150"),)}
    return RawSignal(**{**base, **kw})


def test_risk_and_reward_are_derived_from_the_published_levels():
    s = make_signal()
    assert s.risk_per_unit == Decimal("50")
    assert s.reward_to_risk() == Decimal("3")
    assert s.reward_to_risk(5) is None                    # no such target
    assert make_signal(entry=None).risk_per_unit is None


def test_a_signal_serialises_with_its_conditions_and_provenance():
    conds = (ConditionResult(name="a", passed=True, value=1, threshold=0),
             ConditionResult(name="b", passed=False, reason="WARMING"))
    s = make_signal(conditions=conds, snapshot_id="snap-1", parameter_hash="ph",
                    code_version="0.1.0+abc")
    d = s.to_dict()
    import json
    json.dumps(d)
    assert d["reward_to_risk"] == "3.0000" and d["risk_per_unit"] == "50"
    assert d["conditions_summary"]["failed"] == ["b"]
    assert d["snapshot_id"] == "snap-1" and d["parameter_hash"] == "ph"


def test_no_setup_still_carries_the_full_condition_evaluation():
    """Without the rejection record, "Class 1 never fired" is an observation with no diagnosis."""
    conds = (ConditionResult(name="funding_extreme", passed=False, value=0.5, threshold=2.0),
             ConditionResult(name="basis_confirms", passed=True, value=-4.0, threshold=0.0))
    outcome = no_setup(conds, reason="funding is not extreme")
    assert outcome.kind == "NoSetup"
    assert len(outcome.conditions) == 2
    assert outcome.summary["failed"] == ["funding_extreme"]
    assert outcome.to_dict()["signal"] is None


def test_a_setup_outcome_carries_the_signal_and_its_act_state():
    a = ActState(direction=Direction.LONG, x_star=Decimal("950"), e_t=Decimal("1000"),
                 current=Decimal("960"), unit=UNIT, delta=DELTA, bars_since_armed=3,
                 triggered=False)
    outcome = setup(make_signal(act=a), ())
    assert outcome.kind == "Setup" and outcome.act is a
    assert outcome.to_dict()["act"]["x_star"] == "950"
