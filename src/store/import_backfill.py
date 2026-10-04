"""Import the Phase-1 backfill into SQLite (PHASE-05, decision D-4).

``data/backfill/**.parquet`` is an **immutable verified artifact**: 42 series, sorted, unique, with
per-series completeness recorded in ``verify.json`` (F-018). This loads it into the store so SQLite
becomes the single source of truth (specs/11 §8.1); afterwards Parquet is regenerated *from* SQLite
and never written directly.

    python -m store.import_backfill --timeframes 1h,1d,15m      # coarse first (fast)
    python -m store.import_backfill                              # everything, 1m included

Resumable by construction: rows already present are left untouched, so a re-run is a no-op and an
interrupted run continues.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

from core.errors import ConfigError, StoreError
from core.logging import get_logger, setup_logging
from normalize.canonical import Product, Timeframe
from store.sqlite_store import SqliteStore, open_store

log = get_logger("store.import_backfill")

#: backfill resolution directory name -> canonical timeframe, per product.
SPOT_RES = {"1m": Timeframe.M1, "15m": Timeframe.M15, "1h": Timeframe.H1, "1d": Timeframe.D1}
FUT_RES = {"1": Timeframe.M1, "5": Timeframe.M5, "15": Timeframe.M15, "30": Timeframe.M30,
           "60": Timeframe.H1, "240": Timeframe.H4, "1D": Timeframe.D1}
#: 480/3D/1M are served (F-008) but are not in the canonical timeframe stack (specs/02 §3), so they
#: stay in Parquet as evidence and are not imported.
SKIPPED_RES = {"480", "3D", "1M"}

BATCH_ROWS = 250_000


def _series(backfill_dir: Path) -> list[tuple[str, Product, str, Timeframe, Path]]:
    out = []
    for product, table in (("spot", SPOT_RES), ("futures", FUT_RES)):
        pdir = backfill_dir / product
        if not pdir.is_dir():
            continue
        for asset_dir in sorted(p for p in pdir.iterdir() if p.is_dir()):
            for parquet in sorted(asset_dir.glob("*.parquet")):
                res = parquet.stem
                if res in SKIPPED_RES:
                    continue
                if res not in table:
                    log.warning("unknown_resolution_skipped", product=product, resolution=res,
                                path=str(parquet))
                    continue
                out.append((asset_dir.name, Product(product), res, table[res], parquet))
    return out


def import_series(store: SqliteStore, asset: str, product: Product, timeframe: Timeframe,
                  path: Path, *, fetched_at_ms: int) -> int:
    """Stream one Parquet series into the store. Values are read as strings to stay exact."""
    pf = pq.ParquetFile(path)
    total = 0
    for batch in pf.iter_batches(batch_size=BATCH_ROWS, columns=["time_ms", "open", "high", "low",
                                                                "close", "volume"]):
        d = batch.to_pydict()
        rows = zip(
            [asset] * len(d["time_ms"]), [product.value] * len(d["time_ms"]),
            [timeframe.value] * len(d["time_ms"]), d["time_ms"],
            [str(x) for x in d["open"]], [str(x) for x in d["high"]],
            [str(x) for x in d["low"]], [str(x) for x in d["close"]], [str(x) for x in d["volume"]],
        )
        total += store.bulk_insert_bars(
            ((a, p, tf, t, o, h, low, c, v) for a, p, tf, t, o, h, low, c, v in rows),
            source=f"backfill:{path.name}", fetched_at_ms=fetched_at_ms, is_final=True)
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="store.import_backfill", description=__doc__)
    ap.add_argument("--backfill-dir", type=Path, default=Path("data/backfill"))
    ap.add_argument("--db", type=Path, default=Path("data/state.db"))
    ap.add_argument("--timeframes", help="comma-separated canonical timeframes to import (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(Path("logs"), console_level="INFO", log_name="import_backfill")

    if not args.backfill_dir.is_dir():
        raise ConfigError(
            f"backfill directory not found: {args.backfill_dir}",
            component="store.import_backfill", operation="main",
            context={"path": str(args.backfill_dir)},
            likely_cause="the one-time backfill has not been run, or --backfill-dir is wrong",
            remediation="run `python -m backfill` first; it writes data/backfill/{product}/{asset}/{res}.parquet",
        )
    verify = args.backfill_dir / "verify.json"
    if not verify.exists():
        log.warning("verify_json_missing", detail="importing unverified history",
                    remediation="run `python -m backfill --verify` to record completeness first")

    wanted = {t.strip() for t in args.timeframes.split(",")} if args.timeframes else None
    series = [s for s in _series(args.backfill_dir) if wanted is None or s[3].value in wanted]
    if not series:
        raise ConfigError(
            "no matching series found to import",
            component="store.import_backfill", operation="main",
            context={"backfill_dir": str(args.backfill_dir), "timeframes": sorted(wanted) if wanted else None},
            likely_cause="a --timeframes filter that matches nothing, or an empty backfill directory",
            remediation="list data/backfill/*/*/ to see which resolutions exist",
        )

    log.info("import_starting", series=len(series), db=str(args.db),
             timeframes=sorted({s[3].value for s in series}))
    if args.dry_run:
        for asset, product, res, tf, path in series:
            print(f"{product.value:8s} {asset:14s} {res:4s} -> {tf.value:4s}  {path.stat().st_size/1e6:7.1f} MB")
        return 0

    fetched_at_ms = int(time.time() * 1000)
    summary: dict[str, dict] = {}
    store = open_store(args.db)
    try:
        for asset, product, res, tf, path in series:
            t0 = time.time()
            try:
                written = import_series(store, asset, product, tf, path, fetched_at_ms=fetched_at_ms)
            except StoreError:
                raise
            except Exception as e:  # noqa: BLE001 - reclassified with the series that failed
                raise StoreError(
                    f"importing {product.value}:{asset}:{res} failed",
                    component="store.import_backfill", operation="import_series",
                    context={"path": str(path), "asset": asset, "resolution": res},
                    likely_cause="a malformed parquet file, or a schema change in the backfill writer",
                    remediation=f"re-run `python -m backfill --only {product.value}:{asset}:{res} --force` for that series"
                                ,
                    cause=e,
                ) from e
            count = store.bar_count(asset, product, tf)
            summary[f"{product.value}:{asset}:{tf.value}"] = {"written": written, "in_store": count,
                                                             "seconds": round(time.time() - t0, 1)}
            log.info("series_imported", product=product.value, asset=asset, timeframe=tf.value,
                     written=written, in_store=count, seconds=round(time.time() - t0, 1))
        out = args.backfill_dir / "import_summary.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info("import_complete", series=len(summary), total_rows=sum(v["in_store"] for v in summary.values()),
                 summary=str(out))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
