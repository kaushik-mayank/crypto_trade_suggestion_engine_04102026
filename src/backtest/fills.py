"""The fill model (specs/09 §3) — where naive backtests lie.

> "The single largest source of inflated backtest results is optimistic fills."

Four rules, each conservative, each stated as a rule rather than a judgement made per bar:

1. **Entry fills at the worse of {trigger price, next 1m bar open}, plus slippage.** Never at the
   trigger itself. The signal confirms on a 1m *close* (specs/03 §2), so the earliest moment anyone
   could act is the next bar — filling at the trigger assumes a time machine.

2. **If a bar's range gaps beyond the stop, the fill is the bar's OPEN, not the stop price.** specs/09
   §3.2 calls this the most important rule in the section. Naive backtests assume stops fill at the
   stop level; in crypto they frequently do not, and the shortfall concentrates in exactly the tail
   events that decide whether a strategy survives. The spec's own example is worked in the tests: a
   long stopped at 100 whose next bar opens at 96 and trades to 94 fills at **96**, a loss of 1.33R on
   a trade a naive model records as −1.00R.

3. **Targets fill at the target price when the bar's range contains it.** A target is a limit order,
   so it does not suffer the gap problem in the adverse direction — the favourable gap is simply not
   credited, which is the conservative side.

4. **When one bar contains both the stop and the target, the stop is assumed first. Always.** Not
   resolved by dropping to finer data, because doing that on some bars and not others introduces a
   bias whose direction is impossible to reason about. The rule being unconditional is the point.

**Costs come from the live module**, never re-implemented here: specs/08 acceptance criterion 10
requires the backtest and the live system to charge the same costs, and two implementations of a fee
calculation is how a backtest ends up measuring a different strategy than the one that runs.

Slippage is applied as a fraction of price, taken from the 75th percentile of the P14 measurements
(specs/09 §3.4). Every headline result is re-run at **2× slippage** (:data:`SENSITIVITY_MULTIPLIER`):
a strategy whose edge disappears when the slippage estimate doubles is not robust enough to deploy,
because the estimate itself carries error.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from core.errors import StrategyError
from normalize.canonical import Bar, InstrumentMeta
from strategy.act import Direction

COMPONENT = "backtest.fills"

#: specs/09 §3.4 requires every headline result re-run at double the slippage assumption.
SENSITIVITY_MULTIPLIER = Decimal(2)


class FillKind(str, Enum):
    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    TRAIL = "trail"
    #: The window closed with the position still open: marked out at the last close, flagged so it is
    #: never counted as a realised exit.
    MARK_TO_MARKET = "mark_to_market"


@dataclass(frozen=True)
class Fill:
    """One fill, with the evidence for the price.

    ``gapped`` and ``basis`` exist because the *reason* for a price is what makes a backtest
    auditable. "stopped at 96" is unremarkable; "stopped at 96 because the bar gapped through a stop
    at 100" is the finding that re-derives ``liq_safety`` from measurement instead of assumption.
    """

    kind: FillKind
    price: Decimal
    at_ms: int
    basis: str
    slippage_price: Decimal = Decimal(0)
    gapped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "price": str(self.price), "at_ms": self.at_ms,
                "basis": self.basis, "slippage_price": str(self.slippage_price),
                "gapped": self.gapped}


@dataclass(frozen=True)
class CostModel:
    """Costs charged in the backtest, sourced from the live instrument metadata.

    ``taker_fee`` is read from :class:`normalize.canonical.InstrumentMeta`, which the ingest process
    refreshes from ``/derivatives/futures/data/instrument`` — hard invariant 1 forbids hardcoding it,
    and the documented sample values are wrong for BTC (doc says taker 0.075, live BTC is 0.059).
    """

    #: Fraction of price lost to slippage on a market fill. Entries and stops are market orders;
    #: targets are limits and pay none.
    slippage_fraction: Decimal
    #: Multiplier applied to slippage for the sensitivity re-run. 1 for the headline, 2 for the check.
    slippage_multiplier: Decimal = Decimal(1)

    @property
    def effective_slippage(self) -> Decimal:
        return self.slippage_fraction * self.slippage_multiplier

    def slippage_on(self, price: Decimal, direction: Direction, *, entering: bool) -> Decimal:
        """Signed slippage: always against the position, entering and exiting alike.

        Entering long pays *up*; exiting long (a stop) sells *down*. Both are adverse, which is the
        only direction slippage is ever applied — a model that let slippage help would flatter every
        result by a number nobody could audit.
        """
        magnitude = price * self.effective_slippage
        if entering:
            return magnitude if direction is Direction.LONG else -magnitude
        return -magnitude if direction is Direction.LONG else magnitude

    def round_trip_fee_price(self, price: Decimal, instrument: InstrumentMeta) -> Decimal:
        """Round-trip taker fee expressed in price units — the same ``cost_price`` the risk engine uses.

        Deliberately identical to ``risk.engine.RiskEngine._k_net``'s formula rather than imported,
        because that method is private and takes a signal; the formula is one line and the test asserts
        the two agree. If they ever diverge, the backtest is grading a different strategy.
        """
        return 2 * instrument.taker_fee.fraction * price

    def to_dict(self) -> dict[str, Any]:
        return {"slippage_fraction": str(self.slippage_fraction),
                "slippage_multiplier": str(self.slippage_multiplier),
                "effective_slippage": str(self.effective_slippage)}


def entry_fill(*, trigger_price: Decimal, next_bar: Bar | None, direction: Direction,
               costs: CostModel) -> Fill:
    """specs/09 §3.1: the **worse** of the trigger and the next bar's open, plus slippage.

    ``next_bar`` is ``None`` only when the window ends on the trigger bar. That is not a fill: the
    caller must treat it as a trade that never opened, because inventing one would manufacture a
    trade from the absence of data.
    """
    if next_bar is None:
        raise StrategyError(
            "no bar follows the trigger, so there is no price at which an entry could have filled",
            component=COMPONENT, operation="entry_fill",
            context={"trigger_price": str(trigger_price), "direction": direction.value},
            likely_cause="the backtest window ends on the trigger bar",
            remediation="the trade did not open; count it as a non-trade rather than filling at the "
                        "trigger, which would assume action at a close that had not happened yet",
        )
    open_price = next_bar.open
    # "Worse" is direction-dependent: a long wants the lower price, so the worse of the two is the
    # higher one.
    if direction is Direction.LONG:
        base = max(trigger_price, open_price)
        chose = "next bar open" if open_price > trigger_price else "trigger"
    else:
        base = min(trigger_price, open_price)
        chose = "next bar open" if open_price < trigger_price else "trigger"
    slip = costs.slippage_on(base, direction, entering=True)
    return Fill(kind=FillKind.ENTRY, price=base + slip, at_ms=next_bar.open_time_ms,
                slippage_price=abs(slip),
                basis=f"worse of trigger {trigger_price} and next-bar open {open_price} = {base} "
                      f"({chose}), plus {abs(slip)} slippage. Never the trigger price itself: the "
                      f"signal confirms on a close, so the earliest action is the next bar")


def stop_fill(*, stop: Decimal, bar: Bar, direction: Direction, costs: CostModel,
              kind: FillKind = FillKind.STOP) -> Fill | None:
    """specs/09 §3.2, the gap rule. ``None`` when the bar never reached the stop.

    A long is stopped when the bar's **low** reaches the stop. If the bar *opened* already beyond it,
    the fill is the open — the price actually available — not the stop. That difference is the whole
    rule, and it is concentrated in the tail events that decide survival.
    """
    long = direction is Direction.LONG
    touched = bar.low <= stop if long else bar.high >= stop
    if not touched:
        return None
    gapped = bar.open < stop if long else bar.open > stop
    base = bar.open if gapped else stop
    slip = costs.slippage_on(base, direction, entering=False)
    basis = (f"the bar opened at {bar.open}, already beyond the stop {stop}: the fill is the OPEN, "
             f"not the stop — the stop price was never available"
             if gapped else f"the bar's range reached the stop {stop}")
    return Fill(kind=kind, price=base + slip, at_ms=bar.open_time_ms, slippage_price=abs(slip),
                gapped=gapped, basis=f"{basis}, plus {abs(slip)} slippage")


def target_fill(*, target: Decimal, bar: Bar, direction: Direction) -> Fill | None:
    """specs/09 §3.3: fills at the target when the bar's range contains it. **No slippage.**

    A target is a limit order, so it does not suffer the adverse gap problem. The symmetric
    consequence is that a *favourable* gap is not credited either: a long whose target is 110 and
    whose bar opens at 115 still books 110. That under-states the result, which is the correct side to
    err on.
    """
    long = direction is Direction.LONG
    reached = bar.high >= target if long else bar.low <= target
    if not reached:
        return None
    return Fill(kind=FillKind.TARGET, price=target, at_ms=bar.open_time_ms,
                basis=f"the bar's range [{bar.low}, {bar.high}] contains the target {target}; a limit "
                      f"order fills at its price and pays no slippage. A favourable gap beyond the "
                      f"target is deliberately not credited")


def resolve_bar(*, bar: Bar, direction: Direction, stop: Decimal | None,
                target: Decimal | None, costs: CostModel,
                stop_kind: FillKind = FillKind.STOP) -> Fill | None:
    """What happened to an open position during one bar. **Stop first, always** (specs/09 §3.3).

    When a single bar contains both levels the stop is taken, unconditionally. The spec is explicit
    that this must not be resolved by dropping to finer data: doing so on some bars and not others
    introduces a bias whose direction cannot be reasoned about, and a model that is conservative only
    sometimes is not a conservative model.
    """
    hit_stop = (stop_fill(stop=stop, bar=bar, direction=direction, costs=costs, kind=stop_kind)
                if stop is not None else None)
    if hit_stop is not None:
        return hit_stop
    return target_fill(target=target, bar=bar, direction=direction) if target is not None else None


def mark_to_market(*, bar: Bar, direction: Direction) -> Fill:
    """The window ended with the position open. Marked, never counted as an exit.

    Treating an open position as a closed winner at the final close is a classic way to inflate a
    backtest's last fold, so the kind is distinct and the accounting excludes it from realised
    statistics rather than relying on a reader to notice.
    """
    return Fill(kind=FillKind.MARK_TO_MARKET, price=bar.close, at_ms=bar.open_time_ms,
                basis="the backtest window ended with the position still open; marked at the final "
                      "close and excluded from realised statistics, because an unrealised mark is "
                      "not a result")
