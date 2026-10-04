"""Class 4 — established methodologies and the benchmark suite (specs/06).

Classes 1–3 are bespoke designs justified by mechanism and reasoning; **no one has ever traded them
and they have never been tested by anyone, anywhere.** Class 4 draws on methodologies with decades of
published, cross-market scrutiny. That is a different *epistemic status*, not a different mechanism,
and it gives the class two roles — the second of which matters more:

1. tradeable strategies, implemented rigorously rather than as textbook versions, and
2. **the benchmark against which Classes 1–3 must justify their own existence.**

If C1-B cannot beat an unconditioned short-horizon reversal rule, its variance-ratio conditioning is
decoration. If C2-A cannot beat a Donchian breakout, its pullback machinery adds nothing but failure
modes. specs/06 §6 pre-registers those comparisons and attaches teeth: **a bespoke strategy that
fails its benchmark is cut, not tuned.** :data:`BENCHMARK_FOR` is that table in code, published on
every Class 4 signal so the pairing cannot drift from the spec silently.

⚠ **Everything about the external evidence base is conceptual knowledge, not live data.** Its role is
strictly to justify *including a strategy for testing*, never to justify *believing it works*. No
performance figure, Sharpe ratio, win rate or return number from that literature appears anywhere in
this module, in any default, or in any signal — specs/06 §2 and acceptance criterion 8. The named
authors below are referenced for a general conclusion only.

**Dual-mode operation (specs/06 §3).** Benchmark mode is always on and never suppressed: censoring
exactly the periods where the bespoke strategies were active would destroy the comparison. Suggestion
mode deduplicates — a Class 4 signal reaches the user only when no Class 1–3 signal is active on the
same asset in the same direction. That deduplication is **not** done here; it is
:func:`arbitration.pipeline.merge_same_direction`, which merges on (asset, direction) and picks the
primary by class seniority, so a Class 4 signal loses to a bespoke one by construction and is recorded
with the superseding id. Implementing a second dedup inside the strategy would be a second rule that
could disagree with the first.

**Parameters: zero (specs/06 §8).** Everything is reused — the shared lookbacks for the channel and
the momentum horizon, ``s`` for every stop and trail, ``t_min`` for C4-A's direction filter, ``δ`` for
C4-B's ACT entry, and the 15m-close confirmation rule borrowed from C3-A. The volume-tail exclusion,
"no pyramiding" and "return sign rather than a moving-average crossover" are fixed structural choices.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.errors import StrategyError
from features.value import FeatureSet, FeatureStatus
from normalize.canonical import Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot, SeriesKey
from strategy.act import ActState, Direction
from strategy.base import Lifecycle, SetupState, StrategyOutcome, no_setup, setup, terminal, update
from strategy.conditions import Condition, ConditionContext, ConditionResult, evaluate_all
from strategy.evaluation import (
    Levels,
    atr_1h,
    build_signal,
    compute_act,
    detail_of,
    effective_stop,
    price_now,
    signal_id,
    update_trail,
)
from strategy.lifecycle import Invalidation, advance

COMPONENT = "strategy.c4"

#: specs/06 §10. Class 4 rests on external evidence this system has not verified; Classes 1-3 rest on
#: mechanism reasoning that no one has verified. Those are different claims, and the UI shows which
#: one a suggestion rests on rather than presenting both as the same kind of warrant.
EVIDENCE_BASIS = "external_literature"

#: The specs/06 §6 comparison protocol, in code. Published on every signal so a reader can see what
#: each member is a control for, and so the Phase 9 pre-registration can be generated from one source
#: rather than transcribed.
BENCHMARK_FOR: dict[str, tuple[str, ...]] = {
    "C4-A": ("C2-A", "C3-A"),
    "C4-B": ("C2-A", "C2-B", "C3-B"),
    "C4-C": ("C1-B",),
}

#: C4-C's only filter: the top decile of volume, as a crude news proxy. A *structural* constant, not a
#: parameter — specs/06 §8 fixes it, and the literature is genuinely mixed on the direction of the
#: volume interaction, so tuning it would be fitting noise with a citation attached.
VOLUME_TAIL_DECILE = 0.90

#: Top-decile ``VOL_RATIO`` as a z-score stand-in where the regime model has no fitted boundaries.
#: Breakouts there are usually exhaustion rather than initiation (specs/06 §4.1).
EXHAUSTION_VOL_RATIO = 2.0

EXPIRY_BARS = 24


def _mode_detail(strategy: Any) -> dict[str, Any]:
    """The specs/06 §10 signal additions, identical for every member."""
    return {
        "mode": strategy.mode,
        "evidence_basis": EVIDENCE_BASIS,
        "benchmark_for": list(BENCHMARK_FOR.get(strategy.id, ())),
        "control": None,
        "seed": None,
        # Set by arbitration when suggestion-mode deduplication supersedes this signal. Published as
        # null here rather than omitted, so a consumer never has to distinguish "not suppressed" from
        # "the field is missing".
        "suppressed_by": None,
        "evidence_caveat": "the warrant for this strategy is external literature this system cannot "
                           "verify; the only evidence that counts for deployment is this system's own "
                           "backtest on this exchange's data (specs/06 §2)",
    }


def _channel(ctx: ConditionContext, horizon: str = "medium") -> tuple[float, float]:
    """The Donchian channel: the window's ``[min low, max high]``.

    Read from ``STR_RANGEPOS``'s own detail rather than recomputed, so the channel a condition tests
    and the channel a level is placed at are the same two numbers. The horizon is a shared lookback
    (specs/02 §4) — the channel introduces no lookback of its own, which is what keeps this class at
    zero parameters.
    """
    fv = ctx.features.get("STR_RANGEPOS", Timeframe.H1, horizon)
    if fv.status is not FeatureStatus.OK:
        raise StrategyError(
            f"STR_RANGEPOS({horizon}) is {fv.status.value}, so there is no channel to break out of",
            component=COMPONENT, operation="channel",
            context={"asset": ctx.asset, "horizon": horizon, "reason": fv.reason},
            likely_cause=fv.reason,
            remediation="the strategy declines; a breakout needs a defined channel",
        )
    low, high = fv.detail.get("range_low"), fv.detail.get("range_high")
    if low is None or high is None or high <= low:
        raise StrategyError(
            "the channel window has no usable high/low pair",
            component=COMPONENT, operation="channel",
            context={"low": low, "high": high},
            likely_cause="a degenerate range",
            remediation="the strategy declines",
        )
    return float(low), float(high)


def _not_exhaustion(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """``VOL_RATIO`` outside the top decile — shared by C4-A and C4-C.

    One of only two filters C4-A applies. specs/06 §4.1 is explicit that each additional filter is a
    parameter fitted to a specific history and that breakout systems are especially easy to
    over-filter into non-existence, so the count stays at two.
    """
    current = ctx.regime.axis_values.get("vol_ratio")
    if current is None:
        raise StrategyError(
            f"VOL_RATIO is unavailable: {ctx.regime.reason or 'unclassified'}",
            component=COMPONENT, operation="not_exhaustion",
            context={"regime_reason": ctx.regime.reason},
            likely_cause="the V axis feature was not OK at this as_of",
            remediation="the strategy declines rather than skipping its volatility filter",
        )
    boundaries = ctx.regime.axis_boundaries.get("vol_ratio") or []
    limit = float(boundaries[-1]) if boundaries else EXHAUSTION_VOL_RATIO
    return current < limit, current, limit


# ---- C4-A --------------------------------------------------------------------------------

@dataclass
class C4A:
    """C4-A — Donchian channel breakout with a volatility-scaled exit (specs/06 §4.1). Tier A.

    Channel-breakout trend following is among the most extensively documented systematic approaches in
    finance — the Donchian rules, the Turtle system, the managed-futures literature, and the
    time-series momentum work of Moskowitz, Ooi and Pedersen with its subsequent replications. Three
    mechanisms rather than a pattern: gradual information diffusion producing under-reaction;
    positive-feedback and herding extending moves; and a risk-transfer premium for taking the other
    side of hedging demand. The payoff is deliberately fat-tailed — a minority of large winners funds
    a majority of small losers.

    Four departures from the textbook, each removing an arbitrary choice rather than adding a clever
    one: the exit is a volatility-scaled trail instead of an M-period opposite channel (one fewer
    lookback); **no pyramiding** (it adds parameters and concentrates risk); only breakouts aligned
    with ``sign(TRD_TSTAT(1d))`` are taken; and breakouts in the top decile of ``VOL_RATIO`` are
    skipped as exhaustion. **No T1 at all** — capping a trend strategy's winner is the canonical way
    to destroy its expectancy.

    specs/06 §4.1 also makes a falsifiable prediction: poor performance in Axis P mean-reverting
    cells. **If the backtest does not show that, the regime model, the strategy, or the backtest is
    wrong** — and finding out which is worth more than the headline result. The regime cell travels on
    every signal so that analysis needs no extra plumbing.
    """

    params: dict[str, Any] = field(default_factory=dict)
    mode: str = "suggestion"
    id: str = "C4-A"
    version: str = "1.0.0"
    strategy_class: int = 4
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("STR_RANGEPOS", "TRD_TSTAT", "VOL_RATIO", "ATR")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="breakout_confirmed",
                      description="a confirmed 15m close beyond the channel boundary — the same "
                                  "confirmation rule as C3-A, reused rather than reinvented",
                      body=self._breakout_confirmed, required_features=("STR_RANGEPOS",)),
            Condition(name="daily_direction_aligned",
                      description="the breakout agrees with a significant daily trend; the single "
                                  "highest-value filter for any breakout system",
                      body=self._daily_aligned, required_features=("TRD_TSTAT",)),
            Condition(name="not_exhaustion_volatility",
                      description="volatility is outside the top decile, where breakouts are usually "
                                  "exhaustion rather than initiation",
                      body=_not_exhaustion),
        )

    def _breakout_confirmed(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        low, high = _channel(ctx)
        key = SeriesKey(ctx.asset, ctx.features.product, Timeframe.M15)
        series = ctx.snapshot.series.get(key)
        if series is None or not series.bars:
            raise StrategyError(
                f"no closed 15m bars for {ctx.asset}, so a breakout cannot be confirmed",
                component=COMPONENT, operation="breakout_confirmed",
                context={"asset": ctx.asset},
                likely_cause="the 15m series is missing from the snapshot",
                remediation="add the 15m series to the SnapshotRequest; the confirmation rule is a "
                            "15m close",
            )
        close = series.bars[-1].close
        ctx.scratch["channel_low"], ctx.scratch["channel_high"] = low, high
        if close > Decimal(repr(high)):
            ctx.scratch["breakout"] = Direction.LONG
            ctx.scratch["boundary"] = high
            extension = (float(close) - high) / max(high - low, 1e-12)
            ctx.scratch["extension"] = extension
            return True, f"15m close {close} above the channel high {high:.2f}", "beyond a boundary"
        if close < Decimal(repr(low)):
            ctx.scratch["breakout"] = Direction.SHORT
            ctx.scratch["boundary"] = low
            extension = (low - float(close)) / max(high - low, 1e-12)
            ctx.scratch["extension"] = extension
            return True, f"15m close {close} below the channel low {low:.2f}", "beyond a boundary"
        return False, f"15m close {close} inside [{low:.2f}, {high:.2f}]", "beyond a boundary"

    def _daily_aligned(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        daily = ctx.feature("TRD_TSTAT", Timeframe.D1, "medium")
        t_min = float(ctx.param("trend_tstat_threshold"))
        breakout = ctx.scratch.get("breakout")
        ctx.scratch["tstat_1d"] = daily
        if breakout is None:
            # Condition 1 found no breakout, so there is no direction to align with. Reported rather
            # than passed: "no breakout" and "breakout against the daily trend" are different states
            # and the rejection record must distinguish them.
            raise StrategyError(
                "there is no breakout to align, so the direction filter is not applicable",
                component=COMPONENT, operation="daily_aligned",
                context={"tstat_1d": daily},
                likely_cause="condition 1 did not find a confirmed breakout",
                remediation="none; this is the normal state most of the time",
            )
        aligned = daily >= t_min if breakout is Direction.LONG else daily <= -t_min
        return aligned, f"1d t = {daily:+.2f} vs {breakout.value}", f"|t| >= {t_min} and same sign"

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))
        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, results)
        if not all(r.passed for r in results):
            failed = [r.name for r in results if not r.passed]
            return no_setup(results, reason=f"{len(failed)} condition(s) not met: {', '.join(failed)}")
        return self._arm(snapshot, features, regime, ctx, results)

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        direction = ctx.scratch["breakout"]
        armed_at = snapshot.as_of_ms
        boundary = Decimal(repr(float(ctx.scratch["boundary"])))
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"boundary": str(boundary),
                     "channel_low": ctx.scratch["channel_low"],
                     "channel_high": ctx.scratch["channel_high"],
                     "extension": ctx.scratch.get("extension"),
                     "tstat_1d": ctx.scratch.get("tstat_1d")})
        levels = self._levels(features, state)
        act_state = self._entry_state(state, features, price_now(features, snapshot))
        rationale = (
            f"a confirmed 15m close carried price {ctx.scratch['extension']:.1%} of the channel's own "
            f"width beyond its {direction.value.lower()} boundary at {boundary}, with the daily trend "
            f"significant and agreeing (t = {ctx.scratch['tstat_1d']:+.2f}) and volatility outside the "
            f"exhaustion decile. **No target is published**: the position is trailed from entry, "
            f"because capping a trend strategy's winner destroys its expectancy."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={**_mode_detail(self), "strategy_class": 4,
                                      "strength_inputs": {"breakout_extension":
                                                          ctx.scratch.get("extension"),
                                                          "abs_tstat_1d":
                                                          abs(ctx.scratch.get("tstat_1d", 0.0))},
                                      "filters_applied": 2,
                                      "filter_note": "deliberately only two filters: each additional "
                                                     "one is a parameter fitted to a specific "
                                                     "history, and breakout systems are especially "
                                                     "easy to over-filter into non-existence",
                                      "pyramiding": False,
                                      "regime_prediction": "specs/06 §4.1 predicts poor performance "
                                                           "in Axis P mean-reverting cells; if the "
                                                           "backtest does not show that, the regime "
                                                           "model, the strategy or the backtest is "
                                                           "wrong"})
        return setup(signal, results, state, reason=f"breakout armed {direction.value}")

    def _entry_state(self, state: SetupState, features: FeatureSet, price: Decimal) -> ActState:
        """The breakout is its own trigger: the confirming 15m close has already happened.

        Not an ``act()`` call, and ``delta`` is zero so the record shows plainly that no arrest was
        confirmed — C4-A enters *with* the move, as C3-A does, and ACT confirms arrest when entering
        *against* one.
        """
        boundary = Decimal(state.context["boundary"])
        return ActState(direction=state.direction, x_star=boundary, e_t=boundary, current=price,
                        unit=atr_1h(features), delta=Decimal(0), bars_since_armed=1, triggered=True)

    def _levels(self, features: FeatureSet, state: SetupState) -> Levels:
        """Entry at the boundary, stop ``s * ATR_1h`` **inside** the channel, **no targets**."""
        s = Decimal(str(self.params["stop_buffer_atr_multiple"]))
        atr = atr_1h(features)
        boundary = Decimal(state.context["boundary"])
        stop = boundary - s * atr if state.direction is Direction.LONG else boundary + s * atr
        trail = state.context.get("trail_level")
        return Levels(entry=boundary, stop=stop, targets=(),
                      detail={"channel": [state.context["channel_low"], state.context["channel_high"]],
                              "stop_basis": "s x ATR_1h inside the channel",
                              "targets": "none by design: the position is trailed from entry at "
                                         "s x ATR_1h from the running extreme, because a fixed target "
                                         "caps the fat tail this strategy exists to capture",
                              "trail_level": str(trail) if trail is not None else None})

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        price = price_now(features, snapshot)
        act_state = self._entry_state(state, features, price)
        update_trail(state, atr=atr_1h(features),
                     s=Decimal(str(self.params["stop_buffer_atr_multiple"])), price=price)
        levels = self._levels(features, state)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._reentered_channel(state, price),),
                           stop=effective_stop(state, levels.stop), targets=())
        update_trail(state, atr=atr_1h(features),
                     s=Decimal(str(self.params["stop_buffer_atr_multiple"])), price=price)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=self._levels(features, state), conditions=results,
                              rationale=progress.reason, state=state, act_state=act_state,
                              detail={**_mode_detail(self), "strategy_class": 4,
                                      "progress": progress.to_dict(),
                                      "trail_level": state.context.get("trail_level")})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _reentered_channel(self, state: SetupState, price: Decimal) -> Invalidation:
        """Price back inside the channel before the trade has moved: the breakout was false.

        Checked only before the trail has engaged. Once trailing, the trail is the exit — adding a
        second exit rule on the same position would mean two mechanisms racing to close it, and the
        backtest result would depend on which one the loop happened to check first.
        """
        if state.context.get("trail_level") is not None:
            return Invalidation(name="reentered_channel", fired=False,
                                detail="the trail has engaged and is now the exit")
        low = Decimal(repr(float(state.context["channel_low"])))
        high = Decimal(repr(float(state.context["channel_high"])))
        inside = low <= price <= high
        return Invalidation(
            name="reentered_channel", fired=inside,
            detail=(f"price {price} returned inside the channel [{low}, {high}]: the breakout was "
                    "false, which is this strategy's most common outcome and is expected"
                    if inside else f"price {price} holds outside the channel"),
            value=str(price), threshold=f"[{low}, {high}]")


# ---- C4-B --------------------------------------------------------------------------------

@dataclass
class C4B:
    """C4-B — Dual-timeframe momentum with volatility targeting (specs/06 §4.2). Tier A.

    Time-series momentum as the **sign of a trailing return**, with a longer-horizon filter and
    exposure scaled inversely to realised volatility: the canonical CTA formulation. The
    volatility-management component draws on the finding associated with Moreira and Muir that
    scaling exposure inversely to realised volatility improves a factor strategy's risk-adjusted
    profile; the long-term trend filter as a tactical overlay is associated with Faber's work.

    **Why both C4-A and C4-B are here.** C4-A is *event-driven* — it waits for a breakout. C4-B is
    *state-driven* — it holds a position whenever the signal is on. Same premise, materially
    different trade profile and different failure timing, which is what isolates whether an observed
    edge comes from the trend premise itself or from the breakout entry mechanic specifically.

    Return *sign* rather than a moving-average crossover, because a crossover needs two lookbacks to
    express the same information. The signal runs on the 1h medium lookback with 1d as the filter so
    holding periods land in days and match the stated horizon — a pure daily-signal version would
    hold for weeks and exceed it.

    **ACT decides the fill, the signal decides the direction.** A state-driven strategy flips when the
    sign flips, and entering at that instant would mean entering into whatever spike caused the flip.
    This is ACT's fourth consumer and still the one implementation.

    Exposure scaling is *declared*, not applied here: ``portfolio_vol_target`` belongs to the risk
    engine (specs/08), and a strategy that pre-scaled its own size would be sized twice.
    """

    params: dict[str, Any] = field(default_factory=dict)
    mode: str = "suggestion"
    id: str = "C4-B"
    version: str = "1.0.0"
    strategy_class: int = 4
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("TRD_RET", "VOL_CC", "ATR")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="momentum_signal_on",
                      description="the 1h medium-horizon trailing return has a definite sign",
                      body=self._signal_on, required_features=("TRD_RET",)),
            Condition(name="daily_agrees",
                      description="the daily short-horizon return agrees — the tactical overlay, and "
                                  "the thing that keeps this out of counter-trend noise",
                      body=self._daily_agrees, required_features=("TRD_RET",)),
            Condition(name="volatility_measurable",
                      description="realised volatility is available, since exposure is scaled "
                                  "inversely to it and an unscaled position is not this strategy",
                      body=self._vol_measurable, required_features=("VOL_CC",)),
        )

    def _signal_on(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        ret = ctx.feature("TRD_RET", Timeframe.H1, "medium")
        ctx.scratch["ret_1h"] = ret
        if ret == 0:
            # Exactly zero is the signal boundary, which specs/06 §4.2 names as the whipsaw case.
            # Declining at the boundary is the conservative reading: there is no sign to take.
            return False, ret, "a non-zero sign"
        ctx.scratch["signal_direction"] = Direction.LONG if ret > 0 else Direction.SHORT
        return True, f"{ret:+.5f}", "a non-zero sign"

    def _daily_agrees(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        daily = ctx.feature("TRD_RET", Timeframe.D1, "short")
        ctx.scratch["ret_1d"] = daily
        direction = ctx.scratch.get("signal_direction")
        if direction is None:
            raise StrategyError(
                "the 1h signal has no sign, so there is nothing for the daily filter to agree with",
                component=COMPONENT, operation="daily_agrees",
                context={"ret_1d": daily},
                likely_cause="condition 1 found a zero or unavailable 1h return",
                remediation="none; this is the normal state at the signal boundary",
            )
        agrees = daily > 0 if direction is Direction.LONG else daily < 0
        # The *degree* of agreement is one of the two strength inputs (specs/06 §11): both horizons
        # pointing strongly the same way is a different state from one barely agreeing.
        ctx.scratch["agreement"] = abs(daily) if agrees else 0.0
        return agrees, f"1d {daily:+.5f} vs 1h {direction.value}", "same sign"

    def _vol_measurable(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        vol = ctx.feature("VOL_CC", Timeframe.H1, "medium")
        ctx.scratch["vol_cc"] = vol
        return vol > 0, vol, "> 0"

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
        direction = ctx.scratch["signal_direction"]
        armed_at = snapshot.as_of_ms
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"ret_1h_at_arming": ctx.scratch["ret_1h"],
                     "vol_cc_at_arming": ctx.scratch["vol_cc"]})
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product, direction,
                                          armed_at, self.params)
        state.x_star, state.e_t = act_state.x_star, act_state.e_t
        levels = self._levels(features, state, act_state)
        vol = ctx.scratch["vol_cc"]
        target_vol = self.params.get("portfolio_vol_target")
        rationale = (
            f"the 1h medium-horizon return is {ctx.scratch['ret_1h']:+.2%} and the daily return "
            f"agrees at {ctx.scratch['ret_1d']:+.2%}, so the momentum state is "
            f"{direction.value.lower()}. This is state-driven, not event-driven: the position is held "
            f"while the signal is on and exits on a sign flip or the trail. ACT sets the fill so a "
            f"signal flip does not buy the spike that caused it."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={**_mode_detail(self), "strategy_class": 4,
                                      "act_unit_bars": unit_bars,
                                      "strength_inputs": {"abs_return_1h": abs(ctx.scratch["ret_1h"]),
                                                          "agreement_1d": ctx.scratch["agreement"]},
                                      "realised_vol_1h": vol,
                                      # Declared for the risk engine; deliberately not applied here.
                                      "vol_scaling": {
                                          "basis": "exposure scales inversely to realised VOL_CC "
                                                   "toward the portfolio volatility target",
                                          "realised_vol": vol,
                                          "target_vol": target_vol,
                                          "applied_by": "risk.engine (specs/08); a strategy that "
                                                        "pre-scaled its own size would be sized twice",
                                      },
                                      "signal_type": "state-driven",
                                      "crossover_note": "return sign rather than a moving-average "
                                                        "crossover: a crossover needs two lookbacks "
                                                        "to express the same information"})
        return setup(signal, results, state, reason=f"momentum state {direction.value}")

    def _levels(self, features: FeatureSet, state: SetupState, act_state: ActState) -> Levels:
        """Entry at the ACT trigger, stop ``s * ATR_1h``, **no target** — exit is flip or trail."""
        s = Decimal(str(self.params["stop_buffer_atr_multiple"]))
        atr = atr_1h(features)
        stop = (act_state.x_star - s * atr if state.direction is Direction.LONG
                else act_state.x_star + s * atr)
        trail = state.context.get("trail_level")
        return Levels(entry=act_state.e_t, stop=stop, targets=(),
                      detail={"stop_basis": "s x ATR_1h beyond X*, the running adverse extreme ACT "
                                            "measured — so R is set by how far the market actually "
                                            "went, not by a chosen number",
                              "exit_rule": "signal flip or the s x ATR_1h trail, whichever comes first",
                              "trail_level": str(trail) if trail is not None else None})

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        update_trail(state, atr=atr_1h(features),
                     s=Decimal(str(self.params["stop_buffer_atr_multiple"])), price=price)
        levels = self._levels(features, state, act_state)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._signal_flipped(features, state),),
                           stop=effective_stop(state, levels.stop), targets=())
        update_trail(state, atr=atr_1h(features),
                     s=Decimal(str(self.params["stop_buffer_atr_multiple"])), price=price)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=self._levels(features, state, act_state), conditions=results,
                              rationale=progress.reason, state=state, act_state=act_state,
                              detail={**_mode_detail(self), "strategy_class": 4,
                                      "act_unit_bars": unit_bars, "progress": progress.to_dict(),
                                      "trail_level": state.context.get("trail_level")})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _signal_flipped(self, features: FeatureSet, state: SetupState) -> Invalidation:
        """The state-driven exit: the sign that put the position on has gone away."""
        fv = features.get("TRD_RET", Timeframe.H1, "medium")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="signal_flipped", fired=False,
                                detail=f"TRD_RET(1h, medium) is {fv.status.value}: {fv.reason}")
        long = state.direction is Direction.LONG
        fired = fv.value <= 0 if long else fv.value >= 0
        return Invalidation(
            name="signal_flipped", fired=fired,
            detail=(f"the 1h momentum signal is now {fv.value:+.5f}, no longer "
                    f"{state.direction.value}: a state-driven strategy exits when the state ends"
                    if fired else f"the signal holds at {fv.value:+.5f}"),
            value=round(fv.value, 6), threshold=0.0)


# ---- C4-C --------------------------------------------------------------------------------

@dataclass
class C4C:
    """C4-C — Short-horizon reversal (specs/06 §4.3). Tier A, **structurally short-volatility**.

    Short-horizon return reversal is long documented in equities — the Jegadeesh and Lehmann work on
    weekly and monthly reversal — with the standard explanation being compensation earned by liquidity
    providers who absorb transient order-flow imbalance, plus overreaction to non-informational flow.
    Crypto-specific study exists but is thinner and shorter.

    The mechanism is **not** "price went down so it goes up". It is that a move driven by liquidity
    *demand* rather than information temporarily displaces price, and whoever takes the other side is
    paid for the inventory risk. Where the move *is* informational, no reversal should be expected —
    which is the entire basis of the single filter applied.

    ⚠ **Short-gamma, and that changes how it must be judged.** Many small wins, occasional large
    loss. specs/06 §4.3 requires this enforced in evaluation: the hit rate **will look excellent and
    will be misleading** — a strategy can win 70% of the time and lose money — and the Sharpe ratio
    flatters short-gamma profiles by under-penalising rare large losses. Maximum drawdown, worst
    single trade and the loss distribution's tail are required output; hit rate and Sharpe alone are
    **rejected as sufficient**. :data:`EVALUATION_REQUIREMENTS` carries that onto every signal so the
    backtest cannot quietly report the flattering pair.

    It also gives the system real diversification in **payoff shape** — short-gamma where Class 3 is
    long-gamma — which is more meaningful than four strategies with different indicators and the same
    payoff profile.

    **Deliberately unconditioned, and that is the point.** The only filter is a volume-tail exclusion.
    **No regime gate.** C1-B is the conditioned version of this same observation, gated on
    ``MR_VR < 1``; adding that gate here would collapse C4-C into C1-B and destroy the comparison that
    answers whether the variance-ratio gate adds anything at all.

    The one departure from the textbook — which is "buy the biggest recent loser" with no stop,
    exactly how short-gamma strategies produce a catastrophic loss — is a hard volatility-scaled stop
    plus a fixed target at the short-lookback mean. specs/06 §4.3 closes with the instruction that
    matters most: **if the backtest tail analysis is poor, cut it — do not widen the stop.**
    """

    params: dict[str, Any] = field(default_factory=dict)
    mode: str = "suggestion"
    id: str = "C4-C"
    version: str = "1.0.0"
    strategy_class: int = 4
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("DEV_Z", "VLM_ZROBUST", "ATR")

    #: Published on every C4-C signal. specs/06 §4.3 and acceptance criterion 6 reject hit rate and
    #: Sharpe as sufficient output for a short-gamma strategy, so the requirement travels with the
    #: signal rather than living only in a document the backtest author may not read.
    EVALUATION_REQUIREMENTS: tuple[str, ...] = (
        "max_drawdown", "worst_single_trade", "loss_distribution_tail")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="displaced",
                      description="price is displaced from its short-horizon mean by more than the "
                                  "deviation threshold",
                      body=self._displaced, required_features=("DEV_Z",)),
            Condition(name="not_volume_tail",
                      description="volume is outside the top decile — the only filter, a crude proxy "
                                  "for 'this move was information, not liquidity demand'",
                      body=self._not_volume_tail, required_features=("VLM_ZROBUST",)),
        )

    def _displaced(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        z = ctx.feature("DEV_Z", Timeframe.H1, "short")
        limit = float(ctx.param("deviation_z_threshold"))
        ctx.scratch["dev_z"] = z
        if abs(z) >= limit:
            # Reversal: the position opposes the displacement.
            ctx.scratch["signal_direction"] = Direction.LONG if z < 0 else Direction.SHORT
        return abs(z) >= limit, z, f"|z| >= {limit}"

    def _not_volume_tail(self, ctx: ConditionContext) -> tuple[bool, Any, Any]:
        """Top-decile volume excluded. A *large move on low volume* is the cleaner liquidity case.

        ``VLM_ZROBUST`` is a robust z-score, so the decile is expressed as its normal-quantile
        equivalent rather than as a fitted percentile — there is no stored volume distribution to take
        a percentile from inside a single snapshot, and inventing one would be a second definition of
        the same cut.
        """
        z = ctx.feature("VLM_ZROBUST", Timeframe.H1, "short")
        # Phi^-1(0.90) = 1.2816. Structural, from the stated decile — not a tunable threshold.
        limit = 1.2816
        ctx.scratch["volume_z"] = z
        return z < limit, z, f"< {limit} (the {VOLUME_TAIL_DECILE:.0%} quantile of a normal)"

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))
        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, results)
        if not all(r.passed for r in results):
            failed = [r.name for r in results if not r.passed]
            return no_setup(results, reason=f"{len(failed)} condition(s) not met: {', '.join(failed)}")
        return self._arm(snapshot, features, regime, ctx, results)

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        direction = ctx.scratch["signal_direction"]
        armed_at = snapshot.as_of_ms
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"dev_z": ctx.scratch["dev_z"], "volume_z": ctx.scratch["volume_z"]})
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product, direction,
                                          armed_at, self.params)
        state.x_star, state.e_t = act_state.x_star, act_state.e_t
        levels = self._levels(features, state, act_state)
        rationale = (
            f"price is displaced {ctx.scratch['dev_z']:+.2f} sigma from its short-horizon mean on "
            f"volume outside the top decile (z = {ctx.scratch['volume_z']:+.2f}) — a large move on "
            f"ordinary volume is the cleaner liquidity-demand case, where the provider taking the "
            f"other side is paid for inventory risk. **No regime gate is applied, by design**: C1-B "
            f"is the conditioned version of this idea, and gating C4-C would collapse the comparison "
            f"that tests whether the conditioning adds anything."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={**_mode_detail(self), "strategy_class": 4,
                                      "act_unit_bars": unit_bars,
                                      "strength_inputs": {
                                          "displacement": abs(ctx.scratch["dev_z"]),
                                          # Inverse volume rank: lower volume is the cleaner case.
                                          "inverse_volume_rank": -ctx.scratch["volume_z"]},
                                      "payoff_shape": "short_gamma",
                                      "payoff_warning": "many small wins, occasional large loss. The "
                                                        "hit rate will look excellent and will be "
                                                        "misleading -- a strategy can win 70% of the "
                                                        "time and lose money -- and Sharpe flatters "
                                                        "short-gamma profiles by under-penalising "
                                                        "rare large losses",
                                      "evaluation_requires": list(self.EVALUATION_REQUIREMENTS),
                                      "hit_rate_and_sharpe": "rejected as sufficient output for this "
                                                             "strategy (specs/06 §13 criterion 6)",
                                      "unconditioned_by_design": "no regime gate; C1-B is the "
                                                                 "conditioned version and the "
                                                                 "comparison is the point",
                                      "if_tail_is_poor": "cut the strategy -- do not widen the stop"})
        return setup(signal, results, state, reason=f"reversal armed {direction.value}")

    def _levels(self, features: FeatureSet, state: SetupState, act_state: ActState) -> Levels:
        """Hard stop at ``s * ATR_1h``; target the short-lookback mean. The stop is the whole point.

        The textbook version has no stop, which is exactly how a short-gamma strategy produces a
        catastrophic loss. The target is the mean the displacement was measured against, so entry and
        exit are defined by one quantity rather than two.
        """
        s = Decimal(str(self.params["stop_buffer_atr_multiple"]))
        atr = atr_1h(features)
        long = state.direction is Direction.LONG
        stop = act_state.x_star - s * atr if long else act_state.x_star + s * atr
        sma = detail_of(features, "DEV_Z", Timeframe.H1, "short").get("sma")
        targets: tuple[Decimal, ...] = ()
        basis = ("the short-lookback mean is unavailable, so no target is published rather than a "
                 "substituted one")
        if sma is not None:
            targets = (Decimal(repr(float(sma))),)
            basis = "the short-lookback mean price displacement was measured against"
        return Levels(entry=act_state.e_t, stop=stop, targets=targets,
                      detail={"stop_basis": "a hard s x ATR_1h stop beyond X*. The textbook version "
                                            "has no stop, which is how short-gamma strategies produce "
                                            "a catastrophic loss; this single addition is the "
                                            "difference between a tested strategy and an untested one",
                              "t1_basis": basis})

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        levels = self._levels(features, state, act_state)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._mean_reached(features, state),),
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={**_mode_detail(self), "strategy_class": 4,
                                      "act_unit_bars": unit_bars, "progress": progress.to_dict(),
                                      "payoff_shape": "short_gamma",
                                      "evaluation_requires": list(self.EVALUATION_REQUIREMENTS)})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _mean_reached(self, features: FeatureSet, state: SetupState) -> Invalidation:
        """The displacement closed before the trigger fired: the trade the signal described is gone.

        An invalidation rather than a target, because this fires *pre-entry*. Treating a closed
        displacement as a win would book a profit on a position that was never entered — which is the
        single easiest way to make a mean-reversion backtest look excellent and be wrong.
        """
        if state.lifecycle is Lifecycle.TRIGGERED:
            return Invalidation(name="mean_reached", fired=False,
                                detail="entered; the target now handles this")
        fv = features.get("DEV_Z", Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="mean_reached", fired=False,
                                detail=f"DEV_Z is {fv.status.value}: {fv.reason}")
        armed_z = float(state.context.get("dev_z", 0.0))
        # Sign flip or collapse toward zero: either way the displacement that justified the setup is
        # no longer there.
        fired = (fv.value >= 0 if armed_z < 0 else fv.value <= 0) or abs(fv.value) < 0.5
        return Invalidation(
            name="mean_reached", fired=fired,
            detail=(f"the displacement closed from {armed_z:+.2f} to {fv.value:+.2f} before the "
                    "trigger fired: the trade the signal described no longer exists, and booking it "
                    "as a win would be a profit on a position never entered"
                    if fired else f"the displacement holds at {fv.value:+.2f}"),
            value=round(fv.value, 4), threshold=round(armed_z, 4))
