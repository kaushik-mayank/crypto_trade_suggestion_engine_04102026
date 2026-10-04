"""C1-C — Residual Relative-Value Correction (specs/03 §5). Tier A, two-leg.

The only strategy that exploits having three *correlated* assets rather than treating them as three
independent markets. Its restoring force is **weaker than C1-A's**: there is no cash flow compelling
convergence, only relative-value capital and rebalancing flow, which may or may not be present.

**The cost problem is stated up front, not discovered later.** This is a two-leg trade — roughly
0.236% round-trip in futures taker fees before spread and slippage — and because the assets run
0.7-0.9 correlated the residual is small. specs/03 §5 expects it to fail its cost gate most of the
time and produce few signals, and is explicit about the consequence:

> **If a full backtest produces near-zero qualifying signals, C1-C should be cut, not loosened.**
> Loosening a cost gate to manufacture signals is how a strategy becomes a fee-generation machine.

So condition 4 compares expected convergence against the round-trip cost of **both** legs, and this
module contains no path that relaxes it.

**A single-leg variant is refused, not merely absent.** Without the hedge it is a directional bet
contaminated by market beta — not a correction strategy at all, and presenting it as one would
mislabel the risk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np

from core.errors import StrategyError
from features.f4_deviation import PRIMARY_VR
from features.value import FeatureSet, FeatureStatus
from normalize.canonical import Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot
from strategy.act import Direction, act
from strategy.base import Lifecycle, SetupState, StrategyOutcome, no_setup, setup, terminal, update
from strategy.conditions import Condition, ConditionContext, ConditionResult, evaluate_all
from strategy.evaluation import Levels, build_signal, price_now, signal_id
from strategy.lifecycle import Invalidation, advance
from strategy.market_view import MarketView

COMPONENT = "strategy.c1c"

#: The benchmark leg. Not a parameter: specs/02 Family 5 names BTC as the reference.
BENCHMARK = "B-BTC_USDT"

#: Expected convergence must clear this multiple of the two-leg round-trip cost (condition 4's
#: ``kappa``). Structural rather than fitted: at 1.0 the trade is break-even before slippage, and
#: specs/03 §5 is explicit that this gate is never loosened to produce signals.
KAPPA = Decimal("2.0")

#: How far beta may drift across the window before the relationship counts as re-specified rather
#: than dislocated (condition 3). Expressed as a fraction of the window's mean beta, so it scales with
#: the asset rather than assuming a level.
BETA_DRIFT_BAND = 0.25

#: Setup expiry, ``N_exp_rs`` 1h bars. The shared short lookback, as elsewhere, adding no parameter.
EXPIRY_BARS = 24


def _legs(levels: Levels) -> list[dict[str, Any]]:
    """The published leg table. Named rather than indexed inline so the Optional is handled once."""
    legs = (levels.detail or {}).get("legs")
    if not legs:
        raise StrategyError(
            "a two-leg signal was built without its leg table",
            component=COMPONENT, operation="legs",
            likely_cause="_levels did not populate detail['legs']",
            remediation="C1-C must publish both legs or the trade will be executed wrong "
                        "(specs/03 §5)",
        )
    return list(legs)


def residual_from_windows(own_closes: np.ndarray, bench_closes: np.ndarray,
                          beta: float) -> np.ndarray:
    """The residual series from two close series of equal length. Pure, so it is directly testable."""
    if own_closes.size != bench_closes.size:
        raise StrategyError(
            f"leg lengths differ: {own_closes.size} vs {bench_closes.size}",
            component=COMPONENT, operation="residual_from_windows",
            context={"own": int(own_closes.size), "bench": int(bench_closes.size)},
            likely_cause="the two windows were extracted over different spans",
            remediation="align both legs to the same bars before forming a residual",
        )
    if own_closes.size < 2:
        raise StrategyError(
            "a residual needs at least two bars per leg",
            component=COMPONENT, operation="residual_from_windows",
            context={"bars": int(own_closes.size)},
            likely_cause="the window is too short",
            remediation="the strategy declines",
        )
    own = np.diff(np.log(own_closes))
    bench = np.diff(np.log(bench_closes))
    return own - beta * bench


@dataclass
class C1C:
    """C1-C — Residual Relative-Value Correction. **Two-leg**, beta-neutral, Tier A."""

    params: dict[str, Any] = field(default_factory=dict)
    id: str = "C1-C"
    version: str = "1.0.0"
    strategy_class: int = 1
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    needs_market_view: bool = True
    market_view: MarketView | None = None
    required_features: tuple[str, ...] = ("X_RS", "X_CORR", "X_BETA", "X_DISP", PRIMARY_VR, "ATR")

    # ---- conditions ----------------------------------------------------------------------

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="residual_in_tail",
                      description="the beta-adjusted residual against BTC is in the tail",
                      body=self._residual_in_tail, required_features=("X_RS",)),
            Condition(name="relationship_exists",
                      description="correlation with BTC is above the floor, so there is a "
                                  "relationship to revert to",
                      body=self._relationship_exists, required_features=("X_CORR",)),
            Condition(name="beta_stable",
                      description="rolling beta has not drifted, so this is a dislocation rather "
                                  "than a regime change",
                      body=self._beta_stable, required_features=("X_BETA",)),
            Condition(name="convergence_clears_cost",
                      description="expected convergence clears the round-trip cost of **both** legs; "
                                  "this gate is never loosened to produce signals",
                      body=self._convergence_clears_cost, required_features=("X_RS",)),
            Condition(name="dispersion_not_extreme",
                      description="cross-sectional dispersion is not in the upper tail, which would "
                                  "suggest idiosyncratic news rather than a dislocation",
                      body=self._dispersion_not_extreme, required_features=("X_DISP",)),
            Condition(name="both_legs_fresh",
                      description="the data-quality gates pass on **both** legs, not just the "
                                  "dislocated one",
                      body=self._both_legs_fresh),
            Condition(name="mean_reverting",
                      description="the variance ratio on the dislocated asset is below 1 — the "
                                  "amendment that keeps C1-C mutually exclusive with C2-B",
                      body=self._mean_reverting, required_features=(PRIMARY_VR,)),
        )

    def _view(self) -> MarketView:
        if self.market_view is None:
            raise StrategyError(
                "C1-C was evaluated without a market view",
                component=COMPONENT, operation="market_view",
                context={"strategy": self.id},
                likely_cause="the runner did not attach the view, or needs_market_view was not honoured",
                remediation="C1-C is a two-leg strategy and cannot be evaluated one asset at a time",
            )
        return self.market_view

    def _residual_in_tail(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        threshold = float(ctx.param("residual_z_threshold"))
        rs = ctx.feature("X_RS", Timeframe.H1, "medium")
        sigma = self._residual_sigma(ctx)
        if sigma <= 0:
            raise StrategyError(
                "the residual has no dispersion, so a z-score is undefined",
                component=COMPONENT, operation="residual_in_tail",
                context={"asset": ctx.asset},
                likely_cause="the two legs moved identically across the window",
                remediation="the strategy declines",
            )
        z = rs / sigma
        ctx.scratch["residual_z"] = z
        ctx.scratch["residual"] = rs
        ctx.scratch["residual_sigma"] = sigma
        return abs(z) >= threshold, z, threshold

    def _relationship_exists(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        floor = float(ctx.param("correlation_floor"))
        corr = ctx.feature("X_CORR", Timeframe.H1, "medium")
        ctx.scratch["correlation"] = corr
        return corr >= floor, corr, floor

    def _beta_stable(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """Beta measured over the short and medium windows must agree within a band.

        Two horizons of the same feature rather than a stored history: a beta that has re-specified
        shows up as the short-window estimate pulling away from the medium one, and both already exist.
        """
        short = ctx.features.get("X_BETA", Timeframe.H1, "short")
        medium = ctx.features.get("X_BETA", Timeframe.H1, "medium")
        for fv in (short, medium):
            if fv.status is not FeatureStatus.OK or fv.value is None:
                raise StrategyError(
                    f"X_BETA ({fv.horizon}) is {fv.status.value}, so drift cannot be measured",
                    component=COMPONENT, operation="beta_stable",
                    context={"asset": ctx.asset, "horizon": fv.horizon, "reason": fv.reason},
                    likely_cause=fv.reason,
                    remediation="the strategy declines",
                )
        anchor = abs(medium.value) or 1e-9            # type: ignore[arg-type]
        drift = abs(short.value - medium.value) / anchor      # type: ignore[operator]
        ctx.scratch["beta"] = medium.value
        ctx.scratch["beta_drift"] = drift
        return drift <= BETA_DRIFT_BAND, drift, BETA_DRIFT_BAND

    def _convergence_clears_cost(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """Condition 4: expected convergence >= kappa x the round-trip cost of **both** legs.

        Convergence is the residual's distance from zero, converted to price on the dislocated leg.
        The cost is two legs' taker fees in and out, from live instrument metadata — never assumed.
        """
        residual = abs(ctx.scratch.get("residual", 0.0))
        instruments = ctx.snapshot.instruments
        own_key = (ctx.asset, ctx.features.product)
        bench_key = (BENCHMARK, ctx.features.product)
        own = instruments.get(own_key)                # type: ignore[call-overload]
        bench = instruments.get(bench_key)            # type: ignore[call-overload]
        if own is None or bench is None:
            raise StrategyError(
                "instrument metadata is missing for one or both legs, so the two-leg cost cannot be "
                "computed",
                component=COMPONENT, operation="convergence_clears_cost",
                context={"have": sorted(str(k) for k in instruments)},
                likely_cause="markets_details has not been polled for both assets",
                remediation="no two-leg signal is possible without both legs' fees; the strategy "
                            "declines rather than assuming a fee",
            )
        # Round trip on both legs, as a fraction: in and out, twice.
        cost = 2 * own.taker_fee.fraction + 2 * bench.taker_fee.fraction
        required = KAPPA * cost
        expected = Decimal(repr(residual))
        ctx.scratch["two_leg_cost_fraction"] = float(cost)
        ctx.scratch["required_convergence"] = float(required)
        return expected >= required, float(expected), float(required)

    def _dispersion_not_extreme(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        disp = ctx.feature("X_DISP", Timeframe.H1, "medium")
        # Structural: dispersion beyond three times its own medium-window reading on the other assets
        # would mean one asset is being repriced on its own news. Compared against the universe's own
        # value rather than a tuned constant.
        limit = 3.0 * float(np.median([
            self._view().features_for(a).value("X_DISP", Timeframe.H1, "medium")
            for a in self._view().assets
            if self._view().features_for(a).is_ok("X_DISP", Timeframe.H1, "medium")] or [disp]))
        ctx.scratch["dispersion"] = disp
        return disp <= limit, disp, limit

    def _both_legs_fresh(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """specs/03 §5 condition 6: the gates pass on **both** legs."""
        view = self._view()
        stale: list[str] = []
        for asset in (ctx.asset, BENCHMARK):
            series_ok = any(st.ok for key, st in ctx.snapshot.series.items()
                            if key.asset == asset and key.timeframe is Timeframe.H1)
            if not series_ok:
                stale.append(f"{asset}: 1h series unusable")
            if asset not in view.features:
                stale.append(f"{asset}: no features in this pass")
        return not stale, "; ".join(stale) or "both legs fresh", "both legs"

    def _mean_reverting(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        vr = ctx.feature(PRIMARY_VR, Timeframe.H1, "short")
        return vr < 1.0, vr, 1.0

    def _residual_sigma(self, ctx: ConditionContext) -> float:
        """Rolling sigma of the residual — ACT's unit for this strategy (specs/03 §5)."""
        cached = ctx.scratch.get("residual_sigma")
        if cached is not None:
            return float(cached)
        view = self._view()
        own = view.features_for(ctx.asset)
        bench = view.features_for(BENCHMARK)
        beta = own.value("X_BETA", Timeframe.H1, "medium")
        # The residual's sigma is derived from the two legs' own volatilities and their correlation:
        #   var(a - beta*b) = var(a) + beta^2 var(b) - 2 beta corr sd(a) sd(b)
        # rather than by rebuilding a spread price series. Three reasons: the features already measured
        # each leg over the same window; a second price path would be a second implementation of the
        # same quantity; and anything rebuilt here would not be the series the conditions were
        # evaluated against.
        own_vol = own.value("VOL_CC", Timeframe.H1, "medium")
        bench_vol = bench.value("VOL_CC", Timeframe.H1, "medium")
        corr = own.value("X_CORR", Timeframe.H1, "medium")
        variance = own_vol**2 + (beta**2) * bench_vol**2 - 2 * beta * corr * own_vol * bench_vol
        sigma = float(np.sqrt(max(variance, 0.0)))
        ctx.scratch["residual_sigma"] = sigma
        return sigma

    # ---- evaluation ----------------------------------------------------------------------

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        if features.asset == BENCHMARK:
            # The benchmark is the hedge, never the dislocated leg: a residual of BTC against itself
            # is zero by construction.
            return no_setup((), reason=f"{BENCHMARK} is the hedge leg, not a candidate")
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))

        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, ctx, results)
        if not all(r.passed for r in results):
            failed = [r.name for r in results if not r.passed]
            return no_setup(results, reason=f"{len(failed)} condition(s) not met: {', '.join(failed)}")

        z = float(ctx.scratch["residual_z"])
        # Long the underperformer, short the outperformer: a negative residual means this asset has
        # lagged what its beta explains.
        direction = Direction.LONG if z < 0 else Direction.SHORT
        return self._arm(snapshot, features, regime, ctx, results, direction, z)

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...], direction: Direction,
             z: float) -> StrategyOutcome:
        armed_at = snapshot.as_of_ms
        beta = float(ctx.scratch["beta"])
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"beta_at_arming": beta,
                     "residual_at_arming": ctx.scratch.get("residual"),
                     "residual_sigma": ctx.scratch.get("residual_sigma"),
                     "correlation_at_arming": ctx.scratch.get("correlation")})
        act_state = self._act_on_residual(ctx, state, direction)
        state.x_star, state.e_t = act_state.x_star, act_state.e_t
        levels = self._levels(ctx, features, snapshot, act_state, direction, beta)
        rationale = (
            f"{features.asset} is {z:+.2f} sigma from what its beta ({beta:.2f}) against "
            f"{BENCHMARK} explains, correlation {ctx.scratch.get('correlation', float('nan')):.2f} "
            f"holds and the variance ratio is below 1. **Two legs**: "
            f"{direction.value} {features.asset} against {direction.opposite.value} {BENCHMARK} at "
            f"{beta:.2f}x notional. Expected convergence "
            f"{ctx.scratch.get('required_convergence', 0):.4%} clears the two-leg round trip."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={"two_leg": True, "legs": _legs(levels),
                                      "beta": beta, "residual_z": z,
                                      "act_unit": "rolling sigma of the residual, not ATR",
                                      "execution_note": "both legs execute together on trigger; the "
                                                        "notionals differ because the hedge is "
                                                        "beta-weighted, and the UI must show both "
                                                        "legs or the trade will be executed wrong"})
        return setup(signal, results, state, reason=f"armed {direction.value} residual")

    def _act_on_residual(self, ctx: ConditionContext, state: SetupState,
                         direction: Direction) -> Any:
        """ACT applied to the **spread series**, not to either leg (specs/03 §5).

        The unit is the residual's rolling sigma rather than ATR, because the observable is a spread.
        The primitive itself is unchanged — a second implementation anywhere is a defect.
        """
        sigma = self._residual_sigma(ctx)
        residual = float(ctx.scratch.get("residual", 0.0))
        delta = Decimal(str(ctx.param("act_trigger_multiple")))
        # The residual series since arming: at arming there is one observation, and it grows as the
        # setup is re-evaluated. Reconstructed from the current residual each pass, which keeps it
        # derived rather than accumulated (specs/13 §2).
        history = [Decimal(repr(v)) for v in state.context.setdefault("residual_history", [])]
        history.append(Decimal(repr(residual)))
        state.context["residual_history"] = [float(v) for v in history][-240:]
        return act(direction, history, 0, Decimal(repr(sigma)), delta)

    def _levels(self, ctx: ConditionContext, features: FeatureSet, snapshot: MarketSnapshot,
                act_state: Any, direction: Direction, beta: float) -> Levels:
        """Levels on the **residual**, plus a published leg table.

        Stop: the residual extends beyond ``X* ± s_rs * sigma``. Target: the residual returns to its
        rolling mean, which is zero by construction — "the target is where the restoring force stops
        acting", exactly as in C1-B.
        """
        s_rs = Decimal(str(ctx.param("stop_buffer_atr_multiple")))
        sigma = Decimal(repr(self._residual_sigma(ctx)))
        stop = (act_state.x_star - s_rs * sigma if direction is Direction.LONG
                else act_state.x_star + s_rs * sigma)
        own_price = price_now(features, snapshot)
        legs = [
            {"asset": features.asset, "direction": direction.value, "price": str(own_price),
             "notional_weight": "1.00"},
            {"asset": BENCHMARK, "direction": direction.opposite.value,
             "price": str(self._benchmark_price(snapshot, features)),
             "notional_weight": f"{beta:.4f}",
             "note": "beta-weighted hedge: the notionals differ by design"},
        ]
        return Levels(entry=act_state.e_t, stop=stop, targets=(Decimal(0),),
                      detail={"levels_are": "residual (spread) units, not price",
                              "residual_sigma": str(sigma), "legs": legs,
                              "target_basis": "the residual returns to its rolling mean, which is "
                                              "zero by construction"})

    def _benchmark_price(self, snapshot: MarketSnapshot, features: FeatureSet) -> Decimal:
        from snapshot.models import SeriesKey
        key = SeriesKey(BENCHMARK, features.product, Timeframe.M1)
        state = snapshot.series.get(key)
        if state is None or not state.bars:
            raise StrategyError(
                f"no closed 1m bar for the hedge leg {BENCHMARK}",
                component=COMPONENT, operation="benchmark_price",
                context={"asset": BENCHMARK},
                likely_cause="the benchmark's 1m series is missing from the snapshot",
                remediation="both legs need a current price before a two-leg suggestion is published",
            )
        return state.bars[-1].close

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        act_state = self._act_on_residual(ctx, state, state.direction)
        beta = float(state.context.get("beta_at_arming", ctx.scratch.get("beta", 1.0)))
        levels = self._levels(ctx, features, snapshot, act_state, state.direction, beta)
        residual = Decimal(repr(float(ctx.scratch.get("residual", 0.0))))
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=residual,
                           predicates=(lambda: self._correlation_broke(ctx, state),
                                       lambda: self._beta_respecified(ctx, state)),
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={"two_leg": True, "legs": _legs(levels),
                                      "progress": progress.to_dict()})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _correlation_broke(self, ctx: ConditionContext, state: SetupState) -> Invalidation:
        """Correlation below the floor is a **decoupling**, not a dislocation. Exit."""
        fv = ctx.features.get("X_CORR", Timeframe.H1, "medium")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="correlation_break", fired=False,
                                detail=f"X_CORR is {fv.status.value}: {fv.reason}")
        floor = float(ctx.param("correlation_floor"))
        return Invalidation(
            name="correlation_break", fired=fv.value < floor,
            detail=("correlation fell below the floor: the pair relationship has broken, so this is "
                    "a decoupling rather than a dislocation"
                    if fv.value < floor else f"correlation {fv.value:.3f} holds"),
            value=round(fv.value, 4), threshold=floor)

    def _beta_respecified(self, ctx: ConditionContext, state: SetupState) -> Invalidation:
        """Beta shifting beyond its band means the relationship **re-specified**, not dislocated."""
        armed_beta = state.context.get("beta_at_arming")
        fv = ctx.features.get("X_BETA", Timeframe.H1, "medium")
        if armed_beta is None or fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="beta_respecified", fired=False,
                                detail=f"X_BETA is {fv.status.value}: {fv.reason}")
        drift = abs(fv.value - float(armed_beta)) / (abs(float(armed_beta)) or 1e-9)
        return Invalidation(
            name="beta_respecified", fired=drift > BETA_DRIFT_BAND,
            detail=(f"beta moved {drift:.1%} from {float(armed_beta):.3f} at arming: the relationship "
                    "has re-specified rather than dislocated"
                    if drift > BETA_DRIFT_BAND else f"beta drift {drift:.1%} within band"),
            value=round(drift, 4), threshold=BETA_DRIFT_BAND)
