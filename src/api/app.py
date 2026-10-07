"""The HTTP layer (specs/14 §9-§11).

Thin by construction: every handler picks a representation of what a service returned and adds
nothing. specs/14 §2 makes duplicating logic between the JSON and HTML paths a defect, so the
fragment handlers call the same service methods the JSON handlers do.

Three rules enforced here rather than documented:

**The bind guard.** The app refuses to bind a non-localhost address unless an auth token is
configured. Exposure without authentication is not a supported configuration, not merely discouraged
(specs/10 §9), so it is a startup failure rather than a warning.

**The cache cannot lie about age.** The cache stores the envelope with the payload and the age is
recomputed on every serve from ``data_as_of_ms``. A cached response that reports itself as fresh
turns a visible outage into an invisible one.

**A failure is never an empty page.** A service error renders as ``DOWN`` with its reason and a
correlation id, never as a clean empty list — "no setups" and "we cannot see" must look different.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from api.services import HealthService, MarketService, ServiceResult, SignalService
from core.errors import ALL_ERRORS, ConfigError, StartupError
from core.logging import get_logger

log = get_logger("api.app")
COMPONENT = "api.app"

#: Live signals change at most once per minute (the 1m trigger cadence), so a short cache is safe.
LIVE_CACHE_SECONDS = 2.0


@dataclass
class _CacheEntry:
    result: ServiceResult
    stored_at: float


@dataclass
class ResponseCache:
    """A tiny TTL cache that re-stamps the envelope on serve.

    The stored envelope keeps its ``data_as_of_ms``; only ``served_at_ms`` is refreshed, so
    ``age_seconds`` always describes the **data**, never the cache entry. That is the whole point of
    §3's cache trap, and :func:`tests.test_api.test_a_cached_response_reports_data_age_not_cache_age`
    holds it in place.
    """

    ttl_seconds: float = LIVE_CACHE_SECONDS
    _entries: dict[str, _CacheEntry] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, key: str) -> ServiceResult | None:
        entry = self._entries.get(key)
        if entry is None or (time.monotonic() - entry.stored_at) > self.ttl_seconds:
            self.misses += 1
            return None
        self.hits += 1
        envelope = entry.result.envelope
        refreshed = type(envelope)(
            served_at_ms=int(time.time() * 1000), data_as_of_ms=envelope.data_as_of_ms,
            system_status=envelope.system_status, sources=dict(envelope.sources),
            degraded_reasons=list(envelope.degraded_reasons),
            correlation_id=envelope.correlation_id)
        return ServiceResult(payload=entry.result.payload, envelope=refreshed,
                             error=entry.result.error)

    def put(self, key: str, result: ServiceResult) -> ServiceResult:
        # A failed result is not cached: repeating a failure for two seconds would delay recovery and
        # make an outage look steadier than it is.
        if result.ok:
            self._entries[key] = _CacheEntry(result=result, stored_at=time.monotonic())
        return result

    def clear(self) -> None:
        self._entries.clear()


def assert_bind_allowed(host: str, auth_token: str, *, public_via_tunnel: bool = False) -> None:
    """specs/10 §9 / specs/14 §11. A startup failure, not a warning.

    ``public_via_tunnel`` covers the case the bind address cannot: a reverse proxy or tunnel
    forwarding public traffic to a loopback bind. The process looks local and is not, so the token
    becomes mandatory on the operator's declaration rather than on the address.
    """
    if public_via_tunnel and not auth_token:
        raise StartupError(
            "refusing to serve through a tunnel without an auth token",
            component=COMPONENT, operation="assert_bind_allowed",
            context={"host": host, "public_via_tunnel": True, "auth_token_configured": False},
            likely_cause="a tunnel or reverse proxy forwards public traffic to this loopback bind, "
                         "so the bind address no longer means the process is unreachable",
            remediation="set CT_SERVER__AUTH_TOKEN to a long random value. An unguessable tunnel "
                        "URL is obscurity, not authentication",
        )
    localhost = host in ("127.0.0.1", "localhost", "::1")
    if not localhost and not auth_token:
        raise StartupError(
            f"refusing to bind {host} without an auth token",
            component=COMPONENT, operation="assert_bind_allowed",
            context={"host": host, "auth_token_configured": False},
            likely_cause="the server was configured to listen on a non-loopback address with no "
                         "authentication",
            remediation="bind 127.0.0.1 and reach it through an SSH tunnel, or set CT_SERVER__AUTH_TOKEN. "
                        "Exposure without authentication is not a supported configuration",
        )


def status_code_for(result: ServiceResult) -> int:
    """HTTP status from the envelope. A degraded system still answers 200 with its reasons — the
    envelope carries the truth, and a 500 would hide a partially useful response."""
    if result.error is not None:
        return 503
    return 200


def create_app(*, signals: SignalService, market: MarketService, health: HealthService,
               host: str = "127.0.0.1", auth_token: str = "", public_via_tunnel: bool = False,
               regimes_source: Any = None, features_source: Any = None) -> Any:
    """Build the FastAPI app. Raises before serving if the exposure is not permitted."""
    assert_bind_allowed(host, auth_token, public_via_tunnel=public_via_tunnel)
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as e:                                    # pragma: no cover - env guard
        raise ConfigError(
            "FastAPI is not installed, so the API cannot be served",
            component=COMPONENT, operation="create_app",
            likely_cause="the optional web dependencies are missing from this environment",
            remediation="pip install -e . — fastapi, uvicorn and jinja2 are core dependencies, "
                        "not an optional extra",
            cause=e,
        ) from e

    app = FastAPI(title="crypto-trader suggestions", docs_url="/api/docs",
                  description="Read-only. This system never places orders.")
    cache = ResponseCache()
    app.state.cache = cache
    app.state.auth_token = auth_token

    def respond(result: ServiceResult) -> Any:
        return JSONResponse(result.to_dict(), status_code=status_code_for(result))

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        """Authentication when configured, and a structured error for anything unhandled.

        No endpoint accepts a parameter that reaches the strategy or risk engines: the API is
        read-only in the strongest sense (specs/14 §11), which is a property of the routes below
        rather than something this middleware can enforce — so the routes take no such parameter.
        """
        if auth_token:
            supplied = request.headers.get("x-auth-token", "")
            # Constant-time: a plain `!=` returns as soon as two bytes differ, so the time taken
            # leaks how much of the token was guessed correctly. That is only worth a few thousand
            # requests to an attacker who can measure it, and the fix is one import.
            if not secrets.compare_digest(supplied, auth_token):
                return JSONResponse({"error": {"code": "API_UNAUTHORISED",
                                               "message": "a valid x-auth-token header is required"}},
                                    status_code=401)
        try:
            return await call_next(request)
        except ALL_ERRORS as e:
            log.error("request_failed", path=str(request.url.path), error=e.to_dict())
            return JSONResponse({"error": e.to_dict()}, status_code=503)
        except Exception as e:                                  # noqa: BLE001 - never leak a trace
            log.error("request_crashed", path=str(request.url.path), error=str(e),
                      error_type=type(e).__name__)
            return JSONResponse(
                {"error": {"code": "API_UNHANDLED", "problem_class": "application_bug",
                           "message": "an unhandled error occurred; see the logs for the trace",
                           "path": str(request.url.path)}}, status_code=500)

    # ---- JSON -----------------------------------------------------------------------------

    @app.get("/api/signals/live")
    def live(asset: str | None = None) -> Any:
        key = f"live:{asset or '*'}"
        cached = cache.get(key)
        if cached is not None:
            return respond(cached)
        return respond(cache.put(key, signals.live(asset=asset)))

    @app.get("/api/signals/history")
    def history(limit: int = 50, cursor_ms: int | None = None, cursor_id: str | None = None) -> Any:
        cursor = (cursor_ms, cursor_id) if cursor_ms is not None and cursor_id else None
        return respond(signals.history(cursor=cursor, limit=min(limit, 200)))

    @app.get("/api/signals/{signal_id}")
    def detail(signal_id: str) -> Any:
        return respond(signals.detail(signal_id))

    @app.get("/api/signals/{signal_id}/audit")
    def audit(signal_id: str) -> Any:
        return respond(signals.audit(signal_id))

    @app.get("/api/events")
    def events(since: int = 0) -> Any:
        return respond(signals.events_since(since))

    @app.get("/api/assets/{asset}")
    def asset_state(asset: str) -> Any:
        return respond(market.asset_state(asset))

    @app.get("/api/assets/{asset}/rejections")
    def rejections(asset: str) -> Any:
        return respond(market.rejections(asset))

    @app.get("/api/regime")
    def regime() -> Any:
        source = regimes_source() if callable(regimes_source) else regimes_source
        return respond(market.regime(source))

    @app.get("/api/health")
    def health_endpoint() -> Any:
        return respond(health.health())

    @app.get("/api/warmup")
    def warmup() -> Any:
        source = features_source() if callable(features_source) else features_source
        return respond(health.warmup(source))

    # ---- HTML fragments -------------------------------------------------------------------
    #
    # The same service calls, rendered as HTMX fragments. Thin on purpose.

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        from api.render import render_page
        return HTMLResponse(render_page(signals.live(), health.health()))

    @app.get("/fragments/signals", response_class=HTMLResponse)
    def fragment_signals(asset: str | None = None) -> Any:
        from api.render import render_signals
        key = f"live:{asset or '*'}"
        result = cache.get(key) or cache.put(key, signals.live(asset=asset))
        return HTMLResponse(render_signals(result))

    @app.get("/fragments/health", response_class=HTMLResponse)
    def fragment_health() -> Any:
        from api.render import render_health
        return HTMLResponse(render_health(health.health()))

    @app.get("/fragments/rejections/{asset}", response_class=HTMLResponse)
    def fragment_rejections(asset: str) -> Any:
        from api.render import render_rejections
        return HTMLResponse(render_rejections(market.rejections(asset)))

    log.info("api_created", host=host, authenticated=bool(auth_token),
             cache_ttl_s=cache.ttl_seconds)
    return app
