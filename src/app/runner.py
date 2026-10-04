"""The evaluation runner: snapshot -> features -> regime -> strategies -> risk -> arbitration.

One pass per asset per evaluation, with every stage's refusal preserved. The runner is the only place
that holds the store, the clock and the strategy state; everything downstream of the snapshot is a pure
function of it, which is what makes a replay possible (specs/10 §2).

**Startup is deliberately loud.** :meth:`AppRunner.preflight` checks the parameter registry, the
snapshot request's depth, every registered feature's inputs and the regime artifacts *before* the first
evaluation, and raises a single :class:`StartupError` naming everything wrong at once. The alternative —
discovering a shallow request as a wall of WARMING features an hour later — trains an operator to ignore
the status that also means "the venue is down".

**One asset's failure never silences another.** Each asset is evaluated inside its own try, and a
failure is recorded against that asset with its classification. A snapshot-level failure, by contrast,
stops the pass: if the snapshot itself could not be built there is nothing to evaluate, and pretending
otherwise would publish signals from a market nobody observed.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from arbitration.pipeline import ArbitrationResult, arbitrate, expose_portfolio
from core.errors import ALL_ERRORS, CryptoTraderError, SetupNotReady, StartupError
from core.health import ComponentHealth, HealthReport, SystemStatus
from core.logging import get_logger
from features.engine import FeatureEngine
from features.value import FeatureSet
from normalize.canonical import Product, Timeframe
from regime.classifier import CLASSIFY_TF, RegimeClassifier
from regime.model import AXIS_P, AXIS_V, RegimeModel, RegimeState
from risk.engine import RiskEngine, Signal
from snapshot.builder import SnapshotBuilder, SnapshotRequest
from snapshot.models import MarketSnapshot, SeriesKey
from strategy.base import SetupState, StrategyOutcome
from strategy.market_view import MarketView, requires_view

log = get_logger("app.runner")
COMPONENT = "app.runner"


@dataclass
class AssetResult:
    """Everything one asset's evaluation produced, including why it produced nothing."""

    asset: str
    features: FeatureSet | None = None
    regime: RegimeState | None = None
    outcomes: dict[str, StrategyOutcome] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    error: CryptoTraderError | None = None
    #: Failures attributable to one strategy rather than to the asset's data. Kept separate because a
    #: broken strategy must not make a tradeable market look unreadable.
    strategy_errors: dict[str, CryptoTraderError] = field(default_factory=dict)
    #: Strategies that could not proceed because their inputs are warming or holed. **Not failures**
    #: -- a separate bucket so health can say "waiting for data" without saying "something is wrong",
    #: which are different sentences and lead to different actions on a host.
    strategy_gates: dict[str, CryptoTraderError] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset, "ok": self.ok, "elapsed_ms": self.elapsed_ms,
            "features": ({"computed": len(self.features), "ok": self.features.ok_count,
                          "statuses": self.features.status_counts()} if self.features else None),
            "regime": self.regime.to_dict() if self.regime else None,
            "outcomes": {sid: o.to_dict() for sid, o in self.outcomes.items()},
            "signals": [s.to_dict() for s in self.signals],
            "error": self.error.to_dict() if self.error else None,
            "strategy_errors": {k: v.to_dict() for k, v in self.strategy_errors.items()},
            "strategy_gates": {k: v.to_dict() for k, v in self.strategy_gates.items()},
        }


@dataclass
class EvaluationResult:
    """One full pass: the snapshot, every asset, and what arbitration published."""

    as_of_ms: int
    snapshot: MarketSnapshot | None
    assets: dict[str, AssetResult] = field(default_factory=dict)
    arbitration: ArbitrationResult | None = None
    portfolio: dict[str, Any] = field(default_factory=dict)
    error: CryptoTraderError | None = None
    elapsed_ms: int = 0

    @property
    def published(self) -> tuple[Signal, ...]:
        """Signals that survived risk and arbitration, filtered to tradeable ones.

        The ``tradeable`` filter is the API's firewall against the control strategies, applied here as
        well so a control cannot reach a caller that forgets (specs/13 §13).
        """
        if self.arbitration is None:
            return ()
        return tuple(s for s in self.arbitration.published if s.tradeable)

    def to_dict(self) -> dict[str, Any]:
        return {"as_of_ms": self.as_of_ms, "elapsed_ms": self.elapsed_ms,
                "snapshot": self.snapshot.to_dict() if self.snapshot else None,
                "assets": {a: r.to_dict() for a, r in self.assets.items()},
                "arbitration": self.arbitration.to_dict() if self.arbitration else None,
                "portfolio": dict(self.portfolio),
                "published": [s.raw.signal_id for s in self.published],
                "error": self.error.to_dict() if self.error else None}


@dataclass
class AppRunner:
    """Drives one evaluation pass. Holds the store handle; nothing downstream of the snapshot does."""

    builder: SnapshotBuilder
    engine: FeatureEngine
    request: SnapshotRequest
    strategies: Sequence[Any]
    risk: RiskEngine
    #: One classifier per asset: the regime model is fitted per asset and two of three are degraded
    #: (F-033), so a single shared classifier would apply one asset's verdict to another.
    classifiers: dict[str, RegimeClassifier] = field(default_factory=dict)
    assets: tuple[str, ...] = ()
    product: Product = Product.FUTURES
    #: Armed setups, keyed by (strategy_id, asset). In live these are persisted; in backtest they live
    #: here and reset per run (specs/13 §2).
    states: dict[tuple[str, str], SetupState] = field(default_factory=dict)
    evaluations: int = 0
    failures: int = 0
    last_error: CryptoTraderError | None = None

    # ---- startup -------------------------------------------------------------------------

    def preflight(self) -> list[str]:
        """Check everything checkable before the first evaluation. Raises once, naming all problems."""
        problems: list[str] = []
        try:
            self.engine.assert_request_sufficient(self.request)
        except StartupError as e:
            problems.extend(e.context.get("problems", [e.message]))

        requested = {k.asset for k in self.request.keys}
        for asset in self.assets:
            if asset not in requested:
                problems.append(f"{asset}: no series requested for it at all")
            if asset not in self.classifiers:
                problems.append(f"{asset}: no regime model loaded (fit one, or load the artifact from "
                                f"config/regime_model_{asset}.json)")
        for key in (SeriesKey(a, self.product, tf) for a in self.assets
                    for tf in (Timeframe.M1, Timeframe.M15)):
            if key not in set(self.request.keys):
                problems.append(f"{key}: the trigger and confirmation timeframes must be requested — "
                                "ACT confirms on a 1m close and sizes its unit on 15m")
        for s in self.strategies:
            if getattr(s, "tradeable", True) is False:
                problems.append(f"{s.id} is a control strategy and must not be in a live runner "
                                "(specs/13 §13)")
            if not getattr(s, "params", None):
                problems.append(f"{s.id} was constructed without parameters")

        if problems:
            raise StartupError(
                f"the runner cannot start: {len(problems)} problem(s)",
                component=COMPONENT, operation="preflight",
                context={"problems": problems},
                likely_cause="the request, the strategies and the feature registry disagree about what "
                             "this run needs",
                remediation="fix every problem listed; starting with any of them would produce signals "
                            "from inputs the system cannot justify",
            )
        log.info("preflight_passed", assets=list(self.assets),
                 strategies=[s.id for s in self.strategies],
                 series=len(self.request.series),
                 degraded_regimes=[a for a, c in self.classifiers.items() if c.model.degraded])
        return problems

    # ---- one pass ------------------------------------------------------------------------

    def evaluate(self, as_of_ms: int, *, dq: dict[str, Any] | None = None) -> EvaluationResult:
        started = time.perf_counter()
        result = EvaluationResult(as_of_ms=as_of_ms, snapshot=None)
        try:
            snapshot = self.builder.build(self.request, as_of_ms, built_by="live", dq=dq)
        except ALL_ERRORS as e:
            # A snapshot-level failure stops the pass: with no snapshot there is nothing to evaluate,
            # and publishing signals from a market nobody observed would be worse than publishing none.
            self.failures += 1
            self.last_error = e
            log.error("snapshot_failed", as_of_ms=as_of_ms, error=e.to_dict())
            result.error = e
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return result

        return self.evaluate_snapshot(snapshot, result=result, started=started)

    def evaluate_snapshot(self, snapshot: MarketSnapshot, *,
                          result: EvaluationResult | None = None,
                          started: float | None = None) -> EvaluationResult:
        """Evaluate a snapshot the caller already holds.

        Split out so the backtest harness can feed snapshots from
        :class:`pipeline.drivers.BacktestDriver` through **this** method rather than reimplementing
        the pass. specs/09 requires the harness to exercise the production path: a harness with its
        own copy of the two-pass ordering, the market view, or the arbitration call would grade a
        different system than the one that runs, and the divergence would be invisible because both
        halves look right.
        """
        started = time.perf_counter() if started is None else started
        result = EvaluationResult(as_of_ms=snapshot.as_of_ms, snapshot=snapshot) \
            if result is None else result
        result.snapshot = snapshot
        as_of_ms = snapshot.as_of_ms

        # Pass 1: features and regime for every asset. Nothing is evaluated yet, because a
        # cross-asset strategy (C1-C's second leg, C2-B's expression choice) must compare peers at the
        # *same* as_of -- comparing an asset computed now against one computed later in the same loop
        # would be the timestamp-alignment error of F-023 wearing a different hat.
        for asset in self.assets:
            result.assets[asset] = self._measure_asset(asset, snapshot)

        view = self._market_view(snapshot, result)

        # Pass 2: evaluate strategies, with the view attached to those that declared they need it.
        all_signals: list[Signal] = []
        for asset in self.assets:
            asset_result = result.assets[asset]
            if asset_result.ok:
                self._evaluate_strategies(asset, snapshot, asset_result, view)
            all_signals.extend(asset_result.signals)

        result.arbitration = arbitrate(all_signals, at_ms=as_of_ms)
        result.portfolio = expose_portfolio(result.arbitration.published)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.evaluations += 1
        log.info("evaluation_complete", as_of_ms=as_of_ms, ms=result.elapsed_ms,
                 assets=len(result.assets),
                 signals=len(all_signals), published=len(result.published),
                 suppressed=len(result.arbitration.suppressed),
                 failed_assets=[a for a, r in result.assets.items() if not r.ok])
        return result

    def _measure_asset(self, asset: str, snapshot: MarketSnapshot) -> AssetResult:
        """Pass 1: features and regime only."""
        started = time.perf_counter()
        out = AssetResult(asset=asset)
        try:
            out.features = self.engine.compute(snapshot, asset, self.product)
            out.regime = self._classify(asset, out.features, snapshot)
        except ALL_ERRORS as e:
            # One asset's failure must not silence the others: a SOL data problem is not a reason to
            # stop reporting BTC.
            self.failures += 1
            self.last_error = e
            out.error = e
            log.error("asset_measurement_failed", asset=asset, error=e.to_dict())
        out.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return out

    def _market_view(self, snapshot: MarketSnapshot, result: EvaluationResult) -> MarketView:
        armed: dict[str, list[str]] = {}
        for (strategy_id, asset), state in self.states.items():
            if state.lifecycle.active:
                armed.setdefault(strategy_id, []).append(asset)
        return MarketView(
            as_of_ms=snapshot.as_of_ms,
            features={a: r.features for a, r in result.assets.items() if r.features is not None},
            regimes={a: r.regime for a, r in result.assets.items() if r.regime is not None},
            armed={k: tuple(v) for k, v in armed.items()})

    def _evaluate_strategies(self, asset: str, snapshot: MarketSnapshot, out: AssetResult,
                             view: MarketView) -> None:
        """Pass 2: run every strategy for one asset, isolating each strategy's failure.

        A strategy that raises is recorded against *itself*, not against the asset: one broken
        strategy must not remove the other strategies' output for a tradeable market.
        """
        features, regime = out.features, out.regime
        if features is None or regime is None:
            return
        instrument = snapshot.instruments.get((asset, self.product))  # type: ignore[call-overload]
        for strategy in self.strategies:
            key = (strategy.id, asset)
            try:
                if requires_view(strategy):
                    strategy.market_view = view
                state = self.states.get(key)
                outcome = strategy.evaluate(snapshot, features, regime, state)
                out.outcomes[strategy.id] = outcome
                self._remember(key, outcome)
                if outcome.signal is not None:
                    out.signals.append(self.risk.evaluate(
                        outcome.signal, snapshot=snapshot, features=features, regime=regime,
                        instrument=instrument, expansion_seeking=strategy.expansion_seeking,
                        strategy_class=strategy.strategy_class))
            except SetupNotReady as e:
                # A gate, not a failure. The strategy is fine and its inputs are not there yet --
                # warming history, a hole in the 1m series. Counting these as failures would mark a
                # fresh deployment DEGRADED on its first evaluation and, worse, bury a genuine bug
                # among dozens of warming notices. Recorded per strategy so it is still visible.
                out.strategy_gates[strategy.id] = e
                log.info("strategy_not_ready", asset=asset, strategy=strategy.id,
                         code=e.code, message=e.message)
            except ALL_ERRORS as e:
                self.failures += 1
                self.last_error = e
                out.strategy_errors[strategy.id] = e
                log.error("strategy_evaluation_failed", asset=asset, strategy=strategy.id,
                          error=e.to_dict())

    def _remember(self, key: tuple[str, str], outcome: StrategyOutcome) -> None:
        """Keep an armed setup; forget a terminal one, so it cannot be revived next pass."""
        if outcome.state is None:
            return
        if outcome.state.lifecycle.terminal:
            self.states.pop(key, None)
        else:
            self.states[key] = outcome.state

    def _classify(self, asset: str, features: FeatureSet, snapshot: MarketSnapshot) -> RegimeState:
        classifier = self.classifiers.get(asset)
        if classifier is None:
            raise StartupError(
                f"no regime classifier for {asset}",
                component=COMPONENT, operation="classify",
                context={"asset": asset, "loaded": sorted(self.classifiers)},
                likely_cause="preflight was skipped, or the artifact was not loaded for this asset",
                remediation="load a fitted regime model per asset; a regime gate cannot default to open",
            )
        history = self._axis_history(asset, snapshot, classifier.quantile_lookback)
        return classifier.classify(asset, features, axis_history=history)

    def _axis_history(self, asset: str, snapshot: MarketSnapshot,
                      lookback: int) -> dict[str, np.ndarray]:
        """The trailing axis samples the regime quantiles are cut from.

        Computed with the same vectorised implementations the live features use, so the boundaries a
        live classification uses and the ones the fit used come from one code path.
        """
        from features.f2_volatility import vol_cc_series
        from features.f4_deviation import _vr_series
        from features.windows import extract

        key = SeriesKey(asset, self.product, CLASSIFY_TF)
        state = snapshot.series.get(key)
        if state is None or len(state.bars) < 2:
            return {}
        need = min(len(state.bars) - 1, lookback + int(self.engine.params["lookback_1h_long"]))
        outcome = extract(state, lookback=need, extra_bars=1, min_bars=2)
        if outcome.window is None:
            return {}
        window = outcome.window
        short = int(self.engine.params["lookback_1h_short"])
        long = int(self.engine.params["lookback_1h_long"])
        vol_short, vol_long = vol_cc_series(window, short), vol_cc_series(window, long)
        with np.errstate(invalid="ignore", divide="ignore"):
            axis_v = np.where(vol_long > 0, vol_short / vol_long, np.nan)
        axis_p = _vr_series(2)(window, short)
        return {AXIS_V: axis_v[np.isfinite(axis_v)], AXIS_P: axis_p[np.isfinite(axis_p)]}

    # ---- health --------------------------------------------------------------------------

    def health(self) -> HealthReport:
        """One report, classified so the operator knows *where* to look (the whole point of
        :class:`core.errors.ProblemClass`)."""
        components: list[ComponentHealth] = []
        store_health = getattr(self.builder.store, "health", None)
        if callable(store_health):
            components.append(store_health())
        for asset, classifier in self.classifiers.items():
            h = classifier.health()
            components.append(ComponentHealth(name=f"regime:{asset}", status=h.status,
                                              detail=h.detail, problem_class=h.problem_class,
                                              context=h.context, remediation=h.remediation))
        # A failure outranks "not started yet": a runner whose every attempt has failed must not
        # report WARMING, which reads as "give it time" and hides the failure entirely.
        if self.last_error is not None:
            components.append(ComponentHealth(
                name="runner", status=SystemStatus.DEGRADED,
                detail=f"{self.failures} failure(s); last: {self.last_error.message}",
                problem_class=self.last_error.problem_class,
                context={"code": self.last_error.code, "component": self.last_error.component,
                         "successful_evaluations": self.evaluations},
                remediation=self.last_error.remediation))
        elif self.evaluations == 0:
            components.append(ComponentHealth(name="runner", status=SystemStatus.WARMING,
                                              detail="no evaluation completed yet"))
        else:
            components.append(ComponentHealth.healthy(
                "runner", detail=f"{self.evaluations} evaluation(s), no failures",
                strategies=len(self.strategies), armed_setups=len(self.states)))
        return HealthReport(components=components)


def load_classifiers(assets: Sequence[str], *, directory: str = "config") -> dict[str, RegimeClassifier]:
    """Load one fitted regime model per asset.

    A missing artifact raises rather than defaulting to a degraded model: "degraded because the market
    has no stable regime structure" and "degraded because nobody ran the fit" need different responses,
    and conflating them would hide the second forever.
    """
    from pathlib import Path

    out: dict[str, RegimeClassifier] = {}
    for asset in assets:
        path = Path(directory) / f"regime_model_{asset}.json"
        out[asset] = RegimeClassifier(model=RegimeModel.load(path))
    log.info("regime_models_loaded", assets=list(out),
             degraded=[a for a, c in out.items() if c.model.degraded],
             versions={a: c.model.model_version for a, c in out.items()})
    return out
