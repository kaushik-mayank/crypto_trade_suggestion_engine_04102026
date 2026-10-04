"""PHASE-27 — calibrate ``swing_atr_multiple`` (θ) from pivot structure alone.

    python -m probes.swing_calibration            # calibrate and print the grid
    python -m probes.swing_calibration --apply    # also write the value into config/parameters.toml

**No returns, no performance, no P&L enters this calculation.** That is the point and it is not a
convenience. θ is a *feature-layer* parameter: it decides what counts as a swing pivot, which decides
what C2-A's stop references, which decides every ``R`` downstream. Choosing it by what made money on
the training window would fit the structure *definition* to one price path, and every strategy reading
``STR_SWING`` would inherit the fit without anything looking wrong. specs/02 §10 says "calibrate once
on training data; single value shared across assets and timeframes" — this module is what makes
"calibrate" mean something checkable rather than "pick one".

**The criteria are tied to what each consumer actually needs**, not to a generic goodness score:

* **availability** — the fraction of evaluation points where the feature a consumer reads is
  *defined*. That requirement differs by pair, so it is declared per pair in :data:`MEASURED`:
  ``1h/short`` is read for ``last_swing_high`` / ``last_swing_low``, so it needs **one pivot of each
  kind**; ``1h/medium`` feeds ``STR_SEQ`` through C2-A's ``structure_intact``, which needs **two of
  each** to label HH-HL or LH-LL. Measuring both against one rule would either excuse a useless θ or
  reject a usable one. Availability is the floor because its failure mode is *silence* — the
  conditions never evaluate, and the strategies that need them stop producing, which looks exactly
  like "no setups today".

* **spacing** — median bars between consecutive confirmed pivots, against a target derived from the
  timeframe's **short** lookback: consecutive pivots alternate high and low, so one swing cycle is two
  spacings, and structure defining an hourly-to-daily horizon should complete about one cycle per
  short lookback. On 1h that is 24 / 2 = 12 bars. The target is per **timeframe**, not per window —
  pivot spacing is a property of the price process and θ, so looking back 168 bars instead of 24
  reveals more swings, not longer ones. (Getting that wrong was the first run's second mistake, F-038.)

  On ``1h/short`` this criterion barely moves: the median spacing is 4 bars for every θ from 0.25 to
  1.5, because with ``k = 3`` confirmation in a 24-bar window two confirmable pivots cannot sit much
  closer than ``k + 1`` bars. The window's geometry, not θ, decides it there. The per-pair medians are
  reported on every run so that is visible rather than buried in an average.

* **cross-asset dispersion** — coefficient of variation of pivot density across BTC, ETH and SOL. One
  shared θ is only defensible if it means something comparable on all three. A large value is an
  argument *against* sharing it, and is reported rather than smoothed away.

**Selection.** Among the θ clearing the availability floor, take the lowest spacing error, breaking
ties on dispersion. Every candidate's numbers are printed and written to JSON, so the choice can be
audited and the shape of the optimum is visible — a very flat one would mean θ barely matters, which
is worth knowing before anyone defends a particular value.

**The two criteria disagree, and the probe says so rather than averaging over it.** Availability caps
θ at about 1.0: beyond that, a 24-bar window stops containing a confirmed pivot of each kind and
C2-A's structural stop has nothing to reference. Spacing wants θ near 2.0, where swings finally reach
the 12-bar scale the trading horizon implies. Both cannot hold, because ``lookback_1h_short = 24`` with
``k = 3`` is too small a window to host 12-bar swings — which is a statement about the *lookbacks*,
not about θ. :func:`tension` computes and reports it, and it is raised as an open question rather than
resolved here: changing a shared lookback would alter every feature on the 1h timeframe, and that is
not this probe's decision to make.

**Training window only.** ``--holdout-start-ms`` ends the window at the holdout boundary. specs/09
§1.2 makes the holdout readable exactly once, after all decisions are final; a parameter chosen with
the holdout in view would burn it before a single backtest ran.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from config.parameters import load_registry
from config.settings import Settings
from core.errors import ALL_ERRORS, ConfigError
from core.logging import get_logger, setup_logging
from features.f2_volatility import true_range
from features.f3_structure import MIN_PIVOT_BARS, find_pivots
from features.registry import lookback_for
from features.windows import Window
from normalize.canonical import Product, Timeframe
from snapshot.models import SeriesKey
from store import open_store

log = get_logger("probes.swing_calibration")
COMPONENT = "probes.swing_calibration"

ASSETS = ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")


@dataclass(frozen=True)
class MeasuredPair:
    """A ``(timeframe, horizon)`` a strategy reads, and what being "available" means for it."""

    timeframe: Timeframe
    horizon: str
    #: Minimum confirmed pivots **of each kind** for the consumer's feature to be defined.
    pivots_of_each_kind: int
    consumer: str

    @property
    def key(self) -> str:
        return f"{self.timeframe.value}/{self.horizon}"


#: Scoped to real usage rather than to every registered key, which is a correctness point rather than
#: an optimisation. ``STR_SWING`` is registered for all nine (timeframe, horizon) combinations, but
#: the 4h and 1d *short* lookbacks are 6 and 7 bars — fewer than the ``2k + 2 = 8`` a pivot needs to
#: exist at all. Including them drove availability to zero for every candidate and made the entire
#: grid unviable on the first run (F-038). Those pairs are now skipped by ``min_lookback`` in the
#: feature registry; measuring them here would still be measuring an impossibility.
MEASURED = (
    MeasuredPair(Timeframe.H1, "short", pivots_of_each_kind=1,
                 consumer="STR_SWING last_swing_high / last_swing_low — C2-A's structural stop, "
                          "C3-B's new-extreme confirmation, C3-C's boundary"),
    MeasuredPair(Timeframe.H1, "medium", pivots_of_each_kind=2,
                 consumer="STR_SEQ — C2-A/B/C's structure_intact condition, which needs two pivots "
                          "of each kind to label HH-HL / LH-LL"),
)

#: The candidate grid. Coarse on purpose: a fine grid invites reading noise as signal, and the whole
#: argument for θ is that it is a scale, not a tuned threshold.
CANDIDATES = (0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)

#: Availability floor. High because the failure mode is silence: a condition that cannot evaluate
#: produces no signal and no error, and "no setups today" is exactly what that looks like.
MIN_AVAILABILITY = 0.80

#: Consecutive pivots alternate high and low, so one full swing cycle is two spacings.
SPACINGS_PER_CYCLE = 2

#: Evaluation points per (asset, pair). Enough for a stable median without rescanning the whole
#: series at every candidate.
SAMPLE_POINTS = 120

OUT = Path("data/probe_results/swing_calibration.json")

#: ``timeframe.value -> target median spacing in bars``. Filled from the registry at run time.
TARGET_SPACING: dict[str, float] = {}


def target_spacing(params: dict[str, Any]) -> dict[str, float]:
    """``lookback_<tf>_short / SPACINGS_PER_CYCLE``, one target per **timeframe**.

    Derived through :func:`features.registry.lookback_for` so the target cannot drift from the
    lookbacks the features actually use.
    """
    return {pair.timeframe.value: lookback_for(params, pair.timeframe, "short") / SPACINGS_PER_CYCLE
            for pair in MEASURED}


@dataclass
class CellResult:
    """One (asset, pair, θ) measurement."""

    asset: str
    timeframe: str
    horizon: str
    theta: float
    required_of_each_kind: int
    windows_evaluated: int = 0
    windows_available: int = 0
    pivots_total: int = 0
    median_spacing_bars: float | None = None
    bars_per_window: int = 0

    @property
    def pair_key(self) -> str:
        return f"{self.timeframe}/{self.horizon}"

    @property
    def availability(self) -> float:
        if not self.windows_evaluated:
            return 0.0
        return self.windows_available / self.windows_evaluated

    @property
    def density_per_100(self) -> float:
        """Confirmed pivots per 100 bars examined — the cross-asset comparability measure."""
        examined = self.windows_evaluated * self.bars_per_window
        return 0.0 if not examined else 100.0 * self.pivots_total / examined

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "availability": round(self.availability, 4),
                "density_per_100": round(self.density_per_100, 3)}


def _finite(value: float) -> float | None:
    return None if value == float("inf") else round(value, 4)


def spacing_evidence(results: list[ThetaResult]) -> dict[str, Any]:
    """Per-pair median spacing across the grid, so an average cannot hide an unresponsive criterion."""
    observed: dict[str, dict[float, float]] = {}
    for r in results:
        for c in r.cells:
            if c.median_spacing_bars is not None:
                observed.setdefault(c.pair_key, {})[r.theta] = c.median_spacing_bars
    return {key: {"median_by_theta": {str(t): v for t, v in sorted(by_theta.items())},
                  "distinct_medians": sorted(set(by_theta.values())),
                  "responds_to_theta": len(set(by_theta.values())) > 1}
            for key, by_theta in sorted(observed.items())}


def tension(results: list[ThetaResult]) -> dict[str, Any] | None:
    """Whether the availability ceiling and the spacing optimum are incompatible, and by how much.

    Reported rather than resolved. If the θ that best matches the derived spacing target is one the
    availability floor rejects, then no single θ satisfies both — and the cause is the *lookback*,
    not θ. Silently picking the available-but-badly-spaced end would present a compromise as a
    measurement.
    """
    by_spacing = [r for r in results if r.spacing_error < float("inf")]
    if not by_spacing:
        return None
    best_spacing = min(by_spacing, key=lambda r: r.spacing_error)
    viable = [r for r in results if r.viable]
    if not viable or best_spacing.viable:
        return None
    ceiling = max(viable, key=lambda r: r.theta)
    return {
        "kind": "availability_ceiling_below_spacing_optimum",
        "availability_ceiling_theta": ceiling.theta,
        "availability_at_ceiling": round(ceiling.availability, 4),
        "spacing_optimum_theta": best_spacing.theta,
        "spacing_error_at_optimum": round(best_spacing.spacing_error, 4),
        "availability_at_spacing_optimum": round(best_spacing.availability, 4),
        "spacing_error_at_ceiling": round(ceiling.spacing_error, 4),
        "detail": "no single theta satisfies both criteria. Beyond the availability ceiling a "
                  "24-bar window stops containing a confirmed pivot of each kind, so C2-A's "
                  "structural stop has nothing to reference; below the spacing optimum, confirmed "
                  "swings are smaller than the trading horizon implies. The cause is "
                  "lookback_1h_short = 24 with swing_confirmation_bars = 3, which cannot host "
                  "swings at the 12-bar scale -- a statement about the lookbacks, not about theta.",
        "resolution": "raised as an open question. Changing a shared lookback would alter every "
                      "feature on the 1h timeframe and is not this probe's decision; the chosen "
                      "theta is the availability-respecting end, which is why it is provisional.",
    }


@dataclass
class ThetaResult:
    """One θ, aggregated across every (asset, pair)."""

    theta: float
    cells: list[CellResult] = field(default_factory=list)

    @property
    def availability(self) -> float:
        """The **worst** cell, not the mean.

        A θ that works for BTC 1h/short and fails for SOL 1h/medium is not a shared value, and an
        average would conceal precisely that.
        """
        return min((c.availability for c in self.cells), default=0.0)

    @property
    def spacing_error(self) -> float:
        """Mean absolute *relative* error of median spacing against each timeframe's target.

        Relative so timeframes with different targets contribute comparably — the criterion is "about
        one swing cycle per short lookback", which is scale-free by construction.
        """
        errors = [abs(c.median_spacing_bars - TARGET_SPACING[c.timeframe])
                  / TARGET_SPACING[c.timeframe]
                  for c in self.cells
                  if c.median_spacing_bars is not None and c.timeframe in TARGET_SPACING]
        return float("inf") if not errors else sum(errors) / len(errors)

    @property
    def dispersion(self) -> float:
        """Cross-asset coefficient of variation of pivot density, averaged over pairs."""
        per_pair: list[float] = []
        for key in sorted({c.pair_key for c in self.cells}):
            densities = [c.density_per_100 for c in self.cells if c.pair_key == key]
            if len(densities) < 2:
                continue
            mean = statistics.fmean(densities)
            if mean > 0:
                per_pair.append(statistics.stdev(densities) / mean)
        return float("inf") if not per_pair else sum(per_pair) / len(per_pair)

    @property
    def viable(self) -> bool:
        return self.availability >= MIN_AVAILABILITY and self.spacing_error < float("inf")

    def to_dict(self) -> dict[str, Any]:
        return {"theta": self.theta,
                "availability_worst_cell": round(self.availability, 4),
                "spacing_error": _finite(self.spacing_error),
                "cross_asset_dispersion": _finite(self.dispersion),
                "viable": self.viable,
                "worst_cell": (min(self.cells, key=lambda c: c.availability).pair_key
                               if self.cells else None),
                "cells": [c.to_dict() for c in self.cells]}


def _window(bars: list[Any], asset: str, tf: Timeframe) -> Window:
    return Window(
        key=SeriesKey(asset, Product.FUTURES, tf),
        lookback=len(bars),
        open_time_ms=np.array([b.open_time_ms for b in bars], dtype=np.int64),
        open=np.array([float(b.open) for b in bars]),
        high=np.array([float(b.high) for b in bars]),
        low=np.array([float(b.low) for b in bars]),
        close=np.array([float(b.close) for b in bars]),
        volume=np.array([float(b.volume_base) for b in bars]))


def _atr(w: Window) -> float:
    """Wilder true range, averaged — the same quantity ``ATR`` computes.

    Reusing :func:`features.f2_volatility.true_range` rather than approximating it, because pivots
    must be scaled by the volatility measure the rest of the system uses; otherwise θ means one thing
    in calibration and another in production.
    """
    tr = true_range(w)
    return float(np.mean(tr)) if tr.size else 0.0


def measure(bars: list[Any], pair: MeasuredPair, theta: float, k: int, *, asset: str,
            lookback: int, points: int = SAMPLE_POINTS) -> CellResult:
    """Walk evaluation points over the series and measure pivot structure at this θ.

    Each point examines the trailing ``lookback`` bars — the same window ``STR_SWING`` sees live — so
    this measures the feature as it will actually be computed. A statistic taken over four years in
    one pass would describe a window no strategy ever reads.
    """
    result = CellResult(asset=asset, timeframe=pair.timeframe.value, horizon=pair.horizon,
                        theta=theta, required_of_each_kind=pair.pivots_of_each_kind,
                        bars_per_window=lookback)
    if len(bars) < lookback + 1:
        return result

    starts = np.linspace(0, len(bars) - lookback, num=min(points, len(bars) - lookback + 1),
                         dtype=int)
    spacings: list[int] = []
    for start in starts:
        window = _window(bars[int(start):int(start) + lookback], asset, pair.timeframe)
        atr = _atr(window)
        if atr <= 0:
            continue
        result.windows_evaluated += 1
        try:
            pivots = find_pivots(window, k=k, theta=theta, atr=atr)
        except ALL_ERRORS:
            # A degenerate window (zero ATR, a flat range) is not a verdict on θ. It counts as
            # evaluated with no pivots, which is what it is.
            continue
        if not pivots:
            continue
        result.pivots_total += len(pivots)
        highs = sum(1 for p in pivots if p.is_high)
        lows = len(pivots) - highs
        if highs >= pair.pivots_of_each_kind and lows >= pair.pivots_of_each_kind:
            result.windows_available += 1
        indices = sorted(p.index for p in pivots)
        spacings.extend(b - a for a, b in zip(indices, indices[1:]))
    if spacings:
        result.median_spacing_bars = float(statistics.median(spacings))
    return result


def calibrate(store: Any, params: dict[str, Any], *, holdout_start_ms: int | None = None,
              candidates: tuple[float, ...] | None = None,
              points: int = SAMPLE_POINTS) -> dict[str, Any]:
    # Resolved here rather than as a default argument, which Python binds once at import and would
    # make the module's grid unoverridable from a test or a caller.
    candidates = CANDIDATES if candidates is None else candidates
    global TARGET_SPACING
    TARGET_SPACING = target_spacing(params)
    k = int(params["swing_confirmation_bars"])

    series: dict[tuple[str, Timeframe], list[Any]] = {}
    for asset in ASSETS:
        for tf in {p.timeframe for p in MEASURED}:
            end = holdout_start_ms if holdout_start_ms is not None else 2 ** 62
            series[(asset, tf)] = list(
                store.get_bars(asset, Product.FUTURES, tf, 0, end, final_only=True))
            log.info("series_loaded", asset=asset, timeframe=tf.value,
                     bars=len(series[(asset, tf)]))

    scoped: list[tuple[str, MeasuredPair, int]] = []
    skipped: list[dict[str, Any]] = []
    for asset in ASSETS:
        for pair in MEASURED:
            lookback = lookback_for(params, pair.timeframe, pair.horizon)
            if lookback < MIN_PIVOT_BARS:
                skipped.append({"asset": asset, "pair": pair.key, "lookback": lookback,
                                "needed": MIN_PIVOT_BARS,
                                "reason": "a pivot cannot exist in a window this short, for any theta"})
                continue
            scoped.append((asset, pair, lookback))
    if not scoped:
        raise ConfigError(
            "no measured (timeframe, horizon) pair has a window long enough to hold a pivot",
            component=COMPONENT, operation="calibrate",
            context={"measured": [p.key for p in MEASURED], "skipped": skipped},
            likely_cause="the shared lookbacks or swing_confirmation_bars changed",
            remediation=f"a pivot needs {MIN_PIVOT_BARS} bars; nothing can be calibrated without one",
        )

    results: list[ThetaResult] = []
    for theta in candidates:
        tr = ThetaResult(theta=theta)
        for asset, pair, lookback in scoped:
            tr.cells.append(measure(series[(asset, pair.timeframe)], pair, theta, k, asset=asset,
                                    lookback=lookback, points=points))
        results.append(tr)
        log.info("theta_measured", theta=theta, availability=round(tr.availability, 3),
                 spacing_error=_finite(tr.spacing_error), dispersion=_finite(tr.dispersion))

    conflict = tension(results)
    if conflict is not None:
        log.warning("criteria_conflict", **{k: v for k, v in conflict.items()
                                            if k not in ("detail", "resolution")})

    viable = [r for r in results if r.viable]
    # Rounded to 3 decimals before ranking, so two θ differing only in the fourth decimal of spacing
    # error are treated as tied and decided on dispersion instead. Ranking on noise is how a
    # structural calibration turns into a fit.
    chosen = min(viable, key=lambda r: (round(r.spacing_error, 3), r.dispersion)) if viable else None
    return {
        "generated_at_ms": int(time.time() * 1000),
        "method": "structural: per-pair pivot availability, median spacing against the derived "
                  "target, and cross-asset density dispersion. No returns, no performance, no P&L.",
        "measured_pairs": [{"pair": p.key, "requires_of_each_kind": p.pivots_of_each_kind,
                            "consumer": p.consumer} for p in MEASURED],
        "skipped_pairs": skipped,
        "target_spacing_bars": dict(TARGET_SPACING),
        "target_basis": f"lookback_short / {SPACINGS_PER_CYCLE} per timeframe — consecutive pivots "
                        "alternate high and low, so one swing cycle is two spacings, and structure "
                        "defining an hourly-to-daily horizon should complete about one cycle per "
                        "short lookback",
        "spacing_evidence": spacing_evidence(results),
        "tension": conflict,
        "min_availability": MIN_AVAILABILITY,
        "confirmation_bars_k": k,
        "holdout_start_ms": holdout_start_ms,
        "sample_points": points,
        "candidates": [r.to_dict() for r in results],
        "chosen": None if chosen is None else {
            "theta": chosen.theta,
            "availability_worst_cell": round(chosen.availability, 4),
            "spacing_error": round(chosen.spacing_error, 4),
            "cross_asset_dispersion": round(chosen.dispersion, 4),
            "selection_rule": "lowest spacing error (to 3 dp) among candidates clearing the "
                              "availability floor, ties broken on cross-asset dispersion",
        },
    }


def apply_to_registry(theta: float, path: Path) -> None:
    """Write θ into ``config/parameters.toml`` and move its status from ``unset`` to ``provisional``.

    A targeted text edit rather than a TOML round-trip: ``tomllib`` is read-only, and re-serialising
    would discard the comments and the ``justification`` / ``overfitting_exposure`` prose, which is
    the part of that file carrying the reasoning. The edit asserts the shape it expects and refuses
    rather than writing somewhere it did not recognise.

    ``provisional``, not ``fixed``: the value is now *measured* rather than guessed, but it has never
    been through a backtest, and specs/09 reserves promotion for Stage 10.
    """
    text = path.read_text(encoding="utf-8")
    needle = 'name = "swing_atr_multiple"'
    if needle not in text:
        raise ConfigError(
            f"could not find the swing_atr_multiple entry in {path}",
            component=COMPONENT, operation="apply_to_registry",
            context={"path": str(path)},
            likely_cause="the registry was reformatted or the parameter was renamed",
            remediation="set it by hand; this writer refuses rather than guessing where it goes",
        )
    start = text.index(needle)
    end = text.index("[[parameter]]", start)
    block = text[start:end]
    for field_name, replacement in (("value", f"value = {theta}"),
                                    ("status", 'status = "provisional"')):
        lines = [ln for ln in block.split("\n") if ln.startswith(f"{field_name} = ")]
        if len(lines) != 1:
            raise ConfigError(
                f"expected exactly one `{field_name} =` line in the swing_atr_multiple block, found "
                f"{len(lines)}",
                component=COMPONENT, operation="apply_to_registry",
                context={"field": field_name, "found": lines},
                likely_cause="the block was reformatted",
                remediation="set it by hand rather than letting this writer guess",
            )
        block = block.replace(lines[0], replacement)
    block = block.replace(
        'set_by = "PHASE-14 / specs/16 Stage 3 — calibrated ONCE on the training window"',
        'set_by = "PHASE-27 — calibrated once on the training window by probes.swing_calibration, '
        'structurally: per-pair pivot availability, median spacing against lookback_short/2, and '
        'cross-asset density dispersion. No returns were used. See docs/findings.md F-038."')
    path.write_text(text[:start] + block + text[end:], encoding="utf-8")
    log.info("registry_updated", parameter="swing_atr_multiple", value=theta, status="provisional")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="calibrate swing_atr_multiple structurally")
    ap.add_argument("--apply", action="store_true",
                    help="write the chosen value into config/parameters.toml")
    ap.add_argument("--points", type=int, default=SAMPLE_POINTS)
    ap.add_argument("--holdout-start-ms", type=int, default=None,
                    help="exclude bars at or after this time; the training window ends here")
    args = ap.parse_args(argv)

    settings = Settings()
    setup_logging(settings.paths.log_dir, log_name="swing_calibration")
    registry = load_registry(settings.paths.parameters_file)
    params = {p.name: p.value for p in registry.parameters}
    store = open_store(settings.paths.state_db, migrate=False)
    try:
        report = calibrate(store, params, holdout_start_ms=args.holdout_start_ms,
                           points=args.points)
    finally:
        store.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{'theta':>6s} {'avail':>7s} {'worst cell':>12s} {'spacing_err':>12s} "
          f"{'dispersion':>11s}  viable")
    for row in report["candidates"]:
        print(f"{row['theta']:6.2f} {row['availability_worst_cell']:7.3f} "
              f"{str(row['worst_cell']):>12s} {str(row['spacing_error']):>12s} "
              f"{str(row['cross_asset_dispersion']):>11s}  {'yes' if row['viable'] else 'no'}")
    print(f"\ntarget spacing (bars): {report['target_spacing_bars']}")
    for key, ev in report["spacing_evidence"].items():
        print(f"  {key} median spacing across the grid: {ev['distinct_medians']}"
              + ("" if ev["responds_to_theta"] else "  (does not respond to theta)"))

    conflict = report["tension"]
    if conflict is not None:
        print(f"\nCRITERIA CONFLICT: availability caps theta at "
              f"{conflict['availability_ceiling_theta']} (spacing error "
              f"{conflict['spacing_error_at_ceiling']}), but spacing is best at "
              f"{conflict['spacing_optimum_theta']} (availability "
              f"{conflict['availability_at_spacing_optimum']}).")
        print(f"  {conflict['detail']}")
        print(f"  {conflict['resolution']}")
    for pair in report["measured_pairs"]:
        print(f"  {pair['pair']}: needs {pair['requires_of_each_kind']} of each kind")

    chosen = report["chosen"]
    if chosen is None:
        print(f"\nNO VIABLE THETA: every candidate failed the availability floor "
              f"({MIN_AVAILABILITY}). This is not a value to pick by hand -- it means the pivot "
              f"definition, the lookbacks, or the available history is the problem.", file=sys.stderr)
        print(f"grid written to {OUT}")
        return 1

    print(f"\nchosen theta = {chosen['theta']}  "
          f"(availability {chosen['availability_worst_cell']}, "
          f"spacing error {chosen['spacing_error']}, dispersion {chosen['cross_asset_dispersion']})")
    print(f"grid written to {OUT}")

    if args.apply:
        apply_to_registry(chosen["theta"], Path(settings.paths.parameters_file))
        print(f"config/parameters.toml updated: swing_atr_multiple = {chosen['theta']} (provisional)")
    else:
        print("re-run with --apply to write it into the registry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
