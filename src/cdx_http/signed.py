"""Signed-request support. The ONLY module that touches credentials (invariant 12).

Only two endpoints need a key: futures ``data/stats`` and ``conversions``. Nothing in
this codebase may call an order, cancel, transfer or leverage endpoint (invariant 11).

Credentials come from the process environment or ``.env``; they are never logged, and
``Credentials.__repr__`` hides them so they cannot leak through tracebacks.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

KEY_VAR = "COINDCX_API_KEY"
SECRET_VAR = "COINDCX_API_SECRET"


@dataclass(frozen=True)
class Credentials:
    key: str
    secret: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    __str__ = __repr__

    def secrets(self) -> tuple[str, str]:
        """Strings that must be scrubbed from anything written to disk."""
        return (self.key, self.secret)


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_credentials(env_file: str | Path = ".env") -> Credentials | None:
    """Environment variables take precedence over ``.env``. None if either is empty."""
    file_vals = _read_env_file(Path(env_file))
    key = os.environ.get(KEY_VAR) or file_vals.get(KEY_VAR, "")
    secret = os.environ.get(SECRET_VAR) or file_vals.get(SECRET_VAR, "")
    if not key or not secret:
        return None
    return Credentials(key, secret)


def serialize(payload: dict) -> bytes:
    """Byte-stable body: compact separators, insertion order preserved, never re-ordered."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the exact body bytes, hex digest."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signed_request(creds: Credentials, payload: dict | None = None, timestamp_unit: str = "ms") -> tuple[bytes, dict[str, str]]:
    """Build ``(body, headers)`` with a fresh ``timestamp`` field.

    ``timestamp_unit`` is "ms" or "s" — the vendor docs contradict themselves (probe P8).
    """
    if timestamp_unit not in ("ms", "s"):
        raise ValueError("timestamp_unit must be 'ms' or 's'")
    ts = time.time_ns() // 1_000_000
    if timestamp_unit == "s":
        ts //= 1000
    body = serialize({**(payload or {}), "timestamp": ts})
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": creds.key,
        "X-AUTH-SIGNATURE": sign(creds.secret, body),
    }
    return body, headers
