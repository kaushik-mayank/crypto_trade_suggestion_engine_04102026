"""Shared HTTP client (specs/01-verification.md §0).

Returns bytes plus metadata; never parses. ``request`` makes exactly one attempt
(the recorder schedules its own retries so one failing stream cannot stall the
others). ``request_with_retry`` is the blocking retry loop probes use.
"""
from __future__ import annotations

import itertools
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

from .backoff import Kind, backoff_delay, classify, parse_retry_after
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

SENSITIVE_HEADERS = {"x-auth-apikey", "x-auth-signature", "authorization", "cookie", "set-cookie"}
REDACTED = "<redacted>"
USER_AGENT = "crypto-trader-phase1/0.1 (+research; read-only)"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: (REDACTED if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def build_url(url: str, params: Mapping[str, object] | None) -> str:
    """Append query params exactly as given (list values become repeated keys)."""
    if not params:
        return url
    return url + ("&" if urlsplit(url).query else "?") + urlencode(params, doseq=True)


@dataclass(frozen=True)
class HttpResult:
    method: str
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    sent_at_ms: int
    received_at_ms: int

    @property
    def kind(self) -> Kind:
        return classify(self.status)

    @property
    def ok(self) -> bool:
        return self.kind is Kind.OK

    @property
    def latency_ms(self) -> int:
        return self.received_at_ms - self.sent_at_ms


class TransportError(Exception):
    """Connection failure or timeout. Carries timing; never carries request headers."""

    def __init__(self, method: str, url: str, sent_at_ms: int, failed_at_ms: int, cause: BaseException):
        super().__init__(f"{method} {url}: {type(cause).__name__}: {cause}")
        self.method = method
        self.url = url
        self.sent_at_ms = sent_at_ms
        self.failed_at_ms = failed_at_ms
        self.cause_name = type(cause).__name__


@dataclass
class HttpClient:
    """Per-host rate-limited HTTP client with optional raw capture."""

    host_rates: dict[str, float] = field(default_factory=dict)
    default_rate: float = 5.0
    timeout_s: float = 20.0
    capture_dir: Path | None = None
    session: requests.Session | None = None
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._seq = itertools.count(1)
        if self.session is None:
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if self.capture_dir is not None:
            self.capture_dir = Path(self.capture_dir)
            self.capture_dir.mkdir(parents=True, exist_ok=True)

    def bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(self.host_rates.get(host, self.default_rate))
        return self._buckets[host]

    def request(
        self,
        method: str,
        url: str,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResult:
        """One attempt. Returns any HTTP status; raises ``TransportError`` on no response."""
        full_url = build_url(url, params)
        host = urlsplit(full_url).hostname or ""
        self.bucket(host).acquire()
        hdrs = dict(headers or {})
        sent = now_ms()
        try:
            # data= works for GET too: the P8 sample code sends a signed body on GET.
            resp = self.session.request(method, full_url, headers=hdrs, data=body, timeout=self.timeout_s)
            content = resp.content
        except requests.RequestException as e:
            failed = now_ms()
            err = TransportError(method, full_url, sent, failed, e)
            self._capture(method, full_url, hdrs, body, sent, failed, None, None, None, str(err))
            raise err from None
        received = now_ms()
        result = HttpResult(method, full_url, resp.status_code, dict(resp.headers), content, sent, received)
        if result.kind is Kind.RATE_LIMITED:
            self.bucket(host).penalize()
            log.warning("429 from %s - halving request rate to %.3f/s", host, self.bucket(host).rate)
        self._capture(method, full_url, hdrs, body, sent, received, result.status, result.headers, content, None)
        return result

    def request_with_retry(
        self,
        method: str,
        url: str,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        max_attempts: int = 6,
        sign: Callable[[], tuple[bytes, dict[str, str]]] | None = None,
    ) -> HttpResult:
        """Retry on 429/5xx/transport with backoff. Returns the last result.

        4xx other than 429 is returned immediately. ``sign`` (if given) rebuilds body
        and headers per attempt so the signed timestamp stays fresh.
        """
        attempt = 0
        while True:
            attempt += 1
            if sign is not None:
                body, headers = sign()
            try:
                res = self.request(method, url, params, headers, body)
            except TransportError:
                if attempt >= max_attempts:
                    raise
                kind, retry_after = Kind.TRANSPORT, None
            else:
                kind = res.kind
                if kind in (Kind.OK, Kind.CLIENT_ERROR, Kind.UNAUTHORIZED) or attempt >= max_attempts:
                    return res
                retry_after = parse_retry_after(res.headers)
            delay = backoff_delay(kind, attempt, retry_after)
            if kind is Kind.MAINTENANCE:
                log.warning("503 MAINTENANCE (documented) on %s - backing off %.0fs", url, delay)
            else:
                log.info("%s on %s - retry %d in %.1fs", kind.value, url, attempt, delay)
            self.sleep(delay)

    def _capture(self, method, url, req_headers, req_body, sent, received, status, resp_headers, content, error):
        if self.capture_dir is None:
            return
        stem = f"{sent}_{next(self._seq):05d}"
        meta = {
            "method": method,
            "url": url,
            "request_headers": redact_headers(req_headers),
            "request_body_len": None if req_body is None else len(req_body),
            "sent_at_ms": sent,
            "received_at_ms": received,
            "status": status,
            "response_headers": resp_headers,
            "error": error,
            "body_file": None if content is None else f"{stem}.body",
        }
        if content is not None:
            (self.capture_dir / f"{stem}.body").write_bytes(content)
        (self.capture_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
