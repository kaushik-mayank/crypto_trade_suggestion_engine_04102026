"""PHASE-14 — the regime model: fit, validity tests, escalation, degraded mode, live classification.

The degraded path is not a hypothetical here: fitted on real history (F-033), BTC and SOL fail the
persistence test and run degraded while ETH does not. So these tests treat degradation as a normal
operating mode, which is what specs/12 §10 designed it to be.
"""
import json

import numpy as np
import pytest

from core.errors import ConfigError, RegimeError
from core.health import SystemStatus
from features.value import FeatureKey, FeatureSet, FeatureStatus, FeatureValue
from normalize.canonical import Product
from regime import AXIS_P, AXIS_V, CLASSIFY_TF, FitInputs, RegimeClassifier, RegimeModel, fit
from regime.fit import (
    MIN_MEDIAN_DWELL,
    MIN_OCCUPANCY,
    SEPARATION_FORWARD_BARS,
    forward_returns,
)
from regime.model import classify, quantile_edges

H = 3_600_000
T0 = 1767225600000
ASSET = "B-BTC_USDT"
LOOKBACK = 200          # shorter than production's 720, to keep the fixtures quick


def make_inputs(n=2000, *, seed=1, persistent=True, separating=True, lookback=LOOKBACK):
    """Synthetic axes with controllable persistence and separation.

    ``persistent`` makes the axes slow-moving (long dwell); ``separating`` makes forward returns depend
    on the cell. Both are properties the validity tests are supposed to detect.
    """
    rng = np.random.default_rng(seed)
    if persistent:
        # a slow random walk: the label stays put for many bars
        axis_v = np.cumsum(rng.normal(0, 0.05, n))
        axis_p = np.cumsum(rng.normal(0, 0.05, n))
    else:
        axis_v = rng.normal(0, 1, n)          # white noise: the label flips constantly
        axis_p = rng.normal(0, 1, n)
    fwd = rng.normal(0, 0.01, n)
    if separating:
        fwd = fwd + 0.05 * np.sign(axis_p)    # cells differ in what follows them
    return FitInputs(times_ms=T0 + np.arange(n, dtype=np.int64) * H,
                     axis_v=axis_v, axis_p=axis_p, forward_return=fwd, asset=ASSET)


# ---- the pieces ---------------------------------------------------------------------------

def test_quantile_edges_are_trailing_and_equal_count():
    values = np.arange(300, dtype=np.float64)
    assert quantile_edges(values, 3) == pytest.approx([99.667, 199.333], abs=0.5)
    assert len(quantile_edges(values, 2)) == 1
    assert classify(50.0, [99.667, 199.333]) == 0
    assert classify(150.0, [99.667, 199.333]) == 1
    assert classify(250.0, [99.667, 199.333]) == 2


def test_edges_refuse_a_sample_too_small_to_cut():
    with pytest.raises(RegimeError) as ei:
        quantile_edges(np.arange(5, dtype=np.float64), 3)
    assert "only 5 finite observations" in ei.value.message
    assert "lengthen the quantile lookback" in ei.value.remediation


def test_a_grid_of_one_has_no_cut_points():
    with pytest.raises(RegimeError, match="no cut points"):
        quantile_edges(np.arange(100, dtype=np.float64), 1)


def test_forward_returns_look_ahead_by_the_declared_horizon_only():
    closes = np.exp(np.arange(50) * 0.01) * 100
    fwd = forward_returns(closes, horizon=5)
    assert np.isnan(fwd[-5:]).all()                      # nothing invented past the data
    assert fwd[0] == pytest.approx(np.log(closes[5] / closes[0]))
    assert SEPARATION_FORWARD_BARS == 24                 # the spec-anchored default (F-033)
    with pytest.raises(RegimeError, match="cannot be measured"):
        forward_returns(closes, horizon=0)


# ---- the three validity tests -------------------------------------------------------------

def test_a_well_behaved_model_passes_all_three_tests():
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    assert model.usable
    assert model.grid in (3, 2)              # whichever granularity survived the escalation
    assert {v.name for v in model.validity} == {"occupancy", "persistence", "separation"}
    assert all(v.passed for v in model.validity)
    assert model.failed_tests == ()
    assert model.separation_horizon_bars == SEPARATION_FORWARD_BARS


def test_a_label_that_flips_every_bar_fails_persistence_and_is_called_a_noisy_indicator():
    model = fit(make_inputs(persistent=False), quantile_lookback=LOOKBACK)
    persistence = next(v for v in model.validity if v.name == "persistence")
    assert not persistence.passed
    assert persistence.statistic < MIN_MEDIAN_DWELL
    assert "noisy indicator, not a regime" in persistence.detail


def test_cells_that_do_not_differ_in_forward_returns_fail_separation():
    model = fit(make_inputs(separating=False), quantile_lookback=LOOKBACK)
    separation = next(v for v in model.validity if v.name == "separation")
    assert not separation.passed
    assert separation.statistic > 0.05
    assert "no economic content" in separation.detail
    assert separation.to_dict()["forward_horizon_bars"] == SEPARATION_FORWARD_BARS


def test_occupancy_reports_the_thin_cells_by_name():
    """A concentrated axis leaves cells nearly empty; the test must name them, not just fail."""
    n = 1200
    rng = np.random.default_rng(5)
    axis_v = np.where(rng.random(n) < 0.97, 0.0, 10.0)   # 3% of observations in the top bucket
    inputs = FitInputs(times_ms=T0 + np.arange(n, dtype=np.int64) * H, axis_v=axis_v,
                       axis_p=np.cumsum(rng.normal(0, 0.05, n)),
                       forward_return=rng.normal(0, 0.01, n), asset=ASSET)
    model = fit(inputs, quantile_lookback=LOOKBACK)
    occ = next(v for v in model.validity if v.name == "occupancy")
    assert not occ.passed
    assert occ.threshold == MIN_OCCUPANCY
    assert occ.to_dict()["thin_cells"] or occ.to_dict()["cells_occupied"] < 9


# ---- escalation and degradation ----------------------------------------------------------

def test_a_failure_at_three_by_three_collapses_to_two_by_two():
    """specs/12 §10 step 2, asserted on the escalation itself rather than on a contrived fixture:
    this input fails at 3x3 and passes at 2x2, so `fit` must return the 2x2 model."""
    from regime.fit import _attempt

    inputs = make_inputs()
    three, _ = _attempt(inputs, 3, LOOKBACK)
    two, _ = _attempt(inputs, 2, LOOKBACK)
    assert not three.usable, "fixture no longer exercises the collapse: 3x3 passes"
    assert two.usable

    model = fit(inputs, quantile_lookback=LOOKBACK)
    assert model.grid == 2 and model.usable
    assert model.model_version.startswith("2x2-")
    assert len(model.occupancy) == 4                 # all four cells of the coarser grid


def test_failing_at_every_granularity_returns_a_degraded_model_rather_than_raising():
    """"No stable regime structure" is a legitimate result (specs/02 §6), not a crash."""
    model = fit(make_inputs(persistent=False, separating=False), quantile_lookback=LOOKBACK)
    assert model.grid == 1
    assert model.degraded and not model.usable
    assert model.model_version.startswith("degraded-")
    assert "unusable at every granularity" in model.notes
    assert "docs/findings.md" in model.notes
    assert model.occupancy == {} and model.transition_matrix == {}
    # the validity results of every attempt are kept, so the artifact records what was tried
    assert len(model.validity) >= 6


def test_a_degraded_model_publishes_no_persistence_probability():
    """specs/05 §4: C3-B must publish null with a reason, never a number the model cannot support."""
    model = fit(make_inputs(persistent=False, separating=False), quantile_lookback=LOOKBACK)
    assert model.persistence_prob((0, 0)) is None
    assert model.median_dwell((0, 0)) is None


def test_a_fit_window_shorter_than_its_lookback_is_refused():
    with pytest.raises(RegimeError) as ei:
        fit(make_inputs(n=100), quantile_lookback=LOOKBACK)
    assert "not more than the" in ei.value.message
    assert "lengthen the training window" in ei.value.remediation


# ---- the artifact -------------------------------------------------------------------------

def test_the_model_round_trips_through_its_artifact(tmp_path):
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    path = model.save(tmp_path / "regime_model.json")
    loaded = RegimeModel.load(path)
    assert loaded.model_version == model.model_version
    assert loaded.grid == model.grid and loaded.usable == model.usable
    assert loaded.occupancy == model.occupancy
    assert loaded.separation_horizon_bars == model.separation_horizon_bars
    assert [v.name for v in loaded.validity] == [v.name for v in model.validity]


def test_a_missing_or_corrupt_artifact_fails_as_configuration(tmp_path):
    with pytest.raises(ConfigError) as ei:
        RegimeModel.load(tmp_path / "absent.json")
    assert "never fits its own model" in ei.value.remediation
    assert ei.value.problem_class.value == "configuration"

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        RegimeModel.load(bad)

    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"model_version": "x", "grid": 3}), encoding="utf-8")
    with pytest.raises(ConfigError) as ei2:
        RegimeModel.load(partial)
    assert "axis_definitions" in ei2.value.message


def test_a_model_fitted_on_different_axes_is_refused(tmp_path):
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    path = model.save(tmp_path / "m.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["axis_definitions"] = {"V": "VOL_CC", "P": "MR_AC1"}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        RegimeModel.load(path)
    assert "fitted on different axes" in ei.value.message
    assert "different quantity than it was validated on" in ei.value.remediation


# ---- live classification ------------------------------------------------------------------

def feature_set(*, vol_ratio=1.0, mr_vr=1.0, as_of=T0 + 900 * H, ok=True):
    def fv(name, value, tf, horizon):
        if ok:
            return FeatureValue(name=name, version="1.0.0", status=FeatureStatus.OK, value=value,
                                timeframe=tf, horizon=horizon)
        return FeatureValue(name=name, version="1.0.0", status=FeatureStatus.WARMING,
                            reason="need 721 1h bars, have 300", timeframe=tf, horizon=horizon)

    values = {FeatureKey(AXIS_V, CLASSIFY_TF, "-"): fv(AXIS_V, vol_ratio, CLASSIFY_TF, "-"),
              FeatureKey(AXIS_P, CLASSIFY_TF, "short"): fv(AXIS_P, mr_vr, CLASSIFY_TF, "short")}
    return FeatureSet(asset=ASSET, product=Product.FUTURES, as_of_ms=as_of, values=values)


def history(n=LOOKBACK, *, seed=3):
    rng = np.random.default_rng(seed)
    return {AXIS_V: rng.normal(1.0, 0.2, n), AXIS_P: rng.normal(1.0, 0.2, n)}


def test_a_classification_carries_the_boundaries_it_used():
    """Without the boundaries the classification is not replayable: the cut points move every bar."""
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=model)
    state = clf.classify(ASSET, feature_set(vol_ratio=1.4, mr_vr=1.3), axis_history=history())
    assert state.classified
    assert set(state.axis_boundaries) == {"vol_ratio", "mr_vr"}
    assert len(state.axis_boundaries["vol_ratio"]) == model.grid - 1
    assert state.axis_values["vol_ratio"] == 1.4
    assert state.model_version == model.model_version
    assert state.cell_label and "/" in state.cell_label


def test_dwell_starts_at_one_after_a_restart_and_grows_per_bar():
    """Dwell is a property of the run, not the artifact: a fresh process must not claim stability it
    has not observed."""
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=model)
    hist = history()
    first = clf.classify(ASSET, feature_set(vol_ratio=1.0, mr_vr=1.0, as_of=T0 + 900 * H),
                         axis_history=hist)
    assert first.dwell_bars == 1
    second = clf.classify(ASSET, feature_set(vol_ratio=1.0, mr_vr=1.0, as_of=T0 + 901 * H),
                          axis_history=hist)
    assert second.dwell_bars == 2 and second.cell == first.cell
    assert second.entered_at_ms == first.entered_at_ms


def test_a_cell_change_resets_dwell():
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=model)
    hist = history()
    low = clf.classify(ASSET, feature_set(vol_ratio=0.5, mr_vr=0.5, as_of=T0 + 900 * H),
                       axis_history=hist)
    high = clf.classify(ASSET, feature_set(vol_ratio=2.0, mr_vr=2.0, as_of=T0 + 901 * H),
                        axis_history=hist)
    assert high.cell != low.cell
    assert high.dwell_bars == 1 and high.entered_at_ms == T0 + 901 * H


def test_dwell_stability_is_the_condition_that_disappears_when_degraded():
    """C1-B (3), C2-A (7) and C3-B (7) depend on it, and specs/12 §10 drops exactly this."""
    good = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=good)
    hist = history()
    state = clf.classify(ASSET, feature_set(), axis_history=hist)
    for i in range(1, 20):                      # accumulate dwell
        state = clf.classify(ASSET, feature_set(as_of=T0 + (900 + i) * H), axis_history=hist)
    if state.median_dwell is not None:
        assert state.dwell_is_stable == (state.dwell_bars >= state.median_dwell)

    degraded = fit(make_inputs(persistent=False, separating=False), quantile_lookback=LOOKBACK)
    dclf = RegimeClassifier(model=degraded)
    dstate = dclf.classify(ASSET, feature_set(), axis_history=hist)
    assert dstate.degraded
    assert dstate.dwell_is_stable is False             # unavailable, never assumed true
    assert dstate.cell is None and dstate.persistence_prob is None


def test_degraded_mode_still_publishes_the_raw_axis_values():
    """The raw-axis thresholds are what survive degradation, so the values must reach the strategies."""
    degraded = fit(make_inputs(persistent=False, separating=False), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=degraded)
    state = clf.classify(ASSET, feature_set(vol_ratio=1.7, mr_vr=0.8), axis_history=history())
    assert state.axis_values == {"vol_ratio": 1.7, "mr_vr": 0.8}
    assert "raw-axis thresholds still apply" in state.reason
    assert clf.health().status is SystemStatus.DEGRADED
    assert clf.health().problem_class.value == "modelling"       # not a bug, not a feed problem


def test_an_unavailable_axis_leaves_the_regime_unclassified_rather_than_guessing():
    """A gate that defaults to open when its input is missing is not a gate."""
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=model)
    state = clf.classify(ASSET, feature_set(ok=False), axis_history=history())
    assert not state.classified
    assert state.cell_label == "unclassified"
    assert "WARMING" in state.reason and "need 721" in state.reason
    assert clf.unclassified == 1


def test_a_short_axis_history_is_refused_with_both_numbers():
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=model)
    state = clf.classify(ASSET, feature_set(), axis_history=history(n=50))
    assert not state.classified
    assert f"the model was fitted with {LOOKBACK}" in state.reason
    assert "50 trailing observations" in state.reason


def test_missing_axis_history_names_what_the_caller_forgot():
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=model)
    state = clf.classify(ASSET, feature_set(), axis_history={})
    assert not state.classified
    assert "no trailing sample" in state.reason


def test_the_state_serialises_with_all_of_its_evidence():
    model = fit(make_inputs(), quantile_lookback=LOOKBACK)
    clf = RegimeClassifier(model=model)
    d = clf.classify(ASSET, feature_set(), axis_history=history()).to_dict()
    json.dumps(d)                                       # the API publishes this
    for field in ("cell", "cell_label", "dwell_bars", "axis_values", "axis_boundaries",
                  "model_version", "grid", "degraded", "persistence_prob", "median_dwell",
                  "occupancy", "dwell_is_stable"):
        assert field in d


# ---- the real fitted artifacts ------------------------------------------------------------

def test_the_fitted_artifacts_on_disk_load_and_state_their_verdict():
    """F-033: fitted on real history, ETH is usable at 2x2 and BTC/SOL are degraded. Whatever the
    verdict, the artifact must load and carry its evidence."""
    import pathlib
    found = sorted(pathlib.Path("config").glob("regime_model_*.json"))
    if not found:
        pytest.skip("no fitted regime artifacts in config/")
    for path in found:
        model = RegimeModel.load(path)
        assert model.axis_definitions == {"V": AXIS_V, "P": AXIS_P}
        assert model.validity, f"{path.name} carries no validity results"
        assert model.separation_horizon_bars == SEPARATION_FORWARD_BARS
        if model.degraded:
            assert model.failed_tests, "a degraded model must name what failed"
        else:
            assert model.occupancy and model.dwell_statistics
