"""Class 3 — transition strategies (specs/05).

Three strategies that trade *changes of state* rather than states.

**C3-A — volatility contraction resolution.** The one place in the system where the asymmetry is
**mechanical rather than hoped for**: the stop is scaled by *contracted* volatility and the target by
*expanded* volatility, so the ratio is approximately ``ATR_long / contraction_range``. A ``VOL_RATIO``
of 0.4 implies roughly 2.5:1 *before any target is chosen*, and the R:R gate becomes a filter on the
contraction's tightness rather than an arbitrary requirement — loose contractions fail it
automatically. It is also the only strategy that **does not use ACT**: ACT confirms arrest when
entering *against* a move, and this enters *with* the resolution, so a directionless bracket of two
exact structural levels is used instead. It is the one strategy that declares
``expansion_seeking = True``, which is what exempts it from the ``VOL_EXTREME`` veto — a declaration
the risk engine reads, never a special case inside it.

**C3-B — persistence transition (trend birth).** Captures what C2-A concedes by waiting for
``|TRD_TSTAT| >= t_min``. Hard-partitioned from C2-A on that same threshold and on pullback depth:
C2-A takes ``[1.0, 3.0]`` ATR, C3-B takes the contiguous band **below** it, ``[0, 1.0)``. Both
partitions are derived from constants that already exist, so they consume **zero new parameters**. When
the trend matures the position **hands off to C2-A** — that is success, not invalidation.

**C3-C — structural boundary resolution.** The honest limit is stated in the signal itself: the
premise is that stops cluster beyond obvious levels, and **we cannot observe any of this** — there is
no liquidation feed and no message-level order flow. The mechanism is *inferred from structure, not
measured*, which is a weaker footing than C1-A's funding cash flow, and specs/05 §5 requires that to
appear in the reasoning output rather than be left implicit. Its dominant failure mode is whipsaw, and
the spec says plainly that condition 4 will not be sufficient.
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
from snapshot.models import MarketSnapshot, SeriesKey
from strategy.act import Direction, act
from strategy.base import Lifecycle, SetupState, StrategyOutcome, no_setup, setup, terminal, update
from strategy.c2_participation import PULLBACK_MAX_ATR, PULLBACK_MIN_ATR
from strategy.conditions import Condition, ConditionContext, ConditionResult, evaluate_all
from strategy.evaluation import (
    Levels,
    atr_1h,
    build_signal,
    compute_act,
    detail_of,
    price_now,
    signal_id,
)
from strategy.lifecycle import Invalidation, advance

COMPONENT = "strategy.c3"

#: C3-A's expected-payoff gate, applied at setup (specs/05 §3 condition 6). The same 3.0 as the
#: Class 3 ``k_net`` floor, and derived the same way — not a separate parameter.
MIN_EXPECTED_PAYOFF = 3.0

#: C3-C's fixed structural counts (specs/05 §5): a boundary needs two prior touches to be
#: significant, and a reclaim must happen within two 15m bars. Both fixed deliberately — a long
#: reclaim window would let almost any pullback qualify as a rejection, which is how this pattern
#: becomes unfalsifiable.
MIN_TOUCHES = 2
RECLAIM_BARS = 2

#: C3-B's transition must be fresh: this edge decays quickly (specs/05 §4 condition 7).
FRESH_BARS = 6

EXPIRY_BARS = 24


# ---- C3-A --------------------------------------------------------------------------------

@dataclass
class C3A:
    """C3-A — Volatility Contraction Resolution (specs/05 §3). Tier A, **expansion-seeking**."""

    params: dict[str, Any] = field(default_factory=dict)
    id: str = "C3-A"
    version: str = "1.0.0"
    strategy_class: int = 3
    tier: str = "A"
    tradeable: bool = True
    #: The declaration the ``VOL_EXTREME`` veto reads. C3-A arms *inside* a contraction expecting
    #: expansion, so vetoing it for expanding volatility would veto its entire premise.
    expansion_seeking: bool = True
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("VOL_RATIO", "VOL_OF_VOL", "ATR", "STR_SWING",
                                          "STR_RANGEPOS")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="compression_unusual",
                      description="volatility compression is in the bottom tail of its own trailing "
                                  "distribution, so it is genuinely unusual rather than merely quiet",
                      body=self._compression_unusual, required_features=("VOL_RATIO",)),
            Condition(name="compression_persisted",
                      description="the compression has held for at least the short lookback, so it "
                                  "is not one quiet bar",
                      body=self._compression_persisted, required_features=("VOL_RATIO",)),
            Condition(name="contraction_stable",
                      description="the volatility of volatility is low, so this is a stable "
                                  "contraction rather than a pause inside chaos",
                      body=self._contraction_stable, required_features=("VOL_OF_VOL",)),
            Condition(name="range_compressed",
                      description="the realised range agrees with the statistical measure",
                      body=self._range_compressed, required_features=("ATR",)),
            Condition(name="boundaries_defined",
                      description="the contraction has a well-defined high and low, so there is "
                                  "something to break",
                      body=self._boundaries_defined),
            Condition(name="payoff_sufficient",
                      description="the expansion target over the contraction range clears the "
                                  "Class 3 floor — a filter on the contraction's tightness, not an "
                                  "arbitrary requirement",
                      body=self._payoff_sufficient, required_features=("ATR",)),
        )

    def _vol_ratio_history(self, ctx: ConditionContext) -> np.ndarray:
        """The trailing ``VOL_RATIO`` series, for a percentile rather than a fixed threshold."""
        regime = ctx.regime
        # The regime classifier already cut the V axis from this exact series; reusing its boundaries
        # keeps one definition of "the bottom tail".
        boundaries = regime.axis_boundaries.get("vol_ratio") or []
        current = regime.axis_values.get("vol_ratio")
        if current is None:
            raise StrategyError(
                f"VOL_RATIO is unavailable: {regime.reason or 'unclassified'}",
                component=COMPONENT, operation="vol_ratio_history",
                context={"regime_reason": regime.reason},
                likely_cause="the V axis feature was not OK at this as_of",
                remediation="the strategy declines; compression cannot be judged without the axis",
            )
        return np.asarray(boundaries, dtype=np.float64)

    def _compression_unusual(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """``VOL_RATIO`` at or below the ``q_contract`` percentile of its trailing distribution.

        The percentile is compared against the regime model's own lower boundary where one exists,
        because that boundary *is* the trailing quantile — recomputing it here would be a second
        definition of the same cut.
        """
        q = float(ctx.param("contraction_percentile"))
        current = ctx.regime.axis_values.get("vol_ratio")
        if current is None:
            raise StrategyError(
                f"VOL_RATIO is unavailable: {ctx.regime.reason or 'unclassified'}",
                component=COMPONENT, operation="compression_unusual",
                context={"regime_reason": ctx.regime.reason},
                likely_cause="the V axis feature was not OK",
                remediation="the strategy declines",
            )
        boundaries = self._vol_ratio_history(ctx)
        if boundaries.size:
            # The lowest fitted boundary marks the bottom bucket of the trailing distribution.
            limit = float(boundaries[0])
            basis = f"the regime model's lower V boundary ({limit:.3f})"
        else:
            # Degraded mode: no fitted boundaries. A contraction is then judged against 1.0 scaled by
            # q, which is the raw-axis reading specs/12 §10 preserves, and the basis is reported so
            # nobody reads it as the fitted quantile.
            limit = q * 2
            basis = f"degraded fallback: {q} x 2 (no fitted boundary available)"
        ctx.scratch["vol_ratio"] = current
        ctx.scratch["compression_basis"] = basis
        return current <= limit, current, limit

    def _compression_persisted(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """Dwell in the contracted state, measured by the regime's own dwell counter where available."""
        dwell = ctx.regime.dwell_bars
        needed = int(ctx.param("lookback_1h_short"))
        if ctx.regime.degraded or ctx.regime.cell is None:
            # Without a cell there is no dwell counter. VOL_OF_VOL being low is the available evidence
            # that the contraction is not a single bar, and condition 3 already requires it — so this
            # reports unavailable rather than passing on an unmeasured claim.
            raise StrategyError(
                "the regime is degraded or unclassified, so compression dwell cannot be counted; "
                "condition 3 (stable contraction) remains the available evidence",
                component=COMPONENT, operation="compression_persisted",
                context={"degraded": ctx.regime.degraded, "reason": ctx.regime.reason},
                likely_cause="the regime model failed its validity tests for this asset (F-033)",
                remediation="C3-A cannot confirm persistence without a cell; it declines rather than "
                            "arming on one quiet bar",
            )
        return dwell >= needed, dwell, needed

    def _contraction_stable(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        vov = ctx.feature("VOL_OF_VOL", Timeframe.H1)   # NO_HORIZON in the registry
        vol = ctx.feature("VOL_CC", Timeframe.H1, "short")
        # Scale-free: the dispersion of short-horizon volatility relative to its own level. A ratio
        # below 0.5 means the contraction is steady rather than lurching, and it is structural rather
        # than tuned because it compares a quantity with itself.
        ratio = vov / vol if vol > 0 else float("inf")
        ctx.scratch["vol_of_vol_ratio"] = ratio
        return ratio < 0.5, ratio, 0.5

    def _range_compressed(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """Condition 4: the realised range agrees with the statistical measure.

        Two independent readings of the same compression. If they disagree, one of them is wrong and
        the setup is not armed — which is the same reasoning as C1-A's funding/basis sign check.
        """
        short_atr = ctx.feature("ATR", Timeframe.H1, "short")
        long_atr = ctx.feature("ATR", Timeframe.H1, "long")
        if long_atr <= 0:
            raise StrategyError(
                "the long-horizon ATR is zero, so range compression cannot be measured",
                component=COMPONENT, operation="range_compressed",
                context={"asset": ctx.asset},
                likely_cause="a degenerate window",
                remediation="the strategy declines",
            )
        ratio = short_atr / long_atr
        limit = float(ctx.param("contraction_percentile")) * 4
        ctx.scratch["atr_ratio"] = ratio
        ctx.scratch["atr_long"] = long_atr
        return ratio <= limit, ratio, limit

    def _boundaries_defined(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """The contraction window's high and low — the two bracket levels.

        Taken from ``STR_RANGEPOS``, which already measures position within the window's
        ``[min low, max high]``, so the boundaries come from the same reading the condition is
        expressed in.
        """
        fv = ctx.features.get("STR_RANGEPOS", Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK:
            raise StrategyError(
                f"STR_RANGEPOS is {fv.status.value}, so the contraction has no measured boundaries",
                component=COMPONENT, operation="boundaries_defined",
                context={"reason": fv.reason},
                likely_cause=fv.reason,
                remediation="the strategy declines; a bracket needs two exact levels",
            )
        low = fv.detail.get("range_low")
        high = fv.detail.get("range_high")
        if low is None or high is None or high <= low:
            raise StrategyError(
                "the contraction window has no usable high/low pair",
                component=COMPONENT, operation="boundaries_defined",
                context={"low": low, "high": high},
                likely_cause="a degenerate range",
                remediation="the strategy declines",
            )
        ctx.scratch["contraction_low"] = low
        ctx.scratch["contraction_high"] = high
        ctx.scratch["contraction_range"] = high - low
        return True, f"[{low:.2f}, {high:.2f}]", "a defined high and low"

    def _payoff_sufficient(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """``ATR_long_1h / contraction_range >= 3.0`` — the §2.2 gate applied at setup.

        This is the mechanical asymmetry made explicit: the setup computes and publishes its own
        expected payoff from measured quantities, so a loose contraction fails here rather than being
        rescued by a stretched target.
        """
        contraction_range = ctx.scratch.get("contraction_range")
        atr_long = ctx.scratch.get("atr_long") or ctx.feature("ATR", Timeframe.H1, "long")
        if not contraction_range:
            raise StrategyError(
                "the contraction range is unknown, so the expected payoff cannot be computed",
                component=COMPONENT, operation="payoff_sufficient",
                context={},
                likely_cause="condition 5 did not establish the boundaries",
                remediation="the strategy declines",
            )
        payoff = atr_long / contraction_range
        ctx.scratch["expected_payoff"] = payoff
        return payoff >= MIN_EXPECTED_PAYOFF, payoff, MIN_EXPECTED_PAYOFF

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
        return self._arm(snapshot, features, regime, ctx, results)

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        """Arms **pending**, with no direction: the timing is partially predictable, the direction is
        not, so a bracket is published and the resolution assigns the side."""
        armed_at = snapshot.as_of_ms
        high = Decimal(repr(float(ctx.scratch["contraction_high"])))
        low = Decimal(repr(float(ctx.scratch["contraction_low"])))
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            # A pending bracket has no direction yet. LONG is recorded as a placeholder and corrected
            # the moment a boundary resolves; the published detail carries both triggers so nothing
            # downstream reads the placeholder as a view.
            direction=Direction.LONG,
            armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"pending": True, "entry_up": str(high), "entry_dn": str(low),
                     "contraction_range": float(ctx.scratch["contraction_range"]),
                     "expected_payoff": float(ctx.scratch["expected_payoff"]),
                     "atr_long": float(ctx.scratch["atr_long"])})
        triggers = {"long": str(high), "short": str(low)}
        levels = Levels(entry=None, stop=None, targets=(),
                        detail={"entry_triggers": triggers,
                                "trigger_rule": "a confirmed 15m close beyond a boundary; a 15m close "
                                                "rather than an ATR buffer filters wicks without "
                                                "introducing a parameter",
                                "expected_payoff": float(ctx.scratch["expected_payoff"]),
                                "payoff_basis": "ATR_long / contraction_range, computed from measured "
                                                "quantities before any target is chosen"})
        rationale = (
            f"volatility is compressed ({ctx.scratch.get('compression_basis')}), the contraction has "
            f"held {regime.dwell_bars} bars and is stable, and the expansion target over the "
            f"contraction range gives an expected payoff of "
            f"{ctx.scratch['expected_payoff']:.1f}:1 before any target is chosen. **Direction is not "
            f"predicted**: a bracket is armed at {low} / {high} and the first confirmed 15m close "
            f"beyond a boundary assigns the side."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=Direction.LONG, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              detail={"pending_direction": True,
                                      "entry_triggers": triggers,
                                      "no_act": "ACT is an arrest-confirmation mechanism for entering "
                                                "against a move; this enters with the resolution, so "
                                                "a structural bracket is used instead",
                                      "dominant_failure_mode": "false breakouts, which are common and "
                                                               "expected; the R:R floor is the defence "
                                                               "and no filters are added to suppress "
                                                               "them (specs/05 §3)"})
        return setup(signal, results, state, reason="armed pending: bracket published, no direction")

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        price = price_now(features, snapshot)
        resolved = self._resolve(state, snapshot, features)
        if resolved is not None and state.context.get("pending"):
            state.direction = resolved
            state.context["pending"] = False
            state.context["resolved_at_ms"] = snapshot.as_of_ms
        levels = self._levels(state, features, ctx)
        act_like = self._bracket_state(state, price, resolved)
        progress = advance(state, act_state=act_like, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._expansion_without_resolution(ctx, state),),
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_like,
                              detail={"pending_direction": bool(state.context.get("pending")),
                                      "entry_triggers": {"long": state.context["entry_up"],
                                                         "short": state.context["entry_dn"]},
                                      "progress": progress.to_dict()})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_like, results, state)

    def _resolve(self, state: SetupState, snapshot: MarketSnapshot,
                 features: FeatureSet) -> Direction | None:
        """Which boundary a **confirmed 15m close** cleared, if any."""
        key = SeriesKey(features.asset, features.product, Timeframe.M15)
        series = snapshot.series.get(key)
        if series is None or not series.bars:
            return None
        last = series.bars[-1]
        up = Decimal(state.context["entry_up"])
        dn = Decimal(state.context["entry_dn"])
        if last.close > up:
            return Direction.LONG
        if last.close < dn:
            return Direction.SHORT
        return None

    def _bracket_state(self, state: SetupState, price: Decimal, resolved: Direction | None) -> Any:
        """A trigger state for the lifecycle machinery, built from the bracket rather than from ACT.

        Deliberately not an ``act()`` call: C3-A has no arrest to confirm. The shape is reused so the
        lifecycle transitions are identical, and ``delta`` is 0 to make it obvious in the record that
        no ACT ratchet is involved.
        """
        from strategy.act import ActState

        up = Decimal(state.context["entry_up"])
        dn = Decimal(state.context["entry_dn"])
        triggered = resolved is not None or not state.context.get("pending", True)
        target = up if state.direction is Direction.LONG else dn
        contraction = Decimal(repr(state.context["contraction_range"]))
        return ActState(direction=state.direction, x_star=dn if state.direction is Direction.LONG
                        else up, e_t=target, current=price, unit=contraction, delta=Decimal(0),
                        bars_since_armed=1, triggered=triggered,
                        distance_units=(target - price) / contraction if contraction else Decimal(0))

    def _levels(self, state: SetupState, features: FeatureSet, ctx: ConditionContext) -> Levels:
        """Stop at the **opposite boundary** — full structural invalidation. T1 is ``entry ± ATR_long``.

        The stop is wide *relative to the contraction* and narrow in absolute terms, which is the
        point: if price breaks up, re-enters, and crosses the entire compressed range, the break failed.
        """
        up = Decimal(state.context["entry_up"])
        dn = Decimal(state.context["entry_dn"])
        atr_long = Decimal(repr(state.context["atr_long"]))
        if state.context.get("pending"):
            return Levels(entry=None, stop=None, targets=(),
                          detail={"entry_triggers": {"long": str(up), "short": str(dn)}})
        if state.direction is Direction.LONG:
            entry, stop, target = up, dn, up + atr_long
        else:
            entry, stop, target = dn, up, dn - atr_long
        trail = state.context.get("trail_level")
        return Levels(entry=entry, stop=stop, targets=(target,),
                      detail={"stop_basis": "the opposite boundary: full structural invalidation",
                              "t1_basis": "entry +/- ATR_long: the expansion target from the "
                                          "un-contracted norm, not a parameter",
                              "trail_level": str(trail) if trail is not None else None,
                              "expected_payoff": state.context.get("expected_payoff")})

    def _expansion_without_resolution(self, ctx: ConditionContext,
                                      state: SetupState) -> Invalidation:
        """``VOL_RATIO`` returning to normal **without** a directional resolution.

        The expansion happened as chop rather than as a move, so the premise is spent — and the setup
        voids even though no trigger ever fired. That is the whole mechanism invalidation for C3-A.
        """
        if not state.context.get("pending"):
            return Invalidation(name="expansion_without_resolution", fired=False,
                                detail="already resolved directionally")
        current = ctx.regime.axis_values.get("vol_ratio")
        if current is None:
            return Invalidation(name="expansion_without_resolution", fired=False,
                                detail=f"VOL_RATIO unavailable: {ctx.regime.reason}")
        normal = 1.0
        fired = current >= normal
        return Invalidation(
            name="expansion_without_resolution", fired=fired,
            detail=(f"VOL_RATIO returned to {current:.3f} with no directional resolution: the "
                    "expansion occurred as chop rather than as a move, so the premise is spent"
                    if fired else f"VOL_RATIO {current:.3f} still compressed"),
            value=round(current, 4), threshold=normal)


# ---- C3-B --------------------------------------------------------------------------------

@dataclass
class C3B:
    """C3-B — Persistence Transition / trend birth (specs/05 §4). Tier A."""

    params: dict[str, Any] = field(default_factory=dict)
    id: str = "C3-B"
    version: str = "1.0.0"
    strategy_class: int = 3
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = (PRIMARY_VR, "TRD_RET", "TRD_TSTAT", "STR_SWING",
                                          "VOL_RATIO", "ATR", "DEV_ATR")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="vr_crossed_above_one",
                      description="the variance ratio has crossed from below 1 to above it — the "
                                  "transition event itself",
                      body=self._vr_crossed, required_features=(PRIMARY_VR,)),
            Condition(name="directional_impulse",
                      description="a directional impulse accompanies the crossing, which is what "
                                  "establishes the side",
                      body=self._directional_impulse, required_features=("TRD_RET",)),
            Condition(name="trend_not_established",
                      description="the trend is **not** yet established — the hard partition from "
                                  "C2-A, on the same threshold",
                      body=self._trend_not_established, required_features=("TRD_TSTAT",)),
            Condition(name="new_swing_extreme",
                      description="the impulse created a new swing extreme, confirming it structurally",
                      body=self._new_swing_extreme, required_features=("STR_SWING",)),
            Condition(name="volatility_expanding",
                      description="volatility is expanding, as transitions normally are; disagreement "
                                  "means one measure is wrong",
                      body=self._volatility_expanding),
            Condition(name="daily_not_opposed",
                      description="the daily trend is not significantly opposed, so this is not "
                                  "fighting the higher timeframe",
                      body=self._daily_not_opposed, required_features=("TRD_TSTAT",)),
            Condition(name="shallow_pullback",
                      description="the pullback is in the band below C2-A's — shallow, as it is at "
                                  "trend birth when urgency is high",
                      body=self._shallow_pullback, required_features=("DEV_ATR",)),
        )

    def _vr_crossed(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """The crossing itself, read from the two VR horizons rather than from stored history.

        ``MR_VR_K2`` above 1 with the longer ``MR_VR_K8`` still at or below 1 is a *recent* crossing:
        the short horizon responds first. That keeps the condition derived from the snapshot rather
        than from accumulated state, exactly as specs/13 §2 requires of everything else.
        """
        short = ctx.feature(PRIMARY_VR, Timeframe.H1, "short")
        long = ctx.features.get("MR_VR_K8", Timeframe.H1, "short")
        if long.status is not FeatureStatus.OK or long.value is None:
            raise StrategyError(
                f"MR_VR_K8 is {long.status.value}, so the crossing cannot be dated",
                component=COMPONENT, operation="vr_crossed",
                context={"reason": long.reason},
                likely_cause=long.reason,
                remediation="the strategy declines; without the longer horizon a crossing cannot be "
                            "distinguished from an established trend",
            )
        crossed = short > 1.0 >= long.value
        ctx.scratch["vr_short"] = short
        ctx.scratch["vr_long"] = long.value
        return crossed, f"k=2 {short:.3f}, k=8 {long.value:.3f}", "k=2 above 1, k=8 not yet"

    def _directional_impulse(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """``|TRD_RET(short)|`` in the upper tail, measured against the window's own volatility."""
        ret = ctx.feature("TRD_RET", Timeframe.H1, "short")
        vol = ctx.feature("VOL_CC", Timeframe.H1, "short")
        lookback = int(ctx.param("lookback_1h_short"))
        # An N-bar return is in the upper tail when it exceeds ~2 sigma of an N-bar random walk,
        # which is sigma * sqrt(N). Structural: derived from the measured volatility, not a threshold.
        expected = vol * (lookback ** 0.5)
        z = ret / expected if expected > 0 else 0.0
        ctx.scratch["impulse_z"] = z
        ctx.scratch["impulse_return"] = ret
        return abs(z) >= 2.0, z, 2.0

    def _trend_not_established(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        t_min = float(ctx.param("trend_tstat_threshold"))
        tstat = ctx.feature("TRD_TSTAT", Timeframe.H1, "medium")
        ctx.scratch["tstat_1h"] = tstat
        return abs(tstat) < t_min, abs(tstat), t_min

    def _new_swing_extreme(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        fv = ctx.features.get("STR_SWING", Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK:
            raise StrategyError(
                f"STR_SWING is {fv.status.value}, so a new extreme cannot be confirmed",
                component=COMPONENT, operation="new_swing_extreme",
                context={"reason": fv.reason},
                likely_cause=fv.reason,
                remediation="calibrate swing_atr_multiple; structural confirmation is condition 4",
            )
        impulse = ctx.scratch.get("impulse_return", 0.0)
        up = impulse > 0
        level = fv.detail.get("last_swing_high" if up else "last_swing_low")
        level_ms = fv.detail.get("last_swing_high_ms" if up else "last_swing_low_ms")
        if level is None or level_ms is None:
            return False, "no pivot of the needed kind", "a new swing extreme"
        age_bars = (ctx.as_of_ms - int(level_ms)) // Timeframe.H1.ms
        ctx.scratch["impulse_swing"] = level
        # "New" means within the short lookback: an extreme from last week is not this impulse's.
        return age_bars <= FRESH_BARS, f"{age_bars} bars old", f"<= {FRESH_BARS} bars"

    def _volatility_expanding(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        current = ctx.regime.axis_values.get("vol_ratio")
        if current is None:
            raise StrategyError(
                f"VOL_RATIO is unavailable: {ctx.regime.reason or 'unclassified'}",
                component=COMPONENT, operation="volatility_expanding",
                context={"regime_reason": ctx.regime.reason},
                likely_cause="the V axis feature was not OK",
                remediation="the strategy declines",
            )
        return current > 1.0, current, 1.0

    def _daily_not_opposed(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        daily = ctx.feature("TRD_TSTAT", Timeframe.D1, "medium")
        impulse = ctx.scratch.get("impulse_return", 0.0)
        t_min = float(ctx.param("trend_tstat_threshold"))
        opposed = (daily < -t_min) if impulse > 0 else (daily > t_min)
        return not opposed, f"1d {daily:+.2f} vs impulse {impulse:+.4f}", "not significantly opposed"

    def _shallow_pullback(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """``[0, 1.0)`` ATR — the contiguous band **below** C2-A's, so the two never overlap."""
        dev = ctx.feature("DEV_ATR", Timeframe.H1, "medium")
        depth = abs(dev)
        ctx.scratch["pullback_atr"] = depth
        return depth < PULLBACK_MIN_ATR, depth, f"< {PULLBACK_MIN_ATR} ATR (C2-A takes " \
                                                f"[{PULLBACK_MIN_ATR}, {PULLBACK_MAX_ATR}])"

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
        direction = Direction.LONG if ctx.scratch["impulse_return"] > 0 else Direction.SHORT
        return self._arm(snapshot, features, regime, ctx, results, direction)

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...],
             direction: Direction) -> StrategyOutcome:
        armed_at = snapshot.as_of_ms
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"impulse_origin": self._impulse_origin(ctx, features, snapshot, direction),
                     "impulse_return": ctx.scratch.get("impulse_return"),
                     "t_min": float(ctx.param("trend_tstat_threshold"))})
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product, direction,
                                          armed_at, self.params)
        state.x_star, state.e_t = act_state.x_star, act_state.e_t
        levels = self._levels(ctx, features, act_state, direction, state)
        persistence = regime.persistence_prob
        rationale = (
            f"the variance ratio crossed above 1 (k=2 {ctx.scratch.get('vr_short', float('nan')):.2f}, "
            f"k=8 {ctx.scratch.get('vr_long', float('nan')):.2f}) with a "
            f"{ctx.scratch.get('impulse_z', float('nan')):+.1f}-sigma impulse, while "
            f"|t| = {ctx.scratch.get('tstat_1h', float('nan')):.2f} is still below t_min — so the "
            f"trend is being born, not established. Entry takes the shallow pullback band C2-A leaves "
            f"vacant."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={"act_unit_bars": unit_bars,
                                      # specs/05 §4: a labelled regime statistic, never a trade win
                                      # rate. None in degraded mode, with the reason.
                                      "regime_persistence_prob": persistence,
                                      "confidence_unavailable_reason": (
                                          None if persistence is not None else
                                          "the regime model is degraded for this asset, so the "
                                          "transition matrix is unavailable (specs/12 §10)"),
                                      "persistence_caveat": "regime persistence and trade success are "
                                                            "different events; this is not a win rate",
                                      "partition": "hard partition from C2-A on t_min and on pullback "
                                                   "depth; both derived, zero new parameters"})
        return setup(signal, results, state, reason=f"armed {direction.value} at trend birth")

    def _impulse_origin(self, ctx: ConditionContext, features: FeatureSet,
                        snapshot: MarketSnapshot, direction: Direction) -> str | None:
        """Where the impulse began — the structural invalidation level."""
        key = SeriesKey(features.asset, features.product, Timeframe.H1)
        series = snapshot.series.get(key)
        if series is None or not series.bars:
            return None
        lookback = int(ctx.param("lookback_1h_short"))
        window = series.bars[-lookback:]
        origin = min(b.low for b in window) if direction is Direction.LONG \
            else max(b.high for b in window)
        return str(origin)

    def _levels(self, ctx: ConditionContext, features: FeatureSet, act_state: Any,
                direction: Direction, state: SetupState) -> Levels:
        """Stop beyond the impulse origin; T1 the measured move projected from the pullback extreme."""
        s = Decimal(str(ctx.param("stop_buffer_atr_multiple")))
        atr = atr_1h(features)
        origin = state.context.get("impulse_origin")
        if origin is None:
            raise StrategyError(
                "the impulse origin is unknown, so a structural stop cannot be placed",
                component=COMPONENT, operation="levels",
                context={"asset": features.asset},
                likely_cause="the 1h series was unavailable at arming",
                remediation="the strategy declines rather than substituting a stop",
            )
        origin_d = Decimal(str(origin))
        if direction is Direction.LONG:
            stop = min(origin_d, act_state.x_star) - s * atr
            impulse_length = act_state.x_star - origin_d
            target = act_state.e_t + abs(impulse_length)
        else:
            stop = max(origin_d, act_state.x_star) + s * atr
            impulse_length = origin_d - act_state.x_star
            target = act_state.e_t - abs(impulse_length)
        return Levels(entry=act_state.e_t, stop=stop, targets=(target,),
                      detail={"impulse_origin": str(origin_d), "atr_1h": str(atr),
                              "t1_basis": "the measured move: the impulse length projected from the "
                                          "pullback extreme",
                              "void_rule": "if the pullback retraces to the impulse origin the "
                                           "transition is failing and the setup voids rather than "
                                           "trading with a wider stop"})

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        levels = self._levels(ctx, features, act_state, state.direction, state)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._transition_failed(features),
                                       lambda: self._beyond_impulse_origin(state, price),
                                       lambda: self._matured_handoff(features, state)),
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={"act_unit_bars": unit_bars, "progress": progress.to_dict(),
                                      "regime_persistence_prob": regime.persistence_prob})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _transition_failed(self, features: FeatureSet) -> Invalidation:
        fv = features.get(PRIMARY_VR, Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="transition_failed", fired=False,
                                detail=f"{PRIMARY_VR} is {fv.status.value}: {fv.reason}")
        return Invalidation(
            name="transition_failed", fired=fv.value < 1.0,
            detail=("the variance ratio fell back below 1: the transition failed"
                    if fv.value < 1.0 else f"the transition holds at {fv.value:.3f}"),
            value=round(fv.value, 4), threshold=1.0)

    def _beyond_impulse_origin(self, state: SetupState, price: Decimal) -> Invalidation:
        origin = state.context.get("impulse_origin")
        if origin is None:
            return Invalidation(name="impulse_origin", fired=False, detail="no origin recorded")
        origin_d = Decimal(str(origin))
        fired = price < origin_d if state.direction is Direction.LONG else price > origin_d
        return Invalidation(
            name="impulse_origin", fired=fired,
            detail=(f"price {price} retraced to where the impulse began ({origin_d}): the transition "
                    "is failing, and the setup voids rather than trading with a wider stop"
                    if fired else f"price {price} holds beyond the origin {origin_d}"),
            value=str(price), threshold=str(origin_d))

    def _matured_handoff(self, features: FeatureSet, state: SetupState) -> Invalidation:
        """``|TRD_TSTAT|`` reaching ``t_min`` is **success**: the trend matured into one C2-A
        recognises, and the position transfers to C2-A's management rules."""
        fv = features.get("TRD_TSTAT", Timeframe.H1, "medium")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="matured_handoff", fired=False,
                                detail=f"TRD_TSTAT is {fv.status.value}: {fv.reason}")
        t_min = float(state.context.get("t_min", self.params["trend_tstat_threshold"]))
        fired = abs(fv.value) >= t_min
        return Invalidation(
            name="matured_handoff", fired=fired,
            detail=(f"HANDOFF C3-B -> C2-A: |t| reached {abs(fv.value):.2f}, at or beyond t_min "
                    f"({t_min}). The trend has matured into one C2-A recognises — this is success, "
                    "not invalidation, and the position transfers to C2-A's management rules"
                    if fired else f"|t| {abs(fv.value):.2f} still below t_min"),
            value=round(abs(fv.value), 4), threshold=t_min)


# ---- C3-C --------------------------------------------------------------------------------

@dataclass
class C3C:
    """C3-C — Structural Boundary Resolution (specs/05 §5). Tier A.

    Arms **pending** and lets the resolution assign the direction: acceptance trades with the break,
    rejection trades against it. The rejection branch carries the better geometry — a tight stop just
    beyond a failed extreme against a target spanning the range — and acceptance will frequently fail
    the R:R gate, which specs/05 §5 says to allow rather than stretching its target to qualify.
    """

    params: dict[str, Any] = field(default_factory=dict)
    id: str = "C3-C"
    version: str = "1.0.0"
    strategy_class: int = 3
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("STR_SWING", "STR_DIST_PDH", "VOL_RATIO", "VLM_ZROBUST",
                                          "ATR", "DEV_Z")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="near_boundary",
                      description="price is within c x ATR of a defined structural boundary",
                      body=self._near_boundary,
                      required_features=("STR_SWING", "STR_DIST_PDH")),
            Condition(name="boundary_significant",
                      description="the boundary has been respected at least twice before, so it is "
                                  "not an arbitrary line",
                      body=self._boundary_significant, required_features=("STR_SWING",)),
            Condition(name="not_a_blowout",
                      description="volatility is not in the top decile, where levels do not hold and "
                                  "the premise fails",
                      body=self._not_a_blowout),
            Condition(name="not_already_chopping",
                      description="the level has not already been breached and reclaimed repeatedly "
                                  "in this approach — chop voids the setup",
                      body=self._not_already_chopping, required_features=("STR_SWING",)),
        )

    def _near_boundary(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """The nearest defined boundary, taking the *closer* of the two prior-period extremes.

        ``STR_DIST_PDH``'s scalar is the distance to the prior **high** only; its ``detail`` carries
        both, and taking only the high would make a price sitting on the prior low look far from
        structure. Both candidates are considered and the nearest wins.
        """
        c = float(ctx.param("act_trigger_multiple"))
        candidates: list[tuple[str, float, float]] = []

        swing = ctx.features.get("STR_SWING", Timeframe.H1, "short")
        if swing.status is FeatureStatus.OK and swing.value is not None:
            level = swing.detail.get("nearest_level")
            if level is not None:
                candidates.append(("STR_SWING", abs(swing.value), float(level)))

        pdh = ctx.features.get("STR_DIST_PDH", Timeframe.H1, "short")
        if pdh.status is FeatureStatus.OK:
            for side, dist_key in (("prior_high", "dist_high_atr"), ("prior_low", "dist_low_atr")):
                level = pdh.detail.get(side)
                dist = pdh.detail.get(dist_key)
                if level is not None and dist is not None:
                    candidates.append((f"STR_DIST_PD/{side}", abs(float(dist)), float(level)))

        best = min(candidates, key=lambda t: t[1]) if candidates else None
        if best is None:
            raise StrategyError(
                "no defined structural boundary is available",
                component=COMPONENT, operation="near_boundary",
                context={"asset": ctx.asset},
                likely_cause="swing_atr_multiple is uncalibrated and no prior-period extreme is "
                             "available",
                remediation="calibrate swing_atr_multiple, or ensure the 1d series is present",
            )
        source, distance, level = best
        ctx.scratch["boundary"] = level
        ctx.scratch["boundary_source"] = source
        ctx.scratch["boundary_distance_atr"] = distance
        return distance <= c, f"{source} at {distance:.3f} ATR", f"<= {c} ATR"

    def _boundary_significant(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        fv = ctx.features.get("STR_SWING", Timeframe.H1, "short")
        source = ctx.scratch.get("boundary_source")
        touches = (fv.detail.get("nearest_touches")
                   if source == "STR_SWING" and fv.status is FeatureStatus.OK else None)
        if touches is None:
            # The prior-period extreme has no touch count; specs/05 §5 condition 2 is about swing
            # levels, so a prior-day boundary is reported as unverifiable rather than assumed valid.
            raise StrategyError(
                "this boundary has no touch count, so its significance cannot be verified",
                component=COMPONENT, operation="boundary_significant",
                context={"source": ctx.scratch.get("boundary_source")},
                likely_cause="the nearest boundary is a prior-period extreme rather than a confirmed "
                             "swing level",
                remediation="the strategy declines; condition 2 exists so the level is not arbitrary",
            )
        ctx.scratch["boundary_touches"] = touches
        return touches >= MIN_TOUCHES, touches, MIN_TOUCHES

    def _not_a_blowout(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        current = ctx.regime.axis_values.get("vol_ratio")
        if current is None:
            raise StrategyError(
                f"VOL_RATIO is unavailable: {ctx.regime.reason or 'unclassified'}",
                component=COMPONENT, operation="not_a_blowout",
                context={"regime_reason": ctx.regime.reason},
                likely_cause="the V axis feature was not OK",
                remediation="the strategy declines",
            )
        boundaries = ctx.regime.axis_boundaries.get("vol_ratio") or []
        # The top bucket of the fitted distribution is the decile stand-in; in degraded mode a raw
        # reading of 2.0 (short-horizon vol at twice the long-horizon norm) is used and reported.
        limit = float(boundaries[-1]) if boundaries else 2.0
        return current < limit, current, limit

    def _not_already_chopping(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """Condition 4, and specs/05 §5 says plainly it **will not be sufficient**.

        Touch count is the available proxy for "breached and reclaimed repeatedly": a level that has
        been traded through many times is not holding. The spec's own warning is carried into the
        signal's detail rather than being left in the documentation.
        """
        touches = ctx.scratch.get("boundary_touches", 0)
        limit = 6
        return touches <= limit, touches, f"<= {limit} touches"

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
        return self._arm(snapshot, features, regime, ctx, results)

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        armed_at = snapshot.as_of_ms
        boundary = Decimal(repr(float(ctx.scratch["boundary"])))
        # Which side price approached from. Recorded at arming because the whole acceptance/rejection
        # judgement is relative to it, and re-deriving it later from a price that has since crossed
        # would invert the reading.
        approach = price_now(features, snapshot)
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=Direction.LONG,                   # placeholder; the resolution assigns it
            armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"pending": True, "boundary": str(boundary),
                     "boundary_source": ctx.scratch.get("boundary_source"),
                     "touches": ctx.scratch.get("boundary_touches"),
                     "approach_price": str(approach),
                     "inside_is_below": approach < boundary,
                     "breaks_seen": []})
        levels = Levels(entry=None, stop=None, targets=(),
                        detail={"boundary": str(boundary),
                                "resolution_rule": "acceptance = a 15m close beyond, then a second "
                                                   "that does not reclaim, with volume above median; "
                                                   "rejection = a close beyond then back inside "
                                                   f"within {RECLAIM_BARS} bars"})
        rationale = (
            f"price is {ctx.scratch['boundary_distance_atr']:.2f} ATR from a "
            f"{ctx.scratch['boundary_source']} boundary at {boundary} that has been respected "
            f"{ctx.scratch.get('boundary_touches')} times. **Direction is not predicted**: the setup "
            f"waits for acceptance or rejection and trades whichever occurs. The premise is that stops "
            f"cluster beyond obvious levels — **we cannot observe that**: there is no liquidation feed "
            f"and no order-flow data, so the mechanism is inferred from structure rather than measured."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=Direction.LONG, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              detail={"pending_direction": True, "boundary": str(boundary),
                                      "epistemic_caveat": "the stop-clustering mechanism is inferred "
                                                          "from structure, not measured: there is no "
                                                          "liquidation feed and no message-level order "
                                                          "flow (specs/00 §6). This is a weaker footing "
                                                          "than C1-A's funding cash flow",
                                      "dominant_failure_mode": "whipsaw. Of all strategies here this "
                                                               "is the most likely to underperform its "
                                                               "backtest, because whipsaw is exactly "
                                                               "what historical bars under-represent "
                                                               "(specs/05 §5)"})
        return setup(signal, results, state, reason="armed pending: awaiting acceptance or rejection")

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        branch, direction = self._resolve(state, snapshot, features)
        if branch and state.context.get("pending"):
            state.context["pending"] = False
            state.context["branch"] = branch
            state.direction = direction
        price = price_now(features, snapshot)
        levels = self._levels(ctx, features, snapshot, state)
        act_state = self._trigger_state(state, features, snapshot, price)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._chop_voided(state),),
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={"branch": state.context.get("branch"),
                                      "pending_direction": bool(state.context.get("pending")),
                                      "progress": progress.to_dict()})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _resolve(self, state: SetupState, snapshot: MarketSnapshot,
                 features: FeatureSet) -> tuple[str | None, Direction]:
        """Acceptance or rejection, from the confirmed 15m closes since arming.

        The direction of the *break* is relative to which side of the boundary price was on when the
        setup armed — without that reference, "three closes below the level" would read as an
        acceptance of a downside break even when price never approached from above, which is how a
        boundary strategy ends up permanently short of every level above it.

        The reclaim window is **two bars, fixed** (``RECLAIM_BARS``). A long window would let almost
        any pullback qualify as a rejection, which is how this pattern becomes unfalsifiable.
        """
        key = SeriesKey(features.asset, features.product, Timeframe.M15)
        series = snapshot.series.get(key)
        if series is None or not series.bars:
            return None, state.direction

        boundary = Decimal(state.context["boundary"])
        inside_is_below = bool(state.context["inside_is_below"])
        bars = [b for b in series.bars if b.open_time_ms >= state.armed_at_ms]
        if len(bars) < 2:
            return None, state.direction

        def beyond(price: Decimal) -> bool:
            return price > boundary if inside_is_below else price < boundary

        break_dir = Direction.LONG if inside_is_below else Direction.SHORT
        flags = [beyond(b.close) for b in bars]

        # Find the most recent crossing from inside to beyond, and judge what followed it.
        last_break = None
        for i in range(1, len(flags)):
            if flags[i] and not flags[i - 1]:
                last_break = i
                self._note_break(state, int(bars[i].open_time_ms))
        if last_break is None:
            return None, state.direction

        after = flags[last_break + 1:]
        if len(after) >= RECLAIM_BARS and all(after[:RECLAIM_BARS]):
            # Held beyond for the whole reclaim window: acceptance, trading *with* the break. Volume
            # above median is required -- a break on thin volume is the whipsaw case.
            if not self._volume_above_median(features):
                return None, state.direction
            return "acceptance", break_dir
        if after and not after[-1]:
            # Closed back inside within the window: rejection, trading *against* the break.
            return "rejection", break_dir.opposite
        return None, state.direction

    def _note_break(self, state: SetupState, at_ms: int) -> None:
        """Record a breach so :meth:`_chop_voided` can count them. Idempotent on re-evaluation."""
        breaks = state.context.setdefault("breaks_seen", [])
        if at_ms not in breaks:
            breaks.append(at_ms)

    def _volume_above_median(self, features: FeatureSet) -> bool:
        fv = features.get("VLM_ZROBUST", Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return False
        return fv.value > 0

    def _trigger_state(self, state: SetupState, features: FeatureSet, snapshot: MarketSnapshot,
                       price: Decimal) -> Any:
        """Acceptance enters on the confirmed close; rejection enters through **ACT on the reclaim**."""
        from strategy.act import ActState

        branch = state.context.get("branch")
        atr = atr_1h(features)
        if branch == "rejection":
            delta = Decimal(str(self.params["act_trigger_multiple"]))
            extreme = state.context.get("failed_extreme")
            if extreme is None:
                key = SeriesKey(features.asset, features.product, Timeframe.M15)
                series = snapshot.series.get(key)
                bars = series.bars[-(RECLAIM_BARS + 1):] if series else ()
                if bars:
                    extreme = str(max(b.high for b in bars)
                                  if state.direction is Direction.SHORT
                                  else min(b.low for b in bars))
                    state.context["failed_extreme"] = extreme
            if extreme is not None:
                history = [Decimal(str(extreme)), price]
                return act(state.direction, history, 0, atr, delta)
        return ActState(direction=state.direction, x_star=price, e_t=price, current=price, unit=atr,
                        delta=Decimal(0), bars_since_armed=1,
                        triggered=not state.context.get("pending", True))

    def _levels(self, ctx: ConditionContext, features: FeatureSet, snapshot: MarketSnapshot,
                state: SetupState) -> Levels:
        s = Decimal(str(ctx.param("stop_buffer_atr_multiple")))
        atr = atr_1h(features)
        boundary = Decimal(state.context["boundary"])
        branch = state.context.get("branch")
        if state.context.get("pending") or branch is None:
            return Levels(entry=None, stop=None, targets=(),
                          detail={"boundary": str(boundary), "branch": None})
        long = state.direction is Direction.LONG
        if branch == "acceptance":
            entry = boundary
            stop = boundary - s * atr if long else boundary + s * atr
            # The next structural level, or a measured move of one long-horizon ATR.
            target = entry + atr if long else entry - atr
            basis = "the next structural level, approximated by a measured move of one ATR"
            stop_basis = ("s x ATR_1h back inside the boundary: the break is wrong if price returns "
                          "through the level it accepted")
        else:
            extreme = Decimal(str(state.context.get("failed_extreme", boundary)))
            entry = boundary
            stop = extreme + s * atr if not long else extreme - s * atr
            sma = detail_of(features, "DEV_Z", Timeframe.H1, "medium").get("sma")
            target = (Decimal(repr(float(sma))) if sma is not None
                      else (entry + 2 * atr if long else entry - 2 * atr))
            basis = ("the opposite side of the range, approximated by the trailing mean -- wide "
                     "against a tight stop just beyond the failed extreme")
            stop_basis = ("s x ATR_1h beyond the failed extreme -- the tightest stop either branch "
                          "offers, which is why rejection carries the better geometry")
        return Levels(entry=entry, stop=stop, targets=(target,),
                      detail={"boundary": str(boundary), "branch": branch, "atr_1h": str(atr),
                              "t1_basis": basis, "stop_basis": stop_basis,
                              "geometry_note": "the rejection branch carries the better geometry and "
                                               "will usually be the one that clears the k >= 3 gate; "
                                               "acceptance is allowed to fail it rather than having "
                                               "its target stretched"})

    def _chop_voided(self, state: SetupState) -> Invalidation:
        breaks = state.context.get("breaks_seen") or []
        fired = len(breaks) > MIN_TOUCHES
        return Invalidation(
            name="chop_voided", fired=fired,
            detail=(f"the level has been breached and reclaimed {len(breaks)} times in this approach: "
                    "it is not working, and chop voids the setup"
                    if fired else f"{len(breaks)} breach(es) in this approach"),
            value=len(breaks), threshold=MIN_TOUCHES)
