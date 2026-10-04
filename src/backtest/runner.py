"""The backtest runner: drive the live evaluation chain over history and account for what happened.

The design rule that matters: **the harness does not contain a strategy, a feature, or a signal.**
It steps ``as_of`` with :class:`pipeline.drivers.BacktestDriver`, hands each snapshot to the *same*
:class:`app.runner.AppRunner` the live process uses, and then does the one thing live cannot — resolve
what the market did next. Every number a strategy produces comes from the production code path.

That is what makes the comparison meaningful. A harness with its own copy of a condition, a stop
rule, or an ACT computation grades a different system than the one that runs, and the divergence is
invisible precisely because both halves look right.

**Look-ahead is prevented structurally, not by care.** The snapshot is the boundary: the store
re-checks every ``as_of``-bounded read and raises :class:`core.errors.PointInTimeViolation` on a row
newer than the cut (specs/11 §8). The resolver reads bars *after* the snapshot's ``as_of``, which is
exactly what a backtest must do — so it reads them through :meth:`_future_bars`, the only place in
this module that touches the future, kept small enough to audit in one sitting.

**One thing live cannot do and this must: fill.** The fill model is :mod:`backtest.fills`, and the
rules there are unconditional by design so they cannot be softened on the bars where it matters.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.runner import AppRunner
from backtest.accounting import Ledger, Leg, Trade
from backtest.fills import CostModel, Fill, FillKind, entry_fill, mark_to_market, resolve_bar
from backtest.metrics import portfolio_report, strategy_report
from backtest.protocol import ProtocolLock, verify
from core.errors import ALL_ERRORS, BacktestError, CryptoTraderError, SetupNotReady
from core.logging import get_logger
from core.version import code_version
from normalize.canonical import Bar, Timeframe
from pipeline.drivers import BacktestDriver
from strategy.base import Lifecycle

log = get_logger("backtest.runner")
COMPONENT = "backtest.runner"

#: The timeframe fills are resolved on. 1m because that is what the trigger confirms on (specs/03 §2)
#: and because a coarser resolution would hide the gap events specs/09 §3.2 is about — a 1h bar
#: containing both a stop and a target tells you nothing about which came first.
RESOLVE_TF = Timeframe.M1

#: How far past a trigger the resolver will look for an exit before giving up and marking to market.
#: Bounded so one unresolved position cannot read the rest of history looking for an exit.
MAX_RESOLVE_BARS = 60 * 24 * 14          # fourteen days of 1m bars


@dataclass
class OpenPosition:
    """A triggered position the resolver is tracking forward."""

    trade: Trade
    stop: Decimal
    target: Decimal | None
    trail: Decimal | None = None
    #: Index into the resolution series where the search for an exit continues.
    cursor: int = 0


@dataclass
class BacktestResult:
    """One run: the ledger, the per-strategy reports, and the provenance to reproduce it."""

    window_name: str
    start_ms: int
    end_ms: int
    ledger: Ledger
    protocol: dict[str, Any]
    costs: dict[str, Any]
    steps: int = 0
    step_failures: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0

    def reports(self, strategies: Sequence[Any]) -> dict[str, Any]:
        """Per-strategy and portfolio reports (specs/09 §5, §6)."""
        out: dict[str, Any] = {}
        for s in strategies:
            trades = self.ledger.for_strategy(s.id)
            out[s.id] = strategy_report(trades, strategy_id=s.id,
                                        short_gamma=(s.id == "C4-C"))
        return {"by_strategy": out, "portfolio": portfolio_report(self.ledger.closed)}

    def to_dict(self, strategies: Sequence[Any]) -> dict[str, Any]:
        return {
            "window": {"name": self.window_name, "start_ms": self.start_ms, "end_ms": self.end_ms},
            "steps": self.steps, "step_failures": dict(self.step_failures),
            "elapsed_ms": self.elapsed_ms,
            "code_version": code_version(),
            "protocol": dict(self.protocol),
            "costs": dict(self.costs),
            "ledger": self.ledger.to_dict(),
            "reports": self.reports(strategies),
        }


@dataclass
class BacktestRunner:
    """Steps history through the live evaluation chain and resolves the fills."""

    runner: AppRunner
    driver: BacktestDriver
    costs: CostModel
    lock: ProtocolLock
    window_name: str = "train"
    #: Resolution bars, loaded once per asset. Held here rather than re-read per step because the
    #: resolver walks forward monotonically and re-querying would dominate the run time.
    _bars: dict[str, list[Bar]] = field(default_factory=dict, init=False)
    _open: dict[str, OpenPosition] = field(default_factory=dict, init=False)

    def run(self) -> BacktestResult:
        started = time.perf_counter()
        checked = verify(self.lock, parameter_hash=self.runner.engine.parameter_hash)
        log.info("backtest_started", window=self.window_name,
                 protocol_version=checked["protocol_version"], sha256=checked["sha256"][:12],
                 start_ms=self.driver.start_ms, end_ms=self.driver.end_ms,
                 steps=len(self.driver.as_of_grid()), costs=self.costs.to_dict())

        ledger = Ledger()
        failures: dict[str, int] = {}
        self._load_resolution_bars()

        steps = 0
        for snapshot in self.driver.snapshots():
            steps += 1
            try:
                result = self.runner.evaluate_snapshot(snapshot)
            except SetupNotReady as e:
                failures[e.code] = failures.get(e.code, 0) + 1
                continue
            except ALL_ERRORS as e:
                # A step's failure is counted and the run continues, but it is counted *by code* so a
                # run that failed half its steps cannot be mistaken for a run that found no setups.
                failures[e.code] = failures.get(e.code, 0) + 1
                log.warning("backtest_step_failed", as_of_ms=snapshot.as_of_ms, error=e.to_dict())
                continue
            self._collect(result, ledger)
            self._advance_open(snapshot.as_of_ms, ledger)

        self._close_remaining(ledger)
        failed = sum(failures.values())
        if steps and failed / steps > 0.5:
            raise BacktestError(
                f"{failed} of {steps} steps failed; the run is not a result",
                component=COMPONENT, operation="run",
                context={"steps": steps, "failures": dict(failures),
                         "window": self.window_name},
                likely_cause="the window predates the stored history, or a required series is "
                             "missing for most of it",
                remediation="a backtest over mostly-failed steps measures the gaps, not the "
                            "strategies. Check coverage with BacktestDriver.coverage() first",
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        log.info("backtest_finished", window=self.window_name, steps=steps,
                 trades=len(ledger.trades), closed=len(ledger.closed),
                 non_trades=ledger.non_trades.total, failures=failed, ms=elapsed)
        return BacktestResult(window_name=self.window_name, start_ms=self.driver.start_ms,
                              end_ms=self.driver.end_ms, ledger=ledger, protocol=checked,
                              costs=self.costs.to_dict(), steps=steps, step_failures=failures,
                              elapsed_ms=elapsed)

    # ---- the future, in one place --------------------------------------------------------

    def _load_resolution_bars(self) -> None:
        """Load the 1m series once per asset, spanning the window plus the resolution horizon.

        The **only** place this module reads data later than a snapshot's ``as_of``, which is
        deliberate: a backtest must look forward to resolve a fill, and confining that to one method
        is what makes the look-ahead audit of specs/09 §9 a reading rather than an investigation.
        """
        horizon = MAX_RESOLVE_BARS * RESOLVE_TF.ms
        for asset in self.runner.assets:
            bars = self.driver.builder.store.get_bars(
                asset, self.runner.product, RESOLVE_TF,
                self.driver.start_ms, self.driver.end_ms + horizon, final_only=True)
            self._bars[asset] = list(bars)
            log.info("resolution_bars_loaded", asset=asset, timeframe=RESOLVE_TF.value,
                     bars=len(self._bars[asset]))

    def _future_bars(self, asset: str, after_ms: int) -> tuple[list[Bar], int]:
        """``(series, index of the first bar strictly after ``after_ms``)``."""
        series = self._bars.get(asset, [])
        lo, hi = 0, len(series)
        while lo < hi:
            mid = (lo + hi) // 2
            if series[mid].open_time_ms <= after_ms:
                lo = mid + 1
            else:
                hi = mid
        return series, lo

    # ---- collecting what the evaluation produced -----------------------------------------

    def _collect(self, result: Any, ledger: Ledger) -> None:
        """Turn one evaluation's outcomes into trades and non-trades."""
        for asset, asset_result in result.assets.items():
            for strategy_id, outcome in asset_result.outcomes.items():
                state = outcome.state
                if state is None:
                    continue
                if state.lifecycle in (Lifecycle.EXPIRED, Lifecycle.INVALIDATED, Lifecycle.MISSED):
                    kind = {Lifecycle.EXPIRED: "expired", Lifecycle.INVALIDATED: "invalidated",
                            Lifecycle.MISSED: "missed"}[state.lifecycle]
                    # A handoff terminates the setup but is a *success* and the position carries on,
                    # so it must not be counted as a non-trade.
                    if kind == "invalidated" and "HANDOFF" in (outcome.reason or ""):
                        continue
                    ledger.non_trades.record(kind, strategy_id)
                    continue
                if state.lifecycle is Lifecycle.TRIGGERED and state.signal_id not in self._open:
                    self._open_trade(asset, strategy_id, outcome, ledger)

        if result.arbitration is not None:
            for signal, reason in result.arbitration.suppressed:
                kind = "vetoed" if not signal.approved else "suppressed"
                ledger.non_trades.record(kind, signal.raw.strategy_id)
                log.debug("non_trade", kind=kind, strategy=signal.raw.strategy_id, reason=reason)

    def _open_trade(self, asset: str, strategy_id: str, outcome: Any, ledger: Ledger) -> None:
        """Fill the entry and start tracking the position."""
        signal = outcome.signal
        state = outcome.state
        if signal is None or signal.entry is None or signal.stop is None:
            return
        series, idx = self._future_bars(asset, state.triggered_at_ms or state.armed_at_ms)
        next_bar = series[idx] if idx < len(series) else None
        try:
            fill = entry_fill(trigger_price=signal.entry, next_bar=next_bar,
                              direction=signal.direction, costs=self.costs)
        except CryptoTraderError:
            # No bar follows the trigger: the trade never opened. Counted as its own category so it
            # is read as a data boundary rather than as a strategy outcome.
            ledger.non_trades.record("no_following_bar", strategy_id)
            return
        risk = abs(fill.price - signal.stop)
        if risk <= 0:
            ledger.non_trades.record("vetoed", strategy_id)
            log.warning("zero_risk_entry", strategy=strategy_id, asset=asset,
                        entry=str(fill.price), stop=str(signal.stop),
                        detail="the entry filled at the stop, so R would be zero; not a trade")
            return
        trade = Trade(
            trade_id=f"{state.signal_id}",
            asset=asset, direction=signal.direction,
            originating_strategy_id=strategy_id,
            strategy_class=getattr(outcome, "strategy_class", 0) or self._class_of(strategy_id),
            entry=fill, initial_risk=risk, initial_stop=signal.stop,
            target=signal.targets[0] if signal.targets else None,
            legs=[Leg(strategy_id=strategy_id, strategy_version=signal.strategy_version,
                      entered_at_ms=fill.at_ms, from_price=fill.price)],
            regime_cell=(signal.regime or {}).get("cell_label"),
            parameter_hash=signal.parameter_hash, code_version=signal.code_version,
            signal_id=state.signal_id,
        )
        ledger.add(trade)
        self._open[state.signal_id] = OpenPosition(
            trade=trade, stop=signal.stop,
            target=signal.targets[0] if signal.targets else None,
            cursor=idx + 1)

    def _class_of(self, strategy_id: str) -> int:
        for s in self.runner.strategies:
            if s.id == strategy_id:
                return int(s.strategy_class)
        return 0

    # ---- resolving open positions ---------------------------------------------------------

    def _advance_open(self, as_of_ms: int, ledger: Ledger) -> None:
        """Walk each open position forward to this ``as_of``, looking for an exit.

        Resolved bar by bar rather than by scanning for the extreme, because the stop-first rule only
        means anything if the bars are visited in order: a scan that found the target's bar first
        would silently invert the conservative assumption.
        """
        for signal_id, position in list(self._open.items()):
            series = self._bars.get(position.trade.asset, [])
            fill: Fill | None = None
            while position.cursor < len(series):
                bar = series[position.cursor]
                if bar.open_time_ms > as_of_ms:
                    break
                stop = position.trail if position.trail is not None else position.stop
                kind = FillKind.TRAIL if position.trail is not None else FillKind.STOP
                fill = resolve_bar(bar=bar, direction=position.trade.direction, stop=stop,
                                   target=position.target, costs=self.costs, stop_kind=kind)
                position.cursor += 1
                if fill is not None:
                    break
            if fill is not None:
                self._close(signal_id, position, fill, ledger)
            elif position.cursor - 1 >= 0 and position.cursor > MAX_RESOLVE_BARS:
                log.warning("position_unresolved", signal_id=signal_id,
                            asset=position.trade.asset, bars=position.cursor,
                            detail="no exit within the resolution horizon; marked to market and "
                                   "excluded from realised statistics")
                self._mark(signal_id, position, series, ledger)

    def _close(self, signal_id: str, position: OpenPosition, fill: Fill, ledger: Ledger) -> None:
        trade = position.trade
        trade.exit = fill
        if trade.legs:
            trade.legs[-1].exited_at_ms = fill.at_ms
            trade.legs[-1].to_price = fill.price
        self._open.pop(signal_id, None)
        log.debug("trade_closed", trade_id=trade.trade_id, kind=fill.kind.value,
                  r=str(trade.r_multiple), gapped=fill.gapped)

    def _mark(self, signal_id: str, position: OpenPosition, series: Sequence[Bar],
              ledger: Ledger) -> None:
        if not series:
            self._open.pop(signal_id, None)
            return
        last = series[min(position.cursor, len(series) - 1)]
        position.trade.exit = mark_to_market(bar=last, direction=position.trade.direction)
        position.trade.open_at_window_end = True
        self._open.pop(signal_id, None)

    def _close_remaining(self, ledger: Ledger) -> None:
        """Mark anything still open at the end of the window. Never counted as a realised exit."""
        for signal_id, position in list(self._open.items()):
            self._mark(signal_id, position, self._bars.get(position.trade.asset, []), ledger)


def sensitivity_costs(costs: CostModel) -> CostModel:
    """The 2× slippage re-run required by specs/09 §3.4.

    > "A strategy whose edge disappears at double slippage is not robust enough to deploy, because
    > the slippage estimate itself carries error."
    """
    from backtest.fills import SENSITIVITY_MULTIPLIER

    return CostModel(slippage_fraction=costs.slippage_fraction,
                     slippage_multiplier=costs.slippage_multiplier * SENSITIVITY_MULTIPLIER)
