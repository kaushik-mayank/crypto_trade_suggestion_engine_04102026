"""The parameter registry and settings (PHASE-01).

The registry is the mechanism that keeps the 30-parameter budget honest, so these tests assert the
rules the specs state as non-negotiable rather than just that the file parses.
"""
import textwrap

import pytest
from pydantic import ValidationError

from config import RegistryError, Settings, load_registry, load_settings
from config.parameters import DEFAULT_PATH
from config.parameters import load_registry as load_from

# Hand-derived from specs/02 §4 — calendar-anchored, not read back from the code.
EXPECTED_LOOKBACKS = {
    "lookback_1h_short": 24, "lookback_1h_medium": 168, "lookback_1h_long": 720,
    "lookback_4h_short": 6, "lookback_4h_medium": 42, "lookback_4h_long": 180,
    "lookback_1d_short": 7, "lookback_1d_medium": 30, "lookback_1d_long": 90,
}
# The five entries that raised the cap from 25 to 30 (specs/08 §11).
RISK_ENGINE_PARAMS = {
    26: "risk_per_trade", 27: "portfolio_vol_target", 28: "class3_multiplier",
    29: "unvalidated_multiplier", 30: "liq_safety",
}


@pytest.fixture(scope="module")
def reg():
    return load_registry()


# ---- the budget ---------------------------------------------------------------------------

def test_registry_holds_exactly_thirty_parameters_within_the_cap(reg):
    assert len(reg) == 30
    assert reg.cap == 30
    assert [p.id for p in reg.parameters] == list(range(1, 31))   # contiguous, no gaps


def test_every_parameter_is_justified_and_traceable(reg):
    for p in reg.parameters:
        assert p.justification.strip(), p.name
        assert p.source_spec.startswith("specs/"), p.name
        assert p.overfitting_exposure.strip(), p.name
        assert p.used_by, p.name


def test_shared_lookbacks_match_the_spec_and_are_fixed(reg):
    for name, expected in EXPECTED_LOOKBACKS.items():
        p = reg.get(name)
        assert p.value == expected, name
        assert p.status == "fixed", name
        assert p.numeric_band is None, f"{name}: a fixed parameter must not carry a sweep band"


def test_the_cap_raise_is_exactly_the_five_risk_engine_parameters(reg):
    for pid, name in RISK_ENGINE_PARAMS.items():
        assert reg.by_id(pid).name == name
    # ids 1-25 are the model parameters registered by specs/02-05; 26-30 are the risk engine's
    for i in range(1, 26):
        assert reg.by_id(i).source_spec.startswith(("specs/02", "specs/03", "specs/04", "specs/05")), i
    for i in range(26, 31):
        assert reg.by_id(i).source_spec.startswith("specs/08"), i


# ---- calibrated vs provisional ------------------------------------------------------------

def test_provisional_values_are_not_presented_as_calibrated(reg):
    # specs/09: thresholds are calibrated in Phase 16; anything not fixed or user-set is pending.
    for name in ("act_trigger_multiple", "unvalidated_multiplier", "stop_buffer_atr_multiple", "funding_extreme_multiple",
                 "deviation_z_threshold", "residual_z_threshold", "correlation_floor",
                 "trend_tstat_threshold", "contraction_percentile", "class3_multiplier",
                 "unvalidated_multiplier", "liq_safety"):
        assert reg.get(name).is_calibrated is False, name
    for name in ("lookback_1h_long", "swing_confirmation_bars", "redundancy_rho_cut",
                 "basis_timestamp_tolerance_s", "risk_per_trade", "portfolio_vol_target"):
        assert reg.get(name).is_calibrated is True, name


def test_every_provisional_value_sits_inside_its_own_sweep_band(reg):
    for p in reg.sweepable():
        lo, hi = p.numeric_band
        assert lo <= float(p.value) <= hi, f"{p.name}={p.value} outside [{lo}, {hi}]"


def test_an_unset_parameter_raises_rather_than_returning_a_placeholder(tmp_path):
    """The mechanism, tested against a *constructed* unset entry.

    Asserted on a synthetic registry rather than on whichever parameter happens to be uncalibrated
    today: this test exists to prove that an unset value can never be read as a number, and that
    guarantee must not evaporate the moment the last real parameter is calibrated.
    """
    text = DEFAULT_PATH.read_text(encoding="utf-8")
    text = text.replace("value = 0.75\nunit = \"ATR multiples\"\nstatus = \"provisional\"",
                        "value = 0.0\nunit = \"ATR multiples\"\nstatus = \"unset\"")
    p = tmp_path / "parameters.toml"
    p.write_text(text, encoding="utf-8")
    reg = load_from(p)
    assert reg.get("swing_atr_multiple").status == "unset"
    with pytest.raises(RegistryError, match="unset"):
        reg.value("swing_atr_multiple")


def test_theta_is_calibrated_structurally_and_only_provisionally(reg):
    """PHASE-27 set θ from pivot structure alone — availability, spacing and cross-asset dispersion,
    with no returns. ``provisional`` rather than ``fixed`` because it has never seen a backtest, and
    specs/09 reserves promotion for Stage 10."""
    theta = reg.get("swing_atr_multiple")          # calibrated once, specs/02 §10 #10
    assert theta.status == "provisional"
    assert theta.value == 0.75
    assert "PHASE-27" in theta.set_by and "No returns were used" in theta.set_by
    assert reg.value("swing_atr_multiple") == 0.75


def test_er_min_is_conditional_on_the_redundancy_protocol(reg):
    p = reg.get("efficiency_ratio_floor")
    assert p.status == "conditional"
    assert "TRD_TSTAT" in p.condition and "TRD_ER" in p.condition
    assert "29" in p.condition        # deleting it takes the count to 29 (specs/04 §8)


def test_act_delta_records_that_a_sharp_optimum_means_overfitting(reg):
    # specs/03 §2: a sharp optimum is evidence of overfitting, not a better value.
    p = reg.get("act_trigger_multiple")
    assert p.numeric_band == (0.3, 1.0)
    assert "OVERFITTING" in p.overfitting_exposure.upper()
    assert len(p.used_by) == 9, "delta is shared by nine ACT consumers (specs/13 §3)"


def test_basis_tolerance_is_two_seconds_and_fixed(reg):
    p = reg.get("basis_timestamp_tolerance_s")
    assert p.value == 2 and p.status == "fixed"


# ---- identity ----------------------------------------------------------------------------

def test_parameter_hash_is_stable_across_loads(reg):
    assert load_registry().parameter_hash == reg.parameter_hash
    assert len(reg.parameter_hash) == 64


def test_parameter_hash_changes_when_any_value_changes(tmp_path, reg):
    edited = DEFAULT_PATH.read_text(encoding="utf-8").replace(
        'value = 1.0\nunit = "ATR multiples"', 'value = 1.1\nunit = "ATR multiples"')   # s: 1.0 -> 1.1
    p = tmp_path / "parameters.toml"
    p.write_text(edited, encoding="utf-8")
    assert load_from(p).parameter_hash != reg.parameter_hash


def test_parameter_hash_ignores_prose_changes(tmp_path, reg):
    # Editing a justification must not invalidate a past signal's provenance.
    edited = DEFAULT_PATH.read_text(encoding="utf-8").replace(
        "One day of 1h bars. Calendar-anchored, not optimised.", "One day of 1h bars. Reworded.")
    p = tmp_path / "parameters.toml"
    p.write_text(edited, encoding="utf-8")
    assert load_from(p).parameter_hash == reg.parameter_hash


def test_summary_reports_the_budget_and_what_is_pending(reg):
    s = reg.summary()
    assert s["count"] == 30 and s["cap"] == 30
    # Hand-counted from the spec tables: ids 1-9 (lookbacks) + 11, 12, 13, 15, 16 = 14 fixed.
    # Nothing is `unset` any more: θ was the last one, calibrated structurally in PHASE-27.
    assert s["by_status"] == {"fixed": 14, "provisional": 12, "conditional": 1, "user": 2,
                              "judgment": 1}
    assert s["by_status"].get("unset", 0) == 0
    assert "act_trigger_multiple" in s["provisional"]


# ---- the rules that keep the budget honest ------------------------------------------------

BAD_CASES = {
    "over the cap": ("cap = 30", "cap = 1"),
    "fixed with a sweep band": ('band = "none — a small odd integer, not tuned"', "band = [1, 9]"),
    "provisional outside its band": ('value = 0.15\nunit = "percentile of the trailing VOL_RATIO distribution"',
                                     'value = 0.95\nunit = "percentile of the trailing VOL_RATIO distribution"'),
    "judgment claiming a setting phase": ('status = "judgment"', 'status = "judgment"\nset_by = "PHASE-99"'),
    "empty justification": ('justification = "One week of 1h bars."', 'justification = "   "'),
}


@pytest.mark.parametrize("case", list(BAD_CASES))
def test_registry_rejects_violations(tmp_path, case):
    old, new = BAD_CASES[case]
    text = DEFAULT_PATH.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {case}"
    p = tmp_path / "parameters.toml"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(RegistryError):
        load_from(p)


def test_duplicate_id_is_rejected(tmp_path):
    text = DEFAULT_PATH.read_text(encoding="utf-8")
    p = tmp_path / "parameters.toml"
    p.write_text(text + textwrap.dedent('''
        [[parameter]]
        id = 30
        name = "sneaky_extra"
        value = 1
        unit = ""
        status = "fixed"
        used_by = ["nothing"]
        source_spec = "specs/none"
        justification = "none"
        overfitting_exposure = "none"
        '''), encoding="utf-8")
    with pytest.raises(RegistryError, match="duplicate parameter id"):
        load_from(p)


# ---- settings ----------------------------------------------------------------------------

def test_settings_defaults_are_local_and_localhost():
    s = Settings()
    assert s.environment == "local"
    assert s.server.host == "127.0.0.1"
    s.assert_bind_allowed()                      # must not raise


def test_non_localhost_bind_without_a_token_is_refused():
    s = Settings(server={"host": "0.0.0.0"})
    with pytest.raises(RuntimeError, match="refusing to bind"):
        s.assert_bind_allowed()
    Settings(server={"host": "0.0.0.0", "auth_token": "t"}).assert_bind_allowed()


def test_clock_skew_tolerance_is_no_tighter_than_measured_skew():
    # F-026 measured skew within +/-2 s; D-10 requires >= 3 s.
    assert Settings().freshness.clock_skew_tolerance_s >= 3.0


def test_env_vars_override_settings(monkeypatch):
    monkeypatch.setenv("CT_ENVIRONMENT", "azure")
    monkeypatch.setenv("CT_POLLING__FUTURES_RT_S", "20")
    s = Settings()
    assert s.environment == "azure" and s.polling.futures_rt_s == 20


def test_settings_reject_unknown_keys(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text('environment = "local"\nnot_a_setting = 1\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(p)


def test_no_model_parameter_leaks_into_settings():
    """A quantity that can change a signal must live in the registry, not in settings."""
    reg_names = {p.name for p in load_registry().parameters}
    setting_names: set[str] = set()
    s = Settings()
    for group in ("paths", "polling", "freshness", "numerics", "retention", "backup", "server"):
        setting_names |= set(getattr(s, group).model_dump().keys())
    assert reg_names.isdisjoint(setting_names), reg_names & setting_names
