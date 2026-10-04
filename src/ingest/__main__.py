"""Run the ingest process: poll CoinDCX, normalize, write SQLite.

    python -m ingest                 # run until interrupted
    python -m ingest --check         # validate configuration and the task set, then exit
    python -m ingest --once          # one pass of every due task, then exit
    python -m ingest --futures-only  # skip the spot candle series

This is **process 1** of the two-process topology in specs/10 §3: it owns the only writing connection
to SQLite, and the app process (``python -m app``) reads. Splitting them is what lets the dashboard
restart without interrupting ingestion and ingestion restart without dropping the dashboard.

It is a different thing from ``python -m recorder``, and the distinction caused real confusion worth
recording. The **recorder** (Phase 1) appends unmodified payloads to ``data/raw`` as JSONL — the
raw-first archive of hard invariant 8, which exists so a transform bug can be re-run against the
original bytes. The **ingest process** normalizes into the SQLite tables the feature layer reads.
``store.import_raw`` bridges the two for the Tier B streams only, so running the recorder alone
leaves ``bars`` frozen at whenever ingest last ran — which is exactly what happened here and is
recorded as F-035. Both are wanted on a host: the recorder for the archive, ingest for the live
tables.

``--check`` exists for the same reason ``app --check`` does: the most valuable thing to know before a
deployment is whether startup *would* succeed, and finding out by starting the real thing is a worse
way to learn it.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Any

from config.settings import Settings
from core.errors import ALL_ERRORS, ConfigError, StartupError
from core.logging import get_logger, setup_logging
from ingest.client import IngestClient
from ingest.dq import DQMonitor
from ingest.pipeline import IngestPipeline
from ingest.scheduler import Scheduler
from normalize.canonical import Product, Timeframe
from store import open_store

log = get_logger("ingest.main")
COMPONENT = "ingest.main"

ASSETS = ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")

#: ``/exchange/ticker`` returns every market in one response, so the normalizer needs the mapping from
#: the venue's market name to our asset id. Derived from the asset ids rather than written out twice:
#: ``B-BTC_USDT`` -> ``BTCUSDT``.
def spot_markets(assets: tuple[str, ...] = ASSETS) -> dict[str, str]:
    out: dict[str, str] = {}
    for asset in assets:
        if not asset.startswith("B-") or "_" not in asset:
            raise ConfigError(
                f"cannot derive a spot market name from the asset id {asset!r}",
                component=COMPONENT, operation="spot_markets",
                context={"asset": asset, "expected_shape": "B-<BASE>_<QUOTE>"},
                likely_cause="an asset id that does not follow the venue's futures pair convention",
                remediation="use the venue's own pair ids (B-BTC_USDT); the spot market name is "
                            "derived from them so the mapping cannot drift",
            )
        out[asset[2:].replace("_", "")] = asset
    return out


#: The candle series ingest keeps current. Futures is what every strategy reads; spot is needed for
#: the basis family and the spot/futures disagreement check (specs/11 §9).
FUTURES_CANDLES: tuple[tuple[Product, Timeframe, str], ...] = (
    (Product.FUTURES, Timeframe.M1, "1"),
    (Product.FUTURES, Timeframe.M15, "15"),
    (Product.FUTURES, Timeframe.H1, "60"),
    (Product.FUTURES, Timeframe.H4, "240"),
    (Product.FUTURES, Timeframe.D1, "1D"),
)
SPOT_CANDLES: tuple[tuple[Product, Timeframe, str], ...] = (
    (Product.SPOT, Timeframe.M1, "1m"),
    (Product.SPOT, Timeframe.M15, "15m"),
    (Product.SPOT, Timeframe.H1, "1h"),
    (Product.SPOT, Timeframe.D1, "1d"),
)

#: The 15m futures series is **not optional**: ACT sizes its trigger unit from ATR on 15m and
#: confirms on a 1m close (specs/03 §2). Without these two every strategy arms and then reports
#: "not ready" forever, which is a silent no-op rather than a visible failure — so startup asserts it.
REQUIRED_FOR_ACT = ((Product.FUTURES, Timeframe.M15), (Product.FUTURES, Timeframe.M1))


def build_pipeline(store: Any, settings: Settings, *, assets: tuple[str, ...] = ASSETS
                   ) -> tuple[IngestPipeline, DQMonitor]:
    f = settings.freshness
    windows = {
        "futures_rt": int(f.futures_rt_max_age_s * 1000),
        "spot_ticker": int(f.spot_ticker_max_age_s * 1000),
        "futures_book": int(f.book_max_age_s * 1000),
        "spot_book": int(f.book_max_age_s * 1000),
        "positioning": int(f.positioning_max_age_s * 1000),
    }
    dq = DQMonitor(windows, clock_tolerance_ms=int(f.clock_skew_tolerance_s * 1000),
                   disagree_tolerance_ms=2000)
    pipeline = IngestPipeline(client=IngestClient(), store=store, dq=dq, assets=assets,
                              spot_markets=spot_markets(assets), settings=settings)
    return pipeline, dq


def build_scheduler(store: Any, settings: Settings, *, futures_only: bool = False,
                    assets: tuple[str, ...] = ASSETS) -> tuple[Scheduler, DQMonitor]:
    pipeline, dq = build_pipeline(store, settings, assets=assets)
    combos = FUTURES_CANDLES if futures_only else FUTURES_CANDLES + SPOT_CANDLES
    missing = [f"{p.value}:{tf.value}" for p, tf in REQUIRED_FOR_ACT
               if not any(c[0] is p and c[1] is tf for c in combos)]
    if missing:
        raise StartupError(
            f"the candle set omits {', '.join(missing)}, which ACT needs",
            component=COMPONENT, operation="build_scheduler",
            context={"missing": missing, "combos": [f"{p.value}:{tf.value}" for p, tf, _ in combos]},
            likely_cause="the candle combination list was edited",
            remediation="ACT sizes its unit on 15m and confirms on a 1m close; without both, every "
                        "strategy arms and then waits forever without reporting a failure",
        )
    tasks = pipeline.build_tasks(candle_timeframes=list(combos))
    scheduler = Scheduler(tasks, store=store, max_concurrent=4)
    return scheduler, dq


async def _run(scheduler: Scheduler) -> int:
    """Run until interrupted, installing signal handlers where the platform has them.

    Windows has no ``SIGTERM`` through ``loop.add_signal_handler``, so the handlers are installed
    best-effort and ``KeyboardInterrupt`` is the fallback. A stop is a *clean* stop either way: the
    scheduler finishes its in-flight polls and the store closes in the caller's ``finally``, because
    an abrupt exit mid-write is how a WAL ends up needing recovery.
    """
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, scheduler.stop)
        except (NotImplementedError, RuntimeError, ValueError):
            pass            # Windows: fall through to KeyboardInterrupt
    try:
        await scheduler.run_forever()
    except KeyboardInterrupt:
        log.info("interrupted", detail="stopping cleanly")
        scheduler.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="crypto-trader ingest process (writes SQLite)")
    ap.add_argument("--check", action="store_true", help="validate configuration and tasks, then exit")
    ap.add_argument("--once", action="store_true", help="one pass of every due task, then exit")
    ap.add_argument("--futures-only", action="store_true",
                    help="skip the spot candle series (halves the request budget; disables the basis "
                         "family and the spot/futures disagreement check)")
    args = ap.parse_args(argv)

    settings = Settings()
    setup_logging(settings.paths.log_dir, log_name="ingest")
    store = open_store(settings.paths.state_db, migrate=True)

    try:
        scheduler, dq = build_scheduler(store, settings, futures_only=args.futures_only)
    except StartupError as e:
        print(f"STARTUP FAILED: {e.message}", file=sys.stderr)
        for problem in e.context.get("problems", []):
            print(f"  - {problem}", file=sys.stderr)
        print(f"  fix: {e.remediation}", file=sys.stderr)
        store.close()
        return 2
    except ALL_ERRORS as e:
        print(f"STARTUP FAILED: {e}", file=sys.stderr)
        store.close()
        return 2

    try:
        if args.check:
            print("ingest preflight OK")
            print(f"  database:  {settings.paths.state_db}")
            print(f"  assets:    {', '.join(ASSETS)}")
            print(f"  tasks:     {len(scheduler.tasks)}")
            by_priority: dict[int, int] = {}
            for t in scheduler.tasks:
                by_priority[int(t.priority)] = by_priority.get(int(t.priority), 0) + 1
            for p in sorted(by_priority):
                print(f"    P{p}: {by_priority[p]}")
            print(f"  dq sources: {', '.join(sorted(dq.sources))}")
            return 0

        if args.once:
            asyncio.run(scheduler.tick())
            for s in scheduler.stats():
                if s["successes"] or s["failures"]:
                    print(f"{s['name']:40s} ok={s['successes']} fail={s['failures']} "
                          f"{s['last_latency_ms']} ms")
            return 0

        return asyncio.run(_run(scheduler))
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
