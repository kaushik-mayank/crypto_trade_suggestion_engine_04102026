"""The strategy registry — the one list of what runs, and the one place that enforces the firewall.

Fourteen strategies: twelve tradeable across four classes, plus the two controls of specs/06 §5.

There is a single reason this module exists rather than each caller assembling its own list: the live
runner and the backtest harness must run the *same* strategies, or every comparison between them is
meaningless. A hardcoded list in two places is a divergence waiting to happen, and it would show up as
a backtest result that cannot be reproduced live — the failure this project treats as worst, because
nothing about it looks like an error.

:func:`live_strategies` never returns a control. :func:`backtest_strategies` returns the tradeable set
plus controls across as many seeds as asked for, because specs/06 §5.1 requires C4-X1 to produce a
performance *distribution* rather than a single path — one random path proves nothing.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.errors import StrategyError
from strategy.c1_correction import C1A, C1B
from strategy.c1c_relative import C1C
from strategy.c2_participation import C2A, C2B, C2C
from strategy.c3_transition import C3A, C3B, C3C
from strategy.c4_established import C4A, C4B, C4C
from strategy.controls import C4X1, C4X2

COMPONENT = "strategy.registry"

#: Declaration order. Fixed and meaningful: the arbitration pipeline's seniority is by class
#: (specs/07 §2), and keeping the registry in class order means a reader comparing an evaluation's
#: output against the registry sees them in the same sequence.
TRADEABLE_IDS = ("C1-A", "C1-B", "C1-C",
                 "C2-A", "C2-B", "C2-C",
                 "C3-A", "C3-B", "C3-C",
                 "C4-A", "C4-B", "C4-C")
CONTROL_IDS = ("C4-X1", "C4-X2")


def live_strategies(params: dict[str, Any]) -> list[Any]:
    """The twelve tradeable strategies, in class order. **Never a control.**

    The controls' own ``assert_backtest_only`` is the second layer and the API's ``tradeable`` filter
    the third; this omission is the first. Three layers, because a control signal reaching a user
    would be indistinguishable from a real suggestion.
    """
    strategies: list[Any] = [
        C1A(params=params), C1B(params=params), C1C(params=params),
        C2A(params=params), C2B(params=params), C2C(params=params),
        C3A(params=params), C3B(params=params), C3C(params=params),
        C4A(params=params), C4B(params=params), C4C(params=params),
    ]
    _assert_registry(strategies, expected=TRADEABLE_IDS, tradeable=True)
    return strategies


def control_strategies(params: dict[str, Any], *, seeds: Sequence[int] = (0,)) -> list[Any]:
    """The control suite, one C4-X1 instance per seed plus one C4-X2.

    ``mode="backtest"`` is set explicitly rather than relied on as a default: the default could change,
    and the thing it guards is whether a noise-entry signal can reach a person.
    """
    if not seeds:
        raise StrategyError(
            "the control suite needs at least one seed",
            component=COMPONENT, operation="control_strategies",
            context={"seeds": list(seeds)},
            likely_cause="an empty seed list was passed",
            remediation="specs/06 §5.1 requires many seeds to produce a distribution; a single path "
                        "proves nothing, and zero paths prove less",
        )
    out: list[Any] = [C4X1(params=params, seed=s, mode="backtest") for s in seeds]
    out.append(C4X2(params=params, mode="backtest"))
    for c in out:
        if c.tradeable:
            raise StrategyError(
                f"control {c.id} reports tradeable=True",
                component=COMPONENT, operation="control_strategies",
                context={"strategy_id": c.id},
                likely_cause="the tradeable flag was changed on a control class",
                remediation="controls are never tradeable and never reach the UI (specs/06 §5)",
            )
    return out


def backtest_strategies(params: dict[str, Any], *, seeds: Sequence[int] = (0,)) -> list[Any]:
    """Everything the harness runs: the tradeable set plus the control suite."""
    return live_strategies(params) + control_strategies(params, seeds=seeds)


def benchmark_mode(params: dict[str, Any]) -> list[Any]:
    """The Class 4 members with ``mode="benchmark"`` (specs/06 §3).

    Benchmark mode is **always on and never suppressed**: suppressing a Class 4 signal while a bespoke
    one is active would censor exactly the periods the comparison is about. Returned as separate
    instances rather than by mutating the live ones, so a benchmark evaluation can never change what
    the suggestion stream does.
    """
    return [C4A(params=params, mode="benchmark"),
            C4B(params=params, mode="benchmark"),
            C4C(params=params, mode="benchmark")]


def _assert_registry(strategies: Sequence[Any], *, expected: Sequence[str],
                     tradeable: bool) -> None:
    """Fail at construction, not at the first evaluation, if the registry is inconsistent.

    Three things that would otherwise surface as a quietly missing strategy: a duplicate id (the later
    one would silently shadow the earlier in any ``{id: ...}`` map), a set that does not match the
    declared ids, and a tradeable flag that disagrees with which list a strategy is in.
    """
    ids = [s.id for s in strategies]
    problems: list[str] = []
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        problems.append(f"duplicate strategy ids: {duplicates}")
    if sorted(ids) != sorted(expected):
        missing = sorted(set(expected) - set(ids))
        extra = sorted(set(ids) - set(expected))
        problems.append(f"registry does not match the declared ids (missing {missing}, "
                        f"unexpected {extra})")
    wrong = sorted(s.id for s in strategies if bool(s.tradeable) is not tradeable)
    if wrong:
        problems.append(f"tradeable flag should be {tradeable} but is not for: {wrong}")
    if problems:
        raise StrategyError(
            "; ".join(problems),
            component=COMPONENT, operation="assert_registry",
            context={"ids": ids, "expected": list(expected), "problems": problems},
            likely_cause="a strategy was added, renamed or removed without updating the declared ids",
            remediation="update TRADEABLE_IDS/CONTROL_IDS and the constructor list together; they are "
                        "two halves of one statement about what runs",
        )
