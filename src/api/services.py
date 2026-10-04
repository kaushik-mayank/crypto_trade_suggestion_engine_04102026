"""The service layer (specs/14 §2).

> **Engines write. The API reads. No signal logic, no feature computation, no risk evaluation lives
> behind an HTTP handler.**

A slow or repeated query must never delay signal evaluation, and a dashboard refresh must never change
a signal's content. The two paths touch only through the store.

Both renderings — JSON and HTML fragments — call *these* methods. Duplicating logic between them is a
defect, so the handlers are thin by construction: they choose a representation, never compute one.

**Absence of data is not absence of signals.** An empty panel means "healthy market, no setups"; a
failed query means "we don't know". Conflating them tells the user the market is quiet when the system
is blind, which specs/14 §10 calls the single most dangerous thing this API can do. Every service
method therefore returns a result object carrying its own status, and a failure sets ``DOWN`` with a
reason rather than returning an empty list.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.errors import ALL_ERRORS, CryptoTraderError, ServiceUnavailable
from core.health import HealthReport, SystemStatus
from core.logging import get_logger

log = get_logger("api.services")
COMPONENT = "api.services"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class FreshnessEnvelope:
    """Attached to every response (specs/14 §3).

    ``age_seconds`` is computed from ``data_as_of_ms``, **never** from when a cache entry was created.
    A cached response that reports itself as fresh turns a visible outage into an invisible one, so the
    cache stores this envelope alongside the payload and the age is recomputed on serve.
    """

    served_at_ms: int
    data_as_of_ms: int | None
    system_status: SystemStatus
    sources: dict[str, Any] = field(default_factory=dict)
    degraded_reasons: list[str] = field(default_factory=list)
    correlation_id: str = ""

    @property
    def age_seconds(self) -> float | None:
        if self.data_as_of_ms is None:
            return None
        return max(0.0, (self.served_at_ms - self.data_as_of_ms) / 1000)

    def to_dict(self) -> dict[str, Any]:
        return {"served_at_ms": self.served_at_ms, "data_as_of_ms": self.data_as_of_ms,
                "age_seconds": self.age_seconds, "system_status": self.system_status.value,
                "sources": dict(self.sources), "degraded_reasons": list(self.degraded_reasons),
                "correlation_id": self.correlation_id}


@dataclass
class ServiceResult:
    """A payload plus the envelope that says how much to trust it."""

    payload: Any
    envelope: FreshnessEnvelope
    error: CryptoTraderError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        out = {"envelope": self.envelope.to_dict(), "data": self.payload}
        if self.error is not None:
            # Structured, never a stack trace; the correlation id ties it to the logs.
            out["error"] = {**self.error.to_dict(),
                            "correlation_id": self.envelope.correlation_id}
        return out


@dataclass
class BaseService:
    store: Any
    health_source: Any = None

    def _envelope(self, data_as_of_ms: int | None, *, status: SystemStatus | None = None,
                  reasons: list[str] | None = None) -> FreshnessEnvelope:
        report = self._health_report()
        return FreshnessEnvelope(
            served_at_ms=_now_ms(), data_as_of_ms=data_as_of_ms,
            system_status=status or (report.status if report else SystemStatus.HEALTHY),
            sources={c.name: {"status": c.status.value, "detail": c.detail,
                              "age_s": c.age_seconds, "problem_class":
                                  c.problem_class.value if c.problem_class else None}
                     for c in (report.components if report else ())},
            degraded_reasons=reasons or (list(report.degraded_reasons) if report else []),
            correlation_id=uuid.uuid4().hex[:12])

    def _health_report(self) -> HealthReport | None:
        source = self.health_source
        if source is None:
            return None
        try:
            return source() if callable(source) else source
        except ALL_ERRORS as e:
            log.error("health_source_failed", error=e.to_dict())
            return None

    def _failed(self, operation: str, e: CryptoTraderError, *, payload: Any) -> ServiceResult:
        """A failure returns ``DOWN`` with the reason and an *explicitly empty* payload shape.

        The payload keeps its type — an empty list stays a list — so a caller's rendering does not
        also have to handle None, but the envelope says DOWN so nobody reads the emptiness as calm.
        """
        envelope = self._envelope(None, status=SystemStatus.DOWN,
                                  reasons=[f"{operation}: {e.message}"])
        log.error("service_failed", operation=operation, correlation_id=envelope.correlation_id,
                  error=e.to_dict())
        return ServiceResult(payload=payload, envelope=envelope, error=e)


@dataclass
class SignalService(BaseService):
    """Reads signals, their detail, their audit record and their history."""

    def live(self, *, asset: str | None = None, include_untradeable: bool = False) -> ServiceResult:
        """The current set. Control-strategy signals are filtered out here as well as upstream.

        specs/13 §13 requires that no endpoint ever returns a signal with ``tradeable = False``; the
        filter lives at the boundary as well as in the runner so that forgetting one does not leak.
        """
        try:
            rows = self.store.conn.execute(
                """SELECT * FROM signals
                   WHERE state IN ('SETUP_DETECTED','ENTER_NOW','TRIGGERED')
                   """ + ("AND asset = ? " if asset else "") + """
                   ORDER BY updated_at_ms DESC LIMIT 200""",
                ((asset,) if asset else ())).fetchall()
            signals = [self._row_to_signal(r) for r in rows]
            if not include_untradeable:
                signals = [s for s in signals if s.get("tradeable", True)]
            newest = max((s["as_of_ms"] for s in signals), default=None)
            return ServiceResult(payload={"signals": signals, "count": len(signals),
                                          "by_asset": self._group(signals)},
                                 envelope=self._envelope(newest))
        except ALL_ERRORS as e:
            return self._failed("signals.live", e, payload={"signals": [], "count": 0,
                                                            "by_asset": {}})

    def detail(self, signal_id: str) -> ServiceResult:
        try:
            row = self.store.conn.execute("SELECT * FROM signals WHERE signal_id = ?",
                                          (signal_id,)).fetchone()
            if row is None:
                raise ServiceUnavailable(
                    f"no signal {signal_id}",
                    component=COMPONENT, operation="detail",
                    context={"signal_id": signal_id},
                    likely_cause="the id is wrong, or the signal predates the current store",
                    remediation="list /api/signals/history to find valid ids")
            signal = self._row_to_signal(row)
            events = [dict(r) for r in self.store.conn.execute(
                "SELECT * FROM signal_events WHERE signal_id = ? ORDER BY at_ms", (signal_id,))]
            return ServiceResult(payload={"signal": signal, "events": events},
                                 envelope=self._envelope(signal["as_of_ms"]))
        except ALL_ERRORS as e:
            return self._failed("signals.detail", e, payload=None)

    def audit(self, signal_id: str) -> ServiceResult:
        try:
            row = self.store.conn.execute(
                "SELECT * FROM signal_audit WHERE signal_id = ? ORDER BY as_of_ms DESC LIMIT 1",
                (signal_id,)).fetchone()
            if row is None:
                raise ServiceUnavailable(
                    f"no audit record for {signal_id}",
                    component=COMPONENT, operation="audit",
                    context={"signal_id": signal_id},
                    likely_cause="the signal was produced before the audit trail was wired, or the id "
                                 "is wrong",
                    remediation="every signal written since PHASE-22 carries one")
            record = dict(row)
            record["record"] = json.loads(record["record"])
            return ServiceResult(payload=record, envelope=self._envelope(record["as_of_ms"]))
        except ALL_ERRORS as e:
            return self._failed("signals.audit", e, payload=None)

    def history(self, *, cursor: tuple[int, str] | None = None, limit: int = 50) -> ServiceResult:
        """Cursor-paginated on ``(created_at_ms, signal_id)``.

        Offset pagination is rejected by specs/14 §11 because it skips or duplicates rows when new
        signals arrive mid-page — and this table is written to while it is read.
        """
        try:
            if cursor:
                rows = self.store.conn.execute(
                    """SELECT * FROM signals WHERE (created_at_ms, signal_id) < (?, ?)
                       ORDER BY created_at_ms DESC, signal_id DESC LIMIT ?""",
                    (cursor[0], cursor[1], limit)).fetchall()
            else:
                rows = self.store.conn.execute(
                    "SELECT * FROM signals ORDER BY created_at_ms DESC, signal_id DESC LIMIT ?",
                    (limit,)).fetchall()
            signals = [self._row_to_signal(r) for r in rows]
            next_cursor = ((signals[-1]["created_at_ms"], signals[-1]["signal_id"])
                           if len(signals) == limit else None)
            return ServiceResult(
                payload={"signals": signals, "next_cursor": next_cursor},
                envelope=self._envelope(max((s["as_of_ms"] for s in signals), default=None)))
        except ALL_ERRORS as e:
            return self._failed("signals.history", e, payload={"signals": [], "next_cursor": None})

    def events_since(self, since_ms: int, *, limit: int = 200) -> ServiceResult:
        try:
            rows = self.store.conn.execute(
                "SELECT * FROM signal_events WHERE at_ms > ? ORDER BY at_ms, id LIMIT ?",
                (since_ms, limit)).fetchall()
            events = [dict(r) for r in rows]
            return ServiceResult(payload={"events": events},
                                 envelope=self._envelope(max((e["at_ms"] for e in events),
                                                             default=None)))
        except ALL_ERRORS as e:
            return self._failed("signals.events", e, payload={"events": []})

    def _row_to_signal(self, row: Any) -> dict[str, Any]:
        out = dict(row)
        payload = json.loads(out.pop("payload", "{}") or "{}")
        out["payload"] = payload
        out["tradeable"] = bool(payload.get("tradeable", True))
        out["targets"] = json.loads(out.get("targets") or "[]")
        return out

    def _group(self, signals: list[dict[str, Any]]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for s in signals:
            grouped.setdefault(s["asset"], []).append(s["signal_id"])
        return grouped


@dataclass
class MarketService(BaseService):
    """Per-asset state, the regime, and the rejection record that answers "why is there no signal?"."""

    def asset_state(self, asset: str) -> ServiceResult:
        try:
            row = self.store.conn.execute(
                """SELECT * FROM bars WHERE asset = ? AND timeframe = '1h' AND is_final = 1
                   ORDER BY open_time_ms DESC LIMIT 1""", (asset,)).fetchone()
            latest = dict(row) if row else None
            rt = self.store.conn.execute(
                "SELECT * FROM futures_rt WHERE asset = ? ORDER BY ts_ms DESC LIMIT 1",
                (asset,)).fetchone()
            book = self.store.conn.execute(
                """SELECT * FROM book_snapshots WHERE asset = ? AND product = 'futures'
                   ORDER BY ts_ms DESC LIMIT 1""", (asset,)).fetchone()
            payload = {"asset": asset, "last_bar": latest,
                       "futures_rt": dict(rt) if rt else None,
                       "book": {k: dict(book)[k] for k in ("ts_ms", "version")} if book else None}
            as_of = latest["open_time_ms"] if latest else None
            return ServiceResult(payload=payload, envelope=self._envelope(as_of))
        except ALL_ERRORS as e:
            return self._failed("market.asset_state", e, payload={"asset": asset})

    def rejections(self, asset: str, *, limit: int = 50) -> ServiceResult:
        """specs/14 §7 — the endpoint most systems cannot offer, because they never recorded this."""
        try:
            rows = self.store.conn.execute(
                """SELECT * FROM rejections WHERE asset = ? ORDER BY at_ms DESC LIMIT ?""",
                (asset, limit)).fetchall()
            rejections = []
            for r in rows:
                item = dict(r)
                item["conditions"] = json.loads(item.get("conditions") or "[]")
                rejections.append(item)
            binding = self.store.conn.execute(
                """SELECT strategy_id, binding_condition, COUNT(*) AS n FROM rejections
                   WHERE asset = ? AND binding_condition IS NOT NULL
                   GROUP BY strategy_id, binding_condition ORDER BY n DESC LIMIT 10""",
                (asset,)).fetchall()
            return ServiceResult(
                payload={"asset": asset, "rejections": rejections,
                         "most_binding": [dict(b) for b in binding]},
                envelope=self._envelope(max((r["as_of_ms"] for r in rejections), default=None)))
        except ALL_ERRORS as e:
            return self._failed("market.rejections", e,
                                payload={"asset": asset, "rejections": [], "most_binding": []})

    def regime(self, regimes: dict[str, Any] | None = None) -> ServiceResult:
        """Regime state per asset. Supplied by the runner rather than recomputed — the API never
        computes (specs/14 §1)."""
        payload = {"assets": regimes or {},
                   "note": "regime state is written by the engine; the API reads it"}
        reasons = [f"{a}: regime degraded" for a, r in (regimes or {}).items()
                   if isinstance(r, dict) and r.get("degraded")]
        return ServiceResult(payload=payload,
                             envelope=self._envelope(
                                 max((r.get("as_of_ms", 0) for r in (regimes or {}).values()),
                                     default=None) or None,
                                 status=SystemStatus.DEGRADED if reasons else None,
                                 reasons=reasons))


@dataclass
class HealthService(BaseService):
    """Health and warm-up. Warm-up is a **state**, not an error (specs/14 §5)."""

    def health(self) -> ServiceResult:
        report = self._health_report()
        if report is None:
            e = ServiceUnavailable(
                "no health source is attached to the API",
                component=COMPONENT, operation="health",
                likely_cause="the app was constructed without passing the runner's health callable",
                remediation="pass health_source=runner.health when building the services")
            return self._failed("health", e, payload={"components": []})
        return ServiceResult(payload=report.to_dict(),
                             envelope=self._envelope(None, status=report.status,
                                                     reasons=list(report.degraded_reasons)))

    def warmup(self, feature_sets: dict[str, Any] | None = None) -> ServiceResult:
        """Per-asset warm-up: what is still warming, and how much longer.

        A warming system is not a broken one, and the dashboard must be able to say which.
        """
        assets: dict[str, Any] = {}
        for asset, fs in (feature_sets or {}).items():
            warming = [v.to_dict() for v in fs if v.status.value == "WARMING"]
            assets[asset] = {
                "computed": len(fs), "ok": fs.ok_count,
                "statuses": fs.status_counts(),
                "warming": warming[:20],
                "hours_to_warm": max((v.get("detail", {}).get("hours_to_warm", 0) or 0
                                      for v in warming), default=0),
            }
        any_warming = any(a["statuses"].get("WARMING") for a in assets.values())
        return ServiceResult(
            payload={"assets": assets, "warming": any_warming},
            envelope=self._envelope(None,
                                    status=SystemStatus.WARMING if any_warming else None,
                                    reasons=["features are still warming"] if any_warming else []))


def build_services(store: Any, *, health_source: Any = None
                   ) -> tuple[SignalService, MarketService, HealthService]:
    return (SignalService(store=store, health_source=health_source),
            MarketService(store=store, health_source=health_source),
            HealthService(store=store, health_source=health_source))
