"""Class 2 — trend participation (specs/04).

The defining divergence from Class 1 is **where the stop goes**. Class 1 stops beyond the dislocation
extreme, because the dislocation *is* the thesis. Class 2 stops beyond **structural invalidation**,
because the structure is the thesis:

    LONG:  stop = last_confirmed_swing_low  - s * ATR_1h

and specs/04 §3 is explicit about the consequence: *"If that stop produces an R larger than the
position sizing allows, there is no trade. Do not tighten the stop to make the trade fit"* — that
severs the stop from its meaning. So this module never adjusts a stop to fit a size; the risk engine
refuses the signal instead, which is the correct outcome rather than a missed one.

**Targets are partial, then trailed.** Trends are fat-tailed, and capping the winner at a fixed
multiple is the single most common way to destroy a trend strategy's expectancy, because the whole
value sits in the tail. T1 is the prior swing extreme — structural, measurable, publishable. The
remainder trails at ``max(high since entry) - s * ATR_1h``, monotone, never loosening. **T2 is
deliberately not published**: a number there would be fiction, so the UI shows the live trail instead.

The three strategies:

* **C2-A** — the pullback in a measured trend.
* **C2-B** — the same trade, on whichever asset best *expresses* a BTC-driven factor move. Its
  expression score is divided by beta, because a naive score always selects the highest-beta asset,
  and that is not a better expression — it is the same exposure with more leverage, and leverage is a
  sizing decision rather than a selection one. At most **one** C2-B position exists across all three
  assets, enforced here and not only in the risk engine.
* **C2-C** — Tier B. Follows *moderate* funding backed by notional size, and is **hard-partitioned**
  from C1-A on the same threshold: C1-A needs ``FND_MULT >= m_extreme``, C2-C needs ``<``. If both
  ever arm on one asset that is a **bug**, not a conflict to resolve, and this module raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.errors import StrategyError
from features.f4_deviation import PRIMARY_VR
from features.value import FeatureSet, FeatureStatus
from normalize.canonical import Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot
from strategy.act import Direction
from strategy.base import Lifecycle, SetupState, StrategyOutcome, no_setup, setup, terminal, update
from strategy.c1_correction import FUNDING_BASELINE
from strategy.conditions import (
    Condition,
    ConditionContext,
    ConditionResult,
    dwell_stable,
    evaluate_all,
    threshold,
)
from strategy.evaluation import (
    Levels,
    atr_1h,
    build_signal,
    compute_act,
    effective_stop,
    price_now,
    signal_id,
    update_trail,
)
from strategy.lifecycle import Invalidation, advance
from strategy.market_view import MarketView

COMPONENT = "strategy.c2"
BENCHMARK = "B-BTC_USDT"

#: Pullback depth band, in ATR, **fixed by reasoning and not fitted** (specs/04 §3): below 1 ATR a
#: pullback is indistinguishable from bar noise, and beyond 3 ATR the structural premise is already
#: under threat. Structural constants in the sense of specs/02 §4 — they consume no parameter budget
#: and must not be tuned.
PULLBACK_MIN_ATR = 1.0
PULLBACK_MAX_ATR = 3.0

#: Trend decay invalidation: ``|TRD_TSTAT|`` below half ``t_min`` means the drift is no longer
#: distinguishable from noise (specs/04 §3). The half is structural, derived from t_min rather than
#: being its own parameter.
TREND_DECAY_FRACTION = Decimal("0.5")

EXPIRY_BARS = 24


# ---- shared Class 2 condition bodies -----------------------------------------------------

def _higher_timeframe_agrees(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """Condition 2: the 1d t-statistic shares the 1h sign."""
    hourly = ctx.feature("TRD_TSTAT", Timeframe.H1, "medium")
    daily = ctx.feature("TRD_TSTAT", Timeframe.D1, "medium")
    agree = (hourly > 0) == (daily > 0) and hourly != 0 and daily != 0
    ctx.scratch["tstat_1h"] = hourly
    ctx.scratch["tstat_1d"] = daily
    return agree, f"1h {hourly:+.2f}, 1d {daily:+.2f}", "same sign"


def _persistence_operative(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """Condition 3 — **the mirror of C1-B's gate**: a majority of VR horizons above 1."""
    from features.f4_deviation import VR_HORIZONS

    above, available, missing = 0, 0, []
    for k in VR_HORIZONS:
        fv = ctx.features.get(f"MR_VR_K{k}", Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            missing.append(f"k={k}: {fv.status.value}")
            continue
        available += 1
        if fv.value > 1.0:
            above += 1
    if available == 0:
        raise StrategyError(
            f"no variance-ratio horizon is available ({'; '.join(missing)})",
            component=COMPONENT, operation="persistence_operative",
            context={"missing": missing},
            likely_cause="the 1h short window is warming or holed",
            remediation="the strategy declines; this gate is the mirror of C1-B's and has no substitute",
        )
    ctx.scratch["vr_above_one"] = above
    ctx.scratch["vr_available"] = available
    return above * 2 > available, f"{above}/{available} above 1", "a majority"


def _pullback_depth(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """Condition 5: the pullback is 1-3 ATR **against** the trend.

    The sign matters: a move *with* the trend of the same size is not a pullback, it is an extension,
    and entering there is the opposite trade.
    """
    dev = ctx.feature("DEV_ATR", Timeframe.H1, "medium")
    tstat = ctx.scratch.get("tstat_1h")
    if tstat is None:
        tstat = ctx.feature("TRD_TSTAT", Timeframe.H1, "medium")
    against_trend = (dev < 0) if tstat > 0 else (dev > 0)
    depth = abs(dev)
    ctx.scratch["pullback_atr"] = depth
    ctx.scratch["pullback_against_trend"] = against_trend
    ok = against_trend and PULLBACK_MIN_ATR <= depth <= PULLBACK_MAX_ATR
    return (ok, f"{depth:.2f} ATR {'against' if against_trend else 'with'} the trend",
            f"[{PULLBACK_MIN_ATR}, {PULLBACK_MAX_ATR}] ATR against")


def _structure_intact(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """Condition 6: ``STR_SEQ`` still agrees with the trend and the pullback has not taken out the
    last confirmed swing.

    Both halves matter. The sequence label says the structure is still the structure; the swing level
    says *this* pullback has not broken it. A label alone would keep the trade alive through the break
    that invalidates it.
    """
    tstat = ctx.scratch.get("tstat_1h") or ctx.feature("TRD_TSTAT", Timeframe.H1, "medium")
    up = tstat > 0
    seq = ctx.features.get("STR_SEQ", Timeframe.H1, "medium")
    swing = ctx.features.get("STR_SWING", Timeframe.H1, "medium")
    if seq.status is not FeatureStatus.OK or swing.status is not FeatureStatus.OK:
        raise StrategyError(
            f"structure is unavailable (STR_SEQ {seq.status.value}, STR_SWING {swing.status.value})",
            component=COMPONENT, operation="structure_intact",
            context={"seq_reason": seq.reason, "swing_reason": swing.reason},
            likely_cause="swing_atr_multiple is uncalibrated, or the window is warming",
            remediation="calibrate swing_atr_multiple; Class 2's stop and T1 are both structural and "
                        "cannot be placed without it",
        )
    wanted = "HH-HL" if up else "LH-LL"
    label_ok = seq.label == wanted
    level = swing.detail.get("last_swing_low" if up else "last_swing_high")
    close = swing.detail.get("close")
    if level is None or close is None:
        raise StrategyError(
            "no confirmed swing level to measure the pullback against",
            component=COMPONENT, operation="structure_intact",
            context={"detail": sorted(swing.detail)},
            likely_cause="no pivot of the needed kind is confirmed yet",
            remediation="the strategy declines; the stop has nowhere to sit",
        )
    held = close > level if up else close < level
    ctx.scratch["swing_level"] = level
    ctx.scratch["structure_label"] = seq.label
    return (label_ok and held,
            f"{seq.label}, close {close:.2f} vs swing {level:.2f}",
            f"{wanted} and the swing holding")


def _factor_direction(ctx: ConditionContext, view: MarketView) -> tuple[bool, Any, Any]:
    """C2-B condition 1: BTC's daily trend is established and its hourly agrees."""
    btc = view.features_for(BENCHMARK)
    t_min = float(ctx.param("trend_tstat_threshold"))
    daily = btc.value("TRD_TSTAT", Timeframe.D1, "medium")
    hourly = btc.value("TRD_TSTAT", Timeframe.H1, "medium")
    agree = (daily > 0) == (hourly > 0)
    ctx.scratch["factor_tstat_1d"] = daily
    ctx.scratch["factor_tstat_1h"] = hourly
    return (abs(daily) >= t_min and agree,
            f"BTC 1d {daily:+.2f}, 1h {hourly:+.2f}", f"|1d| >= {t_min} and same sign")


def expression_score(features: FeatureSet, *, residual_sigma: float | None = None) -> float:
    """``(TRD_TSTAT(1h) + z(X_RS)) / X_BETA`` — specs/04 §4.

    **Divided by beta, weight fixed at 1, structural and not fitted.** An asset earns selection by
    trending *more than its beta explains*, not by moving more. Without the division the score always
    picks the highest-beta asset, which is the same exposure with more leverage.
    """
    tstat = features.value("TRD_TSTAT", Timeframe.H1, "medium")
    rs = features.value("X_RS", Timeframe.H1, "medium")
    beta = features.value("X_BETA", Timeframe.H1, "medium")
    if beta == 0:
        raise StrategyError(
            f"{features.asset} has zero beta against the benchmark, so an expression score is "
            "undefined",
            component=COMPONENT, operation="expression_score",
            context={"asset": features.asset},
            likely_cause="the beta regression collapsed",
            remediation="exclude this asset from selection rather than dividing by zero",
        )
    sigma = residual_sigma if residual_sigma else (abs(rs) or 1.0)
    z_rs = rs / sigma
    return (tstat + z_rs) / beta


# ---- C2-A --------------------------------------------------------------------------------

@dataclass
class C2A:
    """C2-A — Trend Continuation on Volatility-Normalized Pullback (specs/04 §3). Tier A."""

    params: dict[str, Any] = field(default_factory=dict)
    id: str = "C2-A"
    version: str = "1.0.0"
    strategy_class: int = 2
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("TRD_TSTAT", PRIMARY_VR, "TRD_ER", "DEV_ATR", "STR_SEQ",
                                          "STR_SWING", "ATR")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            threshold("trend_significant",
                      "the 1h drift is statistically distinguishable from noise",
                      "TRD_TSTAT", timeframe=Timeframe.H1, horizon="medium", op=">=",
                      limit_param="trend_tstat_threshold", transform=abs),
            Condition(name="higher_timeframe_agrees",
                      description="the daily trend agrees with the hourly one on direction",
                      body=_higher_timeframe_agrees, required_features=("TRD_TSTAT",)),
            Condition(name="persistence_operative",
                      description="a majority of variance-ratio horizons read above 1 — the mirror "
                                  "of C1-B's gate",
                      body=_persistence_operative),
            threshold("path_is_direct",
                      "the path is direct rather than a drift through chop",
                      "TRD_ER", timeframe=Timeframe.H1, horizon="medium", op=">=",
                      limit_param="efficiency_ratio_floor"),
            Condition(name="pullback_depth",
                      description="the pullback is deep enough to be real and shallow enough not to "
                                  "be a break",
                      body=_pullback_depth, required_features=("DEV_ATR",)),
            Condition(name="structure_intact",
                      description="the swing sequence still agrees with the trend and the pullback "
                                  "has not taken out the last confirmed swing",
                      body=_structure_intact, required_features=("STR_SEQ", "STR_SWING")),
            dwell_stable(),
        )

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))
        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, ctx, results)
        if not all(r.passed for r in results):
            failed = [r.name for r in results if not r.passed]
            return no_setup(results, reason=f"{len(failed)} condition(s) not met: {', '.join(failed)}")
        direction = Direction.LONG if ctx.scratch["tstat_1h"] > 0 else Direction.SHORT
        return self._arm(snapshot, features, regime, ctx, results, direction)

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...],
             direction: Direction, extra_detail: dict[str, Any] | None = None) -> StrategyOutcome:
        armed_at = snapshot.as_of_ms
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"swing_level": ctx.scratch.get("swing_level"),
                     "tstat_at_arming": ctx.scratch.get("tstat_1h"),
                     "structure_label": ctx.scratch.get("structure_label"),
                     "t_min": float(ctx.param("trend_tstat_threshold"))})
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product, direction,
                                          armed_at, self.params)
        state.x_star, state.e_t = act_state.x_star, act_state.e_t
        levels = self._levels(ctx, features, act_state, direction, state)
        rationale = (
            f"the 1h trend is significant (t={ctx.scratch.get('tstat_1h', float('nan')):+.2f}), the "
            f"daily agrees, {ctx.scratch.get('vr_above_one')}/{ctx.scratch.get('vr_available')} "
            f"variance-ratio horizons read above 1, and price has pulled back "
            f"{ctx.scratch.get('pullback_atr', float('nan')):.2f} ATR against it with "
            f"{ctx.scratch.get('structure_label')} structure intact. The stop sits beyond the last "
            f"confirmed swing, not beyond the pullback extreme."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={"act_unit_bars": unit_bars,
                                      "stop_basis": "structural: the last confirmed swing, buffered. "
                                                    "If R is larger than sizing allows there is no "
                                                    "trade -- the stop is never tightened to fit",
                                      "t2": "deliberately unpublished: the remainder trails, and a "
                                            "number there would be fiction",
                                      **(extra_detail or {})})
        return setup(signal, results, state, reason=f"armed {direction.value}")

    def _levels(self, ctx: ConditionContext, features: FeatureSet, act_state: Any,
                direction: Direction, state: SetupState) -> Levels:
        s = Decimal(str(ctx.param("stop_buffer_atr_multiple")))
        atr = atr_1h(features)
        level = state.context.get("swing_level") or ctx.scratch.get("swing_level")
        if level is None:
            raise StrategyError(
                "no confirmed swing level, so a structural stop cannot be placed",
                component=COMPONENT, operation="levels",
                context={"asset": features.asset},
                likely_cause="swing_atr_multiple is uncalibrated or no pivot is confirmed",
                remediation="Class 2 stops at structural invalidation; without a level there is no "
                            "trade rather than a substituted stop",
            )
        swing = Decimal(repr(float(level)))
        stop = swing - s * atr if direction is Direction.LONG else swing + s * atr
        # T1 is the prior swing extreme in the trend direction, taken from the same feature that
        # provided the stop's level so both come from one structure reading.
        swing_detail = features.get("STR_SWING", Timeframe.H1, "medium").detail
        prior = swing_detail.get("last_swing_high" if direction is Direction.LONG
                                 else "last_swing_low")
        targets = (Decimal(repr(float(prior))),) if prior is not None else ()
        trail = state.context.get("trail_level")
        return Levels(entry=act_state.e_t, stop=stop, targets=targets,
                      detail={"swing_level": str(swing), "atr_1h": str(atr),
                              "trail_level": str(trail) if trail is not None else None,
                              "t1_basis": "the prior swing extreme in the trend direction",
                              "remainder": "trailed at max(high since entry) - s x ATR, monotone"})

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        self._update_trail(state, features, price)
        levels = self._levels(ctx, features, act_state, state.direction, state)
        effective_stop = self._effective_stop(state, levels)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._structural_invalidation(state, price),
                                       lambda: self._mechanism_invalidation(features),
                                       lambda: self._trend_decay(features, state)),
                           stop=effective_stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={"act_unit_bars": unit_bars, "progress": progress.to_dict(),
                                      "effective_stop": str(effective_stop),
                                      "trail_level": state.context.get("trail_level")})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _update_trail(self, state: SetupState, features: FeatureSet, price: Decimal) -> None:
        """Delegates to the shared monotone trail. C4-A and C3-B use the same one deliberately."""
        update_trail(state, atr=atr_1h(features),
                     s=Decimal(str(self.params["stop_buffer_atr_multiple"])), price=price)

    def _effective_stop(self, state: SetupState, levels: Levels) -> Decimal | None:
        return effective_stop(state, levels.stop)

    def _structural_invalidation(self, state: SetupState, price: Decimal) -> Invalidation:
        """A 1h close beyond the last confirmed swing against the position (specs/04 §3)."""
        level = state.context.get("swing_level")
        if level is None:
            return Invalidation(name="structural", fired=False,
                                detail="no swing level was recorded at arming")
        level_d = Decimal(str(level))
        fired = price < level_d if state.direction is Direction.LONG else price > level_d
        return Invalidation(
            name="structural", fired=fired,
            detail=(f"price {price} closed beyond the last confirmed swing {level_d}: the structure "
                    "that was the thesis has broken"
                    if fired else f"price {price} holds above {level_d}"),
            value=str(price), threshold=str(level_d))

    def _mechanism_invalidation(self, features: FeatureSet) -> Invalidation:
        """``MR_VR`` falling below 1 ends measured persistence — the thesis is void regardless of
        price, and this is the exact mirror of C1-B's mechanism stop."""
        fv = features.get(PRIMARY_VR, Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="mechanism_mr_vr", fired=False,
                                detail=f"{PRIMARY_VR} is {fv.status.value}: {fv.reason}")
        return Invalidation(
            name="mechanism_mr_vr", fired=fv.value < 1.0,
            detail=("the variance ratio fell below 1: measured persistence has ended, so the thesis "
                    "is void independent of price"
                    if fv.value < 1.0 else "persistence still operative"),
            value=round(fv.value, 4), threshold=1.0)

    def _trend_decay(self, features: FeatureSet, state: SetupState) -> Invalidation:
        """``|TRD_TSTAT|`` below half ``t_min``: the drift is no longer distinguishable from noise."""
        fv = features.get("TRD_TSTAT", Timeframe.H1, "medium")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="trend_decay", fired=False,
                                detail=f"TRD_TSTAT is {fv.status.value}: {fv.reason}")
        t_min = Decimal(str(state.context.get("t_min", self.params["trend_tstat_threshold"])))
        floor = float(t_min * TREND_DECAY_FRACTION)
        return Invalidation(
            name="trend_decay", fired=abs(fv.value) < floor,
            detail=(f"|t| fell to {abs(fv.value):.2f}, below half t_min ({floor:.2f}): the drift is "
                    "no longer distinguishable from noise"
                    if abs(fv.value) < floor else f"|t| {abs(fv.value):.2f} holds above {floor:.2f}"),
            value=round(abs(fv.value), 4), threshold=floor)


# ---- C2-B --------------------------------------------------------------------------------

@dataclass
class C2B(C2A):
    """C2-B — Market-Factor Trend with Best-Expression Selection (specs/04 §4). Tier A.

    Inherits C2-A's entry, stop, targets and invalidations — specs/04 §4 says "as C2-A, applied to the
    selected asset", and inheriting is how that stays true rather than being re-copied and drifting.
    What it adds: the factor conditions, the beta-normalised selection, the one-position constraint,
    and two extra invalidations (factor flip, correlation break).
    """

    id: str = "C2-B"
    version: str = "1.0.0"
    needs_market_view: bool = True
    market_view: MarketView | None = None
    required_features: tuple[str, ...] = ("TRD_TSTAT", PRIMARY_VR, "X_CORR", "X_BETA", "X_RS",
                                          "X_DISP", "DEV_ATR", "STR_SEQ", "STR_SWING", "ATR")

    def _view(self) -> MarketView:
        if self.market_view is None:
            raise StrategyError(
                "C2-B was evaluated without a market view",
                component=COMPONENT, operation="market_view",
                context={"strategy": self.id},
                likely_cause="the runner did not attach the view",
                remediation="C2-B selects among assets and cannot be evaluated one asset at a time",
            )
        return self.market_view

    @property
    def conditions(self) -> tuple[Condition, ...]:
        view_conditions = (
            Condition(name="factor_direction",
                      description="BTC's daily trend is established and its hourly agrees",
                      body=lambda ctx: _factor_direction(ctx, self._view()),
                      required_features=("TRD_TSTAT",)),
            Condition(name="factor_persistence",
                      description="BTC's variance ratio is above 1, so factor persistence is operative",
                      body=self._factor_persistence, required_features=(PRIMARY_VR,)),
            Condition(name="factor_structure_intact",
                      description="every asset's correlation with BTC is above the floor, so factor "
                                  "logic still applies",
                      body=self._factor_structure, required_features=("X_CORR",)),
            Condition(name="dispersion_not_extreme",
                      description="dispersion is not in the upper tail, so this is not an "
                                  "idiosyncratic-news regime",
                      body=self._dispersion_not_extreme, required_features=("X_DISP",)),
            Condition(name="best_expression",
                      description="this asset is the best beta-normalised expression of the factor "
                                  "move, and no other C2-B position is open",
                      body=self._best_expression,
                      required_features=("TRD_TSTAT", "X_RS", "X_BETA")),
        )
        # specs/04 §4 condition 5: "Selected asset satisfies C2-A conditions 5-8". Reusing the parent's
        # objects rather than restating them keeps one definition of each.
        inherited = tuple(c for c in C2A.conditions.fget(self)          # type: ignore[attr-defined]
                          if c.name in ("pullback_depth", "structure_intact", "regime_dwell_stable"))
        return view_conditions + inherited

    def _factor_persistence(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        btc = self._view().features_for(BENCHMARK)
        vr = btc.value(PRIMARY_VR, Timeframe.H1, "short")
        return vr > 1.0, vr, 1.0

    def _factor_structure(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        floor = float(ctx.param("correlation_floor"))
        view = self._view()
        worst, worst_asset = None, None
        for asset in view.assets:
            fs = view.features_for(asset)
            if asset == BENCHMARK or not fs.is_ok("X_CORR", Timeframe.H1, "medium"):
                continue
            corr = fs.value("X_CORR", Timeframe.H1, "medium")
            if worst is None or corr < worst:
                worst, worst_asset = corr, asset
        if worst is None:
            raise StrategyError(
                "no asset has a usable correlation with the benchmark",
                component=COMPONENT, operation="factor_structure",
                context={"assets": list(view.assets)},
                likely_cause="X_CORR is warming or unavailable across the board",
                remediation="the strategy declines; factor logic needs the factor structure",
            )
        return worst >= floor, f"min {worst:.3f} ({worst_asset})", floor

    def _dispersion_not_extreme(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        disp = ctx.feature("X_DISP", Timeframe.H1, "medium")
        # Structural: dispersion at more than three times the universe's own median reading means one
        # asset is being repriced on its own news, which is when factor logic stops applying.
        import numpy as np
        view = self._view()
        readings = [view.features_for(a).value("X_DISP", Timeframe.H1, "medium")
                    for a in view.assets
                    if view.features_for(a).is_ok("X_DISP", Timeframe.H1, "medium")]
        limit = 3.0 * float(np.median(readings)) if readings else disp * 3
        return disp <= limit, disp, limit

    def _best_expression(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """This asset must be the argmax of the beta-normalised score, **and** no sibling may already
        hold the single permitted position (specs/04 §4's hard constraint)."""
        view = self._view()
        if view.holds_a_position(self.id, other_than=ctx.asset):
            held = [a for a in view.armed_assets(self.id) if a != ctx.asset]
            return (False, f"a C2-B position is already open on {', '.join(held)}",
                    "at most one across all assets")
        scores: dict[str, float] = {}
        for asset in view.assets:
            if asset == BENCHMARK:
                continue
            fs = view.features_for(asset)
            try:
                scores[asset] = expression_score(fs)
            except Exception:                                  # noqa: BLE001 - recorded, not hidden
                continue
        if not scores:
            raise StrategyError(
                "no asset produced an expression score",
                component=COMPONENT, operation="best_expression",
                context={"assets": list(view.assets)},
                likely_cause="TRD_TSTAT, X_RS or X_BETA is unavailable across the board",
                remediation="the strategy declines rather than selecting arbitrarily",
            )
        best = max(scores, key=lambda a: scores[a])
        ctx.scratch["expression_scores"] = {k: round(v, 4) for k, v in scores.items()}
        ctx.scratch["selected_asset"] = best
        return (best == ctx.asset,
                f"{ctx.asset} scores {scores.get(ctx.asset, float('nan')):.3f}; best is {best} "
                f"({scores[best]:.3f})", "this asset is the argmax")

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        if features.asset == BENCHMARK:
            # BTC is the factor, not an expression of it: selecting it would make the strategy
            # indistinguishable from C2-A on BTC, which is exactly the comparison specs/04 §4
            # pre-registers rather than a trade.
            return no_setup((), reason=f"{BENCHMARK} is the factor itself, not an expression of it")
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))
        if state is not None and state.lifecycle.active:
            return self._manage_c2b(snapshot, features, regime, state, ctx, results)
        if not all(r.passed for r in results):
            failed = [r.name for r in results if not r.passed]
            return no_setup(results, reason=f"{len(failed)} condition(s) not met: {', '.join(failed)}")
        factor = ctx.scratch["factor_tstat_1h"]
        direction = Direction.LONG if factor > 0 else Direction.SHORT
        return self._arm(snapshot, features, regime, ctx, results, direction,
                         extra_detail={"expression_scores": ctx.scratch.get("expression_scores"),
                                       "selected_asset": ctx.scratch.get("selected_asset"),
                                       "factor_direction_from": BENCHMARK,
                                       "selection_basis": "beta-normalised: an asset earns selection "
                                                          "by trending more than its beta explains",
                                       "pre_registered_test": "does C2-B beat the same strategy on "
                                                              "BTC only? If not, the selection layer "
                                                              "adds nothing and C2-B reduces to "
                                                              "BTC-only (specs/04 §4)"})

    def _manage_c2b(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                    state: SetupState, ctx: ConditionContext,
                    results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        """C2-A's management plus the factor flip and correlation break."""
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        self._update_trail(state, features, price)
        levels = self._levels(ctx, features, act_state, state.direction, state)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._structural_invalidation(state, price),
                                       lambda: self._mechanism_invalidation(features),
                                       lambda: self._trend_decay(features, state),
                                       lambda: self._factor_flip(state),
                                       lambda: self._correlation_break(ctx, features)),
                           stop=self._effective_stop(state, levels), targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={"act_unit_bars": unit_bars, "progress": progress.to_dict()})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _factor_flip(self, state: SetupState) -> Invalidation:
        """BTC's hourly trend reversing voids the expression regardless of the selected asset."""
        try:
            btc = self._view().features_for(BENCHMARK)
            tstat = btc.value("TRD_TSTAT", Timeframe.H1, "medium")
        except Exception as e:                                 # noqa: BLE001 - reported
            return Invalidation(name="factor_flip", fired=False,
                                detail=f"the factor's trend could not be read: {e}")
        aligned = (tstat > 0) == (state.direction is Direction.LONG)
        return Invalidation(
            name="factor_flip", fired=not aligned,
            detail=("the factor's direction reversed: the expression is void regardless of this "
                    "asset's own behaviour" if not aligned else f"factor still {tstat:+.2f}"),
            value=round(tstat, 4), threshold=0.0)

    def _correlation_break(self, ctx: ConditionContext, features: FeatureSet) -> Invalidation:
        fv = features.get("X_CORR", Timeframe.H1, "medium")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="correlation_break", fired=False,
                                detail=f"X_CORR is {fv.status.value}: {fv.reason}")
        floor = float(ctx.param("correlation_floor"))
        return Invalidation(
            name="correlation_break", fired=fv.value < floor,
            detail=("correlation fell below the floor: the expression logic is void and the position "
                    "is no longer what it was opened as"
                    if fv.value < floor else f"correlation {fv.value:.3f} holds"),
            value=round(fv.value, 4), threshold=floor)


# ---- C2-C --------------------------------------------------------------------------------

@dataclass
class C2C(C2A):
    """C2-C — Size-Backed Trend Participation (specs/04 §5). **Tier B.**

    A trend financed by *moderate*, trend-aligned funding and backed by **notional size** is more
    durable than one crowded by **account count** — and ``POS_DIVERGENCE`` is the only thing that can
    tell those apart. No OHLCV feature can.

    The partition from C1-A is hard and shares one threshold: C1-A needs ``FND_MULT >= m_extreme``,
    C2-C needs ``<``. A trend that becomes progressively over-financed crosses it and hands off from
    follow to fade, which is coherent rather than contradictory, and is logged as a handoff.
    """

    id: str = "C2-C"
    version: str = "1.0.0"
    tier: str = "B"
    required_features: tuple[str, ...] = ("TRD_TSTAT", PRIMARY_VR, "DEV_ATR", "STR_SEQ", "STR_SWING",
                                          "ATR", "FND_RATE", "POS_DIVERGENCE", "POS_Z_CNT")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        # Condition 1: "C2-A conditions 1-3 and 5-8 hold" -- every parent condition except the
        # efficiency-ratio gate, reused as objects so there is one definition of each.
        inherited = tuple(c for c in C2A.conditions.fget(self)          # type: ignore[attr-defined]
                          if c.name != "path_is_direct")
        return inherited + (
            Condition(name="funding_moderate",
                      description="funding is below the extreme multiple — the hard partition from "
                                  "C1-A, which fades the same threshold from the other side",
                      body=self._funding_moderate, required_features=("FND_RATE",)),
            Condition(name="funding_aligned",
                      description="funding's sign is aligned with the trend, so the trend is being "
                                  "financed normally",
                      body=self._funding_aligned, required_features=("FND_RATE",)),
            Condition(name="size_backed",
                      description="participation is size-backed rather than a crowd of small "
                                  "accounts",
                      body=self._size_backed, required_features=("POS_DIVERGENCE",)),
            Condition(name="positioning_not_extreme",
                      description="positioning is not itself the dislocation — that is C1-A's domain",
                      body=self._positioning_not_extreme, required_features=("POS_Z_CNT",)),
        )

    def _funding_mult(self, ctx: ConditionContext) -> Decimal:
        rate = ctx.feature("FND_RATE")
        return abs(Decimal(str(rate))) / FUNDING_BASELINE

    def _funding_moderate(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        mult = self._funding_mult(ctx)
        limit = Decimal(str(ctx.param("funding_extreme_multiple")))
        ctx.scratch["fnd_mult"] = float(mult)
        return mult < limit, float(mult), float(limit)

    def _funding_aligned(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        rate = ctx.feature("FND_RATE")
        tstat = ctx.scratch.get("tstat_1h") or ctx.feature("TRD_TSTAT", Timeframe.H1, "medium")
        # In an uptrend, longs paying funding is the normal financing of that trend.
        aligned = (rate > 0) == (tstat > 0)
        return aligned, f"fr={rate:+.6f}, t={tstat:+.2f}", "same sign"

    def _size_backed(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """``POS_DIVERGENCE`` must not be strongly skewed toward count in the trend direction.

        Positive divergence means many small accounts are long while larger capital is not. In an
        uptrend that is the crowd, not the size backing the move.
        """
        divergence = ctx.feature("POS_DIVERGENCE")
        tstat = ctx.scratch.get("tstat_1h") or ctx.feature("TRD_TSTAT", Timeframe.H1, "medium")
        # Structural: a 10-point gap between count share and value share is a clear skew on a 0-100
        # scale, and the threshold is stated rather than tuned because no history exists to tune on.
        limit = 10.0
        skewed_with_trend = (divergence > limit) if tstat > 0 else (divergence < -limit)
        ctx.scratch["pos_divergence"] = divergence
        return (not skewed_with_trend, divergence,
                f"not beyond {'+' if tstat > 0 else '-'}{limit} with the trend")

    def _positioning_not_extreme(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        z = ctx.feature("POS_Z_CNT")
        limit = float(ctx.param("deviation_z_threshold"))
        return abs(z) < limit, abs(z), limit

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        outcome = super().evaluate(snapshot, features, regime, state)
        if outcome.signal is not None:
            # specs/04 §5: the C1-A partition is checked on the published signal too, so a bug that
            # let both arm is caught rather than resolved as a conflict.
            assert_c1a_partition(features, self.params, armed=True)
        return outcome

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        """C2-A's management plus the positioning-quality exit and the C1-A handoff."""
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        self._update_trail(state, features, price)
        levels = self._levels(ctx, features, act_state, state.direction, state)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._structural_invalidation(state, price),
                                       lambda: self._mechanism_invalidation(features),
                                       lambda: self._trend_decay(features, state),
                                       lambda: self._participation_deteriorated(ctx, state),
                                       lambda: self._funding_handoff(ctx)),
                           stop=self._effective_stop(state, levels), targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={"act_unit_bars": unit_bars, "progress": progress.to_dict(),
                                      "population_caveat": "POS_DIVERGENCE measures CoinDCX's own "
                                                           "user base, not the market; no amount of "
                                                           "processing removes that"})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _participation_deteriorated(self, ctx: ConditionContext, state: SetupState) -> Invalidation:
        fv = ctx.features.get("POS_DIVERGENCE")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="participation_quality", fired=False,
                                detail=f"POS_DIVERGENCE is {fv.status.value}: {fv.reason}")
        up = state.direction is Direction.LONG
        flipped = fv.value > 10.0 if up else fv.value < -10.0
        return Invalidation(
            name="participation_quality", fired=flipped,
            detail=("positioning flipped to count-crowded: the participation quality that justified "
                    "the trade has deteriorated"
                    if flipped else f"divergence {fv.value:+.2f} still size-backed"),
            value=round(fv.value, 3), threshold=10.0)

    def _funding_handoff(self, ctx: ConditionContext) -> Invalidation:
        """``FND_MULT`` crossing ``m_extreme`` is a **handoff to C1-A**, not a failure.

        The market state genuinely changed: a trend that became over-financed is now a crowded trade
        to fade. Logged as a handoff so the audit reads it as designed behaviour rather than as two
        strategies disagreeing.
        """
        fv = ctx.features.get("FND_RATE")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="funding_handoff", fired=False,
                                detail=f"FND_RATE is {fv.status.value}: {fv.reason}")
        mult = abs(Decimal(str(fv.value))) / FUNDING_BASELINE
        limit = Decimal(str(ctx.param("funding_extreme_multiple")))
        fired = mult >= limit
        return Invalidation(
            name="funding_handoff", fired=fired,
            detail=(f"HANDOFF C2-C -> C1-A: funding reached {mult:.1f}x baseline, crossing the "
                    f"{limit}x partition. The trend is now over-financed, which is C1-A's domain. "
                    "This is designed behaviour, not a disagreement"
                    if fired else f"funding {mult:.1f}x, still moderate"),
            value=float(mult), threshold=float(limit))


def assert_c1a_partition(features: FeatureSet, params: dict[str, Any], *, armed: bool) -> None:
    """specs/04 §5: C1-A and C2-C must never arm together on one asset.

    > If both ever arm on the same asset, that is a **bug** and must raise an alert, not a
    > conflict-resolution decision.

    So this raises rather than deferring to arbitration — arbitration resolving it would make a defect
    look like a legitimate disagreement between two strategies.
    """
    fv = features.get("FND_RATE")
    if not armed or fv.status is not FeatureStatus.OK or fv.value is None:
        return
    mult = abs(Decimal(str(fv.value))) / FUNDING_BASELINE
    limit = Decimal(str(params["funding_extreme_multiple"]))
    if mult >= limit:
        raise StrategyError(
            f"C2-C armed while FND_MULT is {mult:.1f}x, at or beyond the {limit}x partition that "
            "belongs to C1-A",
            component=COMPONENT, operation="assert_c1a_partition",
            context={"asset": features.asset, "fnd_mult": float(mult), "m_extreme": float(limit)},
            likely_cause="the funding_moderate condition passed and then funding moved, or the "
                         "partition threshold is read differently in the two strategies",
            remediation="this is a bug, not a conflict: C1-A fades extreme funding and C2-C follows "
                        "moderate funding on the same threshold, so both arming means one of them "
                        "read it wrong (specs/04 §5)",
        )
