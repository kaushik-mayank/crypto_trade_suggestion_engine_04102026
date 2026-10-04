"""PHASE-20 / PHASE-21 — the risk engine and arbitration.

Two properties dominate here and both are about *refusing well*: the risk engine must never alter a
level a strategy published, and it must evaluate every veto even after one has fired. The second is
what lets an operator tell "the venue was down" from "the trade was uneconomic anyway".
"""
from decimal import Decimal

import pytest

from core.errors import ImmutabilityViolation, RiskError
from features.value import FeatureKey, FeatureSet, FeatureStatus, FeatureValue
from normalize.canonical import Fee, InstrumentMeta, Product, Timeframe
from regime.model import RegimeState
from risk.engine import (
    FUNDING_INTERVALS_PER_HOLD,
    K_NET_FLOOR,
    RiskEngine,
    Signal,
    Tier,
    assert_levels_unchanged,
    breakeven_hit_rate,
    k_net,
    sigma_forward,
)
from snapshot.models import MarketSnapshot, PointInTimeValue, SeriesKey, SeriesState
from strategy.act import Direction
from strategy.base import Lifecycle, RawSignal

T0 = 1767225600000
ASSET = "B-BTC_USDT"
PARAMS = {"risk_per_trade": 0.005, "portfolio_vol_target": 0.15, "unvalidated_multiplier": 0.4,
          "class3_multiplier": 0.5, "clock_skew_tolerance_s": 3.0}


def instrument(**kw):
    base = {"asset": ASSET, "product": Product.FUTURES, "tick": Decimal("0.1"),
            "qty_increment": Decimal("0.001"), "min_notional": Decimal("60"),
            "maker_fee": Fee.from_api_percent("0.02"), "taker_fee": Fee.from_api_percent("0.059"),
            "fetched_at_ms": T0}
    return InstrumentMeta(**{**base, **kw})


def features(overrides: dict | None = None):
    """Feature values keyed by (name, timeframe, horizon); pass None as a value to make it DQ_FAILED."""
    defaults = {("ATR", Timeframe.H1, "short"): 100.0,
                ("VOL_CC", Timeframe.H1, "medium"): 0.004,
                ("VOL_CC", Timeframe.H1, "long"): 0.006,
                ("LIQ_SPREAD", None, "-"): 1.2,
                ("LIQ_DEPTH", None, "-"): 2_000_000.0,
                ("BAS_BPS", None, "-"): -3.5,
                ("FND_RATE", None, "-"): 0.0001}
    defaults.update(overrides or {})
    out = {}
    for (name, tf, horizon), value in defaults.items():
        detail = {}
        if name == "LIQ_SPREAD":
            detail = {"mid": 85000.0}
        if name == "LIQ_DEPTH":
            detail = {"bands": {"25bps": None}, "visible_reach_bps": {"bid": 0.8, "ask": 0.8},
                      "visible_notional": 2_000_000.0}
        if value is None:
            fv = FeatureValue(name=name, version="1", status=FeatureStatus.DQ_FAILED,
                              reason="source unavailable", timeframe=tf, horizon=horizon)
        else:
            fv = FeatureValue(name=name, version="1", status=FeatureStatus.OK, value=value,
                              timeframe=tf, horizon=horizon, detail=detail)
        out[FeatureKey(name, tf, horizon)] = fv
    return FeatureSet(asset=ASSET, product=Product.FUTURES, as_of_ms=T0, values=out)


def snapshot(*, stale=(), clock_ms=None):
    key = SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)
    series = {key: SeriesState(key=key, bars=(), requested=1, min_required=1, ok=True)}
    pit = {}
    for source in ("futures_rt", "spot_ticker", "futures_book"):
        ok = source not in stale
        pit[(source, ASSET)] = PointInTimeValue(
            source=source, asset=ASSET, value=object(), source_ts_ms=T0, age_ms=1000,
            max_age_ms=60_000, ok=ok, reason=None if ok else f"{source} is 900.0s old")
    dq = {"clock_skew_ms": clock_ms} if clock_ms is not None else {}
    return MarketSnapshot(as_of_ms=T0, code_version="v", parameter_hash="ph", series=series,
                          point_in_time=pit, dq=dq)


def raw(**kw):
    base = {"signal_id": "sig-1", "strategy_id": "C1-B", "strategy_version": "1.0.0",
            "asset": ASSET, "product": Product.FUTURES, "direction": Direction.LONG,
            "lifecycle": Lifecycle.SETUP_DETECTED, "as_of_ms": T0,
            "signal_timeframe": Timeframe.H1, "entry": Decimal("85000"),
            "stop": Decimal("84500"), "targets": (Decimal("86500"),),
            "detail": {"tier": "A", "validated": False}}
    return RawSignal(**{**base, **kw})


def regime(**kw):
    base = {"cell": (1, 1), "cell_label": "steady / trending", "dwell_bars": 5, "entered_at_ms": T0,
            "as_of_ms": T0, "axis_values": {"vol_ratio": 1.1, "mr_vr": 0.9}, "axis_boundaries": {},
            "model_version": "v", "grid": 3}
    return RegimeState(**{**base, **kw})


def engine(**kw):
    return RiskEngine(params={**PARAMS, **kw.pop("params", {})}, **kw)


# ---- k_net and the class floors ----------------------------------------------------------

def test_k_net_charges_the_cost_on_both_sides():
    """specs/08 §7.2: the cost is subtracted from the reward *and* added to the risk, because a round
    trip is paid going in and coming out."""
    k = k_net(entry=Decimal("100"), target=Decimal("110"), risk=Decimal("5"),
              cost_price=Decimal("1"))
    assert k == (Decimal("10") - 1) / (Decimal("5") + 1)
    gross = Decimal("10") / Decimal("5")
    assert k < gross                                    # never flattered by ignoring cost


def test_the_class_floors_are_the_derived_constants_not_parameters():
    assert K_NET_FLOOR[3] == Decimal("3.0")             # survives a ~27.5% hit rate
    assert K_NET_FLOOR[1] == K_NET_FLOOR[2] == K_NET_FLOOR[4] == Decimal("1.8")
    import config.parameters as params_mod
    registry = params_mod.load_registry()
    assert "rr_floor" not in registry, "a derived floor must not be tunable"
    assert "k_net_floor" not in registry


def test_the_breakeven_hit_rate_matches_the_identity():
    """p* = (1 + c/R) / (k + 1) — published so a k figure reads as "how often must this work"."""
    p = breakeven_hit_rate(Decimal("1.8"), risk=Decimal("100"), cost_price=Decimal("0"))
    assert p == pytest.approx(Decimal(1) / Decimal("2.8"), abs=1e-9)      # ~35.7%
    p3 = breakeven_hit_rate(Decimal("3.0"), risk=Decimal("100"), cost_price=Decimal("0"))
    assert float(p3) == pytest.approx(0.25, abs=0.01)
    assert breakeven_hit_rate(Decimal("-2"), risk=Decimal("1"), cost_price=Decimal(0)) is None


def test_k_net_refuses_rather_than_dividing_by_zero():
    with pytest.raises(RiskError, match="not positive"):
        k_net(entry=Decimal("100"), target=Decimal("110"), risk=Decimal("0"),
              cost_price=Decimal("0"))


# ---- immutability -------------------------------------------------------------------------

def test_the_risk_engine_wraps_and_never_rewrites():
    signal = raw()
    wrapped = engine().evaluate(signal, snapshot=snapshot(), features=features(), regime=regime(),
                                instrument=instrument(), expansion_seeking=False, strategy_class=1)
    assert wrapped.raw is signal                         # the same object, not a copy
    assert wrapped.raw.entry == Decimal("85000") and wrapped.raw.stop == Decimal("84500")
    assert wrapped.raw.targets == (Decimal("86500"),)


def test_an_altered_level_is_caught_structurally():
    """specs/08 criterion 1. Compared field by field, so a copy that looks equal still has to be."""
    original = raw()
    mutated = raw(stop=Decimal("84000"))
    with pytest.raises(ImmutabilityViolation) as ei:
        assert_levels_unchanged(original, mutated)
    assert ei.value.context["field"] == "stop"
    assert "wraps a RawSignal" in ei.value.remediation
    assert ei.value.severity.value == "critical"


# ---- sizing ------------------------------------------------------------------------------

def test_size_is_the_minimum_of_stop_and_volatility_sizing():
    result = engine(equity=Decimal("10000")).evaluate(
        raw(), snapshot=snapshot(), features=features(), regime=regime(),
        instrument=instrument(), expansion_seeking=False, strategy_class=1).sizing
    assert result.size_stop == (Decimal("10000") * Decimal("0.005")) / Decimal("500")
    assert result.size_vol is not None
    assert result.size == min(result.size_stop, result.size_vol) * Decimal("1") * Decimal("0.4")
    assert result.basis in ("stop", "vol")
    assert result.validation_multiplier == Decimal("0.4")   # nothing is validated before Phase 16


def test_forward_volatility_uses_the_long_horizon_for_an_expansion_seeking_strategy():
    """specs/13 §8.3: C3-A arms inside a contraction, so its forward vol is the un-contracted norm.
    Using the contracted value would give the largest size immediately before an expansion."""
    fs = features({("VOL_CC", Timeframe.H1, "medium"): 0.001,      # contracted now
                     ("VOL_CC", Timeframe.H1, "long"): 0.010})      # the norm it returns to
    contracted = sigma_forward("C1-B", fs, expansion_seeking=False)
    expanded = sigma_forward("C3-A", fs, expansion_seeking=True)
    assert contracted == Decimal("0.001") and expanded == Decimal("0.010")
    assert expanded > contracted * 5


def test_unavailable_forward_volatility_refuses_to_size_rather_than_substituting():
    fs = features({("VOL_CC", Timeframe.H1, "medium"): None})
    sizing = engine().evaluate(raw(), snapshot=snapshot(), features=fs, regime=regime(),
                               instrument=instrument(), expansion_seeking=False,
                               strategy_class=1).sizing
    assert sizing.size is None and sizing.basis == "refused"
    assert "forward volatility is unavailable" in sizing.reason


def test_a_zero_risk_signal_cannot_be_sized():
    sizing = engine().evaluate(raw(stop=Decimal("85000")), snapshot=snapshot(), features=features(),
                               regime=regime(), instrument=instrument(), expansion_seeking=False,
                               strategy_class=1).sizing
    assert sizing.size is None and "R is zero" in sizing.reason


def test_without_equity_sizing_is_a_fraction_and_publishes_the_minimum_equity_needed():
    """specs/08 §2.3 and §9: no balance is read from the exchange, and min_notional is published as a
    constraint rather than satisfied by over-risking."""
    wrapped = engine().evaluate(raw(), snapshot=snapshot(), features=features(), regime=regime(),
                                instrument=instrument(), expansion_seeking=False, strategy_class=1)
    assert wrapped.equity is None
    assert wrapped.sizing.size is not None and wrapped.sizing.notional is None
    assert wrapped.sizing.min_equity_required is not None
    assert "no balance is read from the exchange" in wrapped.sizing.reason
    min_notional_veto = next(v for v in wrapped.vetoes if v.name == "MIN_NOTIONAL")
    assert min_notional_veto.unavailable and not min_notional_veto.fired


def test_the_class_3_multiplier_applies_only_to_class_3():
    one = engine(equity=Decimal("10000")).evaluate(
        raw(), snapshot=snapshot(), features=features(), regime=regime(), instrument=instrument(),
        expansion_seeking=False, strategy_class=1).sizing
    three = engine(equity=Decimal("10000")).evaluate(
        raw(), snapshot=snapshot(), features=features(), regime=regime(), instrument=instrument(),
        expansion_seeking=False, strategy_class=3).sizing
    assert one.class_multiplier == Decimal("1") and three.class_multiplier == Decimal("0.5")
    assert three.size == one.size / 2


# ---- the veto ladder ----------------------------------------------------------------------

def test_every_tier_is_evaluated_even_after_a_veto_fires():
    """specs/08 §8: a signal failing four checks is diagnostically different from one failing one,
    and short-circuiting hides whether an outage was masking an uneconomic trade."""
    wrapped = engine().evaluate(raw(), snapshot=snapshot(stale=("futures_rt",)), features=features(),
                                regime=regime(), instrument=instrument(), expansion_seeking=False,
                                strategy_class=1)
    tiers = {v.tier for v in wrapped.vetoes}
    assert tiers == {Tier.DATA_INTEGRITY, Tier.ECONOMIC, Tier.MARKET, Tier.PORTFOLIO}
    assert any(v.name == "DQ_STALE" and v.fired for v in wrapped.vetoes)
    # the economic tier still ran and still reported
    assert any(v.name == "RR_FLOOR" for v in wrapped.vetoes)
    assert not wrapped.approved


def test_missing_instrument_metadata_vetoes_because_costs_become_uncomputable():
    wrapped = engine().evaluate(raw(), snapshot=snapshot(), features=features(), regime=regime(),
                                instrument=None, expansion_seeking=False, strategy_class=1)
    meta = next(v for v in wrapped.vetoes if v.name == "DQ_META")
    assert meta.fired and "no signal is possible" in meta.detail
    assert not wrapped.approved


def test_the_skw_veto_is_recorded_as_deliberately_not_implemented():
    """specs/08 §8 lists DQ_SKW; F-004 shows it is not a per-row health flag. The deviation is visible
    in the ladder rather than silently omitted."""
    wrapped = engine().evaluate(raw(), snapshot=snapshot(), features=features(), regime=regime(),
                                instrument=instrument(), expansion_seeking=False, strategy_class=1)
    skw = next(v for v in wrapped.vetoes if v.name == "DQ_SKW")
    assert skw.unavailable and not skw.fired
    assert "F-004" in skw.detail


def test_a_tick_degenerate_stop_is_vetoed():
    wrapped = engine().evaluate(raw(stop=Decimal("85000.05")), snapshot=snapshot(),
                                features=features(), regime=regime(), instrument=instrument(),
                                expansion_seeking=False, strategy_class=1)
    tick = next(v for v in wrapped.vetoes if v.name == "TICK_DEGENERATE")
    assert tick.fired and "ticks" in tick.detail


def test_a_wide_spread_is_vetoed_against_the_trades_own_risk():
    """Structural: expressed as a fraction of R so it scales across assets and regimes."""
    wide = features({("LIQ_SPREAD", None, "-"): 300.0})     # 300 bps
    wrapped = engine().evaluate(raw(), snapshot=snapshot(), features=wide, regime=regime(),
                                instrument=instrument(), expansion_seeking=False, strategy_class=1)
    spread = next(v for v in wrapped.vetoes if v.name == "SPREAD")
    assert spread.fired and spread.threshold == 0.10
    assert "of R" in spread.detail


def test_the_depth_veto_reports_unmeasurable_rather_than_passing_silently():
    """F-032: 25 bps depth is not observable at this venue, so the veto says so and names what is."""
    wrapped = engine().evaluate(raw(), snapshot=snapshot(), features=features(), regime=regime(),
                                instrument=instrument(), expansion_seeking=False, strategy_class=1)
    depth = next(v for v in wrapped.vetoes if v.name == "DEPTH")
    assert depth.unavailable and not depth.fired
    assert "F-032" in depth.detail and "visible-book" in depth.detail.lower()


def test_the_volatility_veto_reads_the_flag_and_names_no_strategy():
    """specs/08 §8: 'No special-casing in the risk engine.'"""
    hot = regime(axis_values={"vol_ratio": 2.5, "mr_vr": 0.9})
    normal = engine().evaluate(raw(), snapshot=snapshot(), features=features(), regime=hot,
                               instrument=instrument(), expansion_seeking=False, strategy_class=1)
    seeking = engine().evaluate(raw(), snapshot=snapshot(), features=features(), regime=hot,
                                instrument=instrument(), expansion_seeking=True, strategy_class=3)
    assert next(v for v in normal.vetoes if v.name == "VOL_EXTREME").fired
    exempt = next(v for v in seeking.vetoes if v.name == "VOL_EXTREME")
    assert not exempt.fired and "expansion-seeking" in exempt.detail

    import inspect

    import risk.engine as risk_module
    source = inspect.getsource(risk_module)
    for name in ("C3-A", "C1-B", "C2-A"):
        assert f'"{name}"' not in source, f"the risk engine names {name}"


def test_adverse_funding_is_a_veto_through_the_same_floor_not_an_invented_threshold():
    """specs/08 §7.3. At the ±2% cap the carry is 12% over two days, which no swing target survives —
    and the arithmetic says so without a number anyone chose."""
    extreme = features({("FND_RATE", None, "-"): 0.02})
    wrapped = engine().evaluate(raw(), snapshot=snapshot(), features=extreme, regime=regime(),
                                instrument=instrument(), expansion_seeking=False, strategy_class=1)
    funding = next(v for v in wrapped.vetoes if v.name == "FUNDING_ADVERSE")
    assert funding.fired
    assert str(FUNDING_INTERVALS_PER_HOLD) in funding.detail
    assert funding.threshold == float(K_NET_FLOOR[1])

    benign = engine().evaluate(raw(), snapshot=snapshot(), features=features(), regime=regime(),
                               instrument=instrument(), expansion_seeking=False, strategy_class=1)
    assert not next(v for v in benign.vetoes if v.name == "FUNDING_ADVERSE").fired


def test_a_signal_that_passes_everything_is_approved_and_carries_its_arithmetic():
    wrapped = engine().evaluate(raw(targets=(Decimal("87000"),)), snapshot=snapshot(),
                                features=features(), regime=regime(), instrument=instrument(),
                                expansion_seeking=False, strategy_class=1)
    assert wrapped.approved, [v.name for v in wrapped.fired_vetoes]
    assert wrapped.detail["k_net"] is not None
    assert wrapped.detail["breakeven_hit_rate"] is not None
    assert wrapped.detail["k_net_floor"] == "1.8"
    import json
    json.dumps(wrapped.to_dict())


def test_a_thin_reward_is_refused_on_the_cost_adjusted_figure():
    thin = raw(targets=(Decimal("85400"),))          # gross R:R 0.8, well under the floor
    wrapped = engine().evaluate(thin, snapshot=snapshot(), features=features(), regime=regime(),
                                instrument=instrument(), expansion_seeking=False, strategy_class=1)
    rr = next(v for v in wrapped.vetoes if v.name == "RR_FLOOR")
    assert rr.fired and rr.threshold == 1.8
    assert "k_net" in rr.detail and "breakeven" in rr.detail


def test_class_three_faces_the_higher_floor():
    signal = raw(targets=(Decimal("86200"),))        # clears 1.8, not 3.0
    one = engine().evaluate(signal, snapshot=snapshot(), features=features(), regime=regime(),
                            instrument=instrument(), expansion_seeking=False, strategy_class=1)
    three = engine().evaluate(signal, snapshot=snapshot(), features=features(), regime=regime(),
                              instrument=instrument(), expansion_seeking=False, strategy_class=3)
    assert not next(v for v in one.vetoes if v.name == "RR_FLOOR").fired
    assert next(v for v in three.vetoes if v.name == "RR_FLOOR").fired


def test_concurrent_class_three_positions_are_capped():
    wrapped = engine(open_class3=2).evaluate(
        raw(targets=(Decimal("90000"),)), snapshot=snapshot(), features=features(), regime=regime(),
        instrument=instrument(), expansion_seeking=False, strategy_class=3)
    veto = next(v for v in wrapped.vetoes if v.name == "CLASS3_CONCURRENT")
    assert veto.fired and veto.threshold == 2


def test_a_skewed_clock_vetoes_on_infrastructure_grounds():
    wrapped = engine().evaluate(raw(), snapshot=snapshot(clock_ms=9000), features=features(),
                                regime=regime(), instrument=instrument(), expansion_seeking=False,
                                strategy_class=1)
    clock = next(v for v in wrapped.vetoes if v.name == "DQ_CLOCK")
    assert clock.fired and clock.threshold == 3000.0


# ---- arbitration ---------------------------------------------------------------------------

def approved_signal(strategy_id, direction, *, tier="A", tf=Timeframe.H1, k_net_value="2.5",
                    asset=ASSET, signal_id=None, strategy_class=1):
    from risk.engine import SizingResult

    r = raw(strategy_id=strategy_id, direction=direction, asset=asset,
            signal_id=signal_id or f"{strategy_id}-{asset}-{direction.value}",
            signal_timeframe=tf, detail={"tier": tier, "validated": False,
                                         "strategy_class": strategy_class})
    sizing = SizingResult(size=Decimal("1"), size_stop=Decimal("1"), size_vol=Decimal("2"),
                          basis="stop", sigma_forward=Decimal("0.004"),
                          class_multiplier=Decimal("1"), validation_multiplier=Decimal("0.4"))
    return Signal(raw=r, sizing=sizing, vetoes=(), approved=True,
                  detail={"k_net": k_net_value})


def test_opposing_signals_resolve_on_evidence_before_anything_else():
    from arbitration import arbitrate

    tier_a = approved_signal("C1-B", Direction.LONG, tier="A")
    tier_b = approved_signal("C1-A", Direction.SHORT, tier="B")
    result = arbitrate([tier_a, tier_b], at_ms=T0)
    assert [s.raw.strategy_id for s in result.published] == ["C1-B"]
    assert result.suppressed[0][0].raw.strategy_id == "C1-A"
    assert "evidential standing" in result.events[0].criterion


def test_equal_standing_falls_through_to_timeframe_seniority():
    from arbitration import arbitrate

    daily = approved_signal("C4-A", Direction.SHORT, tier="A", tf=Timeframe.D1)
    hourly = approved_signal("C1-B", Direction.LONG, tier="A", tf=Timeframe.H1)
    result = arbitrate([hourly, daily], at_ms=T0)
    assert [s.raw.strategy_id for s in result.published] == ["C4-A"]
    assert "timeframe seniority" in result.events[0].criterion


def test_a_genuine_tie_suppresses_both_and_marks_the_asset_conflicted():
    """specs/07 §4: ambiguity between equally-supported opposing readings means no trade, and the UI
    must say "conflicting evidence" rather than show nothing."""
    from arbitration import arbitrate

    long = approved_signal("C1-B", Direction.LONG, tier="A")
    short = approved_signal("C2-A", Direction.SHORT, tier="A")
    result = arbitrate([long, short], at_ms=T0)
    assert result.published == ()
    assert len(result.suppressed) == 2
    assert result.conflicting_assets == (ASSET,)
    assert "suppress both" in result.events[0].criterion


def test_same_direction_signals_merge_into_one_risk_allocation():
    from arbitration import arbitrate

    primary = approved_signal("C1-B", Direction.LONG, tier="A")
    corroborator = approved_signal("C1-A", Direction.LONG, tier="B")
    result = arbitrate([primary, corroborator], at_ms=T0)
    assert len(result.published) == 1 and result.published[0].raw.strategy_id == "C1-B"
    assert result.corroborations[primary.raw.signal_id] == (corroborator.raw.signal_id,)
    merge = next(e for e in result.events if e.kind == "merge")
    assert "not** additive" in merge.detail or "not additive" in merge.detail


def test_dedup_runs_after_conflict_resolution_so_a_survivor_is_not_lost():
    """specs/07 §3 warns that dedup-first silently loses good signals."""
    import inspect

    import arbitration.pipeline as pipe

    source = inspect.getsource(pipe.arbitrate)
    assert source.index("resolve_conflicts") < source.index("dedup_class4")
    assert source.index("resolve_conflicts") < source.index("merge_same_direction")


def test_refused_signals_are_suppressed_with_the_veto_names():
    from arbitration import arbitrate
    from risk.engine import SizingResult, VetoResult

    refused = Signal(raw=raw(), sizing=SizingResult(size=None, size_stop=None, size_vol=None,
                                                    basis="refused", sigma_forward=None,
                                                    class_multiplier=Decimal(1),
                                                    validation_multiplier=Decimal(1)),
                     vetoes=(VetoResult(name="DQ_STALE", tier=Tier.DATA_INTEGRITY, fired=True,
                                        detail="stale"),), approved=False)
    result = arbitrate([refused], at_ms=T0)
    assert result.published == ()
    assert "DQ_STALE" in result.suppressed[0][1]


def test_the_portfolio_view_names_the_concentration_risk():
    from arbitration import expose_portfolio

    signals = [approved_signal("C1-B", Direction.LONG, asset=a, signal_id=f"s-{a}")
               for a in ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")]
    view = expose_portfolio(signals)
    assert view["long"] == 3 and view["short"] == 0
    assert "one leveraged position" in view["concentration_note"]


def test_benchmark_mode_bypasses_the_pipeline_uncensored():
    """specs/06 §3: the benchmark measures what the strategies would have done without the system's
    own filtering, so filtering it would defeat the purpose."""
    from arbitration import arbitrate

    long = approved_signal("C4-A", Direction.LONG, tier="A")
    short = approved_signal("C4-B", Direction.SHORT, tier="A")
    result = arbitrate([long, short], at_ms=T0, benchmark_mode=True)
    assert len(result.published) == 2           # both survive, despite opposing
    assert "uncensored" in result.events[0].detail


def test_a_class_four_signal_loses_the_merge_to_a_bespoke_one_on_the_same_side():
    """specs/06 §3 deduplication, and the reason it is a *tiebreak* rather than a separate pass.

    Both signals here are Tier A, unvalidated and on the 1h timeframe, so they tie on evidential
    standing and on seniority. Without the bespoke-over-benchmark tiebreak, ``max()`` would keep
    whichever happened to be evaluated first — and the benchmark would sometimes suppress the very
    strategy it exists to measure, with nothing in the output looking wrong.
    """
    from arbitration import arbitrate

    bespoke = approved_signal("C2-A", Direction.LONG, tier="A", strategy_class=2,
                              signal_id="bespoke-1")
    benchmark = approved_signal("C4-A", Direction.LONG, tier="A", strategy_class=4,
                                signal_id="benchmark-1")
    for order in ([bespoke, benchmark], [benchmark, bespoke]):
        result = arbitrate(order, at_ms=T0)
        assert [s.raw.strategy_id for s in result.published] == ["C2-A"], \
            "the ordering of the input changed the winner"
        suppressed, reason = result.suppressed[0]
        assert suppressed.raw.strategy_id == "C4-A"
        # specs/06 §10 requires `suppressed_by` to name the superseding *signal*, not just its
        # strategy: a strategy id alone would not identify which evaluation superseded it.
        assert "suppressed_by=bespoke-1" in reason
        assert result.corroborations["bespoke-1"] == ("benchmark-1",)
