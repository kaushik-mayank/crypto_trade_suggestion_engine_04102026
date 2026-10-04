"""A thread-safe read-only view of the store, for the API.

A ``sqlite3.Connection`` belongs to the thread that created it, and a sync FastAPI handler runs in a
worker thread — so handing the writer's connection to the API raises *"SQLite objects created in a
thread can only be used in that same thread"* on the first request. That is a real failure, not a test
artefact: it would take the dashboard down in production while the engine carried on writing happily.

The fix is one **read-only connection per thread**, which WAL mode supports concurrently with the
writer. Read-only is enforced by SQLite itself through the ``mode=ro`` URI, not by convention, so the
API cannot write even by accident — which is the same property specs/14 §1 asks for at the
architectural level ("engines write, the API reads"), expressed where it cannot be bypassed.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from core.errors import StoreError
from core.health import ComponentHealth, SystemStatus
from core.logging import get_logger

log = get_logger("store.reader")
COMPONENT = "store.reader"


class ReadOnlyStore:
    """Opens one read-only connection per calling thread, lazily.

    Exposes ``conn`` so the services read exactly as they do from the writer; nothing else about the
    query code changes between a test using the writer and production using this.
    """

    def __init__(self, path: str | Path, *, timeout_s: float = 10.0):
        self.path = Path(path)
        self._timeout_s = timeout_s
        self._local = threading.local()
        self._threads = 0
        self._lock = threading.Lock()
        if not self.path.exists():
            raise StoreError(
                f"no database at {self.path}",
                component=COMPONENT, operation="open",
                context={"path": str(self.path)},
                likely_cause="the API was pointed at a path the engine has not created yet",
                remediation="run the engine (or migrations) first; the API never creates the database, "
                            "because a read path that can create a file will silently create an empty one",
            )

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=self._timeout_s)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(self._timeout_s * 1000)}")
        except sqlite3.Error as e:
            raise StoreError(
                f"cannot open {self.path} for reading",
                component=COMPONENT, operation="open",
                context={"path": str(self.path), "thread": threading.current_thread().name},
                likely_cause=str(e),
                remediation="check the file exists and is readable by this process",
                cause=e,
            ) from e
        self._local.conn = conn
        with self._lock:
            self._threads += 1
        log.debug("read_connection_opened", thread=threading.current_thread().name,
                  connections=self._threads)
        return conn

    def close(self) -> None:
        """Close this thread's connection. Other threads keep theirs; each closes its own."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    def health(self) -> ComponentHealth:
        try:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
        except (sqlite3.Error, StoreError) as e:
            return ComponentHealth(
                name="store:read", status=SystemStatus.DOWN,
                detail=f"the read connection failed: {e}",
                problem_class=__import__("core.errors", fromlist=["ProblemClass"])
                .ProblemClass.INFRASTRUCTURE,
                remediation="check the database file and its permissions")
        return ComponentHealth.healthy("store:read", detail=f"{row['n']} signal(s) stored",
                                       read_connections=self._threads)

    def stats(self) -> dict[str, Any]:
        return {"path": str(self.path), "read_connections": self._threads}
