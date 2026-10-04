"""Run the backtest harness.

    python -m backtest --preregister            # write validation/protocol.lock.json, then stop
    python -m backtest --coverage               # how much of the window is usable, then stop
    python -m backtest --window train           # run the registered window
    python -m backtest --window train --sensitivity   # also re-run at 2x slippage

Pre-registration comes first and the order is enforced, not suggested: ``--window`` loads the lock and
refuses when there is none, because specs/09 §1 requires the protocol to exist **before** the first
backtest. A protocol written afterwards is a description of what happened, not a prediction.

``--coverage`` exists because a backtest over 40% usable steps is not a backtest over that window, and
finding that out from a suspiciously small trade count is the expensive way to learn it.

The holdout is never reachable from here. :class:`store.holdout.HoldoutGuard` is the only way in, it
logs the access, and it refuses a second one — so the flag that would read it is deliberately absent
from this CLI rather than present and defaulted off.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.__main__ import ASSETS, build_request
from app.runner import AppRunner, load_classifiers
from backtest import protocol
from backtest.fills import CostModel
from backtest.runner import BacktestRunner, sensitivity_costs
from config.parameters import load_registry
from config.settings import Settings
from core.errors import ALL_ERRORS, ProtocolViolation
from core.logging import get_logger, setup_logging
from features.engine import FeatureEngine
from normalize.canonical import Timeframe
from pipeline.drivers import BacktestDriver
from risk.engine import RiskEngine
from snapshot.builder import SnapshotBuilder
from store import open_store
from strategy.registry import backtest_strategies, live_strategies

log = get_logger("backtest.main")

OUT_DIR = Path("data/backtest")

#: Slippage as a fraction of price, pending the P14 measurement specs/09 §3.4 calls for.
#:
#: 5 bps is **an assumption, not a measurement**, and it is marked as one in every result. It is
#: deliberately on the pessimistic side of plausible for BTC/ETH/SOL perpetuals at the sizes this
#: system suggests, and the 2x sensitivity re-run is what covers the error in it. When P14 lands, this
#: becomes its 75th percentile and the assumption disappears.
ASSUMED_SLIPPAGE_FRACTION = Decimal("0.0005")

#: specs/09 §2.2: a rolling walk-forward needs a long training window. The default here splits the
#: stored history as absolute dates written into the lock, never as a fraction — a fraction would
#: shift every time the recorder adds a day.
#:
#: The start is **2023-08-01**, and it is derived from coverage rather than chosen. Measured on the
#: stored futures history (F-039):
#:
#: * 1h begins 2022-01-19 (BTC/ETH) and 2022-06-30 (SOL);
#: * **15m and 4h begin 2023-01-23** — the venue's own history, not a gap;
#: * the 1d warm-up needs 180 bars beyond that, which lands in late July 2023.
#:
#: An earlier start does not add usable steps, it adds steps whose features cannot be computed: the
#: first attempt began 2022-02-01 and produced 57% Tier A coverage with 2,978 EMPTY series readings.
#: A backtest over that window would measure the gaps as much as the strategies.
#:
#: 2023-08-01 to 2026-09-30 is 38 months. specs/09 §2.3 calls that the full protocol, and §2.2's
#: 6-month train / 1-month test / 1-month step gives ~26 walk-forward folds on the 32 months before
#: the holdout.
DEFAULT_WINDOWS = {
    "train": {"start_utc": "2023-08-01T00:00:00+00:00", "end_utc": "2025-06-30T23:59:59+00:00"},
    "validate": {"start_utc": "2025-07-01T00:00:00+00:00", "end_utc": "2026-03-31T23:59:59+00:00"},
    #: The most recent ~15% (specs/09 §2.2), declared so its boundary is pre-registered and never
    #: read by this CLI. HoldoutGuard is the only way in, and it burns on second contact (§1.2).
    "holdout": {"start_utc": "2026-04-01T00:00:00+00:00", "end_utc": "2026-09-30T23:59:59+00:00"},
}

#: specs/09 §7.2 requires the trial count declared **before** running, so the multiple-comparison
#: correction cannot be chosen to suit the result. 12 tradeable strategies x 3 assets, plus the two
#: controls across their seeds.
TRIAL_COUNT = 12 * 3 + 2


def build_runner(store: Any, settings: Any, *, include_controls: bool, seeds: tuple[int, ...]):
    registry = load_registry(settings.paths.parameters_file)
    params = {p.name: p.value for p in registry.parameters}
    unset = frozenset(p.name for p in registry.parameters if p.status == "unset")
    engine = FeatureEngine(params, parameter_hash=registry.parameter_hash, unset_params=unset)
    strategies = (backtest_strategies(params, seeds=seeds) if include_controls
                  else live_strategies(params))
    runner = AppRunner(
        builder=SnapshotBuilder.from_settings(store, settings, registry.parameter_hash),
        engine=engine, request=build_request(), strategies=strategies,
        risk=RiskEngine(params=params),
        classifiers=load_classifiers(
            ASSETS, directory=str(Path(settings.paths.parameters_file).parent)),
        assets=ASSETS,
    )
    runner.preflight()
    return runner, registry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="crypto-trader backtest harness (specs/09)")
    ap.add_argument("--preregister", action="store_true",
                    help="write the protocol lock file and exit")
    ap.add_argument("--amend", metavar="REASON",
                    help="bump the protocol version with a stated reason (specs/09 §1.3)")
    ap.add_argument("--after-results", action="store_true",
                    help="with --amend: record that results were already seen, which BURNS the "
                         "holdout (specs/09 §1.3)")
    ap.add_argument("--coverage", action="store_true",
                    help="report what fraction of the window produces a usable snapshot, then exit")
    ap.add_argument("--window", default=None, help="which pre-registered window to run")
    ap.add_argument("--step", default="1h", choices=["1h", "4h", "1d"],
                    help="evaluation cadence; setups are defined on closed signal-timeframe bars")
    ap.add_argument("--sensitivity", action="store_true",
                    help="also re-run at 2x slippage (specs/09 §3.4)")
    ap.add_argument("--controls", action="store_true",
                    help="include C4-X1/C4-X2; required for the §7.1 significance test")
    ap.add_argument("--seeds", type=int, nargs="*", default=[0, 1, 2, 3, 4],
                    help="C4-X1 seeds; many are required to produce a distribution (specs/06 §5.1)")
    args = ap.parse_args(argv)

    settings = Settings()
    setup_logging(settings.paths.log_dir, log_name="backtest")
    store = open_store(settings.paths.state_db, migrate=False)

    try:
        return _dispatch(args, store, settings)
    except ProtocolViolation as e:
        print(f"PROTOCOL VIOLATION: {e.message}", file=sys.stderr)
        print(f"  likely cause: {e.likely_cause}", file=sys.stderr)
        print(f"  fix: {e.remediation}", file=sys.stderr)
        return 3
    except ALL_ERRORS as e:
        print(f"BACKTEST FAILED: {e}", file=sys.stderr)
        return 2
    finally:
        store.close()


def _dispatch(args: Any, store: Any, settings: Any) -> int:
    if args.preregister:
        registry = load_registry(settings.paths.parameters_file)
        lock = protocol.build(windows=DEFAULT_WINDOWS, parameters=registry,
                              trial_count=TRIAL_COUNT)
        path = protocol.write(lock)
        print(f"protocol locked at {path}")
        print(f"  version:        {lock.protocol_version}")
        print(f"  sha256:         {lock.sha256}")
        print(f"  code_version:   {lock.code_version}")
        print(f"  parameter_hash: {lock.parameter_hash}")
        print(f"  windows:        {', '.join(sorted(lock.window_boundaries))}")
        print(f"  trials declared:{lock.trial_count_declared}")
        print(f"  hypotheses:     {', '.join(h['id'] for h in lock.hypotheses)}")
        return 0

    if args.amend:
        lock = protocol.amend(protocol.load(), reason=args.amend,
                              after_results=args.after_results,
                              parameters=load_registry(settings.paths.parameters_file))
        protocol.write(lock, overwrite=True)
        print(f"protocol amended to version {lock.protocol_version}")
        if args.after_results:
            print("  THE HOLDOUT IS BURNED: this change was made after seeing results "
                  "(specs/09 §1.3). It is in-sample from here on.")
        return 0

    if not args.window:
        print("nothing to do: pass --preregister, --coverage or --window", file=sys.stderr)
        return 1

    lock = protocol.load()
    if args.window == "holdout":
        raise ProtocolViolation(
            "the holdout cannot be run from this CLI",
            component="backtest.main", operation="dispatch",
            context={"window": args.window},
            likely_cause="an attempt to evaluate the holdout as if it were another fold",
            remediation="the holdout is read exactly once, through store.holdout.HoldoutGuard, "
                        "which logs the access and refuses a second. That flag is absent here "
                        "deliberately rather than present and defaulted off (specs/09 §1.2)",
        )

    start_ms, end_ms = lock.window(args.window)
    step = {"1h": Timeframe.H1, "4h": Timeframe.H4, "1d": Timeframe.D1}[args.step]
    holdout_start, _ = lock.window("holdout")

    runner, registry = build_runner(store, settings, include_controls=args.controls,
                                    seeds=tuple(args.seeds))
    driver = BacktestDriver(
        builder=runner.builder, request=runner.request,
        start_ms=(start_ms // step.ms) * step.ms, end_ms=end_ms, step=step,
        max_as_of_ms=holdout_start, offset_ms=1000)

    if args.coverage:
        # Walk without evaluating: the question is how much of the window is usable, and answering it
        # by running the strategies would cost the whole run to learn something cheap.
        for _ in driver.snapshots():
            pass
        cov = driver.coverage()
        print(json.dumps(cov, indent=2))
        if cov["usable_fraction"] is not None and cov["usable_fraction"] < 0.9:
            print(f"\nWARNING: only {cov['usable_fraction']:.1%} of steps are usable. A backtest "
                  f"over this window measures the gaps as much as the strategies.", file=sys.stderr)
        return 0

    costs = CostModel(slippage_fraction=ASSUMED_SLIPPAGE_FRACTION)
    results = []
    for label, cost_model in _cost_runs(costs, args.sensitivity):
        # A fresh runner per cost model: strategy state and the regime classifiers' dwell counters
        # accumulate, and reusing them would let the first run's state leak into the second's.
        run_runner, _ = build_runner(store, settings, include_controls=args.controls,
                                     seeds=tuple(args.seeds))
        run_driver = BacktestDriver(
            builder=run_runner.builder, request=run_runner.request,
            start_ms=(start_ms // step.ms) * step.ms, end_ms=end_ms, step=step,
            max_as_of_ms=holdout_start, offset_ms=1000)
        bt = BacktestRunner(runner=run_runner, driver=run_driver, costs=cost_model, lock=lock,
                            window_name=f"{args.window}:{label}")
        result = bt.run()
        payload = result.to_dict(run_runner.strategies)
        payload["coverage"] = run_driver.coverage()
        payload["slippage_is_an_assumption"] = (
            "ASSUMED_SLIPPAGE_FRACTION is an assumption pending the P14 measurement, not a measured "
            "value. The 2x sensitivity re-run is what covers the error in it")
        results.append((label, payload))

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        out = OUT_DIR / f"{args.window}-{label}-{stamp}.json"
        out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\n[{label}] {out}")
        _print_summary(payload)

    if args.sensitivity and len(results) == 2:
        _print_sensitivity(results[0][1], results[1][1])
    return 0


def _cost_runs(costs: CostModel, sensitivity: bool):
    runs = [("headline", costs)]
    if sensitivity:
        runs.append(("slippage2x", sensitivity_costs(costs)))
    return runs


def _print_summary(payload: dict[str, Any]) -> None:
    counts = payload["ledger"]["counts"]
    non = payload["ledger"]["non_trades"]
    cov = payload.get("coverage", {})
    print(f"  steps {payload['steps']}  usable {cov.get('usable_fraction')}  "
          f"failures {sum(payload['step_failures'].values())}")
    print(f"  trades {counts['trades']} (closed {counts['closed']}, "
          f"open at end {counts['open_at_window_end']}, handed off {counts['handed_off']})")
    print(f"  non-trades {non['total']}: expired {non['expired']}, invalidated "
          f"{non['invalidated']}, missed {non['missed']}, suppressed {non['suppressed']}, "
          f"vetoed {non['vetoed']}, no following bar {non['no_following_bar']}")
    portfolio = payload["reports"]["portfolio"]
    print(f"  portfolio: total {portfolio['total_r']:.2f}R, max drawdown "
          f"{portfolio['drawdown']['max_drawdown_r']}R, max concurrent "
          f"{portfolio['max_concurrent_positions']}")
    for sid, report in sorted(payload["reports"]["by_strategy"].items()):
        d = report["distribution"]
        if not d["n"]:
            continue
        ci = d["hit_rate_ci95"]
        print(f"    {sid:6s} n={d['n']:4d}  hit {d['hit_rate']:.3f} "
              f"[{ci[0]:.3f}, {ci[1]:.3f}]  mean {d['mean_r']:+.3f}R  "
              f"median {d['median_r']:+.3f}R  worst {d['worst_r']:+.3f}R")


def _print_sensitivity(headline: dict[str, Any], doubled: dict[str, Any]) -> None:
    """specs/09 §3.4: an edge that disappears at 2x slippage is not robust enough to deploy."""
    print("\nslippage sensitivity (specs/09 §3.4)")
    print(f"  {'strategy':8s} {'total R':>10s} {'at 2x':>10s} {'survives':>9s}")
    a = headline["reports"]["by_strategy"]
    b = doubled["reports"]["by_strategy"]
    for sid in sorted(a):
        ta = a[sid]["concentration"]["total_r"]
        tb = b.get(sid, {}).get("concentration", {}).get("total_r", 0.0)
        if a[sid]["distribution"]["n"] == 0:
            continue
        survives = "yes" if tb > 0 else "NO"
        print(f"  {sid:8s} {ta:10.2f} {tb:10.2f} {survives:>9s}")
    print("  an edge that disappears at double slippage is not robust enough to deploy, because the "
          "slippage estimate itself carries error")


if __name__ == "__main__":
    sys.exit(main())
