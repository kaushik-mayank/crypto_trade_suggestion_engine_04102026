"""Run the app: evaluate on a cadence, serve the dashboard, report health.

    python -m app                 # evaluate once, print the result, exit
    python -m app --serve         # evaluate on a cadence and serve the dashboard
    python -m app --check         # preflight only: prove the system could start, then exit

``--check`` exists because the most valuable thing to know before a deployment is whether startup
*would* succeed, and finding out by starting the real thing is a worse way to learn it.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from api.app import create_app
from api.services import build_services
from app.runner import AppRunner, load_classifiers
from audit.trail import AuditTrail
from config.parameters import load_registry
from config.settings import Settings
from core.errors import ALL_ERRORS, StartupError
from core.logging import get_logger, setup_logging
from features.engine import FeatureEngine
from normalize.canonical import Product, Timeframe
from risk.engine import RiskEngine
from snapshot.builder import WARMUP_BARS, SeriesRequest, SnapshotBuilder, SnapshotRequest
from snapshot.models import InstrumentKey, SeriesKey
from store import open_store
from store.reader import ReadOnlyStore
from strategy.registry import live_strategies

log = get_logger("app.main")

ASSETS = ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")
#: The trigger timeframes ACT needs beyond the alpha set: 15m sizes the unit, 1m confirms.
TRIGGER_BARS = {Timeframe.M15: 200, Timeframe.M1: 240}


def build_request(assets=ASSETS, product=Product.FUTURES) -> SnapshotRequest:
    series = [SeriesRequest(SeriesKey(a, product, tf), bars=WARMUP_BARS[tf])
              for a in assets for tf in (Timeframe.H1, Timeframe.H4, Timeframe.D1)]
    series += [SeriesRequest(SeriesKey(a, product, tf), bars=n)
               for a in assets for tf, n in TRIGGER_BARS.items()]
    return SnapshotRequest(
        series=tuple(series),
        tier_b=tuple((s, a) for a in assets
                     for s in ("futures_rt", "spot_ticker", "positioning", "futures_book",
                               "spot_book")),
        instruments=tuple(InstrumentKey(a, product) for a in assets),
        forming=tuple(SeriesKey(a, product, Timeframe.H1) for a in assets),
        tier_b_history_ms=7 * 86_400_000,
    )


def build_runner(store: Any, settings: Settings) -> AppRunner:
    registry = load_registry(settings.paths.parameters_file)
    params = {p.name: p.value for p in registry.parameters}
    unset = frozenset(p.name for p in registry.parameters if p.status == "unset")
    engine = FeatureEngine(params, parameter_hash=registry.parameter_hash, unset_params=unset)
    runner = AppRunner(
        builder=SnapshotBuilder.from_settings(store, settings, registry.parameter_hash),
        engine=engine, request=build_request(),
        strategies=live_strategies(params),
        risk=RiskEngine(params=params),
        classifiers=load_classifiers(ASSETS, directory=str(Path(settings.paths.parameters_file).parent)),
        assets=ASSETS,
    )
    runner.preflight()
    return runner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="crypto-trader suggestion engine")
    ap.add_argument("--serve", action="store_true", help="serve the dashboard after each evaluation")
    ap.add_argument("--check", action="store_true", help="preflight only, then exit")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between evaluations")
    ap.add_argument("--once", action="store_true", help="evaluate once and exit")
    args = ap.parse_args(argv)

    settings = Settings()
    setup_logging(settings.paths.log_dir, log_name="app")
    store = open_store(settings.paths.state_db, migrate=False)

    try:
        runner = build_runner(store, settings)
    except StartupError as e:
        # Deterministic and loud: every problem at once, on stderr, with a non-zero exit.
        print(f"STARTUP FAILED: {e.message}", file=sys.stderr)
        for problem in e.context.get("problems", []):
            print(f"  - {problem}", file=sys.stderr)
        print(f"  fix: {e.remediation}", file=sys.stderr)
        return 2
    except ALL_ERRORS as e:
        print(f"STARTUP FAILED: {e}", file=sys.stderr)
        return 2

    if args.check:
        print("preflight OK: the system can start")
        print(f"  assets:     {', '.join(runner.assets)}")
        print(f"  strategies: {', '.join(s.id for s in runner.strategies)}")
        print(f"  degraded regimes: "
              f"{[a for a, c in runner.classifiers.items() if c.model.degraded] or 'none'}")
        store.close()
        return 0

    audit = AuditTrail(store=store)

    def evaluate_once() -> Any:
        result = runner.evaluate(int(time.time() * 1000))
        if result.error is None:
            audit.record_evaluation(result)
        return result

    if args.serve:
        import uvicorn

        reader = ReadOnlyStore(settings.paths.state_db)
        signals, market, health = build_services(reader, health_source=runner.health)
        app = create_app(signals=signals, market=market, health=health,
                         host=settings.server.host, auth_token=settings.server.auth_token,
                         public_via_tunnel=settings.server.public_via_tunnel,
                         regimes_source=lambda: {a: c.stats() for a, c in runner.classifiers.items()})
        evaluate_once()
        log.info("serving", host=settings.server.host, port=settings.server.port)
        uvicorn.run(app, host=settings.server.host, port=settings.server.port, log_level="warning")
        return 0

    while True:
        result = evaluate_once()
        published = len(result.published)
        print(f"{result.as_of_ms}: {published} published, {result.elapsed_ms} ms, "
              f"health={runner.health().status.value}")
        if args.once:
            break
        time.sleep(args.interval)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
