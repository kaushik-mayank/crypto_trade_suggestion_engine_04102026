"""PHASE-16/22 — the runner wiring the chain together, and the audit trail that records it.

The failure-isolation tests are the point: one asset's data problem must not silence the other two,
and a snapshot failure must stop the pass rather than publish signals from a market nobody observed.
Those are opposite behaviours and both are deliberate.
"""
import json

import numpy as np
import pytest

from app.runner import AppRunner, load_classifiers
from audit.trail import AuditTrail
from config.parameters import load_registry
from core.errors import ConfigError, StartupError, StoreError
from features.engine import FeatureEngine
from normalize.canonical import Bar, Product, Timeframe
from regime.classifier import RegimeClassifier
from regime.fit import FitInputs, fit
from risk.engine import RiskEngine
from snapshot.builder import SeriesRequest, SnapshotBuilder, SnapshotRequest
from snapshot.models import InstrumentKey, SeriesKey
from store import open_store
from strategy.c1_correction import C1A, C1B

H = 3_600_000
T0 = 1767225600000
ASSETS = ("B-BTC_USDT", "B-ETH_USDT")
REGISTRY = load_registry()
PARAMS = {p.name: p.value for p in REGISTRY.parameters}


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "runner.db")
    yield s
    s.close()


def seed(store, asset, *, hours=1500, minutes=300, seed_value=3, from_hour=0):
    """Enough real-shaped history for the 1h/4h/1d alpha set and the 15m/1m trigger set."""
    rng = np.random.default_rng(seed_value)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.006, hours)))
    bars = []
    for i, c in enumerate(closes):
        if i < from_hour:
            continue
        o = closes[i - 1] if i else c
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.003)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.003)))
        bars.append(Bar(asset=asset, product=Product.FUTURES, timeframe=Timeframe.H1,
                        open_time_ms=T0 + i * H, open=str(o), high=str(hi), low=str(lo),
                        close=str(c), volume_base="100", is_final=True, fetched_at_ms=T0))
    store.upsert_bars(bars)

    end = T0 + hours * H
    for tf, count in ((Timeframe.H4, 400), (Timeframe.D1, 200), (Timeframe.M15, minutes),
                      (Timeframe.M1, minutes)):
        rows = []
        for i in range(count):
            t = end - (count - i) * tf.ms
            if tf in (Timeframe.H4, Timeframe.D1):
                t = (t // tf.ms) * tf.ms
            c = float(closes[-1]) * (1 + rng.normal(0, 0.001))
            rows.append(Bar(asset=asset, product=Product.FUTURES, timeframe=tf, open_time_ms=t,
                            open=str(c), high=str(c * 1.002), low=str(c * 0.998), close=str(c),
                            volume_base="10", is_final=True, fetched_at_ms=T0))
        store.upsert_bars(rows)
    return end


def make_classifier(asset, *, degraded=False):
    rng = np.random.default_rng(1)
    n = 1200
    if degraded:
        axis_v, axis_p = rng.normal(0, 1, n), rng.normal(0, 1, n)
        fwd = rng.normal(0, 0.01, n)
    else:
        axis_v, axis_p = np.cumsum(rng.normal(0, 0.05, n)), np.cumsum(rng.normal(0, 0.05, n))
        fwd = rng.normal(0, 0.01, n) + 0.05 * np.sign(axis_p)
    inputs = FitInputs(times_ms=T0 + np.arange(n, dtype=np.int64) * H, axis_v=axis_v, axis_p=axis_p,
                       forward_return=fwd, asset=asset)
    return RegimeClassifier(model=fit(inputs, quantile_lookback=200))


def build_request(assets=ASSETS, *, bars_1h=1440):
    series = []
    for a in assets:
        series.append(SeriesRequest(SeriesKey(a, Product.FUTURES, Timeframe.H1), bars=bars_1h))
        series.append(SeriesRequest(SeriesKey(a, Product.FUTURES, Timeframe.H4), bars=360))
        series.append(SeriesRequest(SeriesKey(a, Product.FUTURES, Timeframe.D1), bars=180))
        series.append(SeriesRequest(SeriesKey(a, Product.FUTURES, Timeframe.M15), bars=200))
        series.append(SeriesRequest(SeriesKey(a, Product.FUTURES, Timeframe.M1), bars=240))
    return SnapshotRequest(
        series=tuple(series),
        tier_b=tuple((s, a) for a in assets
                     for s in ("futures_rt", "spot_ticker", "positioning", "futures_book",
                               "spot_book")),
        instruments=tuple(InstrumentKey(a, Product.FUTURES) for a in assets),
        forming=tuple(SeriesKey(a, Product.FUTURES, Timeframe.H1) for a in assets),
        tier_b_history_ms=7 * 86_400_000)


def build_runner(store, *, assets=ASSETS, request=None, strategies=None, classifiers=None):
    engine = FeatureEngine(PARAMS, parameter_hash=REGISTRY.parameter_hash,
                           unset_params=frozenset(p.name for p in REGISTRY.parameters
                                                  if p.status == "unset"))
    return AppRunner(
        builder=SnapshotBuilder(store=store, parameter_hash=REGISTRY.parameter_hash),
        engine=engine, request=request or build_request(assets),
        strategies=strategies if strategies is not None else [C1B(params=PARAMS),
                                                              C1A(params=PARAMS)],
        risk=RiskEngine(params=PARAMS),
        classifiers=classifiers or {a: make_classifier(a) for a in assets},
        assets=tuple(assets))


# ---- preflight ------------------------------------------------------------------------------

def test_preflight_names_every_problem_at_once(store):
    """Discovering a shallow request as a wall of WARMING an hour later trains the operator to ignore
    the status that also means "the venue is down"."""
    shallow = SnapshotRequest(series=(SeriesRequest(SeriesKey(ASSETS[0], Product.FUTURES,
                                                              Timeframe.H1), bars=100),))
    runner = build_runner(store, request=shallow)
    with pytest.raises(StartupError) as ei:
        runner.preflight()
    problems = ei.value.context["problems"]
    assert len(problems) > 3, problems
    joined = " | ".join(problems)
    assert "1m" in joined and "15m" in joined            # ACT's timeframes
    assert "tier_b" in joined                            # Tier B sources and history
    assert "cannot start" in ei.value.message


def test_preflight_refuses_a_control_strategy_in_a_live_runner(store):
    from strategy.controls import C4X1

    runner = build_runner(store, strategies=[C4X1(params=PARAMS)])
    with pytest.raises(StartupError) as ei:
        runner.preflight()
    assert any("control strategy" in p for p in ei.value.context["problems"])


def test_preflight_refuses_a_missing_regime_model(store):
    runner = build_runner(store, classifiers={ASSETS[0]: make_classifier(ASSETS[0])})
    with pytest.raises(StartupError) as ei:
        runner.preflight()
    assert any("no regime model loaded" in p for p in ei.value.context["problems"])


def test_preflight_passes_on_a_well_formed_setup(store):
    runner = build_runner(store)
    assert runner.preflight() == []


def test_loading_a_missing_regime_artifact_fails_as_configuration(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_classifiers(["B-BTC_USDT"], directory=str(tmp_path))
    assert "never fits its own model" in ei.value.remediation


# ---- the pass -------------------------------------------------------------------------------

def test_a_full_pass_evaluates_every_asset_and_strategy(store):
    for asset in ASSETS:
        as_of = seed(store, asset)
    runner = build_runner(store)
    runner.preflight()
    result = runner.evaluate(as_of + 60_000)

    assert result.error is None
    assert set(result.assets) == set(ASSETS)
    for asset_result in result.assets.values():
        assert asset_result.ok, asset_result.error
        assert set(asset_result.outcomes) == {"C1-B", "C1-A"}
        assert asset_result.features is not None and len(asset_result.features) > 100
        assert asset_result.regime is not None
    assert result.arbitration is not None
    assert runner.evaluations == 1 and runner.failures == 0


def test_every_outcome_carries_its_full_condition_evaluation(store):
    """Without the rejection record, "Class 1 never fired" is an observation with no diagnosis."""
    for asset in ASSETS:
        as_of = seed(store, asset)
    runner = build_runner(store)
    result = runner.evaluate(as_of + 60_000)
    outcome = result.assets[ASSETS[0]].outcomes["C1-B"]
    assert outcome.kind in ("NoSetup", "Setup")
    assert len(outcome.conditions) == 6                    # every C1-B condition, always
    assert outcome.summary["total"] == 6
    assert all(c.description for c in outcome.conditions)


def test_one_assets_failure_does_not_silence_the_others(store):
    """A SOL data problem is not a reason to stop reporting BTC."""
    as_of = seed(store, ASSETS[0])
    seed(store, ASSETS[1], hours=1500)
    runner = build_runner(store)

    broken = ASSETS[1]
    original = runner.engine.compute

    def compute(snapshot, asset, product, **kw):
        if asset == broken:
            raise StoreError("simulated read failure", component="store", operation="get_bars")
        return original(snapshot, asset, product, **kw)

    runner.engine.compute = compute                        # type: ignore[method-assign]
    result = runner.evaluate(as_of + 60_000)

    assert result.assets[ASSETS[0]].ok                     # the healthy asset still evaluated
    assert not result.assets[broken].ok
    assert result.assets[broken].error.code == "STORE_FAILURE"
    assert runner.failures == 1
    assert runner.health().status.value in ("DEGRADED", "HEALTHY")


def test_a_snapshot_failure_stops_the_pass_rather_than_publishing_from_nothing(store):
    """The opposite decision from per-asset isolation, and deliberately so: with no snapshot there is
    nothing to evaluate, and publishing would mean signals from a market nobody observed."""
    as_of = seed(store, ASSETS[0])
    seed(store, ASSETS[1])
    runner = build_runner(store)

    def broken_build(*a, **kw):
        raise StoreError("database is locked", component="store", operation="get_bars")

    runner.builder.build = broken_build                    # type: ignore[method-assign]
    result = runner.evaluate(as_of + 60_000)

    assert result.error is not None and result.error.code == "STORE_FAILURE"
    assert result.assets == {}
    assert result.published == ()
    assert runner.failures == 1
    health = runner.health()
    assert health.status.value == "DEGRADED"
    assert any(c.problem_class and c.problem_class.value == "infrastructure"
               for c in health.components)


def test_the_health_report_classifies_where_to_look(store):
    """The whole point of ProblemClass: on Azure this decides whether to check the code, the config,
    the VM or the venue."""
    for asset in ASSETS:
        seed(store, asset)
    runner = build_runner(store, classifiers={a: make_classifier(a, degraded=(a == ASSETS[1]))
                                              for a in ASSETS})
    report = runner.health()
    by_name = {c.name: c for c in report.components}
    degraded = by_name[f"regime:{ASSETS[1]}"]
    assert degraded.status.value == "DEGRADED"
    assert degraded.problem_class.value == "modelling"     # not a bug, not a feed problem
    assert "reduced selectivity" in (degraded.remediation or "")
    json.dumps(report.to_dict())


def test_a_terminal_setup_is_forgotten_so_it_cannot_be_revived(store):
    from strategy.act import Direction
    from strategy.base import Lifecycle, SetupState, terminal

    runner = build_runner(store)
    key = ("C1-B", ASSETS[0])
    state = SetupState(signal_id="s1", strategy_id="C1-B", asset=ASSETS[0],
                       product=Product.FUTURES, direction=Direction.LONG, armed_at_ms=T0,
                       armed_snapshot_id="snap", expires_at_ms=T0 + H,
                       lifecycle=Lifecycle.SETUP_DETECTED)
    runner.states[key] = state
    state.lifecycle = Lifecycle.EXPIRED
    runner._remember(key, terminal("expired", (), None, state))
    assert key not in runner.states


# ---- the audit trail -------------------------------------------------------------------------

def test_every_rejection_is_recorded_with_its_binding_condition(store):
    for asset in ASSETS:
        as_of = seed(store, asset)
    runner = build_runner(store)
    result = runner.evaluate(as_of + 60_000)
    counts = AuditTrail(store=store).record_evaluation(result)

    assert counts["rejections"] >= 1
    rows = store.conn.execute("SELECT * FROM rejections").fetchall()
    assert rows
    for row in rows:
        assert row["strategy_id"] in ("C1-B", "C1-A")
        assert row["total_count"] > 0
        conditions = json.loads(row["conditions"])
        assert len(conditions) == row["total_count"]       # ALL results, not just the failing one
        assert row["binding_condition"] is not None


def test_the_audit_write_is_all_or_nothing(store):
    """A partial audit record is worse than none, because it looks complete."""
    for asset in ASSETS:
        as_of = seed(store, asset)
    runner = build_runner(store)
    result = runner.evaluate(as_of + 60_000)
    trail = AuditTrail(store=store)

    def explode(*a, **kw):
        raise RuntimeError("disk full halfway through")

    trail._write_rejection = explode                       # type: ignore[method-assign]
    from core.errors import AuditError
    with pytest.raises(AuditError) as ei:
        trail.record_evaluation(result)
    assert "rolled back" in ei.value.remediation
    assert store.conn.execute("SELECT COUNT(*) AS n FROM rejections").fetchone()["n"] == 0


def test_binding_conditions_answer_which_condition_blocks_most_often(store):
    for asset in ASSETS:
        as_of = seed(store, asset)
    runner = build_runner(store)
    trail = AuditTrail(store=store)
    for i in range(3):
        trail.record_evaluation(runner.evaluate(as_of + 60_000 + i * 60_000))
    binding = trail.binding_conditions()
    assert binding
    assert binding[0]["n"] >= 2
    assert {b["strategy_id"] for b in binding} <= {"C1-B", "C1-A"}
