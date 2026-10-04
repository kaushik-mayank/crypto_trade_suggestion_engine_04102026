"""The strategy layer: ACT, conditions, lifecycle and the 14 strategies (specs/13)."""
from strategy.act import ActState, Direction, act
from strategy.base import (
    Lifecycle,
    RawSignal,
    SetupState,
    Strategy,
    StrategyOutcome,
    no_setup,
    setup,
    terminal,
    update,
)
from strategy.conditions import (
    Condition,
    ConditionContext,
    ConditionResult,
    evaluate_all,
    summarise,
)

__all__ = ["ActState", "Condition", "ConditionContext", "ConditionResult", "Direction",
           "Lifecycle", "RawSignal", "SetupState", "Strategy", "StrategyOutcome", "act",
           "evaluate_all", "no_setup", "setup", "summarise", "terminal", "update"]
