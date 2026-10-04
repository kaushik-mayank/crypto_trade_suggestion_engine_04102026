"""Fitting the regime model offline, with the escalation specs/12 §10 asks for in advance.

The three validity tests (specs/02 §6) are run at 3x3; any failure collapses to 2x2 and retests; a
second failure means **the discretised regime model is unusable** and the system runs in degraded
mode. That path is designed here rather than improvised later, because most strategies carry a regime
gate and "no stable regime structure" is a legitimate — if project-threatening — result.

What survives degradation, per specs/12 §10:

| Condition | Depends on | Survives |
|---|---|---|
| ``MR_VR < 1`` / ``> 1`` (C1-B 2, C1-C 7, C2-A 3, C3-B 1) | the raw axis | yes |
| ``VOL_RATIO`` percentile (C3-A 1) | the raw axis | yes |
| "regime cell stable >= median dwell" (C1-B 3, C2-A 7, C3-B 7) | the **cell** | **no** |
| ``regime_persistence_prob`` (C3-B) | the transition matrix | **no** |

So degraded mode drops the dwell-stability conditions and keeps every raw-axis threshold: the system
survives with **reduced selectivity**, not with broken strategies. The axis measurements were always
doing the substantive work; the cell was a stability filter on top.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import stats

from core.errors import RegimeError
from core.logging import get_logger
from regime.model import (
    AXIS_P,
    AXIS_V,
    RegimeModel,
    ValidityResult,
    cell_label,
    classify,
    quantile_edges,
)

log = get_logger("regime.fit")
COMPONENT = "regime.fit"

#: Minimum share of observations a retained cell must hold (parameter 15, fixed).
MIN_OCCUPANCY = 0.05
#: Median dwell, in bars, below which a label is a noisy indicator rather than a regime.
MIN_MEDIAN_DWELL = 3.0
#: Significance level for the Kruskal-Wallis separation test.
SEPARATION_ALPHA = 0.05
#: Forward horizon, in bars on the classifying timeframe, that the separation test measures over.
#:
#: **Chosen from the specs, not from the p-values.** specs/02 §6 does not fix a horizon, and the choice
#: matters enormously: measured on this data the same cells give p ~ 0.9 at one bar and p ~ 0 at 24
#: (finding F-033). Picking the horizon that passes would be exactly the curve-fitting the spec's
#: trailing-quantile rule exists to prevent, so it is anchored to the horizon the strategies actually
#: operate on: specs/03 §5 expires an untriggered setup after "3 funding intervals (24h)" and specs/06
#: §4 states holding periods "land in days". A regime is a statement about the next day, not the next
#: hour, and a one-bar test asks the model a question no strategy asks.
SEPARATION_FORWARD_BARS = 24


def forward_returns(closes: np.ndarray, horizon: int = SEPARATION_FORWARD_BARS) -> np.ndarray:
    """``ln(P_{t+h} / P_t)`` per bar, NaN where the horizon runs past the data.

    Looking forward is legitimate *here and only here*: this runs offline and its whole purpose is to
    ask whether cells differ in what follows them. Nothing computed in this module reaches a live
    classification -- the live path takes the fitted artifact and never sees a forward return.
    """
    if horizon < 1:
        raise RegimeError(
            f"a forward horizon of {horizon} bars cannot be measured",
            component=COMPONENT, operation="forward_returns",
            context={"horizon": horizon},
            likely_cause="the horizon was computed from an empty or zero-valued setting",
            remediation="use a positive number of bars",
        )
    logp = np.log(closes)
    out = np.full(closes.size, np.nan)
    if closes.size > horizon:
        out[:-horizon] = logp[horizon:] - logp[:-horizon]
    return out


@dataclass
class FitInputs:
    """The axis series and the forward returns to test separation against, all aligned by bar.

    ``forward_return`` looks forward by :data:`SEPARATION_FORWARD_BARS` bars, which is the one thing
    here that legitimately does: the fit is offline and its purpose is to ask whether cells differ in
    what follows them. Nothing from this computation reaches a live classification.
    """

    times_ms: np.ndarray
    axis_v: np.ndarray
    axis_p: np.ndarray
    forward_return: np.ndarray
    asset: str = ""
    #: The horizon ``forward_return`` was built over, carried so the separation verdict is interpretable.
    forward_horizon_bars: int = SEPARATION_FORWARD_BARS

    def __post_init__(self) -> None:
        sizes = {"times": self.times_ms.size, "axis_v": self.axis_v.size,
                 "axis_p": self.axis_p.size, "forward_return": self.forward_return.size}
        if len(set(sizes.values())) != 1:
            raise RegimeError(
                f"fit inputs are not aligned: {sizes}",
                component=COMPONENT, operation="FitInputs",
                context=sizes,
                likely_cause="the axis series were built over different windows",
                remediation="align every series to the same bar index before fitting",
            )

    @property
    def usable_mask(self) -> np.ndarray:
        return np.isfinite(self.axis_v) & np.isfinite(self.axis_p) & np.isfinite(self.forward_return)

    def __len__(self) -> int:
        return int(self.times_ms.size)


def _cells(inputs: FitInputs, grid: int, lookback: int) -> tuple[np.ndarray, list[list[float]]]:
    """Assign each bar to a cell using **trailing** quantiles of the preceding ``lookback`` bars.

    Bar *t* is classified with cut points derived from bars ``t-lookback .. t-1``, so no bar is ever
    classified using its own future. Bars without a full lookback are left unclassified (-1).
    """
    n = len(inputs)
    cells = np.full((n, 2), -1, dtype=np.int64)
    last_edges: list[list[float]] = [[], []]
    for t in range(lookback, n):
        v_hist = inputs.axis_v[t - lookback:t]
        p_hist = inputs.axis_p[t - lookback:t]
        if not (np.isfinite(inputs.axis_v[t]) and np.isfinite(inputs.axis_p[t])):
            continue
        try:
            v_edges = quantile_edges(v_hist, grid)
            p_edges = quantile_edges(p_hist, grid)
        except RegimeError:
            continue                            # not enough finite history yet at this bar
        cells[t] = (classify(float(inputs.axis_v[t]), v_edges),
                    classify(float(inputs.axis_p[t]), p_edges))
        last_edges = [v_edges, p_edges]
    return cells, last_edges


def _occupancy(cells: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    valid = cells[(cells[:, 0] >= 0) & mask]
    total = valid.shape[0]
    if total == 0:
        return {}
    counts = Counter((int(v), int(p)) for v, p in valid)
    return {f"{v},{p}": n / total for (v, p), n in sorted(counts.items())}


def _transitions(cells: np.ndarray) -> dict[str, dict[str, float]]:
    matrix: dict[str, dict[str, float]] = {}
    prev: tuple[int, int] | None = None
    for v, p in cells:
        if v < 0:
            prev = None
            continue
        cur = (int(v), int(p))
        if prev is not None:
            row = matrix.setdefault(f"{prev[0]},{prev[1]}", {})
            key = f"{cur[0]},{cur[1]}"
            row[key] = row.get(key, 0.0) + 1.0
        prev = cur
    return matrix


def _dwells(cells: np.ndarray) -> tuple[dict[str, float], list[int]]:
    """Median dwell per cell, and every run length, for the persistence test."""
    runs: dict[str, list[int]] = {}
    all_runs: list[int] = []
    current: tuple[int, int] | None = None
    length = 0
    for v, p in cells:
        cur = (int(v), int(p)) if v >= 0 else None
        if cur == current and cur is not None:
            length += 1
            continue
        if current is not None and length:
            runs.setdefault(f"{current[0]},{current[1]}", []).append(length)
            all_runs.append(length)
        current, length = cur, 1 if cur is not None else 0
    if current is not None and length:
        runs.setdefault(f"{current[0]},{current[1]}", []).append(length)
        all_runs.append(length)
    return {k: float(np.median(v)) for k, v in sorted(runs.items())}, all_runs


def _test_occupancy(occupancy: dict[str, float], grid: int) -> ValidityResult:
    thin = {k: round(v, 4) for k, v in occupancy.items() if v < MIN_OCCUPANCY}
    expected = grid * grid
    smallest = min(occupancy.values()) if occupancy else 0.0
    return ValidityResult(
        name="occupancy", passed=not thin and len(occupancy) == expected,
        statistic=round(smallest, 4), threshold=MIN_OCCUPANCY,
        detail=(f"all {len(occupancy)} cells hold at least {MIN_OCCUPANCY:.0%} of observations"
                if not thin and len(occupancy) == expected else
                f"{len(thin)} of {len(occupancy)} occupied cells are below {MIN_OCCUPANCY:.0%} "
                f"({thin}); {expected - len(occupancy)} cell(s) never occurred"),
        extra={"thin_cells": thin, "cells_occupied": len(occupancy), "cells_expected": expected})


def _test_persistence(dwells: dict[str, float], all_runs: list[int]) -> ValidityResult:
    median_dwell = float(np.median(all_runs)) if all_runs else 0.0
    return ValidityResult(
        name="persistence", passed=median_dwell >= MIN_MEDIAN_DWELL,
        statistic=round(median_dwell, 3), threshold=MIN_MEDIAN_DWELL,
        detail=(f"median dwell {median_dwell:.2f} bars"
                + ("" if median_dwell >= MIN_MEDIAN_DWELL else
                   " -- a label that flips this often is a noisy indicator, not a regime")),
        extra={"runs": len(all_runs), "per_cell_median_dwell": dwells})


def _test_separation(cells: np.ndarray, inputs: FitInputs) -> ValidityResult:
    """Kruskal-Wallis across cells on forward returns: do the cells differ in what follows them?

    The horizon is :data:`SEPARATION_FORWARD_BARS`, and the result records it, because a separation
    verdict without its horizon is not interpretable (F-033).
    """
    groups: dict[tuple[int, int], list[float]] = {}
    for i, (v, p) in enumerate(cells):
        if v < 0 or not np.isfinite(inputs.forward_return[i]):
            continue
        groups.setdefault((int(v), int(p)), []).append(float(inputs.forward_return[i]))
    usable = [g for g in groups.values() if len(g) >= 20]
    if len(usable) < 2:
        return ValidityResult(
            name="separation", passed=False, statistic=None, threshold=SEPARATION_ALPHA,
            detail=f"only {len(usable)} cell(s) have 20+ forward returns, so no comparison is possible",
            extra={"groups": len(groups)})
    stat, pvalue = stats.kruskal(*usable)
    return ValidityResult(
        name="separation", passed=bool(pvalue < SEPARATION_ALPHA),
        statistic=round(float(pvalue), 6), threshold=SEPARATION_ALPHA,
        detail=(f"Kruskal-Wallis H={stat:.2f}, p={pvalue:.4g} across {len(usable)} cells"
                + ("" if pvalue < SEPARATION_ALPHA else
                   " -- forward returns do not differ measurably, so the model has no economic "
                   "content and should be reduced or abandoned rather than kept for decoration")),
        extra={"groups_compared": len(usable), "h_statistic": round(float(stat), 4),
               "forward_horizon_bars": inputs.forward_horizon_bars})


def _attempt(inputs: FitInputs, grid: int, lookback: int) -> tuple[RegimeModel, list[ValidityResult]]:
    cells, _ = _cells(inputs, grid, lookback)
    occupancy = _occupancy(cells, inputs.usable_mask)
    dwells, all_runs = _dwells(cells)
    tests = [_test_occupancy(occupancy, grid), _test_persistence(dwells, all_runs),
             _test_separation(cells, inputs)]
    passed = all(t.passed for t in tests)
    digest = hashlib.sha256(
        f"{grid}|{lookback}|{inputs.times_ms[0] if len(inputs) else 0}|"
        f"{inputs.times_ms[-1] if len(inputs) else 0}|{sorted(occupancy.items())}".encode()
    ).hexdigest()[:12]
    model = RegimeModel(
        model_version=f"{grid}x{grid}-{digest}", grid=grid,
        fitted_on_window=(int(inputs.times_ms[0]), int(inputs.times_ms[-1])) if len(inputs) else (0, 0),
        quantile_lookback=lookback,
        axis_definitions={"V": AXIS_V, "P": AXIS_P},
        transition_matrix=_transitions(cells), dwell_statistics=dwells, occupancy=occupancy,
        validity=tuple(tests), usable=passed, asset=inputs.asset,
        separation_horizon_bars=inputs.forward_horizon_bars,
        notes="" if passed else "failed: " + ", ".join(t.name for t in tests if not t.passed))
    return model, tests


def fit(inputs: FitInputs, *, quantile_lookback: int = 720,
        grids: tuple[int, ...] = (3, 2)) -> RegimeModel:
    """Fit, test, and escalate: 3x3 -> 2x2 -> degraded (specs/12 §10).

    Always returns a model. A model that failed at every granularity comes back with ``usable=False``
    and ``grid=1``, which is the **degraded mode** the strategies are designed to survive — not an
    exception, because "no stable regime structure" is a result to record, not a crash.
    """
    if len(inputs) <= quantile_lookback:
        raise RegimeError(
            f"the fit window holds {len(inputs)} bars, not more than the {quantile_lookback}-bar "
            "quantile lookback",
            component=COMPONENT, operation="fit",
            context={"bars": len(inputs), "quantile_lookback": quantile_lookback},
            likely_cause="the training window is shorter than the trailing window it needs",
            remediation="lengthen the training window, or shorten quantile_lookback",
        )

    attempts: list[RegimeModel] = []
    for grid in grids:
        model, tests = _attempt(inputs, grid, quantile_lookback)
        attempts.append(model)
        log.info("regime_fit_attempt", asset=inputs.asset, grid=grid, usable=model.usable,
                 tests={t.name: t.passed for t in tests},
                 statistics={t.name: t.statistic for t in tests})
        if model.usable:
            log.info("regime_fit_accepted", asset=inputs.asset, grid=grid,
                     version=model.model_version, cells=len(model.occupancy))
            return model

    last = attempts[-1]
    degraded = RegimeModel(
        model_version=f"degraded-{last.model_version}", grid=1,
        fitted_on_window=last.fitted_on_window, quantile_lookback=quantile_lookback,
        axis_definitions={"V": AXIS_V, "P": AXIS_P}, transition_matrix={},
        dwell_statistics={}, occupancy={}, separation_horizon_bars=last.separation_horizon_bars,
        validity=tuple(v for m in attempts for v in m.validity), usable=False, asset=inputs.asset,
        notes="the discretised regime model is unusable at every granularity tried "
              f"({', '.join(str(g) + 'x' + str(g) for g in grids)}). Degraded mode: raw-axis "
              "thresholds still apply; the dwell-stability conditions and regime_persistence_prob do "
              "not. This is a substantive finding about this market at this horizon and belongs in "
              "docs/findings.md (specs/12 §10).")
    log.warning("regime_model_unusable", asset=inputs.asset,
                tried=[f"{g}x{g}" for g in grids],
                failures={m.model_version: list(m.failed_tests) for m in attempts},
                detail="running in degraded mode: reduced selectivity, not broken strategies")
    return degraded


def summarise(model: RegimeModel) -> dict[str, Any]:
    """A compact report for ``docs/findings.md`` — the outcome must be recorded either way."""
    return {
        "asset": model.asset, "version": model.model_version, "grid": model.grid,
        "usable": model.usable, "degraded": model.degraded,
        "tests": {v.name: {"passed": v.passed, "statistic": v.statistic, "detail": v.detail}
                  for v in model.validity},
        "cells": {k: {"occupancy": round(v, 4),
                      "median_dwell": model.dwell_statistics.get(k),
                      "label": cell_label((int(k.split(",")[0]), int(k.split(",")[1])), model.grid)}
                  for k, v in model.occupancy.items()},
        "notes": model.notes,
    }
