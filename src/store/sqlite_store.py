"""SQLite store — the single source of truth (specs/11 §8.1).

Design notes that matter operationally:

* **WAL + ``synchronous=FULL``.** This runs on a machine that sleeps and gets killed; the source of
  truth is worth the fsync. Volume is ~260k rows/month, so the cost is irrelevant.
* **Decimals are TEXT.** REAL would round exchange prices silently (invariant 5).
* **Every sqlite error is translated** into a classified :mod:`core.errors` failure with a
  remediation. "database is locked", "disk full" and "file is not a database" are four different
  problems with four different fixes, and a bare ``sqlite3.OperationalError`` distinguishes none of
  them.
* **``as_of_ms`` is enforced, not trusted.** Reads that take an ``as_of`` re-check the rows they are
  about to return and raise :class:`PointInTimeViolation` if any is newer. A look-ahead bug is an
  application defect, and the store is the last place it can be caught cheaply.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from core.errors import IntegrityError, MigrationError, PointInTimeViolation, StoreError
from core.health import ComponentHealth, SystemStatus
from core.logging import get_logger
from normalize.canonical import (
    Bar,
    BookSnapshot,
    Fee,
    FuturesRT,
    InstrumentMeta,
    MarginTier,
    Positioning,
    Product,
    SpotTicker,
    Timeframe,
    Trade,
)
from store.interface import BarMutation, DQEvent, RunRecord, UpsertResult
from store.migrations import MIGRATIONS, latest_version

log = get_logger("store.sqlite")

COMPONENT = "store.sqlite"

#: Point-in-time sources and the column that carries their own timestamp.
PIT_SOURCES = {
    "futures_rt": ("futures_rt", "source_ts_ms"),
    "spot_ticker": ("spot_ticker", "ts_ms"),
    "positioning": ("positioning", "ts_ms"),
    "spot_book": ("book_snapshots", "ts_ms"),
    "futures_book": ("book_snapshots", "ts_ms"),
}


def _t(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def _d(v: Any) -> Decimal | None:
    """Optional decimal column."""
    return None if v is None else Decimal(str(v))


def _dreq(v: Any, *, field: str, operation: str) -> Decimal:
    """A NOT NULL decimal column. A NULL here means the row is corrupt, so it fails loudly
    rather than propagating None into a price."""
    if v is None:
        raise IntegrityError(
            f"{field} is NULL in a NOT NULL column",
            component=COMPONENT, operation=operation, context={"field": field},
            likely_cause="the row was written by an older schema, or the file is damaged",
            remediation="rebuild the affected series: bars refetch from the API and raw JSONL is intact",
        )
    return Decimal(str(v))


def canonical_decimal(value: Any) -> str:
    """A string that is equal whenever the *value* is equal, whatever precision it arrived with.

    ``Decimal("105")`` and ``Decimal("105.0")`` are the same price and different strings. Hashing the
    strings makes the second look like a revision of the first, which the store reports as a
    ``BAR_MUTATION`` — logged at ERROR and marking signals non-replayable. The venue is already known
    to send inconsistent precision (hard invariant 6, verified on order books: ``"11570.67000000"``
    beside ``"2001"``), so this is a live hazard rather than a theoretical one, and a flood of false
    mutations would train an operator to ignore the one alert that means history really did change.

    ``normalize()`` collapses trailing zeros but can produce exponent form (``Decimal("100")`` ->
    ``1E+2``), so the result is formatted with ``'f'`` to keep it plain.
    """
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        # Not a number: fall back to the literal, which still compares equal to itself. Reaching here
        # means normalization let through something non-numeric, which is its bug to report, not ours
        # to mask -- so the value is preserved rather than replaced.
        return str(value)


def content_hash(bar: Bar) -> str:
    payload = "|".join(canonical_decimal(x)
                       for x in (bar.open, bar.high, bar.low, bar.close, bar.volume_base))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def same_content(row: Any, bar: Bar) -> bool:
    """Whether a stored row and a fetched bar carry the same OHLCV *values*.

    The authority on "has this bar changed", with :func:`content_hash` as the fast path. Compared by
    value rather than by stored hash so rows written before the canonicalisation above do not all
    read as mutations the first time they are re-fetched.
    """
    return all(canonical_decimal(row[column]) == canonical_decimal(value)
               for column, value in (("open", bar.open), ("high", bar.high), ("low", bar.low),
                                     ("close", bar.close), ("volume_base", bar.volume_base)))


class SqliteStore:
    """Concrete :class:`store.interface.StorageInterface`."""

    def __init__(self, path: str | Path, *, timeout_s: float = 30.0, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None
        self._timeout_s = timeout_s
        self._open()

    # ---- connection and error translation --------------------------------------------------

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StoreError(
                f"cannot create the directory for the database: {self.path.parent}",
                component=COMPONENT, operation="open", context={"path": str(self.path)},
                likely_cause="the data directory is not writable, or the volume is missing",
                remediation="check the path in settings (CT_PATHS__STATE_DB) and its permissions",
                cause=e,
            ) from e
        try:
            if self.read_only:
                conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=self._timeout_s)
            else:
                conn = sqlite3.connect(self.path, timeout=self._timeout_s, isolation_level=None)
            conn.row_factory = sqlite3.Row
            if not self.read_only:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={int(self._timeout_s * 1000)}")
        except sqlite3.Error as e:
            raise self._translate(e, operation="open", context={"path": str(self.path)}) from e
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError(
                "the store is closed",
                component=COMPONENT, operation="conn", context={"path": str(self.path)},
                likely_cause="close() was called and the store was reused afterwards",
                remediation="construct a new SqliteStore; connections are not reopened implicitly",
            )
        return self._conn

    def _translate(self, e: sqlite3.Error, *, operation: str, context: dict[str, Any] | None = None) -> StoreError:
        """Turn an opaque sqlite error into one that names the problem and its fix."""
        msg = str(e).lower()
        ctx = {"path": str(self.path), **(context or {})}
        if isinstance(e, sqlite3.IntegrityError):
            return IntegrityError(
                f"integrity constraint violated: {e}",
                component=COMPONENT, operation=operation, context=ctx,
                likely_cause="a duplicate primary key, or a foreign key pointing at a missing row",
                remediation="this is a logic error in the caller: upserts must be explicit about conflict handling",
                cause=e,
            )
        if "database is locked" in msg or "database table is locked" in msg:
            return StoreError(
                "the database is locked by another writer",
                component=COMPONENT, operation=operation, context=ctx,
                likely_cause="two processes are writing the same state.db (the app and a backtest, "
                             "or two app instances)",
                remediation="run one writer per database file; the recorder has its own JSONL archive "
                            "and must not share this file",
                cause=e,
            )
        if "no such table" in msg or "no such column" in msg:
            return MigrationError(
                f"schema is missing an object: {e}",
                component=COMPONENT, operation=operation, context=ctx,
                likely_cause="migrations were not run, or the database was created by an older version",
                remediation="call SqliteStore.migrate(); if the file predates the current schema, "
                            "rebuild it — bars are refetchable and raw JSONL is intact",
                cause=e,
            )
        if "file is not a database" in msg or "malformed" in msg or "corrupt" in msg:
            return StoreError(
                f"the database file is unreadable or corrupt: {e}",
                component=COMPONENT, operation=operation, context=ctx,
                likely_cause="the file was truncated by a full disk or an unclean host shutdown",
                remediation="move the file aside and rebuild: bars refetch from the API and the "
                            "recorder's raw JSONL is the durable copy (specs/18 §6)",
                cause=e,
            )
        if "disk" in msg and ("full" in msg or "i/o" in msg):
            return StoreError(
                f"the disk rejected a write: {e}",
                component=COMPONENT, operation=operation, context=ctx,
                likely_cause="the volume holding data/ is full, or failing",
                remediation="free space; the health endpoint reports free disk so this is visible "
                            "before writes start failing",
                cause=e,
            )
        if "readonly" in msg or "attempt to write a readonly database" in msg:
            return StoreError(
                "the database is read-only",
                component=COMPONENT, operation=operation, context=ctx,
                likely_cause="the store was opened read_only=True, or the file permissions changed",
                remediation="open with read_only=False for a writer; check file ownership",
                cause=e,
            )
        return StoreError(
            f"sqlite error: {e}",
            component=COMPONENT, operation=operation, context=ctx,
            likely_cause="an unclassified sqlite failure — see the cause",
            remediation="inspect the cause; add a translation here if this recurs",
            cause=e,
        )

    def _execute(self, sql: str, params: Sequence[Any] = (), *, operation: str) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, params)
        except sqlite3.Error as e:
            raise self._translate(e, operation=operation, context={"sql": sql.split("\n")[0][:120]}) from e

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]], *, operation: str) -> sqlite3.Cursor:
        try:
            return self.conn.executemany(sql, rows)
        except sqlite3.Error as e:
            raise self._translate(e, operation=operation, context={"sql": sql.split("\n")[0][:120]}) from e

    # ---- migrations ------------------------------------------------------------------------

    def migrate(self) -> int:
        """Apply pending migrations. Idempotent; a version gap or unknown version is fatal."""
        self._execute("CREATE TABLE IF NOT EXISTS schema_version ("
                      "version INTEGER PRIMARY KEY, applied_ms INTEGER NOT NULL, description TEXT NOT NULL)",
                      operation="migrate")
        applied = {int(r["version"]) for r in self._execute("SELECT version FROM schema_version",
                                                           operation="migrate").fetchall()}
        known = {v for v, _, _ in MIGRATIONS}
        unknown = applied - known
        if unknown:
            raise MigrationError(
                f"the database has migration versions this build does not know: {sorted(unknown)}",
                component=COMPONENT, operation="migrate",
                context={"applied": sorted(applied), "known": sorted(known)},
                likely_cause="the database was written by a NEWER build than this one",
                remediation="deploy the newer build, or rebuild the database; never downgrade a "
                            "schema in place",
            )
        import time
        for version, description, sql in MIGRATIONS:
            if version in applied:
                continue
            if applied and version < max(applied):
                raise MigrationError(
                    f"migration {version} is older than the applied maximum {max(applied)}",
                    component=COMPONENT, operation="migrate",
                    context={"applied": sorted(applied), "pending": version},
                    likely_cause="migrations were reordered or renumbered after being applied",
                    remediation="migrations are forward-only and immutable once released",
                )
            try:
                # Statements are executed individually inside one transaction: executescript()
                # would COMMIT implicitly and make the migration non-atomic.
                self.conn.execute("BEGIN")
                for statement in (s.strip() for s in sql.split(";")):
                    if statement:
                        self.conn.execute(statement)
                self.conn.execute("INSERT INTO schema_version (version, applied_ms, description) VALUES (?, ?, ?)",
                                  (version, int(time.time() * 1000), description))
                self.conn.execute("COMMIT")
            except sqlite3.Error as e:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise MigrationError(
                    f"migration {version} ({description}) failed and was rolled back",
                    component=COMPONENT, operation="migrate",
                    context={"version": version, "description": description},
                    likely_cause="a syntax error in the migration, or an object that already exists",
                    remediation="fix the migration; the database is unchanged, so no partial schema exists",
                    cause=e,
                ) from e
            log.info("migration_applied", version=version, description=description)
        return self.schema_version()

    def schema_version(self) -> int:
        """0 when the database has never been migrated — that is a *state*, not an error, so
        ``require_schema`` can report it with a remediation instead of a raw "no such table"."""
        present = self._execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'",
            operation="schema_version").fetchone()
        if present is None:
            return 0
        row = self._execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version",
                            operation="schema_version").fetchone()
        return int(row["v"])

    def require_schema(self) -> None:
        """Fail fast at startup if the schema is not current (deterministic startup failure)."""
        have, want = self.schema_version(), latest_version()
        if have != want:
            raise MigrationError(
                f"schema version is {have}, this build needs {want}",
                component=COMPONENT, operation="require_schema",
                context={"have": have, "want": want, "path": str(self.path)},
                likely_cause="migrations have not been run since the last deploy",
                remediation="run migrate() during startup before serving or recording",
            )

    # ---- bars ------------------------------------------------------------------------------

    def upsert_bars(self, bars: list[Bar], *, finalize_on_repeat: bool = True) -> UpsertResult:
        """Insert or update bars, detecting the two cases that matter.

        * a bar seen twice with **identical** content becomes ``is_final`` (the two-fetch rule);
        * a bar already ``is_final`` whose content **changed** is a mutation, reported to the caller
          so it can log, alarm, and flag affected signals (specs/11 §6.2).
        """
        result = UpsertResult()
        if not bars:
            return result
        keys = sorted({(b.asset, b.product.value, b.timeframe.value, b.open_time_ms) for b in bars})
        existing: dict[tuple, sqlite3.Row] = {}
        # Chunked so a large batch cannot build a statement past SQLITE_MAX_VARIABLE_NUMBER.
        for i in range(0, len(keys), 200):
            chunk = keys[i:i + 200]
            placeholders = ",".join("(?,?,?,?)" for _ in chunk)
            flat: list[Any] = [x for key in chunk for x in key]
            rows = self._execute(
                "SELECT asset, product, timeframe, open_time_ms, open, high, low, close, volume_base, "
                "is_final, content_hash, fetch_count, first_fetched_ms FROM bars "
                f"WHERE (asset, product, timeframe, open_time_ms) IN ({placeholders})",
                flat, operation="upsert_bars.select").fetchall()
            existing.update({(r["asset"], r["product"], r["timeframe"], r["open_time_ms"]): r for r in rows})

        to_write: list[tuple] = []
        for b in bars:
            key = (b.asset, b.product.value, b.timeframe.value, b.open_time_ms)
            h = content_hash(b)
            prev = existing.get(key)
            if prev is None:
                to_write.append((b.asset, b.product.value, b.timeframe.value, b.open_time_ms,
                                 _t(b.open), _t(b.high), _t(b.low), _t(b.close), _t(b.volume_base),
                                 1 if b.is_final else 0, 1 if b.synthesised else 0, b.source, h,
                                 b.fetched_at_ms, b.fetched_at_ms, 1))
                result.inserted += 1
                continue
            # The hash is the fast path; the value comparison is the authority. A row written before
            # the hash was canonicalised, or one the venue re-sent at a different precision, is the
            # same bar and must not be reported as a revision of itself.
            same = prev["content_hash"] == h or same_content(prev, b)
            was_final = bool(prev["is_final"])
            if same:
                is_final = 1 if (was_final or finalize_on_repeat) else 0
                if is_final and not was_final:
                    result.finalized += 1
                else:
                    result.unchanged += 1
                to_write.append((b.asset, b.product.value, b.timeframe.value, b.open_time_ms,
                                 prev["open"], prev["high"], prev["low"], prev["close"], prev["volume_base"],
                                 is_final, 1 if b.synthesised else 0, b.source, h,
                                 prev["first_fetched_ms"] or b.fetched_at_ms,
                                 b.fetched_at_ms, int(prev["fetch_count"]) + 1))
                continue
            # content changed
            if was_final:
                result.mutations.append(BarMutation(
                    asset=b.asset, product=b.product, timeframe=b.timeframe, open_time_ms=b.open_time_ms,
                    old={k: str(prev[k]) for k in ("open", "high", "low", "close", "volume_base")},
                    new={"open": str(b.open), "high": str(b.high), "low": str(b.low),
                         "close": str(b.close), "volume_base": str(b.volume_base)},
                ))
            result.updated += 1
            to_write.append((b.asset, b.product.value, b.timeframe.value, b.open_time_ms,
                             _t(b.open), _t(b.high), _t(b.low), _t(b.close), _t(b.volume_base),
                             1 if (was_final or b.is_final) else 0, 1 if b.synthesised else 0, b.source, h,
                             b.fetched_at_ms, b.fetched_at_ms, int(prev["fetch_count"]) + 1))

        self._executemany(
            "INSERT INTO bars (asset, product, timeframe, open_time_ms, open, high, low, close, volume_base,"
            " is_final, synthesised, source, content_hash, first_fetched_ms, last_fetched_ms, fetch_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (asset, product, timeframe, open_time_ms) DO UPDATE SET"
            " open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,"
            " volume_base=excluded.volume_base, is_final=excluded.is_final, synthesised=excluded.synthesised,"
            " source=excluded.source, content_hash=excluded.content_hash,"
            " last_fetched_ms=excluded.last_fetched_ms, fetch_count=excluded.fetch_count",
            to_write, operation="upsert_bars")
        return result

    def bulk_insert_bars(self, rows: Iterable[tuple], *, source: str, fetched_at_ms: int,
                         is_final: bool = True) -> int:
        """Fast path for the one-time historical import (PHASE-05).

        ``rows`` yields ``(asset, product, timeframe, open_time_ms, open, high, low, close, volume)``
        with prices already as strings. Deliberately **no** mutation detection: this loads immutable
        history that was verified by ``backfill --verify``, and doing a per-key SELECT for 20M rows
        would take hours. Existing rows are left untouched (``DO NOTHING``), so the import can be
        re-run and can never overwrite live data.
        """
        batch: list[tuple] = []
        written = 0
        sql = ("INSERT INTO bars (asset, product, timeframe, open_time_ms, open, high, low, close,"
               " volume_base, is_final, synthesised, source, content_hash, first_fetched_ms,"
               " last_fetched_ms, fetch_count) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,1)"
               " ON CONFLICT (asset, product, timeframe, open_time_ms) DO NOTHING")

        def flush() -> int:
            if not batch:
                return 0
            self._executemany(sql, batch, operation="bulk_insert_bars")
            n = len(batch)
            batch.clear()
            return n

        try:
            self.conn.execute("BEGIN")
            for asset, product, tf, t, o, h, low, c, v in rows:
                payload = f"{o}|{h}|{low}|{c}|{v}"
                batch.append((asset, product, tf, int(t), o, h, low, c, v, 1 if is_final else 0,
                              source, hashlib.sha256(payload.encode()).hexdigest()[:32],
                              fetched_at_ms, fetched_at_ms))
                if len(batch) >= 20_000:
                    written += flush()
            written += flush()
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return written

    def _bar_from_row(self, r: sqlite3.Row) -> Bar:
        return Bar(
            asset=r["asset"], product=Product(r["product"]), timeframe=Timeframe(r["timeframe"]),
            open_time_ms=r["open_time_ms"],
            open=_dreq(r["open"], field="open", operation="_bar_from_row"),
            high=_dreq(r["high"], field="high", operation="_bar_from_row"),
            low=_dreq(r["low"], field="low", operation="_bar_from_row"),
            close=_dreq(r["close"], field="close", operation="_bar_from_row"),
            volume_base=_dreq(r["volume_base"], field="volume_base", operation="_bar_from_row"),
            is_final=bool(r["is_final"]), synthesised=bool(r["synthesised"]),
            source=r["source"], fetched_at_ms=r["last_fetched_ms"],
        )

    def get_bars(self, asset: str, product: Product, timeframe: Timeframe, start_ms: int, end_ms: int,
                 *, final_only: bool = True, as_of_ms: int | None = None) -> list[Bar]:
        """Bars with ``start_ms <= open_time <= end_ms``, ascending.

        With ``as_of_ms``, only bars that had **closed** by then are returned, and the result is
        re-checked before it is handed back.
        """
        sql = ("SELECT * FROM bars WHERE asset=? AND product=? AND timeframe=? "
               "AND open_time_ms>=? AND open_time_ms<=?")
        params: list[Any] = [asset, product.value, timeframe.value, start_ms, end_ms]
        if final_only:
            sql += " AND is_final=1"
        if as_of_ms is not None:
            sql += " AND open_time_ms + ? <= ?"
            params += [timeframe.ms, as_of_ms]
        sql += " ORDER BY open_time_ms ASC"
        rows = self._execute(sql, params, operation="get_bars").fetchall()
        bars = [self._bar_from_row(r) for r in rows]
        if as_of_ms is not None:
            self._assert_no_lookahead(bars, as_of_ms, asset=asset, timeframe=timeframe)
        return bars

    def _assert_no_lookahead(self, bars: list[Bar], as_of_ms: int, *, asset: str, timeframe: Timeframe) -> None:
        offenders = [b.open_time_ms for b in bars if b.close_time_ms > as_of_ms]
        if offenders:
            raise PointInTimeViolation(
                f"{len(offenders)} bar(s) had not closed by as_of and would have leaked future data",
                component=COMPONENT, operation="get_bars",
                context={"asset": asset, "timeframe": timeframe.value, "as_of_ms": as_of_ms,
                         "offending_open_times": offenders[:5]},
                likely_cause="a query built without the as_of predicate, or a timeframe/ms mismatch",
                remediation="every backtest read must pass as_of_ms; this is the look-ahead boundary",
            )

    def latest_bar_time(self, asset: str, product: Product, timeframe: Timeframe,
                        *, final_only: bool = True) -> int | None:
        sql = "SELECT MAX(open_time_ms) AS t FROM bars WHERE asset=? AND product=? AND timeframe=?"
        if final_only:
            sql += " AND is_final=1"
        row = self._execute(sql, [asset, product.value, timeframe.value], operation="latest_bar_time").fetchone()
        return None if row["t"] is None else int(row["t"])

    def bar_count(self, asset: str, product: Product, timeframe: Timeframe, *, final_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS n FROM bars WHERE asset=? AND product=? AND timeframe=?"
        if final_only:
            sql += " AND is_final=1"
        return int(self._execute(sql, [asset, product.value, timeframe.value],
                                 operation="bar_count").fetchone()["n"])

    def non_final_bar_times(self, asset: str, product: Product, timeframe: Timeframe, *,
                            settled_before_ms: int, limit: int = 1000) -> list[int]:
        """Open times of stored-but-not-final bars that closed before ``settled_before_ms``, oldest first.

        A bar becomes final only when a second fetch returns it unchanged (specs/11 §6.2), so a bar
        pulled once during a catch-up stays non-final forever unless something re-requests it — and
        ``get_bars(final_only=True)``, which is what the snapshot builder uses, cannot see it. The
        result is history that is present and correct yet permanently invisible to the feature layer,
        reported as "warming" indefinitely (F-036). This query is what lets the ingest process find
        those bars and settle them.

        ``settled_before_ms`` excludes bars close enough to now that the venue may legitimately still
        be revising them; re-fetching those would churn without finalizing anything.
        """
        rows = self._execute(
            "SELECT open_time_ms FROM bars WHERE asset=? AND product=? AND timeframe=? "
            "AND is_final=0 AND open_time_ms + ? <= ? ORDER BY open_time_ms ASC LIMIT ?",
            [asset, product.value, timeframe.value, timeframe.ms, settled_before_ms, limit],
            operation="non_final_bar_times").fetchall()
        return [int(r["open_time_ms"]) for r in rows]

    # ---- point-in-time series --------------------------------------------------------------

    def get_point_in_time(self, asset: str, source: str, as_of_ms: int) -> Any | None:
        """The newest record of ``source`` for ``asset`` at or before ``as_of_ms``.

        **Never returns a record newer than ``as_of_ms``** — the result is re-checked.
        """
        if source not in PIT_SOURCES:
            raise StoreError(
                f"unknown point-in-time source {source!r}",
                component=COMPONENT, operation="get_point_in_time",
                context={"source": source, "known": sorted(PIT_SOURCES)},
                likely_cause="a typo, or a new source added without registering it here",
                remediation="register the table and its own-timestamp column in PIT_SOURCES",
            )
        table, ts_col = PIT_SOURCES[source]
        sql = f"SELECT * FROM {table} WHERE asset=? AND {ts_col}<=?"
        params: list[Any] = [asset, as_of_ms]
        if table == "book_snapshots":
            sql += " AND product=?"
            params.append(Product.SPOT.value if source == "spot_book" else Product.FUTURES.value)
        sql += f" ORDER BY {ts_col} DESC LIMIT 1"
        row = self._execute(sql, params, operation="get_point_in_time").fetchone()
        if row is None:
            return None
        ts = int(row[ts_col])
        if ts > as_of_ms:
            raise PointInTimeViolation(
                f"{source} returned a record stamped after as_of",
                component=COMPONENT, operation="get_point_in_time",
                context={"source": source, "asset": asset, "record_ts_ms": ts, "as_of_ms": as_of_ms},
                likely_cause="the ORDER BY/WHERE clause does not match the table's own timestamp column",
                remediation="check PIT_SOURCES for this source",
            )
        return self._hydrate(source, row)


    def get_point_in_time_history(self, asset: str, source: str, start_ms: int, end_ms: int,
                                  *, limit: int = 5000) -> list[Any]:
        """Every record of ``source`` for ``asset`` in ``[start_ms, end_ms]``, oldest first.

        Tier B z-scores and rates of change need a trailing series, not just the latest observation
        (``FND_Z``, ``POS_Z``, ``POS_ROC``, ``BAS_Z``). The window is bounded by the source's **own**
        timestamp, so the same look-ahead rule applies as to :meth:`get_point_in_time`: nothing
        stamped after ``end_ms`` can be returned.

        ``limit`` caps the result and keeps the **newest** rows, because a trailing statistic is
        computed from the recent end. A truncated window is reported by the caller rather than
        silently shortened here.
        """
        if source not in PIT_SOURCES:
            raise StoreError(
                f"unknown point-in-time source {source!r}",
                component=COMPONENT, operation="get_point_in_time_history",
                context={"source": source, "known": sorted(PIT_SOURCES)},
                likely_cause="a typo, or a new source added without registering it here",
                remediation="register the table and its own-timestamp column in PIT_SOURCES",
            )
        if end_ms < start_ms:
            raise StoreError(
                f"history window ends before it starts ({start_ms} -> {end_ms})",
                component=COMPONENT, operation="get_point_in_time_history",
                context={"start_ms": start_ms, "end_ms": end_ms, "source": source},
                likely_cause="the lookback was subtracted from the wrong end",
                remediation="pass start_ms <= end_ms",
            )
        table, ts_col = PIT_SOURCES[source]
        sql = f"SELECT * FROM {table} WHERE asset=? AND {ts_col}>=? AND {ts_col}<=?"
        params: list[Any] = [asset, start_ms, end_ms]
        if table == "book_snapshots":
            sql += " AND product=?"
            params.append(Product.SPOT.value if source == "spot_book" else Product.FUTURES.value)
        sql += f" ORDER BY {ts_col} DESC LIMIT ?"
        params.append(limit)
        rows = self._execute(sql, params, operation="get_point_in_time_history").fetchall()
        out = [self._hydrate(source, r) for r in reversed(rows)]
        offenders = [r for r in rows if int(r[ts_col]) > end_ms]
        if offenders:
            raise PointInTimeViolation(
                f"{len(offenders)} {source} record(s) stamped after as_of were returned",
                component=COMPONENT, operation="get_point_in_time_history",
                context={"source": source, "asset": asset, "end_ms": end_ms},
                likely_cause="the WHERE clause does not match the table's own timestamp column",
                remediation="check PIT_SOURCES for this source",
            )
        return out

    def _hydrate(self, source: str, r: sqlite3.Row) -> Any:
        if source == "futures_rt":
            return FuturesRT(
                asset=r["asset"], ts_ms=r["ts_ms"],
                funding_rate=_dreq(r["funding_rate"], field="funding_rate", operation="_hydrate"),
                predicted_funding_rate=_dreq(r["predicted_funding_rate"], field="predicted_funding_rate",
                                             operation="_hydrate"),
                mark_price=_dreq(r["mark_price"], field="mark_price", operation="_hydrate"),
                last_price=_dreq(r["last_price"], field="last_price", operation="_hydrate"),
                high_24h=_d(r["high_24h"]), low_24h=_d(r["low_24h"]),
                volume_quote_24h=_d(r["volume_quote_24h"]), price_change_pct=_d(r["price_change_pct"]),
                skw=r["skw"], tpe_tick_send_ms=r["tpe_tick_send_ms"], tpe_mark_send_ms=r["tpe_mark_send_ms"],
                received_at_ms=r["received_at_ms"])
        if source == "spot_ticker":
            return SpotTicker(
                asset=r["asset"], ts_ms=r["ts_ms"],
                last_price=_dreq(r["last_price"], field="last_price", operation="_hydrate"), bid=_d(r["bid"]),
                ask=_d(r["ask"]), high_24h=_d(r["high_24h"]), low_24h=_d(r["low_24h"]),
                volume_quote_24h=_d(r["volume_quote_24h"]), change_pct_24h=_d(r["change_pct_24h"]),
                received_at_ms=r["received_at_ms"])
        if source == "positioning":
            return Positioning(
                asset=r["asset"], ts_ms=r["ts_ms"],
                long_count_pct=_dreq(r["long_count_pct"], field="long_count_pct", operation="_hydrate"),
                short_count_pct=_dreq(r["short_count_pct"], field="short_count_pct", operation="_hydrate"),
                long_value_pct=_dreq(r["long_value_pct"], field="long_value_pct", operation="_hydrate"),
                short_value_pct=_dreq(r["short_value_pct"], field="short_value_pct", operation="_hydrate"))
        if source in ("spot_book", "futures_book"):
            return BookSnapshot(
                asset=r["asset"], product=Product(r["product"]), ts_ms=r["ts_ms"],
                bids=tuple((Decimal(p), Decimal(q)) for p, q in json.loads(r["bids"])),
                asks=tuple((Decimal(p), Decimal(q)) for p, q in json.loads(r["asks"])),
                version=r["version"], fetched_at_ms=r["received_at_ms"])
        raise StoreError(f"no hydrator for {source!r}", component=COMPONENT, operation="_hydrate",
                         context={"source": source})

    def upsert_futures_rt(self, rows: list[FuturesRT]) -> int:
        if not rows:
            return 0
        data = []
        for r in rows:
            ts = r.source_ts_ms
            if ts is None:
                raise IntegrityError(
                    "a futures_rt row without its own timestamp reached the store",
                    component=COMPONENT, operation="upsert_futures_rt", context={"asset": r.asset},
                    likely_cause="normalization accepted a row with neither btST nor bmST",
                    remediation="the normalizer must reject such rows (NO_ROW_TIMESTAMP)",
                )
            data.append((r.asset, r.ts_ms, _t(r.funding_rate), _t(r.predicted_funding_rate), _t(r.mark_price),
                         _t(r.last_price), _t(r.high_24h), _t(r.low_24h), _t(r.volume_quote_24h),
                         _t(r.price_change_pct), r.skw, r.tpe_tick_send_ms, r.tpe_mark_send_ms,
                         r.received_at_ms, ts))
        self._executemany(
            "INSERT INTO futures_rt (asset, ts_ms, funding_rate, predicted_funding_rate, mark_price, last_price,"
            " high_24h, low_24h, volume_quote_24h, price_change_pct, skw, tpe_tick_send_ms, tpe_mark_send_ms,"
            " received_at_ms, source_ts_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (asset, source_ts_ms) DO NOTHING", data, operation="upsert_futures_rt")
        return len(data)

    def upsert_spot_ticker(self, rows: list[SpotTicker]) -> int:
        if not rows:
            return 0
        self._executemany(
            "INSERT INTO spot_ticker (asset, ts_ms, last_price, bid, ask, high_24h, low_24h,"
            " volume_quote_24h, change_pct_24h, received_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (asset, ts_ms) DO NOTHING",
            [(r.asset, r.ts_ms, _t(r.last_price), _t(r.bid), _t(r.ask), _t(r.high_24h), _t(r.low_24h),
              _t(r.volume_quote_24h), _t(r.change_pct_24h), r.received_at_ms) for r in rows],
            operation="upsert_spot_ticker")
        return len(rows)

    def upsert_positioning(self, rows: list[Positioning]) -> int:
        if not rows:
            return 0
        self._executemany(
            "INSERT INTO positioning (asset, ts_ms, long_count_pct, short_count_pct, long_value_pct,"
            " short_value_pct) VALUES (?,?,?,?,?,?) ON CONFLICT (asset, ts_ms) DO NOTHING",
            [(r.asset, r.ts_ms, _t(r.long_count_pct), _t(r.short_count_pct), _t(r.long_value_pct),
              _t(r.short_value_pct)) for r in rows], operation="upsert_positioning")
        return len(rows)

    def upsert_books(self, rows: list[BookSnapshot]) -> int:
        if not rows:
            return 0
        self._executemany(
            "INSERT INTO book_snapshots (asset, product, ts_ms, bids, asks, version, received_at_ms,"
            " best_bid, best_ask, levels) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (asset, product, ts_ms) DO NOTHING",
            [(r.asset, r.product.value, r.ts_ms,
              json.dumps([[str(p), str(q)] for p, q in r.bids]),
              json.dumps([[str(p), str(q)] for p, q in r.asks]),
              r.version, r.fetched_at_ms, str(r.bids[0][0]), str(r.asks[0][0]), len(r.bids) + len(r.asks))
             for r in rows], operation="upsert_books")
        return len(rows)

    def upsert_trades(self, rows: list[Trade]) -> int:
        """Dedup is **imperfect**: spot REST has no trade id, so two identical trades in the same
        millisecond collapse. Documented rather than pretended away (specs/11 §8.3)."""
        if not rows:
            return 0
        self._executemany(
            "INSERT INTO trades (asset, product, ts_ms, price, qty, is_buyer_maker) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (asset, product, ts_ms, price, qty, is_buyer_maker) DO NOTHING",
            [(r.asset, r.product.value, r.ts_ms, _t(r.price), _t(r.qty), 1 if r.is_buyer_maker else 0)
             for r in rows], operation="upsert_trades")
        return len(rows)

    def upsert_instrument(self, rows: list[InstrumentMeta]) -> int:
        if not rows:
            return 0
        self._executemany(
            "INSERT INTO instrument_meta (asset, product, fetched_at_ms, tick, qty_increment, min_notional,"
            " maker_fee_fraction, taker_fee_fraction, multiplier_up, multiplier_down, funding_frequency_h,"
            " max_leverage, margin_tiers, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (asset, product, fetched_at_ms) DO NOTHING",
            [(r.asset, r.product.value, r.fetched_at_ms, _t(r.tick), _t(r.qty_increment), _t(r.min_notional),
              str(r.maker_fee.fraction), str(r.taker_fee.fraction), _t(r.multiplier_up), _t(r.multiplier_down),
              r.funding_frequency_h, _t(r.max_leverage),
              json.dumps([[str(t.max_notional), str(t.maintenance_margin_rate)] for t in r.margin_tiers]),
              r.status) for r in rows], operation="upsert_instrument")
        return len(rows)

    def get_instrument(self, asset: str, product: Product, as_of_ms: int | None = None) -> InstrumentMeta | None:
        sql = "SELECT * FROM instrument_meta WHERE asset=? AND product=?"
        params: list[Any] = [asset, product.value]
        if as_of_ms is not None:
            sql += " AND fetched_at_ms<=?"
            params.append(as_of_ms)
        sql += " ORDER BY fetched_at_ms DESC LIMIT 1"
        r = self._execute(sql, params, operation="get_instrument").fetchone()
        if r is None:
            return None
        return InstrumentMeta(
            asset=r["asset"], product=Product(r["product"]),
            tick=_dreq(r["tick"], field="tick", operation="get_instrument"),
            qty_increment=_dreq(r["qty_increment"], field="qty_increment", operation="get_instrument"),
            min_notional=_dreq(r["min_notional"], field="min_notional", operation="get_instrument"),
            maker_fee=Fee.from_fraction(r["maker_fee_fraction"]),
            taker_fee=Fee.from_fraction(r["taker_fee_fraction"]),
            multiplier_up=_d(r["multiplier_up"]), multiplier_down=_d(r["multiplier_down"]),
            funding_frequency_h=r["funding_frequency_h"], max_leverage=_d(r["max_leverage"]),
            margin_tiers=tuple(MarginTier(max_notional=Decimal(a), maintenance_margin_rate=Decimal(b))
                               for a, b in json.loads(r["margin_tiers"])),
            status=r["status"], fetched_at_ms=r["fetched_at_ms"])

    # ---- known-absent bars -----------------------------------------------------------------

    def confirm_absent_bar(self, asset: str, product: Product, timeframe: Timeframe, open_time_ms: int,
                           *, at_ms: int) -> int:
        self._execute(
            "INSERT INTO known_absent_bars (asset, product, timeframe, open_time_ms, confirmations,"
            " first_confirmed_ms, last_confirmed_ms) VALUES (?,?,?,?,1,?,?)"
            " ON CONFLICT (asset, product, timeframe, open_time_ms) DO UPDATE SET"
            " confirmations = confirmations + 1, last_confirmed_ms = excluded.last_confirmed_ms",
            [asset, product.value, timeframe.value, open_time_ms, at_ms, at_ms],
            operation="confirm_absent_bar")
        row = self._execute(
            "SELECT confirmations FROM known_absent_bars WHERE asset=? AND product=? AND timeframe=?"
            " AND open_time_ms=?", [asset, product.value, timeframe.value, open_time_ms],
            operation="confirm_absent_bar").fetchone()
        return int(row["confirmations"])

    def is_absent_confirmed(self, asset: str, product: Product, timeframe: Timeframe, open_time_ms: int,
                            *, required: int = 2) -> bool:
        row = self._execute(
            "SELECT confirmations FROM known_absent_bars WHERE asset=? AND product=? AND timeframe=?"
            " AND open_time_ms=?", [asset, product.value, timeframe.value, open_time_ms],
            operation="is_absent_confirmed").fetchone()
        return row is not None and int(row["confirmations"]) >= required

    def known_absent_bars(self, asset: str, product: Product, timeframe: Timeframe,
                          start_ms: int, end_ms: int, *, required: int = 2) -> set[int]:
        rows = self._execute(
            "SELECT open_time_ms FROM known_absent_bars WHERE asset=? AND product=? AND timeframe=?"
            " AND open_time_ms>=? AND open_time_ms<=? AND confirmations>=?",
            [asset, product.value, timeframe.value, start_ms, end_ms, required],
            operation="known_absent_bars").fetchall()
        return {int(r["open_time_ms"]) for r in rows}

    # ---- events and runs -------------------------------------------------------------------

    def record_dq_event(self, event: DQEvent) -> int:
        cur = self._execute(
            "INSERT INTO dq_events (at_ms, kind, component, operation, problem_class, severity, asset, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [event.at_ms, event.kind, event.component, event.operation, event.problem_class,
             event.severity, event.asset, json.dumps(event.detail, default=str)],
            operation="record_dq_event")
        return int(cur.lastrowid or 0)

    def dq_event_counts(self, since_ms: int) -> dict[str, int]:
        rows = self._execute("SELECT kind, COUNT(*) AS n FROM dq_events WHERE at_ms>=? GROUP BY kind",
                             [since_ms], operation="dq_event_counts").fetchall()
        return {r["kind"]: int(r["n"]) for r in rows}

    def record_run(self, run: RunRecord) -> int:
        cur = self._execute(
            "INSERT INTO runs (process, started_ms, code_version, parameter_hash, settings_hash, notes)"
            " VALUES (?,?,?,?,?,?)",
            [run.process, run.started_ms, run.code_version, run.parameter_hash, run.settings_hash, run.notes],
            operation="record_run")
        return int(cur.lastrowid or 0)

    def close_run(self, run_id: int, *, stopped_ms: int, reason: str) -> None:
        self._execute("UPDATE runs SET stopped_ms=?, stop_reason=? WHERE id=?",
                      [stopped_ms, reason, run_id], operation="close_run")

    # ---- health and lifecycle --------------------------------------------------------------

    def health(self) -> ComponentHealth:
        """Report the store's own state, including an integrity signal and free space."""
        import shutil
        try:
            version = self.schema_version()
            want = latest_version()
            quick = self._execute("PRAGMA quick_check", operation="health").fetchone()[0]
            free_mb = shutil.disk_usage(self.path.parent).free / 1e6
            size_mb = self.path.stat().st_size / 1e6 if self.path.exists() else 0.0
        except (StoreError, OSError, sqlite3.Error) as e:
            err = e if isinstance(e, StoreError) else self._translate(
                e if isinstance(e, sqlite3.Error) else sqlite3.OperationalError(str(e)), operation="health")
            return ComponentHealth.from_error("store", err)

        if quick != "ok":
            return ComponentHealth(
                name="store", status=SystemStatus.DOWN, detail=f"integrity check failed: {quick}",
                problem_class=StoreError.problem_class, context={"quick_check": str(quick)},
                remediation="rebuild the database; bars refetch and raw JSONL is intact")
        if version != want:
            return ComponentHealth(
                name="store", status=SystemStatus.DOWN, detail=f"schema {version}, expected {want}",
                problem_class=MigrationError.problem_class, context={"schema_version": version},
                remediation="run migrations")
        if free_mb < 500:
            return ComponentHealth(
                name="store", status=SystemStatus.DEGRADED,
                detail=f"only {free_mb:.0f} MB free on the data volume",
                problem_class=StoreError.problem_class,
                context={"free_mb": round(free_mb, 1), "db_mb": round(size_mb, 1)},
                remediation="free space before writes start failing")
        return ComponentHealth.healthy("store", detail=f"schema {version}, {size_mb:.1f} MB",
                                       schema_version=version, db_mb=round(size_mb, 1),
                                       free_mb=round(free_mb, 1))

    def vacuum(self) -> None:
        self._execute("VACUUM", operation="vacuum")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass          # a checkpoint failure must not mask the reason we are closing
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_store(path: str | Path, *, migrate: bool = True, read_only: bool = False) -> SqliteStore:
    """Open and (by default) migrate. Startup failures here are deterministic and loud."""
    store = SqliteStore(path, read_only=read_only)
    if migrate and not read_only:
        store.migrate()
    store.require_schema()
    return store
