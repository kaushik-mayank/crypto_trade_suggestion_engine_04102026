"""The two control strategies — firewalled, backtest-only (specs/06 §5, specs/13 §13).

**C4-X1, random entry.** Separates edge that comes from the *signal* from edge that comes from the
*risk management*. A great many published strategies are competent exit logic attached to a noise
entry, and the only way to find out is to run the exit logic with a noise entry. The governing rule
applies to every strategy in the system:

> **If a strategy cannot beat C4-X1 with statistical significance, its signal contributes nothing and
> the strategy is cut.**

**C4-X2, weak evidence.** A textbook unconditioned Bollinger-band-touch reversal, chosen precisely
because external evidence for the unconditioned version is weak to absent. It tests the *harness*, not
the market:

> **If C4-X2 backtests well, the backtest is suspect — not the strategy.**

A strong result there is a signal to audit for look-ahead bias, cost under-modelling, or a fill-logic
bug before accepting any other result from the same harness.

Both carry ``tradeable = False``, both refuse to run outside backtest mode, and the API filters on the
flag — so a control signal cannot reach a response even if something upstream forgets. Three layers,
because a control signal reaching a user would be indistinguishable from a real suggestion.

Seeds are recorded for reproducibility and are **not parameters** (specs/13 §13): they do not tune
anything, they identify a draw.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.errors import StrategyError
from features.value import FeatureSet, FeatureStatus
from normalize.canonical import Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot
from strategy.act import Direction
from strategy.base import Lifecycle, SetupState, StrategyOutcome, no_setup, setup, terminal, update
from strategy.conditions import Condition, ConditionContext, ConditionResult, evaluate_all
from strategy.evaluation import Levels, atr_1h, build_signal, detail_of, price_now, signal_id
from strategy.lifecycle import advance

COMPONENT = "strategy.controls"

#: Bands for C4-X2, the textbook unconditioned rule: 2 standard deviations, no regime filter.
BOLLINGER_SIGMA = Decimal(2)


def assert_backtest_only(strategy_id: str, mode: str) -> None:
    """Controls run **only** in backtest. Called on every evaluation, not just at construction.

    A control evaluated live would produce a signal identical in shape to a real one; the only thing
    separating them is this check and the ``tradeable`` flag, so the check is not a formality.
    """
    if mode != "backtest":
        raise StrategyError(
            f"{strategy_id} is a control strategy and may only run in backtest mode (mode={mode!r})",
            component=COMPONENT, operation="assert_backtest_only",
            context={"strategy_id": strategy_id, "mode": mode},
            likely_cause="the live runner was given the control strategies",
            remediation="controls exist to test the harness and the risk logic, not to suggest trades; "
                        "exclude them from the live registry (specs/13 §13)",
        )


def _seeded_direction(seed: int, strategy_id: str, asset: str, as_of_ms: int) -> Direction:
    """A reproducible coin flip.

    Derived from ``(seed, strategy, asset, as_of)`` rather than drawn from a stateful generator, so the
    same backtest step always produces the same draw however the run is sliced — which is what makes a
    control comparison repeatable across many seeds.
    """
    digest = hashlib.sha256(f"{seed}|{strategy_id}|{asset}|{as_of_ms}".encode()).digest()
    return Direction.LONG if digest[0] % 2 == 0 else Direction.SHORT


@dataclass
class C4X1:
    """Random-entry control. Same instrument, sizing, stop and target logic as a real strategy; the
    entry *direction* is a seeded coin flip and the timing follows the same opportunity distribution."""

    params: dict[str, Any] = field(default_factory=dict)
    seed: int = 0
    mode: str = "backtest"
    id: str = "C4-X1"
    version: str = "1.0.0"
    strategy_class: int = 4
    tier: str = "A"
    tradeable: bool = False
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("ATR", "DEV_Z")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        """One condition: the inputs the *exit* logic needs are available.

        Deliberately no market conditions at all — the entry is noise by construction, and adding a
        filter would make it a strategy rather than a control.
        """
        def inputs_available(ctx: ConditionContext) -> tuple[bool, Any, Any]:
            atr = ctx.features.get("ATR", Timeframe.H1, "short")
            ok = atr.status is FeatureStatus.OK
            return ok, atr.status.value, "OK"

        return (Condition(name="exit_inputs_available",
                          description="the inputs the exit logic needs are available; the entry itself "
                                      "is deliberately unconditioned",
                          body=inputs_available, required_features=("ATR",)),)

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        assert_backtest_only(self.id, self.mode)
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))

        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, results)
        if not all(r.passed for r in results):
            return no_setup(results, reason="the exit logic's inputs are unavailable")

        direction = _seeded_direction(self.seed, self.id, features.asset, snapshot.as_of_ms)
        price = price_now(features, snapshot)
        armed_at = snapshot.as_of_ms
        new_state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + 24 * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            lifecycle=Lifecycle.SETUP_DETECTED, context={"seed": self.seed})
        levels = self._levels(features, price, direction)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, state=new_state,
                              rationale=f"random-entry control, seed {self.seed}: direction drawn "
                                        f"{direction.value} with no market condition attached",
                              detail={"control": True, "seed": self.seed, "tradeable": False,
                                      "purpose": "separates signal edge from risk-management edge "
                                                 "(specs/06 §5.1)"})
        return setup(signal, results, new_state, reason=f"control armed {direction.value}")

    def _levels(self, features: FeatureSet, price: Decimal, direction: Direction) -> Levels:
        """Identical stop/target construction to a real strategy: that is the point of the control."""
        s = Decimal(str(self.params["stop_buffer_atr_multiple"]))
        atr = atr_1h(features)
        if direction is Direction.LONG:
            return Levels(entry=price, stop=price - s * atr, targets=(price + 2 * s * atr,))
        return Levels(entry=price, stop=price + s * atr, targets=(price - 2 * s * atr,))

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        from strategy.act import ActState

        price = price_now(features, snapshot)
        levels = self._levels(features, price, state.direction)
        # The control enters at the bar it arms: there is no ACT ratchet, because a noise entry has no
        # arrest to confirm. A synthetic state keeps the lifecycle machinery identical.
        act_state = ActState(direction=state.direction, x_star=price, e_t=price, current=price,
                             unit=atr_1h(features), delta=Decimal(1), bars_since_armed=1,
                             triggered=True)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, state=state,
                              rationale=progress.reason, act_state=act_state,
                              detail={"control": True, "seed": state.context.get("seed")})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)


@dataclass
class C4X2:
    """Weak-evidence control: a textbook unconditioned Bollinger-band-touch reversal.

    No regime filter, no trend filter — the naive rule, exactly as the literature states it and exactly
    as Bollinger's own writing warns against applying it. It exists to test the harness.
    """

    params: dict[str, Any] = field(default_factory=dict)
    mode: str = "backtest"
    id: str = "C4-X2"
    version: str = "1.0.0"
    strategy_class: int = 4
    tier: str = "A"
    tradeable: bool = False
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("DEV_Z", "ATR")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        def band_touched(ctx: ConditionContext) -> tuple[bool, Any, Any]:
            z = ctx.feature("DEV_Z", Timeframe.H1, "medium")
            return abs(z) >= float(BOLLINGER_SIGMA), z, f"|z| >= {BOLLINGER_SIGMA}"

        return (Condition(name="band_touched",
                          description="price touched a 2-sigma band; no regime or trend filter is "
                                      "applied, which is the point of this control",
                          body=band_touched, required_features=("DEV_Z",)),)

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        assert_backtest_only(self.id, self.mode)
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))

        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, results)
        if not all(r.passed for r in results):
            return no_setup(results, reason="no band touch")

        z = features.value("DEV_Z", Timeframe.H1, "medium")
        direction = Direction.LONG if z < 0 else Direction.SHORT
        price = price_now(features, snapshot)
        armed_at = snapshot.as_of_ms
        new_state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + 24 * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version)
        levels = self._levels(features, price, direction)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, state=new_state,
                              rationale=f"unconditioned band touch at z={z:+.2f}; no regime filter "
                                        f"applied by design",
                              detail={"control": True, "tradeable": False,
                                      "purpose": "if this backtests well, the backtest is suspect "
                                                 "(specs/06 §5.2)"})
        return setup(signal, results, new_state, reason=f"control armed {direction.value}")

    def _levels(self, features: FeatureSet, price: Decimal, direction: Direction) -> Levels:
        """Target the mean, stop at an ATR buffer — the textbook construction."""
        s = Decimal(str(self.params["stop_buffer_atr_multiple"]))
        atr = atr_1h(features)
        sma_detail = detail_of(features, "DEV_Z", Timeframe.H1, "medium")
        sma = Decimal(repr(sma_detail["sma"])) if "sma" in sma_detail else None
        targets = (sma,) if sma is not None else ()
        stop = price - s * atr if direction is Direction.LONG else price + s * atr
        return Levels(entry=price, stop=stop, targets=targets)

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        from strategy.act import ActState

        price = price_now(features, snapshot)
        levels = self._levels(features, price, state.direction)
        act_state = ActState(direction=state.direction, x_star=price, e_t=price, current=price,
                             unit=atr_1h(features), delta=Decimal(1), bars_since_armed=1,
                             triggered=True)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, state=state,
                              rationale=progress.reason, act_state=act_state,
                              detail={"control": True})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)
