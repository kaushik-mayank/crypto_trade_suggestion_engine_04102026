"""Trade accounting (specs/09 §4) — what counts as a trade, and what merely gets logged.

The table is short and every row is a decision that changes the statistics:

| Event | Counts as |
|---|---|
| ``SETUP_DETECTED`` → ``EXPIRED`` | **not a trade.** Logged for frequency analysis |
| ``SUPPRESSED`` by arbitration | **not a trade.** Logged — needed to measure what arbitration costs |
| ``MISSED`` | **not a trade.** Logged; a high rate means the trigger is too slow |
| ``TRIGGERED`` → terminal | **one trade** |
| partial exit at T1, remainder trailed | **one trade**, P&L the weighted sum of both exits |
| **handoff** | **one trade spanning both strategies**, never two |

The handoff rule is the one that needs enforcing rather than documenting. C3-B hands a matured trend
to C2-A and C2-C hands funding-aligned trend to C1-A; counting those as two trades would inflate the
trade count and distort both strategies' statistics **in opposite directions** — the originator would
show a truncated winner and the receiver an entry it never chose. :class:`Trade` therefore carries
``legs``, and P&L is attributed to the originator up to the handoff and the receiver after it.

**R is the unit, not currency.** Every trade's result is expressed in multiples of its own initial
risk, because that is what makes trades comparable across assets whose prices differ by four orders of
magnitude, and because the risk engine sizes on ``R`` in the first place. Currency P&L is derived for
portfolio-level metrics, never for per-trade comparison.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from backtest.fills import Fill, FillKind
from core.errors import StrategyError
from strategy.act import Direction
from strategy.base import Lifecycle

COMPONENT = "backtest.accounting"

#: Lifecycle outcomes that end a setup **without** a trade. Each is counted and reported, because the
#: rates are diagnostic: a high ``EXPIRED`` rate means setups are armed too early, and a high
#: ``MISSED`` rate means the trigger is too slow (specs/09 §4, §11).
NON_TRADE_TERMINALS = (Lifecycle.EXPIRED, Lifecycle.INVALIDATED, Lifecycle.MISSED)


@dataclass
class Leg:
    """One strategy's share of a trade. A trade with more than one leg crossed a handoff."""

    strategy_id: str
    strategy_version: str
    entered_at_ms: int
    exited_at_ms: int | None = None
    #: Price at which this leg's attribution starts and ends. For a single-leg trade these are the
    #: entry and exit fills; across a handoff the boundary price is the mark at the handoff moment.
    from_price: Decimal | None = None
    to_price: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
                "entered_at_ms": self.entered_at_ms, "exited_at_ms": self.exited_at_ms,
                "from_price": str(self.from_price) if self.from_price is not None else None,
                "to_price": str(self.to_price) if self.to_price is not None else None}


@dataclass
class Trade:
    """One trade: ``TRIGGERED`` through a terminal state, however many strategies it crossed."""

    trade_id: str
    asset: str
    direction: Direction
    #: The strategy that armed it. Statistics are grouped by this, with handoff legs reported
    #: separately, so a receiver's numbers are never credited with an entry it did not choose.
    originating_strategy_id: str
    strategy_class: int
    entry: Fill
    #: Initial risk in price units: ``|entry - initial_stop|``. Fixed at entry and never updated,
    #: because R is the denominator every later number is expressed in — recomputing it as the trail
    #: moves would make a trade's own R-multiple drift while the trade was open.
    initial_risk: Decimal
    initial_stop: Decimal
    target: Decimal | None = None
    exit: Fill | None = None
    #: Partial exits, each with the fraction of the position closed. A T1 partial plus a trailed
    #: remainder is still **one** trade whose P&L is the weighted sum.
    partials: list[tuple[Fill, Decimal]] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    regime_cell: str | None = None
    parameter_hash: str = ""
    code_version: str = ""
    signal_id: str = ""
    #: Set when the position was still open at the end of the window.
    open_at_window_end: bool = False

    def __post_init__(self) -> None:
        if self.initial_risk <= 0:
            raise StrategyError(
                f"a trade's initial risk must be positive, got {self.initial_risk}",
                component=COMPONENT, operation="Trade",
                context={"trade_id": self.trade_id, "entry": str(self.entry.price),
                         "initial_stop": str(self.initial_stop)},
                likely_cause="the entry and the stop are equal, or the stop is on the wrong side",
                remediation="R is the denominator of every statistic; a zero or negative R would make "
                            "every R-multiple meaningless rather than merely wrong",
            )

    @property
    def handed_off(self) -> bool:
        return len(self.legs) > 1

    @property
    def closed(self) -> bool:
        """Realised. A mark-to-market at the window's end is **not** closed."""
        return self.exit is not None and self.exit.kind is not FillKind.MARK_TO_MARKET

    def _signed(self, exit_price: Decimal) -> Decimal:
        sign = Decimal(1) if self.direction is Direction.LONG else Decimal(-1)
        return (exit_price - self.entry.price) * sign

    @property
    def r_multiple(self) -> Decimal | None:
        """Result in multiples of initial risk, weighted across partials. ``None`` while open.

        Partials are weighted by the fraction closed; the remainder carries whatever weight is left.
        A trade exited entirely at T1 and one scaled out across two prices are both one number here,
        which is what keeps "one trade" meaning one observation.
        """
        if self.exit is None:
            return None
        total = Decimal(0)
        weight_used = Decimal(0)
        for fill, fraction in self.partials:
            total += self._signed(fill.price) * fraction
            weight_used += fraction
        remainder = Decimal(1) - weight_used
        if remainder < 0:
            raise StrategyError(
                f"partial exits sum to {weight_used}, more than the whole position",
                component=COMPONENT, operation="r_multiple",
                context={"trade_id": self.trade_id, "weight_used": str(weight_used)},
                likely_cause="a partial fraction was recorded twice",
                remediation="fractions must sum to at most 1; the remainder is trailed",
            )
        total += self._signed(self.exit.price) * remainder
        return total / self.initial_risk

    @property
    def stopped(self) -> bool:
        return self.exit is not None and self.exit.kind in (FillKind.STOP, FillKind.TRAIL)

    @property
    def gapped_through_stop(self) -> bool:
        """Whether the exit filled worse than the stop because the bar gapped (specs/09 §3.2).

        Counted because the *distribution* of realised R on stopped trades is a required output: if
        its mean is materially worse than −1.0R, the strategy's real risk exceeds its published risk
        and ``liq_safety`` must be re-derived from this measurement rather than left at its assumed
        value.
        """
        return self.exit is not None and self.exit.gapped

    @property
    def duration_ms(self) -> int | None:
        return None if self.exit is None else self.exit.at_ms - self.entry.at_ms

    def attribute(self) -> dict[str, Decimal]:
        """R attributed per strategy. Single-leg trades attribute everything to the originator.

        Across a handoff, each leg takes the move over its own stretch, divided by the trade's single
        initial risk — so the legs sum to the trade's R exactly and neither strategy's statistics
        gain or lose a fraction of a trade.
        """
        r = self.r_multiple
        if r is None:
            return {}
        if not self.handed_off:
            return {self.originating_strategy_id: r}
        sign = Decimal(1) if self.direction is Direction.LONG else Decimal(-1)
        out: dict[str, Decimal] = {}
        for leg in self.legs:
            if leg.from_price is None or leg.to_price is None:
                raise StrategyError(
                    f"handoff leg {leg.strategy_id} has no price boundary, so its share of the "
                    "trade cannot be attributed",
                    component=COMPONENT, operation="attribute",
                    context={"trade_id": self.trade_id, "leg": leg.to_dict()},
                    likely_cause="the handoff was recorded without marking the price at the moment "
                                 "it happened",
                    remediation="record from_price/to_price when the handoff fires; splitting a "
                                "trade without the boundary price would make the split arbitrary",
                )
            share = (leg.to_price - leg.from_price) * sign / self.initial_risk
            out[leg.strategy_id] = out.get(leg.strategy_id, Decimal(0)) + share
        return out

    def to_dict(self) -> dict[str, Any]:
        r = self.r_multiple
        return {
            "trade_id": self.trade_id, "asset": self.asset, "direction": self.direction.value,
            "originating_strategy_id": self.originating_strategy_id,
            "strategy_class": self.strategy_class,
            "entry": self.entry.to_dict(), "exit": self.exit.to_dict() if self.exit else None,
            "initial_risk": str(self.initial_risk), "initial_stop": str(self.initial_stop),
            "target": str(self.target) if self.target is not None else None,
            "partials": [{"fill": f.to_dict(), "fraction": str(x)} for f, x in self.partials],
            "legs": [leg.to_dict() for leg in self.legs],
            "handed_off": self.handed_off, "closed": self.closed,
            "r_multiple": str(r) if r is not None else None,
            "stopped": self.stopped, "gapped_through_stop": self.gapped_through_stop,
            "duration_ms": self.duration_ms, "regime_cell": self.regime_cell,
            "open_at_window_end": self.open_at_window_end,
            "attribution": {k: str(v) for k, v in self.attribute().items()},
            "parameter_hash": self.parameter_hash, "code_version": self.code_version,
            "signal_id": self.signal_id,
        }


@dataclass
class NonTrades:
    """Everything that ended without a trade. Counted, because the rates are the diagnosis.

    specs/09 §4 requires these logged rather than discarded, and §11 reads them as predictions:
    a high ``expired`` rate means setups arm too early; a high ``missed`` rate means the trigger is
    too slow; a high ``suppressed`` count is the measured cost of arbitration, which is otherwise
    invisible — nobody can see the trades a system declined to suggest.
    """

    expired: int = 0
    invalidated: int = 0
    missed: int = 0
    suppressed: int = 0
    vetoed: int = 0
    #: Triggers that could not fill because the window ended on the trigger bar. Not a trade and not
    #: a strategy's fault; kept separate so it is never read as either.
    no_following_bar: int = 0
    by_strategy: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, kind: str, strategy_id: str) -> None:
        if not hasattr(self, kind):
            raise StrategyError(
                f"unknown non-trade kind {kind!r}",
                component=COMPONENT, operation="record",
                context={"kind": kind, "known": sorted(self.to_dict())},
                likely_cause="a lifecycle outcome was added without deciding how it is counted",
                remediation="specs/09 §4 enumerates what counts as a trade; add the row there first",
            )
        setattr(self, kind, getattr(self, kind) + 1)
        self.by_strategy.setdefault(strategy_id, {})
        self.by_strategy[strategy_id][kind] = self.by_strategy[strategy_id].get(kind, 0) + 1

    @property
    def total(self) -> int:
        return (self.expired + self.invalidated + self.missed + self.suppressed + self.vetoed
                + self.no_following_bar)

    def to_dict(self) -> dict[str, Any]:
        return {"expired": self.expired, "invalidated": self.invalidated, "missed": self.missed,
                "suppressed": self.suppressed, "vetoed": self.vetoed,
                "no_following_bar": self.no_following_bar, "total": self.total,
                "by_strategy": {k: dict(v) for k, v in sorted(self.by_strategy.items())}}


@dataclass
class Ledger:
    """Every trade and non-trade from one run."""

    trades: list[Trade] = field(default_factory=list)
    non_trades: NonTrades = field(default_factory=NonTrades)

    def add(self, trade: Trade) -> None:
        self.trades.append(trade)

    @property
    def closed(self) -> list[Trade]:
        """Realised trades only — the population every statistic is computed over."""
        return [t for t in self.trades if t.closed]

    @property
    def open_at_end(self) -> list[Trade]:
        return [t for t in self.trades if t.open_at_window_end]

    def for_strategy(self, strategy_id: str) -> list[Trade]:
        """Trades a strategy **originated**.

        Grouped by originator rather than by participation, so a handoff receiver's statistics are
        never credited with an entry it did not choose. Its share of the move is in
        :meth:`Trade.attribute`, which is the honest place for it.
        """
        return [t for t in self.closed if t.originating_strategy_id == strategy_id]

    def to_dict(self) -> dict[str, Any]:
        return {"trades": [t.to_dict() for t in self.trades],
                "counts": {"trades": len(self.trades), "closed": len(self.closed),
                           "open_at_window_end": len(self.open_at_end),
                           "handed_off": sum(1 for t in self.trades if t.handed_off)},
                "non_trades": self.non_trades.to_dict()}
