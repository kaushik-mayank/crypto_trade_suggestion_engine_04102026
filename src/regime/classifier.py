"""Applying the fitted regime model at runtime (specs/12 §9.1, §9.3).

The live engine **loads and applies**; it never refits. It also never smooths or centres: bar *t* is
classified from the trailing quantiles of the bars before it, which is the same rule the fit used, so
a live label and a backtest label for the same bar agree by construction.

Dwell is tracked here rather than stored in the model, because dwell is a property of the *run*, not
of the artifact: a fresh process starts with ``dwell_bars = 1`` and says so, instead of claiming a
stability it has not observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from core.errors import ProblemClass, RegimeError
from core.health import ComponentHealth, SystemStatus
from core.logging import get_logger
from features.value import FeatureSet, FeatureStatus
from normalize.canonical import Timeframe
from regime.model import AXIS_P, AXIS_V, RegimeModel, RegimeState, cell_label, classify, quantile_edges

log = get_logger("regime.classifier")
COMPONENT = "regime.classifier"

#: The timeframe the regime is classified on (specs/02 §6: the classifying timeframe for dwell).
CLASSIFY_TF = Timeframe.H1


@dataclass
class _Track:
    """Per-asset dwell tracking. Not persisted: a restart honestly reports a dwell of 1."""

    cell: tuple[int, int] | None = None
    dwell_bars: int = 0
    entered_at_ms: int = 0
    last_bar_ms: int = 0


@dataclass
class RegimeClassifier:
    """Classifies one asset per call, keeping dwell across calls.

    ``axis_history`` supplies the trailing axis samples the quantiles come from. It is passed in rather
    than fetched, for the same reason the strategy engine takes a snapshot: the classifier has no store
    handle and cannot reach past ``as_of``.
    """

    model: RegimeModel
    #: How many trailing axis observations the quantiles need; from the model, so a model fitted on 720
    #: is never applied with a different window.
    _tracks: dict[str, _Track] = field(default_factory=dict)
    classifications: int = 0
    unclassified: int = 0

    @property
    def quantile_lookback(self) -> int:
        return self.model.quantile_lookback

    def classify(self, asset: str, features: FeatureSet, *,
                 axis_history: dict[str, np.ndarray] | None = None) -> RegimeState:
        """The regime state for ``asset`` at this snapshot's ``as_of``.

        Returns an unclassified state — with the reason — when either axis is unavailable or the
        trailing sample is too short. A regime label is never guessed: specs/02 §6 makes it a gate, and
        a gate that defaults to "open" when its input is missing is not a gate.
        """
        as_of = features.as_of_ms
        v = features.get(AXIS_V, CLASSIFY_TF)
        p = features.get(AXIS_P, CLASSIFY_TF, "short")
        for name, fv in ((AXIS_V, v), (AXIS_P, p)):
            if fv.status is not FeatureStatus.OK or fv.value is None:
                self.unclassified += 1
                return self._unclassified(
                    asset, as_of,
                    f"axis {name} is {fv.status.value}: {fv.reason}")

        axis_values = {"vol_ratio": float(v.value), "mr_vr": float(p.value)}  # type: ignore[arg-type]

        if self.model.degraded:
            # specs/12 §10: no cell, no dwell stability, no persistence probability -- but the raw axis
            # values are published, because every raw-axis threshold in the class specs still applies.
            return RegimeState(
                cell=None, cell_label="degraded (raw axes only)", dwell_bars=0, entered_at_ms=0,
                as_of_ms=as_of, axis_values=axis_values, axis_boundaries={},
                model_version=self.model.model_version, grid=self.model.grid, degraded=True,
                reason="the discretised regime model failed its validity tests, so cell-based "
                       "conditions are dropped and raw-axis thresholds still apply "
                       f"(failed: {', '.join(self.model.failed_tests) or 'see the artifact'})")

        history = axis_history or {}
        try:
            v_edges = quantile_edges(self._history(history, AXIS_V, asset), self.model.grid)
            p_edges = quantile_edges(self._history(history, AXIS_P, asset), self.model.grid)
        except RegimeError as e:
            self.unclassified += 1
            return self._unclassified(asset, as_of, e.message, axis_values=axis_values)

        cell = (classify(axis_values["vol_ratio"], v_edges), classify(axis_values["mr_vr"], p_edges))
        track = self._tracks.setdefault(asset, _Track())
        if track.cell != cell:
            track.cell, track.dwell_bars, track.entered_at_ms = cell, 1, as_of
        elif as_of != track.last_bar_ms:
            track.dwell_bars += 1
        track.last_bar_ms = as_of
        self.classifications += 1

        key = f"{cell[0]},{cell[1]}"
        return RegimeState(
            cell=cell, cell_label=cell_label(cell, self.model.grid), dwell_bars=track.dwell_bars,
            entered_at_ms=track.entered_at_ms, as_of_ms=as_of, axis_values=axis_values,
            axis_boundaries={"vol_ratio": v_edges, "mr_vr": p_edges},
            model_version=self.model.model_version, grid=self.model.grid, degraded=False,
            persistence_prob=self.model.persistence_prob(cell),
            median_dwell=self.model.median_dwell(cell),
            occupancy=self.model.occupancy.get(key))

    def _history(self, history: dict[str, np.ndarray], axis: str, asset: str) -> np.ndarray:
        series = history.get(axis)
        if series is None:
            raise RegimeError(
                f"no trailing sample for axis {axis} was supplied",
                component=COMPONENT, operation="classify",
                context={"asset": asset, "axis": axis, "supplied": sorted(history)},
                likely_cause="the caller did not build the axis history the quantiles are cut from",
                remediation="pass axis_history={AXIS_V: ..., AXIS_P: ...}; the classifier has no store "
                            "handle and cannot fetch it",
            )
        if series.size < self.quantile_lookback:
            raise RegimeError(
                f"axis {axis} has {series.size} trailing observations, the model was fitted with "
                f"{self.quantile_lookback}",
                component=COMPONENT, operation="classify",
                context={"axis": axis, "have": int(series.size),
                         "need": self.quantile_lookback, "asset": asset},
                likely_cause="the classifier is running before the axis has warmed to the model's "
                             "quantile lookback",
                remediation="wait for warm-up; classifying on a shorter sample would cut the buckets "
                            "differently than the model was validated with",
            )
        return series[-self.quantile_lookback:]

    def _unclassified(self, asset: str, as_of: int, reason: str,
                      axis_values: dict[str, float] | None = None) -> RegimeState:
        log.debug("regime_unclassified", asset=asset, as_of_ms=as_of, reason=reason)
        return RegimeState(
            cell=None, cell_label="unclassified", dwell_bars=0, entered_at_ms=0, as_of_ms=as_of,
            axis_values=axis_values or {}, axis_boundaries={},
            model_version=self.model.model_version, grid=self.model.grid,
            degraded=self.model.degraded, reason=reason)

    def health(self) -> ComponentHealth:
        if self.model.degraded:
            return ComponentHealth(
                name="regime", status=SystemStatus.DEGRADED,
                detail="the regime model failed its validity tests; running on raw axes with the "
                       "dwell-stability conditions dropped",
                problem_class=ProblemClass.MODELLING,
                context={"model_version": self.model.model_version,
                         "failed_tests": list(self.model.failed_tests)},
                remediation="this is a finding about the market, not a fault to fix: strategies run "
                            "with reduced selectivity (specs/12 §10)")
        total = self.classifications + self.unclassified
        if total == 0:
            return ComponentHealth(name="regime", status=SystemStatus.WARMING,
                                   detail="no classification yet")
        return ComponentHealth.healthy(
            "regime", detail=f"{self.classifications} classified, {self.unclassified} unclassified",
            model_version=self.model.model_version, grid=self.model.grid)

    def stats(self) -> dict[str, Any]:
        return {"model_version": self.model.model_version, "grid": self.model.grid,
                "degraded": self.model.degraded, "classified": self.classifications,
                "unclassified": self.unclassified,
                "tracked_assets": {a: {"cell": list(t.cell) if t.cell else None,
                                       "dwell_bars": t.dwell_bars}
                                   for a, t in self._tracks.items()}}
