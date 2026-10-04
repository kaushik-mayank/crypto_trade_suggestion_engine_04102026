"""Ingest transport (specs/11 §3.2).

Wraps the Phase-1 :class:`cdx_http.HttpClient` (decision D-2: adaptive per-host token bucket,
backoff, raw capture and header redaction are already built and tested) and converts its outcomes
into the classified errors the scheduler and health layer reason about:

| outcome | error | retryable | owner |
|---|---|---|---|
| no response | :class:`TransportFailure` | yes | external |
| 429 | :class:`RateLimited` | yes | external (our rate is too high) |
| 503 | :class:`ExchangeMaintenance` | yes, slowly | external (documented maintenance) |
| other 5xx | :class:`HttpStatusError` (retryable) | yes | external |
| 4xx | :class:`HttpStatusError` (**not** retryable) | no | **our request is wrong** |

The last row is the important one: a 422 means we sent something invalid (capital-``Depth``, an
unserved resolution), and retrying it forever would hide a bug behind a backoff loop.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cdx_http.client import HttpClient, TransportError
from core.errors import ExchangeMaintenance, HttpStatusError, RateLimited, TransportFailure
from core.logging import get_logger

log = get_logger("ingest.client")


@dataclass(frozen=True)
class IngestResult:
    """Raw bytes plus metadata. **Ingest never parses** (specs/11 §3.1)."""

    endpoint: str
    params: dict[str, Any]
    raw: bytes
    http_status: int
    response_headers: dict[str, str]
    sent_at_ms: int
    received_at_ms: int
    url: str

    @property
    def latency_ms(self) -> int:
        return self.received_at_ms - self.sent_at_ms


class IngestClient:
    def __init__(self, http: HttpClient | None = None, *, host_rates: dict[str, float] | None = None,
                 timeout_s: float = 20.0):
        self.http = http or HttpClient(host_rates=host_rates or {}, timeout_s=timeout_s)

    def get(self, endpoint: str, url: str, params: Mapping[str, Any] | None = None) -> IngestResult:
        """One attempt. The scheduler owns retries so a failing endpoint cannot stall the others."""
        try:
            res = self.http.request("GET", url, params=params)
        except TransportError as e:
            raise TransportFailure(
                f"no response from {endpoint}",
                component="ingest.client", operation="get",
                context={"endpoint": endpoint, "url": e.url, "cause": e.cause_name,
                         "elapsed_ms": e.failed_at_ms - e.sent_at_ms},
                likely_cause="no network, DNS failure, TLS failure, or the request timed out",
                remediation="check connectivity; the scheduler will back off and retry. Persistent "
                            "failure is reported as DOWN rather than retried silently forever",
                cause=e,
            ) from e

        if res.status == 429:
            raise RateLimited(
                f"{endpoint} returned 429",
                component="ingest.client", operation="get",
                context={"endpoint": endpoint, "url": res.url},
                likely_cause="our polling rate exceeds the venue's limit (measured 5,000/60s on "
                             "api.coindcx.com in F-022)",
                remediation="the client has already halved this host's rate for 10 minutes; if this "
                            "recurs, lower the configured cadence",
            )
        if res.status == 503:
            raise ExchangeMaintenance(
                f"{endpoint} returned 503",
                component="ingest.client", operation="get",
                context={"endpoint": endpoint, "url": res.url},
                likely_cause="documented exchange maintenance (specs/00 §8) — not our fault",
                remediation="back off long and stay alive; the system goes quiet rather than emitting "
                            "stale signals",
            )
        if res.status >= 500:
            raise HttpStatusError(
                f"{endpoint} returned {res.status}",
                component="ingest.client", operation="get", status=res.status, retryable=True,
                context={"endpoint": endpoint, "url": res.url, "body": res.body[:200].decode(errors="replace")},
                likely_cause="a transient server-side failure at the venue",
                remediation="retry with backoff",
            )
        if res.status >= 400:
            raise HttpStatusError(
                f"{endpoint} returned {res.status}: our request is invalid",
                component="ingest.client", operation="get", status=res.status, retryable=False,
                context={"endpoint": endpoint, "url": res.url, "params": dict(params or {}),
                         "body": res.body[:300].decode(errors="replace")},
                likely_cause="an invalid parameter — e.g. capital-D 'Depth' (F-001), an unserved "
                            "resolution (F-008), or an unsupported depth value (F-014)",
                remediation="fix the request; this is NOT retryable and must not be hidden behind a "
                            "backoff loop",
            )

        return IngestResult(
            endpoint=endpoint, params=dict(params or {}), raw=res.body, http_status=res.status,
            response_headers=dict(res.headers), sent_at_ms=res.sent_at_ms,
            received_at_ms=res.received_at_ms, url=res.url,
        )

    def rate_limit_headers(self, result: IngestResult) -> dict[str, str]:
        """The undocumented IETF RateLimit headers on api.coindcx.com (F-022)."""
        return {k: v for k, v in result.response_headers.items()
                if any(h in k.lower() for h in ("ratelimit", "retry-after"))}
