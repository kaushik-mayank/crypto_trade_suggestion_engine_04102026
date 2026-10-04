"""Response classification and backoff schedules (specs/01b-recorder.md, Error handling)."""
from __future__ import annotations

from enum import Enum


class Kind(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"      # 429
    MAINTENANCE = "maintenance"        # 503 — documented maintenance, not our fault
    SERVER_ERROR = "server_error"      # other 5xx
    UNAUTHORIZED = "unauthorized"      # 401 — never retry in a loop
    CLIENT_ERROR = "client_error"      # other 4xx — almost certainly our request is wrong
    TRANSPORT = "transport"            # connection error / timeout
    MALFORMED = "malformed"            # 2xx with a body that is not valid JSON
    INTERNAL = "internal"              # bug in our own code


# (base seconds, cap seconds); delay = min(cap, base * 2**(attempt-1))
SCHEDULES: dict[Kind, tuple[float, float]] = {
    Kind.RATE_LIMITED: (1.0, 60.0),
    Kind.MAINTENANCE: (30.0, 900.0),
    Kind.SERVER_ERROR: (5.0, 120.0),
    Kind.TRANSPORT: (2.0, 60.0),
    # Not in the spec. A 4xx will not fix itself; retry slowly so the log stays readable.
    Kind.CLIENT_ERROR: (60.0, 3600.0),
    Kind.MALFORMED: (5.0, 120.0),
    Kind.INTERNAL: (60.0, 3600.0),
}


def classify(status: int) -> Kind:
    if 200 <= status < 300:
        return Kind.OK
    if status == 429:
        return Kind.RATE_LIMITED
    if status == 503:
        return Kind.MAINTENANCE
    if status == 401:
        return Kind.UNAUTHORIZED
    if 500 <= status < 600:
        return Kind.SERVER_ERROR
    return Kind.CLIENT_ERROR


def backoff_delay(kind: Kind, attempt: int, retry_after_s: float | None = None) -> float:
    """Delay in seconds before retry number ``attempt`` (1-based) after a failure of ``kind``.

    A server-supplied ``Retry-After`` can lengthen the delay, never shorten it.
    """
    if kind not in SCHEDULES:
        raise ValueError(f"no backoff schedule for {kind}")
    base, cap = SCHEDULES[kind]
    delay = min(cap, base * (2 ** (max(attempt, 1) - 1)))
    if retry_after_s is not None and retry_after_s > delay:
        delay = retry_after_s
    return delay


def parse_retry_after(headers: dict[str, str]) -> float | None:
    """Seconds from a numeric ``Retry-After`` header; HTTP-date form is ignored."""
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return max(0.0, float(v))
            except ValueError:
                return None
    return None
