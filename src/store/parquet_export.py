"""Parquet export — a **derived** artifact, regenerated from SQLite and never written directly.

Why it exists at all: DuckDB reads Parquet far faster than it reads 20M rows through SQLite, which
matters for walk-forward analysis. Why it is derived: two write paths would let the live and backtest
views diverge, which `specs/10` §1 ranks second only to correctness.

Partitioned ``{product}/{asset}/{timeframe}/{year}-{month}.parquet``. Prices stay exact
(``decimal128(38,18)``), matching the one-time backfill's schema.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from core.errors import StoreError
from core.logging import get_logger, setup_logging
from normalize.canonical import Product, Timeframe
from store.sqlite_store import SqliteStore, open_store

log = get_logger("store.parquet_export")

DEC = pa.decimal128(38, 18)
SCHEMA = pa.schema([("time_ms", pa.int64()), ("open", DEC), ("high", DEC), ("low", DEC),
                    ("close", DEC), ("volume", DEC), ("is_final", pa.bool_())])


def _month_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


def export_series(store: SqliteStore, out_dir: Path, asset: str, product: Product,
                  timeframe: Timeframe, *, final_only: bool = True) -> dict[str, int]:
    """Regenerate every month file for one series. Returns ``{month: rows}``."""
    sql = ("SELECT open_time_ms, open, high, low, close, volume_base, is_final FROM bars "
           "WHERE asset=? AND product=? AND timeframe=?")
    params = [asset, product.value, timeframe.value]
    if final_only:
        sql += " AND is_final=1"
    sql += " ORDER BY open_time_ms"
    rows = store._execute(sql, params, operation="export_series").fetchall()

    by_month: dict[str, list] = {}
    for r in rows:
        by_month.setdefault(_month_of(r["open_time_ms"]), []).append(r)

    target = out_dir / product.value / asset / timeframe.value
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    for month, month_rows in by_month.items():
        table = pa.table({
            "time_ms": pa.array([r["open_time_ms"] for r in month_rows], pa.int64()),
            "open": pa.array([Decimal(r["open"]) for r in month_rows], DEC),
            "high": pa.array([Decimal(r["high"]) for r in month_rows], DEC),
            "low": pa.array([Decimal(r["low"]) for r in month_rows], DEC),
            "close": pa.array([Decimal(r["close"]) for r in month_rows], DEC),
            "volume": pa.array([Decimal(r["volume_base"]) for r in month_rows], DEC),
            "is_final": pa.array([bool(r["is_final"]) for r in month_rows], pa.bool_()),
        }, schema=SCHEMA)
        tmp = target / f".{month}.parquet.tmp"
        final = target / f"{month}.parquet"
        try:
            pq.write_table(table, tmp, compression="zstd")
            tmp.replace(final)                     # atomic: a reader never sees a half-written file
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise StoreError(
                f"could not write {final}",
                component="store.parquet_export", operation="export_series",
                context={"path": str(final), "rows": len(month_rows)},
                likely_cause="the data volume is full or read-only",
                remediation="free space; Parquet is derived, so it can also simply be deleted and "
                            "regenerated later",
                cause=e,
            ) from e
        written[month] = len(month_rows)
    return written


def export_all(store: SqliteStore, out_dir: Path, *, final_only: bool = True) -> dict[str, dict[str, int]]:
    series = store._execute(
        "SELECT DISTINCT asset, product, timeframe FROM bars ORDER BY product, asset, timeframe",
        operation="export_all").fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in series:
        key = f"{r['product']}:{r['asset']}:{r['timeframe']}"
        out[key] = export_series(store, out_dir, r["asset"], Product(r["product"]),
                                 Timeframe(r["timeframe"]), final_only=final_only)
        log.info("series_exported", series=key, months=len(out[key]), rows=sum(out[key].values()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="store.parquet_export", description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("data/state.db"))
    ap.add_argument("--out", type=Path, default=Path("data/parquet"))
    ap.add_argument("--include-non-final", action="store_true",
                    help="also export bars that are not yet final (default: final only)")
    args = ap.parse_args(argv)
    setup_logging(Path("logs"), console_level="INFO", log_name="parquet_export")
    store = open_store(args.db, migrate=False)
    try:
        result = export_all(store, args.out, final_only=not args.include_non_final)
    finally:
        store.close()
    total = sum(sum(m.values()) for m in result.values())
    log.info("export_complete", series=len(result), rows=total, out=str(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
