"""PHASE-26 — the backtest harness (specs/09).

The fill-model tests carry the most weight here, because specs/09 §3 opens by naming optimistic fills
"the single largest source of inflated backtest results". Each conservative rule is asserted against
a hand-worked example, including the spec's own: a long stopped at 100 whose next bar opens at 96 and
trades to 94 fills at **96**, a loss of 1.33R on a trade a naive model records as −1.00R.

The protocol tests assert *refusals*. A pre-registration that can be regenerated, edited, or run under
different parameters than it declared is not a pre-registration, so every one of those paths raises.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from backtest import protocol
from backtest.accounting import Ledger, Leg, Trade
from backtest.fills import (
    SENSITIVITY_MULTIPLIER,
    CostModel,
    Fill,
    FillKind,
    entry_fill,
    mark_to_market,
    resolve_bar,
    stop_fill,
    target_fill,
)
from backtest.metrics import (
    LONG_GAMMA_STRATEGIES,
    MIN_TRADES_FOR_STATISTICS,
    by_group,
    concentration,
    distribution,
    drawdown,
    equity_curve,
    histogram,
    risk_adjusted,
    short_gamma_required,
    wilson_interval,
)
from backtest.runner import sensitivity_costs
from config.parameters import load_registry
from core.errors import ProtocolViolation, StrategyError
from normalize.canonical import Bar, Fee, InstrumentMeta, Product, Timeframe
from strategy.act import Direction

T0 = 1767225600000
M1 = 60_000
ASSET = "B-BTC_USDT"
REGISTRY = load_registry()
#: No slippage by default, so a fill test measures the *rule* rather than the rule plus an offset.
FREE = CostModel(slippage_fraction=Decimal(0))


def bar(*, o, h, l, c, at=T0, tf=Timeframe.M1):  # noqa: E741 — OHLC names are conventional here
    return Bar(asset=ASSET, product=Product.FUTURES, timeframe=tf, open_time_ms=at,
               open=str(o), high=str(h), low=str(l), close=str(c), volume_base="10",
               is_final=True, fetched_at_ms=at)


def instrument(taker="0.059"):
    return InstrumentMeta(asset=ASSET, product=Product.FUTURES, tick=Decimal("0.1"),
                          qty_increment=Decimal("0.001"), min_notional=Decimal("60"),
                          maker_fee=Fee.from_api_percent("0.02"),
                          taker_fee=Fee.from_api_percent(taker), fetched_at_ms=T0)


# ---- entry fills (specs/09 §3.1) ----------------------------------------------------------

def test_a_long_entry_fills_at_the_worse_of_the_trigger_and_the_next_open():
    """Hand-computed: a long triggering at 100 whose next bar opens at 101 fills at **101**.

    Never at the trigger. The signal confirms on a 1m close, so the earliest moment anyone could act
    is the next bar — filling at the trigger assumes a time machine.
    """
    fill = entry_fill(trigger_price=Decimal("100"), next_bar=bar(o=101, h=102, l=100, c=101),
                      direction=Direction.LONG, costs=FREE)
    assert fill.price == Decimal("101")
    assert fill.kind is FillKind.ENTRY
    assert "next bar open" in fill.basis


def test_a_favourable_next_open_does_not_improve_the_entry():
    """The other half: a long whose next bar opens *below* the trigger still fills at the trigger.

    If the better price were taken, the model would credit a favourable gap it has no evidence
    anyone received — and "worse of" would be "best of" on half the bars.
    """
    fill = entry_fill(trigger_price=Decimal("100"), next_bar=bar(o=98, h=99, l=97, c=98),
                      direction=Direction.LONG, costs=FREE)
    assert fill.price == Decimal("100")
    assert "(trigger)" in fill.basis


def test_a_short_entry_mirrors_exactly():
    worse = entry_fill(trigger_price=Decimal("100"), next_bar=bar(o=99, h=100, l=98, c=99),
                       direction=Direction.SHORT, costs=FREE)
    assert worse.price == Decimal("99")          # selling lower is worse for a short
    better = entry_fill(trigger_price=Decimal("100"), next_bar=bar(o=102, h=103, l=101, c=102),
                        direction=Direction.SHORT, costs=FREE)
    assert better.price == Decimal("100")


def test_slippage_is_always_adverse_on_both_sides_of_a_trade():
    """A model that let slippage help would flatter every result by a number nobody could audit."""
    costs = CostModel(slippage_fraction=Decimal("0.001"))     # 10 bps
    long_entry = entry_fill(trigger_price=Decimal("100"), next_bar=bar(o=100, h=101, l=99, c=100),
                            direction=Direction.LONG, costs=costs)
    assert long_entry.price == Decimal("100.100")             # paid up
    short_entry = entry_fill(trigger_price=Decimal("100"), next_bar=bar(o=100, h=101, l=99, c=100),
                             direction=Direction.SHORT, costs=costs)
    assert short_entry.price == Decimal("99.900")             # sold down


def test_no_bar_after_the_trigger_is_not_a_trade():
    """Inventing a fill from the absence of data would manufacture a trade out of a window boundary."""
    with pytest.raises(StrategyError, match="no bar follows the trigger"):
        entry_fill(trigger_price=Decimal("100"), next_bar=None, direction=Direction.LONG,
                   costs=FREE)


# ---- the gap rule (specs/09 §3.2) ---------------------------------------------------------

def test_the_specs_own_gap_example_fills_at_the_open_and_costs_more_than_one_R():
    """specs/09 §3.2, worked exactly as written.

    > "if a long's stop is at 100 and the next bar opens at 96 and trades to 94, the fill is **96**,
    > producing a loss of 1.33R on a trade the backtest would otherwise record as −1.00R."

    Entry 103, stop 100, so R = 3. Filling at 96 loses 7, which is 7/3 = 2.33... — the spec's 1.33R
    figure comes from *its* entry; what is asserted here is the rule and the direction of the error.
    """
    fill = stop_fill(stop=Decimal("100"), bar=bar(o=96, h=97, l=94, c=95),
                     direction=Direction.LONG, costs=FREE)
    assert fill is not None
    assert fill.price == Decimal("96"), "the stop price was never available; the open was"
    assert fill.gapped is True
    assert "the fill is the OPEN" in fill.basis

    entry = Fill(kind=FillKind.ENTRY, price=Decimal("103"), at_ms=T0, basis="test")
    trade = Trade(trade_id="t", asset=ASSET, direction=Direction.LONG,
                  originating_strategy_id="C2-A", strategy_class=2, entry=entry,
                  initial_risk=Decimal("3"), initial_stop=Decimal("100"), exit=fill)
    # Hand-computed: (96 - 103) / 3 = -2.333..., materially worse than the -1.0R a naive model books.
    assert trade.r_multiple == pytest.approx(Decimal("-7") / Decimal("3"))
    assert trade.r_multiple < Decimal("-1")
    assert trade.gapped_through_stop is True


def test_a_stop_reached_without_a_gap_fills_at_the_stop():
    fill = stop_fill(stop=Decimal("100"), bar=bar(o=102, h=103, l=99, c=101),
                     direction=Direction.LONG, costs=FREE)
    assert fill is not None
    assert fill.price == Decimal("100")
    assert fill.gapped is False


def test_a_bar_that_never_reaches_the_stop_produces_no_fill():
    assert stop_fill(stop=Decimal("100"), bar=bar(o=105, h=106, l=104, c=105),
                     direction=Direction.LONG, costs=FREE) is None


def test_the_gap_rule_mirrors_for_a_short():
    """A short stopped at 100 whose bar opens at 104 fills at 104, not 100."""
    fill = stop_fill(stop=Decimal("100"), bar=bar(o=104, h=106, l=103, c=105),
                     direction=Direction.SHORT, costs=FREE)
    assert fill is not None and fill.price == Decimal("104") and fill.gapped


# ---- targets and intrabar ambiguity (specs/09 §3.3) ---------------------------------------

def test_a_target_fills_at_its_price_and_pays_no_slippage():
    """A target is a limit order. It does not suffer the adverse gap problem, and the symmetric
    consequence is that a favourable gap beyond it is not credited either."""
    fill = target_fill(target=Decimal("110"), bar=bar(o=115, h=116, l=114, c=115),
                       direction=Direction.LONG)
    assert fill is not None
    assert fill.price == Decimal("110"), "a favourable gap must not be credited"
    assert fill.slippage_price == Decimal(0)


def test_a_bar_containing_both_the_stop_and_the_target_resolves_to_the_stop():
    """specs/09 §3.3, and the word in the spec is "Always".

    The bar here spans 94 to 112, containing a stop at 100 and a target at 110. Resolving it by
    consulting finer data on *this* bar and not others would introduce a bias whose direction is
    impossible to reason about, so the rule is unconditional.
    """
    fill = resolve_bar(bar=bar(o=105, h=112, l=94, c=108), direction=Direction.LONG,
                       stop=Decimal("100"), target=Decimal("110"), costs=FREE)
    assert fill is not None
    assert fill.kind is FillKind.STOP
    assert fill.price == Decimal("100")


def test_a_bar_reaching_only_the_target_resolves_to_the_target():
    fill = resolve_bar(bar=bar(o=105, h=112, l=104, c=108), direction=Direction.LONG,
                       stop=Decimal("100"), target=Decimal("110"), costs=FREE)
    assert fill is not None and fill.kind is FillKind.TARGET


def test_an_open_position_at_the_window_end_is_marked_not_counted():
    """Treating an open position as a closed winner at the final close inflates the last fold."""
    fill = mark_to_market(bar=bar(o=100, h=120, l=99, c=118), direction=Direction.LONG)
    assert fill.kind is FillKind.MARK_TO_MARKET
    entry = Fill(kind=FillKind.ENTRY, price=Decimal("100"), at_ms=T0, basis="test")
    trade = Trade(trade_id="t", asset=ASSET, direction=Direction.LONG,
                  originating_strategy_id="C2-A", strategy_class=2, entry=entry,
                  initial_risk=Decimal("10"), initial_stop=Decimal("90"), exit=fill,
                  open_at_window_end=True)
    assert trade.closed is False, "an unrealised mark is not a result"
    ledger = Ledger()
    ledger.add(trade)
    assert ledger.closed == []
    assert ledger.open_at_end == [trade]


# ---- costs come from the live module -------------------------------------------------------

def test_the_round_trip_fee_matches_what_the_risk_engine_charges():
    """specs/08 acceptance criterion 10: the backtest and the live system charge the same costs.

    Two implementations of a fee calculation is how a backtest ends up grading a different strategy
    than the one that runs, so the formula is asserted equal rather than assumed so.
    """
    meta = instrument(taker="0.059")
    price = Decimal("85000")
    assert FREE.round_trip_fee_price(price, meta) == 2 * meta.taker_fee.fraction * price
    # And it is the live number, not the vendor doc's: hard invariant 1 (doc says 0.075 for BTC).
    assert meta.taker_fee.fraction == Decimal("0.00059")


def test_the_sensitivity_run_doubles_slippage_and_nothing_else():
    base = CostModel(slippage_fraction=Decimal("0.0005"))
    doubled = sensitivity_costs(base)
    assert doubled.slippage_fraction == base.slippage_fraction
    assert doubled.slippage_multiplier == SENSITIVITY_MULTIPLIER
    assert doubled.effective_slippage == base.effective_slippage * SENSITIVITY_MULTIPLIER


# ---- trade accounting (specs/09 §4) -------------------------------------------------------

def make_trade(*, r=None, entry=Decimal("100"), stop=Decimal("90"), exit_price=None,
               direction=Direction.LONG, strategy="C2-A", cls=2, kind=FillKind.TARGET,
               asset=ASSET, at=T0, cell="cell (1, 1)"):
    """A trade with a known R. ``r`` sets the exit price so the expectation is hand-checkable."""
    risk = abs(entry - stop)
    if exit_price is None:
        sign = 1 if direction is Direction.LONG else -1
        exit_price = entry + Decimal(str(r)) * risk * sign
    return Trade(
        trade_id=f"t{at}", asset=asset, direction=direction,
        originating_strategy_id=strategy, strategy_class=cls,
        entry=Fill(kind=FillKind.ENTRY, price=entry, at_ms=at, basis="test"),
        initial_risk=risk, initial_stop=stop, target=None,
        exit=Fill(kind=kind, price=exit_price, at_ms=at + 3_600_000, basis="test"),
        regime_cell=cell)


def test_r_is_measured_against_the_risk_fixed_at_entry():
    """Hand-computed: entry 100, stop 90, exit 130 → R = 30/10 = 3."""
    t = make_trade(entry=Decimal("100"), stop=Decimal("90"), exit_price=Decimal("130"))
    assert t.r_multiple == Decimal("3")


def test_a_partial_exit_and_a_trailed_remainder_are_one_trade():
    """specs/09 §4: P&L is the weighted sum of both exits, not two observations.

    Hand-computed: entry 100, stop 90 so R = 10. Half out at 120 (+2R) and half at 140 (+4R) gives
    0.5*2 + 0.5*4 = 3R.
    """
    t = make_trade(entry=Decimal("100"), stop=Decimal("90"), exit_price=Decimal("140"),
                   kind=FillKind.TRAIL)
    t.partials.append((Fill(kind=FillKind.TARGET, price=Decimal("120"), at_ms=T0, basis="T1"),
                       Decimal("0.5")))
    assert t.r_multiple == Decimal("3")


def test_partials_summing_past_the_whole_position_are_refused():
    t = make_trade(r=1)
    t.partials.append((Fill(kind=FillKind.TARGET, price=Decimal("110"), at_ms=T0, basis="x"),
                       Decimal("0.7")))
    t.partials.append((Fill(kind=FillKind.TARGET, price=Decimal("110"), at_ms=T0, basis="x"),
                       Decimal("0.7")))
    with pytest.raises(StrategyError, match="more than the whole position"):
        _ = t.r_multiple


def test_a_handoff_is_one_trade_split_between_two_strategies():
    """specs/09 §4: counting a handoff as two trades would inflate the count and distort both
    strategies' statistics **in opposite directions** — the originator showing a truncated winner
    and the receiver an entry it never chose.

    Hand-computed: entry 100, stop 90 so R = 10. C3-B holds 100 → 120 (+2R), hands to C2-A which
    holds 120 → 150 (+3R). The trade is +5R, the legs are +2R and +3R, and they sum exactly.
    """
    t = make_trade(entry=Decimal("100"), stop=Decimal("90"), exit_price=Decimal("150"),
                   strategy="C3-B")
    t.legs = [
        Leg(strategy_id="C3-B", strategy_version="1.0.0", entered_at_ms=T0,
            from_price=Decimal("100"), to_price=Decimal("120")),
        Leg(strategy_id="C2-A", strategy_version="1.0.0", entered_at_ms=T0 + 3_600_000,
            from_price=Decimal("120"), to_price=Decimal("150")),
    ]
    assert t.handed_off is True
    assert t.r_multiple == Decimal("5")
    attribution = t.attribute()
    assert attribution == {"C3-B": Decimal("2"), "C2-A": Decimal("3")}
    assert sum(attribution.values()) == t.r_multiple, "the legs must sum to the trade exactly"


def test_a_handoff_without_a_boundary_price_is_refused_rather_than_split_arbitrarily():
    t = make_trade(r=2, strategy="C3-B")
    t.legs = [Leg(strategy_id="C3-B", strategy_version="1", entered_at_ms=T0),
              Leg(strategy_id="C2-A", strategy_version="1", entered_at_ms=T0 + 1)]
    with pytest.raises(StrategyError, match="no price boundary"):
        t.attribute()


def test_a_zero_risk_trade_is_refused_at_construction():
    """R is the denominator of every statistic; a zero R makes every R-multiple meaningless rather
    than merely wrong, so it fails where it is created."""
    with pytest.raises(StrategyError, match="initial risk must be positive"):
        Trade(trade_id="t", asset=ASSET, direction=Direction.LONG,
              originating_strategy_id="C2-A", strategy_class=2,
              entry=Fill(kind=FillKind.ENTRY, price=Decimal("100"), at_ms=T0, basis="x"),
              initial_risk=Decimal(0), initial_stop=Decimal("100"))


def test_non_trades_are_counted_per_kind_and_per_strategy():
    """The rates are the diagnosis: expiries mean setups arm too early, misses mean the trigger is
    too slow, and the suppressed count is the only measure of what arbitration costs."""
    ledger = Ledger()
    ledger.non_trades.record("expired", "C1-B")
    ledger.non_trades.record("expired", "C1-B")
    ledger.non_trades.record("missed", "C2-A")
    ledger.non_trades.record("suppressed", "C4-A")
    assert ledger.non_trades.expired == 2
    assert ledger.non_trades.total == 4
    assert ledger.non_trades.by_strategy["C1-B"] == {"expired": 2}


def test_an_unknown_non_trade_kind_is_refused():
    """specs/09 §4 enumerates what counts as a trade; a new lifecycle outcome must be decided there
    before it can be counted here."""
    with pytest.raises(StrategyError, match="unknown non-trade kind"):
        Ledger().non_trades.record("vanished", "C1-B")


def test_trades_group_by_originator_so_a_receiver_is_not_credited_with_an_entry_it_did_not_choose():
    ledger = Ledger()
    ledger.add(make_trade(r=1, strategy="C3-B"))
    ledger.add(make_trade(r=2, strategy="C2-A", at=T0 + 1))
    assert [t.originating_strategy_id for t in ledger.for_strategy("C3-B")] == ["C3-B"]
    assert len(ledger.for_strategy("C2-A")) == 1


# ---- metrics (specs/09 §5) ----------------------------------------------------------------

def test_the_wilson_interval_is_hand_checkable_and_bounded():
    """7 of 10 gives roughly [0.40, 0.89] — wide, which is the point. The normal approximation can
    fall outside [0, 1] for small n, which is every strategy's first fold."""
    lo, hi = wilson_interval(7, 10)
    assert 0.39 < lo < 0.42 and 0.87 < hi < 0.90
    assert wilson_interval(10, 10)[1] <= 1.0
    assert wilson_interval(0, 10)[0] >= 0.0
    assert wilson_interval(0, 0) is None


def test_a_hit_rate_never_travels_without_its_interval_and_n():
    """70% on 10 trades and on 400 are different claims that a bare percentage makes look identical."""
    few = distribution([make_trade(r=1, at=T0 + i) for i in range(7)]
                       + [make_trade(r=-1, at=T0 + 100 + i) for i in range(3)])
    assert few.hit_rate == pytest.approx(0.7)
    assert few.n == 10
    lo, hi = few.hit_rate_ci95
    assert hi - lo > 0.4, "a 10-trade interval must be visibly wide"

    many = distribution([make_trade(r=1, at=T0 + i) for i in range(280)]
                        + [make_trade(r=-1, at=T0 + 1000 + i) for i in range(120)])
    assert many.hit_rate == pytest.approx(0.7)
    lo2, hi2 = many.hit_rate_ci95
    assert hi2 - lo2 < 0.1, "a 400-trade interval must be visibly narrow"


def test_mean_and_median_are_both_reported_because_they_diverge_for_skewed_payoffs():
    """Hand-built: nine −1R losers and one +20R winner. Mean +1.1R, median −1R. A long-gamma
    strategy showing exactly this is working as designed; reporting only the mean hides which."""
    trades = [make_trade(r=-1, at=T0 + i) for i in range(9)] + [make_trade(r=20, at=T0 + 99)]
    d = distribution(trades)
    assert d.mean_r == pytest.approx(1.1)
    assert d.median_r == pytest.approx(-1.0)
    assert d.mean_r > 0 > d.median_r


def test_the_histogram_is_reported_not_just_summary_statistics():
    trades = [make_trade(r=-1, at=T0 + i) for i in range(3)] + [make_trade(r=2.5, at=T0 + 99)]
    d = distribution(trades)
    assert sum(d.histogram.values()) == 4
    assert d.histogram["[-1.0, -0.5)"] == 3
    assert d.histogram["[2.0, 3.0)"] == 1


def test_the_histogram_buckets_are_fixed_so_two_runs_are_comparable():
    """Data-derived buckets could be chosen after seeing the results, which is the whole hazard."""
    a = histogram([0.5, 1.5, 2.5])
    b = histogram([-4.0, 11.0])
    assert set(a) == set(b), "the bucket labels must not depend on the data"
    assert b["<-5.0"] == 0 and b["[-5.0, -3.0)"] == 1 and b[">=10.0"] == 1


def test_stopped_trades_report_how_much_worse_than_one_R_they_actually_realised():
    """specs/09 §3.2: if the mean is materially worse than −1.0R, real risk exceeds published risk
    and ``liq_safety`` is re-derived from this distribution rather than left at its assumed value."""
    gapped = make_trade(entry=Decimal("100"), stop=Decimal("90"), exit_price=Decimal("85"),
                        kind=FillKind.STOP)
    gapped.exit = Fill(kind=FillKind.STOP, price=Decimal("85"), at_ms=T0 + 1,
                       basis="gapped", gapped=True)
    clean = make_trade(entry=Decimal("100"), stop=Decimal("90"), exit_price=Decimal("90"),
                       kind=FillKind.STOP, at=T0 + 10)
    d = distribution([gapped, clean])
    assert d.stopped_n == 2
    # Hand-computed: (-1.5R + -1.0R) / 2 = -1.25R
    assert d.stopped_mean_r == pytest.approx(-1.25)
    assert d.stop_slippage_r == pytest.approx(0.25)
    assert d.stopped_gapped_n == 1


def test_profit_concentration_is_read_against_the_strategys_intended_payoff_shape():
    """specs/09 §5.2 — the same number, opposite conclusions.

    High concentration in a long-gamma strategy is expected and correct; its absence suggests the
    trailing exit is cutting winners. In a short-gamma one it means the edge rests on a few lucky
    events. So the expectation is published beside the measurement.
    """
    trades = [make_trade(r=-1, at=T0 + i) for i in range(20)] + [make_trade(r=30, at=T0 + 99)]
    long_gamma = concentration(trades, strategy_id="C2-A")
    assert long_gamma["payoff_shape"] == "long_gamma"
    assert "expected and correct" in long_gamma["expectation"]
    assert "not applicable as a failure test" in long_gamma["lottery_test"]

    short_gamma = concentration(trades, strategy_id="C4-C")
    assert short_gamma["payoff_shape"] == "short_gamma_or_mean_reverting"
    assert "RED FLAG" in short_gamma["expectation"]
    assert "lottery, not an edge" in short_gamma["lottery_test"]
    # The measurement itself is identical; only the reading differs.
    assert long_gamma["top5_dependency"] == short_gamma["top5_dependency"]


def test_the_lottery_test_is_computed_not_merely_described():
    trades = [make_trade(r=-1, at=T0 + i) for i in range(20)] + [make_trade(r=30, at=T0 + 99)]
    c = concentration(trades, strategy_id="C4-C")
    # Hand-computed: 20 losers at -1R and one +30R winner. Total = 30 - 20 = +10R.
    # "The 5 best trades" is literal, so with only one winner the top five are [30, -1, -1, -1, -1]
    # summing to 26 — removing them leaves 10 - 26 = -16R. The formula is the spec's as written, and
    # it does not special-case a strategy with fewer than five winners.
    assert c["total_r"] == pytest.approx(10.0)
    assert c["top5_dependency"] == pytest.approx(2.6)        # 26 / 10
    assert c["total_r_excluding_top5"] == pytest.approx(-16.0)
    assert c["profitable_without_top5"] is False


def test_the_payoff_shape_table_matches_the_spec():
    """specs/09 §5.2 names Class 2, Class 3, C4-A and C4-B as long-gamma by design."""
    assert {"C2-A", "C2-B", "C2-C", "C3-A", "C3-B", "C3-C", "C4-A", "C4-B"} == LONG_GAMMA_STRATEGIES
    for cls1 in ("C1-A", "C1-B", "C1-C", "C4-C"):
        assert cls1 not in LONG_GAMMA_STRATEGIES


def test_the_equity_curve_orders_by_exit_because_that_is_when_the_result_was_known():
    early_entry_late_exit = make_trade(r=1, at=T0)
    early_entry_late_exit.exit = Fill(kind=FillKind.TARGET, price=Decimal("110"),
                                      at_ms=T0 + 10_000_000, basis="x")
    later = make_trade(r=-1, at=T0 + 1000)
    curve = equity_curve([early_entry_late_exit, later])
    assert [at for at, _ in curve] == sorted(at for at, _ in curve)
    assert curve[0][1] == pytest.approx(-1.0), "the trade that resolved first comes first"


def test_drawdown_is_measured_in_R_with_its_duration():
    """Hand-computed: +2R, then −3R, then +1R. The curve is 2, −1, 0; the peak is 2 and the trough
    −1, so the drawdown is 3R and it never recovers to 2 inside the window."""
    trades = [make_trade(r=2, at=T0), make_trade(r=-3, at=T0 + 1000),
              make_trade(r=1, at=T0 + 2000)]
    dd = drawdown(equity_curve(trades))
    assert dd["max_drawdown_r"] == pytest.approx(3.0)
    assert dd["recovered"] is False
    assert dd["duration_ms"] > 0
    assert "R, not percent" in dd["unit_note"]


def test_sharpe_is_published_marked_insufficient():
    """specs/09 §5.4 rejects Sharpe alone for any strategy. The flag is in the payload, not only in
    the document, because a rule a reader can forget is one a dashboard can omit."""
    trades = [make_trade(r=1, at=T0 + i) for i in range(10)] + [make_trade(r=-1, at=T0 + 99)]
    ra = risk_adjusted(trades, equity_curve(trades))
    assert ra["sharpe_per_trade"] is not None
    assert ra["sufficient"] is False
    assert "under-penalises rare large losses" in ra["insufficiency_note"]


def test_the_short_gamma_set_leads_with_drawdown_and_the_loss_tail():
    """specs/06 §4.3: a high hit rate and a good Sharpe on C4-C are expected and prove nothing."""
    trades = [make_trade(r=0.4, at=T0 + i) for i in range(14)] + [make_trade(r=-6, at=T0 + 99)]
    sg = short_gamma_required(trades, equity_curve(trades))
    assert sg["worst_trade_r"] == pytest.approx(-6.0)
    assert sg["max_drawdown_r"] == pytest.approx(6.0)
    assert sg["loss_tail_p95_r"] == pytest.approx(6.0)
    assert sg["lead_with_these"] == ["max_drawdown_r", "worst_trade_r", "loss_tail_p95_r",
                                    "loss_tail_p99_r"]
    assert "do not widen the stop" in sg["note"]


def test_a_group_with_too_few_trades_reports_its_count_and_no_statistics():
    """specs/09 §6: "a metric computed from 7 trades is noise wearing a decimal point."

    Suppressed rather than flagged, because a number that is present will be read however it is
    labelled.
    """
    thin = [make_trade(r=1, asset="B-SOL_USDT", at=T0 + i) for i in range(7)]
    thick = [make_trade(r=1, asset="B-BTC_USDT", at=T0 + 100 + i)
             for i in range(MIN_TRADES_FOR_STATISTICS)]
    grouped = by_group(thin + thick, lambda t: t.asset, label="asset")
    assert grouped["groups"]["B-SOL_USDT"]["statistics"] is None
    assert grouped["groups"]["B-SOL_USDT"]["n"] == 7
    assert "noise wearing a decimal point" in grouped["groups"]["B-SOL_USDT"]["reason"]
    assert grouped["groups"]["B-BTC_USDT"]["statistics"] is not None


def test_regime_conditional_statistics_are_grouped_by_cell():
    """Required for the specs/06 §4.1 prediction about C4-A in mean-reverting cells."""
    trades = ([make_trade(r=1, cell="cell (1, 1)", at=T0 + i)
               for i in range(MIN_TRADES_FOR_STATISTICS)]
              + [make_trade(r=-1, cell="cell (0, 0)", at=T0 + 1000 + i)
                 for i in range(MIN_TRADES_FOR_STATISTICS)])
    grouped = by_group(trades, lambda t: t.regime_cell or "unclassified", label="regime cell")
    assert grouped["groups"]["cell (1, 1)"]["statistics"]["mean_r"] == pytest.approx(1.0)
    assert grouped["groups"]["cell (0, 0)"]["statistics"]["mean_r"] == pytest.approx(-1.0)


# ---- pre-registration (specs/09 §1) -------------------------------------------------------

WINDOWS = {"train": {"start_utc": "2022-02-01T00:00:00+00:00",
                     "end_utc": "2025-06-30T23:59:59+00:00"},
           "holdout": {"start_utc": "2026-04-01T00:00:00+00:00",
                       "end_utc": "2026-09-30T23:59:59+00:00"}}


def a_lock():
    return protocol.build(windows=WINDOWS, parameters=REGISTRY, trial_count=38)


def test_a_sealed_lock_hashes_its_own_contents():
    lock = a_lock()
    assert lock.sha256 == lock.digest()
    assert lock.parameter_hash == REGISTRY.parameter_hash
    assert len(lock.parameter_set) == 30, "all thirty parameters, with their bands"


def test_editing_the_lock_breaks_its_hash_and_verification_refuses():
    """"A protocol that can be edited after seeing results is not a protocol." (specs/09 §1)"""
    lock = a_lock()
    lock.trial_count_declared = 1          # the kind of edit that would weaken the correction
    with pytest.raises(ProtocolViolation, match="do not match its own sha256"):
        protocol.verify(lock, parameter_hash=REGISTRY.parameter_hash)


def test_running_under_different_parameters_than_registered_is_refused():
    """A result is only interpretable against the parameters that produced it, so this raises rather
    than warning — a warning is something a run can carry on past."""
    lock = a_lock()
    with pytest.raises(ProtocolViolation, match="parameter registry has changed"):
        protocol.verify(lock, parameter_hash="something-else")


def test_window_boundaries_must_be_absolute_and_timezoned():
    """Relative offsets shift as data accumulates, which moves the goalposts without an edit."""
    lock = protocol.build(windows={"train": {"start_utc": "last 20%", "end_utc": "now"}},
                          parameters=REGISTRY, trial_count=1)
    with pytest.raises(ProtocolViolation, match="not an ISO-8601 datetime"):
        lock.window("train")

    naive = protocol.build(windows={"train": {"start_utc": "2022-02-01T00:00:00",
                                              "end_utc": "2025-06-30T00:00:00"}},
                           parameters=REGISTRY, trial_count=1)
    with pytest.raises(ProtocolViolation, match="no timezone"):
        naive.window("train")


def test_an_undeclared_window_cannot_be_run_ad_hoc():
    with pytest.raises(ProtocolViolation, match="declares no window"):
        a_lock().window("whatever-fits")


def test_the_window_parses_to_milliseconds():
    start, end = a_lock().window("train")
    assert start < end
    assert start == 1643673600000        # 2022-02-01T00:00:00Z, hand-checked


def test_a_lock_file_cannot_be_silently_replaced(tmp_path):
    path = tmp_path / "protocol.lock.json"
    protocol.write(a_lock(), path)
    with pytest.raises(ProtocolViolation, match="already exists"):
        protocol.write(a_lock(), path)


def test_loading_a_missing_lock_says_pre_registration_comes_first(tmp_path):
    with pytest.raises(ProtocolViolation, match="no protocol lock"):
        protocol.load(tmp_path / "absent.json")


def test_a_lock_with_unknown_fields_is_not_treated_as_satisfied(tmp_path):
    """A lock this code cannot fully interpret must not be read as a pre-registration."""
    import json

    path = tmp_path / "protocol.lock.json"
    raw = a_lock().to_dict()
    raw["escape_hatch"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProtocolViolation, match="unknown field"):
        protocol.load(path)


def test_amending_requires_a_reason_and_bumps_the_version():
    lock = a_lock()
    with pytest.raises(ProtocolViolation, match="requires a stated reason"):
        protocol.amend(lock, reason="   ", after_results=False)

    amended = protocol.amend(lock, reason="the slippage assumption was wrong",
                             after_results=False)
    assert amended.protocol_version == lock.protocol_version + 1
    assert amended.previous_sha256 == lock.sha256
    assert amended.sha256 == amended.digest()
    assert amended.amended_after_results is False


def test_amending_after_seeing_results_records_that_the_holdout_is_burned():
    """specs/09 §1.3 calls this the intended consequence, not a penalty to be worked around — so the
    flag is a required argument rather than an inferred default."""
    amended = protocol.amend(a_lock(), reason="we did not like the answer", after_results=True)
    assert amended.amended_after_results is True
    assert amended.amendment_reason == "we did not like the answer"
    checked = protocol.verify(amended, parameter_hash=REGISTRY.parameter_hash)
    assert checked["amended_after_results"] is True


def test_the_locked_metric_definitions_name_the_formulas():
    """A metric that can be redefined after the fact and still look like the same metric is not
    pre-registered, so the formulas themselves go in the lock."""
    lock = a_lock()
    assert "r_multiple" in lock.metric_definitions
    assert "weighted across partial exits" in lock.metric_definitions["r_multiple"]
    assert "REJECTED as sufficient" in lock.metric_definitions["sharpe_per_trade"]
    assert "cut, NOT tuned" in lock.decision_rules["CUT"]
    assert {h["id"] for h in lock.hypotheses} == {"H1", "H2", "H3", "H4", "H5"}
    assert "the fill is the bar's OPEN" in lock.fill_model["stop"]
    assert "STOP is assumed" in lock.fill_model["intrabar_ambiguity"]


def test_the_committed_protocol_lock_matches_the_live_registry():
    """The real lock, if it exists: a run would refuse, so a test should say so first."""
    path = Path("validation/protocol.lock.json")
    if not path.exists():
        pytest.skip("run `python -m backtest --preregister` first")
    lock = protocol.load(path)
    checked = protocol.verify(lock, parameter_hash=REGISTRY.parameter_hash)
    assert checked["sha256"] == lock.sha256
    assert "holdout" in lock.window_boundaries, "the holdout's boundary must be pre-registered"
