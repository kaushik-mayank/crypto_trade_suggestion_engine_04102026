"""Shared CoinDCX HTTP layer: rate limiting, backoff, raw capture.

Named ``cdx_http`` rather than ``http`` (as sketched in CLAUDE.md) because a top-level
package called ``http`` would shadow the standard library and break ``requests``.
"""
from .backoff import Kind, backoff_delay, classify
from .client import HttpClient, HttpResult, TransportError, now_ms
from .jsonutil import loads_decimal

__all__ = [
    "HttpClient", "HttpResult", "TransportError", "Kind",
    "backoff_delay", "classify", "loads_decimal", "now_ms",
]
