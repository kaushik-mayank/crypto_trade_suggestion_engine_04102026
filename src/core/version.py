"""Code identity, for the audit trail.

``specs/10`` §7 stores ``code_version`` on every signal and replays it later; ``specs/13`` §9 treats a
signal that does not reproduce as either a bug or a recorded bar mutation. Distinguishing those two
needs to know whether the *code* changed since the signal was written — so the version is not a
hand-maintained string but a digest of the source actually loaded.

The repository is not a git checkout here, so a commit sha is not available. A digest over
``src/**/*.py`` serves the same purpose and cannot be forgotten on a release.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

#: Human-facing release. Bumped deliberately; the digest below distinguishes builds within it.
RELEASE = "0.1.0"

SRC_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def source_digest() -> str:
    """sha256 over every source file, path-ordered. Deterministic across runs and machines."""
    h = hashlib.sha256()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        h.update(path.relative_to(SRC_ROOT).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@lru_cache(maxsize=1)
def code_version() -> str:
    """``0.1.0+<12 hex>`` — what gets stored on a signal."""
    return f"{RELEASE}+{source_digest()[:12]}"
