"""Append-only JSON Lines storage.

* One file per stream per UTC day: ``<root>/<stream>/<YYYY-MM-DD>.jsonl``.
* Every write is flushed and fsynced. A crash can truncate only the last line.
* When a file is first opened for append and does not end in a newline (a torn
  write from a previous crash), a newline is written first so the torn fragment stays
  isolated on its own line and the next record is intact. Readers skip bad lines.
* Completed days (UTC date before today) are gzip-compressed to ``<YYYY-MM-DD>.jsonl.gz``.
  Compression is lossless and verified: the new ``.gz`` is decompressed and compared
  byte-for-byte (sha256) with the original before the ``.jsonl`` is removed. A crash
  at any point leaves either the ``.jsonl``, or a verified ``.gz`` (maybe beside a
  redundant ``.jsonl``, which the next pass removes after re-verifying).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from cdx_http.jsonutil import one_line

META_DIR = "_meta"
DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$")


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def envelope_line(
    stream: str,
    pair: str | None,
    sent_at_ms: int,
    received_at_ms: int,
    http_status: int,
    source_ts_ms: int | None,
    request_url: str,
    payload_raw: str,
) -> str:
    """One record line (no trailing newline). ``payload_raw`` is embedded verbatim."""
    head = json.dumps(
        {
            "stream": stream,
            "pair": pair,
            "sent_at_ms": sent_at_ms,
            "received_at_ms": received_at_ms,
            "http_status": http_status,
            "source_ts_ms": source_ts_ms,
            "request_url": request_url,
        },
        separators=(",", ":"),
    )
    return head[:-1] + ',"payload":' + one_line(payload_raw) + "}"


def _fsync_file(path: Path) -> None:
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def compress_day_file(path: Path) -> dict:
    """Losslessly gzip a completed day file. Returns a description of what was done.

    If ``<name>.gz`` already exists (a crash after the rename, or records that arrived
    for an already-compressed day), the result holds the existing content followed by
    this file's bytes, unless this file is exactly the existing content, in which case
    it is simply removed.
    """
    gz = path.with_name(path.name + ".gz")
    tmp = path.with_name(path.name + ".gz.tmp")
    data = path.read_bytes()
    existing = gzip.decompress(gz.read_bytes()) if gz.exists() else None
    if existing is not None and existing == data:
        path.unlink()
        return {"file": str(gz), "action": "removed_redundant_jsonl", "raw_bytes": len(data)}
    if existing is None:
        combined = data
    else:
        sep = b"" if not existing or existing.endswith(b"\n") else b"\n"
        combined = existing + sep + data
    digest = hashlib.sha256(combined).hexdigest()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=6) as g:
        g.write(combined)
    tmp.write_bytes(buf.getvalue())
    _fsync_file(tmp)
    if hashlib.sha256(gzip.decompress(tmp.read_bytes())).hexdigest() != digest:
        tmp.unlink()
        raise OSError(f"gzip verification failed for {path}")
    os.replace(tmp, gz)
    path.unlink()
    return {"file": str(gz), "action": "merged" if existing is not None else "compressed",
            "raw_bytes": len(combined), "gz_bytes": gz.stat().st_size, "sha256_raw": digest}


class JsonlStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._open: dict[str, tuple[Path, IO[str]]] = {}
        self.bytes_written: dict[str, int] = {}
        self.repaired: list[Path] = []

    def _handle(self, key: str, path: Path) -> IO[str]:
        cur = self._open.get(key)
        if cur and cur[0] == path:
            return cur[1]
        if cur:
            cur[1].close()
        path.parent.mkdir(parents=True, exist_ok=True)
        needs_nl = False
        if path.exists() and path.stat().st_size > 0:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                needs_nl = f.read(1) != b"\n"
        fh = open(path, "a", encoding="utf-8", newline="\n")
        if needs_nl:
            fh.write("\n")
            self._sync(fh)
            self.repaired.append(path)
        self._open[key] = (path, fh)
        return fh

    @staticmethod
    def _sync(fh: IO[str]) -> None:
        fh.flush()
        os.fsync(fh.fileno())

    def append(self, stream: str, received_at_ms: int, line: str) -> None:
        """Append a record to the stream's file for the UTC day of ``received_at_ms``."""
        self._write(stream, self.root / stream / f"{utc_day(received_at_ms)}.jsonl", line)

    def append_meta(self, name: str, obj: dict) -> None:
        """Append to ``_meta/<name>.jsonl`` (gaps, errors, quality, compression)."""
        self._write(f"{META_DIR}/{name}", self.root / META_DIR / f"{name}.jsonl", json.dumps(obj, separators=(",", ":")))

    def _write(self, key: str, path: Path, line: str) -> None:
        if "\n" in line:
            raise ValueError("record contains a newline")
        fh = self._handle(key, path)
        fh.write(line + "\n")
        self._sync(fh)
        self.bytes_written[key] = self.bytes_written.get(key, 0) + len(line.encode("utf-8")) + 1

    def close(self) -> None:
        for _, fh in self._open.values():
            try:
                self._sync(fh)
            finally:
                fh.close()
        self._open.clear()

    # ---- compression -------------------------------------------------------------------

    def compress_completed(self, today: str) -> list[dict]:
        """Gzip every stream day file dated before ``today`` (YYYY-MM-DD, UTC)."""
        for key, (path, fh) in list(self._open.items()):
            m = DAY_RE.match(path.name)
            if m and m.group(1) < today:
                self._sync(fh)
                fh.close()
                del self._open[key]
        done = []
        if not self.root.is_dir():
            return done
        for d in sorted(p for p in self.root.iterdir() if p.is_dir() and p.name != META_DIR):
            for stale in d.glob("*.jsonl.gz.tmp"):
                stale.unlink()        # the .jsonl it came from is still present
            for f in sorted(d.glob("????-??-??.jsonl")):
                if f.name[:10] < today:
                    info = compress_day_file(f)
                    self.append_meta("compression", {"stream": d.name, **info})
                    done.append(info)
        return done

    # ---- reading (startup only) ------------------------------------------------

    def day_files(self, stream: str) -> list[Path]:
        """This stream's day files (plain and gzipped), newest day first."""
        d = self.root / stream
        if not d.is_dir():
            return []
        files = [p for p in d.iterdir() if DAY_RE.match(p.name)]
        # newest day first; for the same day, the plain file (newer records) first
        return sorted(files, key=lambda p: (p.name[:10], p.suffix != ".gz"), reverse=True)

    def meta_path(self, name: str) -> Path:
        return self.root / META_DIR / f"{name}.jsonl"


def open_text(path: Path) -> IO[str]:
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def iter_records(path: Path) -> Iterator[dict]:
    """Parse each line of a plain or gzipped file; silently skip torn or corrupt lines."""
    with open_text(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


class SingleInstanceLock:
    """Exclusive, non-blocking lock on ``<data_dir>/_meta/recorder.lock``.

    Held for the life of the process; the OS releases it if the process dies.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
