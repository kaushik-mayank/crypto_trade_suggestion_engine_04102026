"""The regime model: fitted offline, applied live (specs/02 §6, specs/12 §9).

Two axes, chosen because they are close to orthogonal by construction — one measures the *scale* of
moves (``VOL_RATIO``), the other their *serial dependence* (``MR_VR``). A third axis is prohibited
without demonstrating it is not a linear combination of these two.

Three design rules carried here:

**Trailing quantiles, never fixed thresholds.** Fixed cut points are the most common form of
accidental curve-fitting in regime models. Cell boundaries therefore move over time, which is correct
and must be recorded: :class:`RegimeState` carries the boundaries actually used, so a classification
is replayable.

**Fitted offline, loaded live.** The live engine applies the artifact and never refits. A silently
drifting regime model would make every regime-gated backtest result unreproducible, so refitting is a
deliberate, versioned event.

**A label with no statistics is not acceptable output** (specs/02 §6). Every state published carries
its cell, dwell, boundaries, occupancy and the model version that produced it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from core.errors import ConfigError, RegimeError
from core.logging import get_logger

log = get_logger("regime.model")
COMPONENT = "regime.model"

#: Axis definitions (specs/12 §9.1). The feature names are part of the artifact, so a model fitted on
#: one definition can never be applied to another without the version changing.
AXIS_V = "VOL_RATIO"
AXIS_P = "MR_VR_K2"

#: Human labels for a 3x3 grid, low to high on each axis.
V_LABELS_3 = ("contracting", "steady", "expanding")
P_LABELS_3 = ("mean-reverting", "random-walk", "trending")
V_LABELS_2 = ("contracting", "expanding")
P_LABELS_2 = ("mean-reverting", "trending")


def cell_label(cell: tuple[int, int], grid: int) -> str:
    v_labels, p_labels = (V_LABELS_3, P_LABELS_3) if grid == 3 else (V_LABELS_2, P_LABELS_2)
    v, p = cell
    if not (0 <= v < len(v_labels) and 0 <= p < len(p_labels)):
        raise RegimeError(
            f"cell {cell} is outside a {grid}x{grid} grid",
            component=COMPONENT, operation="cell_label",
            context={"cell": list(cell), "grid": grid},
            likely_cause="a state built for one grid size was labelled with another",
            remediation="labels come from the same artifact as the boundaries; do not mix versions",
        )
    return f"{v_labels[v]} / {p_labels[p]}"


@dataclass(frozen=True)
class ValidityResult:
    """One of the three tests of specs/02 §6, with the statistic that decided it."""

    name: str
    passed: bool
    statistic: float | None
    threshold: float | None
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "statistic": self.statistic,
                "threshold": self.threshold, "detail": self.detail, **self.extra}


@dataclass(frozen=True)
class RegimeModel:
    """The fitted artifact. Immutable, versioned, and serialisable to ``regime_model.json``."""

    model_version: str
    grid: int                                   # 3 or 2; 1 means the discretised model was abandoned
    fitted_on_window: tuple[int, int]
    quantile_lookback: int
    axis_definitions: dict[str, str]
    #: Transition counts between cells, as a nested map keyed by "v,p" (JSON-safe).
    transition_matrix: dict[str, dict[str, float]]
    dwell_statistics: dict[str, float]          # median dwell per cell
    occupancy: dict[str, float]                 # fraction of observations per cell
    validity: tuple[ValidityResult, ...]
    #: True when all three tests passed at this granularity. False means **degraded mode**: the raw
    #: axis thresholds still apply, the dwell-stability conditions do not (specs/12 §10).
    usable: bool = False
    notes: str = ""
    asset: str = ""
    #: Forward horizon the separation test used, in bars. Part of the artifact because a separation
    #: verdict means nothing without it (F-033).
    separation_horizon_bars: int = 24

    @property
    def degraded(self) -> bool:
        return not self.usable

    @property
    def failed_tests(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.validity if not v.passed)

    def median_dwell(self, cell: tuple[int, int]) -> float | None:
        return self.dwell_statistics.get(f"{cell[0]},{cell[1]}")

    def persistence_prob(self, cell: tuple[int, int]) -> float | None:
        """P(stay in this cell next bar), from the transition matrix.

        ``None`` in degraded mode: specs/05 §4 requires C3-B to publish
        ``regime_persistence_prob: null`` with a reason rather than a number the model cannot support.
        It is a **regime statistic, never a trade probability**.
        """
        if self.degraded:
            return None
        key = f"{cell[0]},{cell[1]}"
        row = self.transition_matrix.get(key)
        if not row:
            return None
        total = sum(row.values())
        return (row.get(key, 0.0) / total) if total > 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version, "grid": self.grid, "asset": self.asset,
            "fitted_on_window": list(self.fitted_on_window),
            "quantile_lookback": self.quantile_lookback,
            "axis_definitions": dict(self.axis_definitions),
            "transition_matrix": {k: dict(v) for k, v in self.transition_matrix.items()},
            "dwell_statistics": dict(self.dwell_statistics),
            "occupancy": dict(self.occupancy),
            "validity_test_results": [v.to_dict() for v in self.validity],
            "separation_horizon_bars": self.separation_horizon_bars,
            "usable": self.usable, "degraded": self.degraded,
            "failed_tests": list(self.failed_tests), "notes": self.notes,
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
        log.info("regime_model_saved", path=str(p), version=self.model_version, grid=self.grid,
                 usable=self.usable, failed=list(self.failed_tests))
        return p

    @classmethod
    def load(cls, path: str | Path) -> RegimeModel:
        p = Path(path)
        if not p.exists():
            raise ConfigError(
                f"no regime model at {p}",
                component=COMPONENT, operation="load",
                context={"path": str(p)},
                likely_cause="the offline fit has not been run, or the artifact was not deployed",
                remediation="run the regime fit (PHASE-14) and ship regime_model.json; the live engine "
                            "never fits its own model",
            )
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(
                f"regime model at {p} is not valid JSON",
                component=COMPONENT, operation="load",
                context={"path": str(p), "error": str(e)},
                likely_cause="a truncated write, or a hand edit",
                remediation="re-fit the model; the writer is atomic, so a truncated file means the "
                            "file was replaced by something else",
                cause=e,
            ) from e
        missing = [k for k in ("model_version", "grid", "axis_definitions", "occupancy") if k not in raw]
        if missing:
            raise ConfigError(
                f"regime model at {p} is missing {', '.join(missing)}",
                component=COMPONENT, operation="load",
                context={"path": str(p), "missing": missing, "present": sorted(raw)},
                likely_cause="the artifact was written by an older, incompatible fitter",
                remediation="re-fit with the current code; do not hand-repair the artifact",
            )
        if raw["axis_definitions"].get("V") != AXIS_V or raw["axis_definitions"].get("P") != AXIS_P:
            raise ConfigError(
                f"regime model at {p} was fitted on different axes: {raw['axis_definitions']}",
                component=COMPONENT, operation="load",
                context={"expected": {"V": AXIS_V, "P": AXIS_P},
                         "found": raw["axis_definitions"]},
                likely_cause="the axis features were renamed or the model predates them",
                remediation="re-fit; applying a model across an axis change would classify on a "
                            "different quantity than it was validated on",
            )
        return cls(
            model_version=raw["model_version"], grid=int(raw["grid"]),
            fitted_on_window=(int(raw.get("fitted_on_window", (0, 0))[0]),
                              int(raw.get("fitted_on_window", (0, 0))[1])),
            quantile_lookback=int(raw.get("quantile_lookback", 0)),
            axis_definitions=dict(raw["axis_definitions"]),
            transition_matrix={k: dict(v) for k, v in raw.get("transition_matrix", {}).items()},
            dwell_statistics=dict(raw.get("dwell_statistics", {})),
            occupancy=dict(raw["occupancy"]),
            validity=tuple(ValidityResult(name=v["name"], passed=bool(v["passed"]),
                                          statistic=v.get("statistic"), threshold=v.get("threshold"),
                                          detail=v.get("detail", ""))
                           for v in raw.get("validity_test_results", ())),
            usable=bool(raw.get("usable", False)), notes=raw.get("notes", ""),
            asset=raw.get("asset", ""),
            separation_horizon_bars=int(raw.get("separation_horizon_bars", 24)),
        )


@dataclass(frozen=True)
class RegimeState:
    """The runtime classification (specs/12 §9.3). Carries the evidence, not just the label."""

    cell: tuple[int, int] | None
    cell_label: str
    dwell_bars: int
    entered_at_ms: int
    as_of_ms: int
    axis_values: dict[str, float]
    #: The trailing quantiles actually used — without these the classification is not replayable.
    axis_boundaries: dict[str, list[float]]
    model_version: str
    grid: int
    degraded: bool = False
    persistence_prob: float | None = None
    median_dwell: float | None = None
    occupancy: float | None = None
    reason: str | None = None                  # why there is no cell, when there is none

    @property
    def classified(self) -> bool:
        return self.cell is not None

    @property
    def dwell_is_stable(self) -> bool:
        """Whether dwell has reached the cell's median — the condition C1-B (3), C2-A (7) and C3-B (7)
        depend on, and the one that **disappears in degraded mode** (specs/12 §10)."""
        if self.degraded or self.cell is None or self.median_dwell is None:
            return False
        return self.dwell_bars >= self.median_dwell

    def to_dict(self) -> dict[str, Any]:
        return {"cell": list(self.cell) if self.cell else None, "cell_label": self.cell_label,
                "dwell_bars": self.dwell_bars, "entered_at_ms": self.entered_at_ms,
                "as_of_ms": self.as_of_ms, "axis_values": dict(self.axis_values),
                "axis_boundaries": {k: list(v) for k, v in self.axis_boundaries.items()},
                "model_version": self.model_version, "grid": self.grid, "degraded": self.degraded,
                "persistence_prob": self.persistence_prob, "median_dwell": self.median_dwell,
                "occupancy": self.occupancy, "dwell_is_stable": self.dwell_is_stable,
                "reason": self.reason}


def quantile_edges(values: np.ndarray, grid: int) -> list[float]:
    """Interior cut points for ``grid`` equal-count buckets of a trailing sample.

    Terciles give two edges, a 2x2 grid one. Computed from the **trailing** sample only: specs/02 §6
    forbids full-sample quantiles, because a cut point derived from data after bar *t* would classify
    bar *t* using the future.
    """
    if grid < 2:
        raise RegimeError(
            f"a grid of {grid} has no cut points",
            component=COMPONENT, operation="quantile_edges",
            context={"grid": grid},
            likely_cause="grid=1 means the discretised model was abandoned; there is nothing to cut",
            remediation="do not ask for edges in degraded mode; use the raw axis values",
        )
    finite = values[np.isfinite(values)]
    if finite.size < grid * 5:
        raise RegimeError(
            f"only {finite.size} finite observations for a {grid}-bucket split",
            component=COMPONENT, operation="quantile_edges",
            context={"observations": int(finite.size), "grid": grid, "minimum": grid * 5},
            likely_cause="the quantile lookback is shorter than the axis' warm-up, or the axis is "
                         "mostly unavailable in this window",
            remediation="lengthen the quantile lookback, or wait for the axis to warm",
        )
    qs = [i / grid for i in range(1, grid)]
    return [float(q) for q in np.quantile(finite, qs)]


def classify(value: float, edges: list[float]) -> int:
    """Which bucket a value falls in, given interior cut points."""
    idx = 0
    for edge in edges:
        if value >= edge:
            idx += 1
    return idx
