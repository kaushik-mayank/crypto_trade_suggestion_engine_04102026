"""Shared normalizer scaffolding.

A normalizer takes raw bytes plus the context of the request that produced them, and returns an
:mod:`normalize.errors` outcome. It never parses *and* stores; it never coerces; and every rejection
names the endpoint, the field and the raw reference so the actual payload can be inspected.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from core.errors import MalformedPayload, SchemaError
from normalize.canonical import Product, Timeframe, to_decimal


@dataclass(frozen=True)
class PayloadContext:
    """What the ingest layer knows about a response, passed to the normalizer unchanged."""

    endpoint: str
    received_at_ms: int
    sent_at_ms: int = 0
    request_url: str = ""
    asset: str | None = None
    product: Product | None = None
    timeframe: Timeframe | None = None
    raw_ref: str | None = None

    @property
    def component(self) -> str:
        return f"normalize.{self.endpoint}"


def parse_json(raw: bytes, ctx: PayloadContext) -> Any:
    """Decode a response body, or raise :class:`MalformedPayload` keeping the raw reference."""
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise MalformedPayload(
            f"response body is not valid JSON ({type(e).__name__})",
            component=ctx.component, operation="parse_json",
            context={"endpoint": ctx.endpoint, "url": ctx.request_url, "bytes": len(raw),
                     "head": raw[:120].decode("utf-8", errors="replace"), "raw_ref": ctx.raw_ref},
            likely_cause="an HTML error page, a truncated response, or a gateway interstitial",
            remediation="inspect the captured raw bytes; if the shape changed, add it as a fixture "
                        "and update this normalizer (specs/19 §8)",
            cause=e,
        ) from e


def require_mapping(obj: Any, ctx: PayloadContext, *, what: str = "payload") -> dict:
    if not isinstance(obj, dict):
        raise SchemaError(
            f"{what} is {type(obj).__name__}, expected an object",
            component=ctx.component, operation="require_mapping",
            context={"endpoint": ctx.endpoint, "url": ctx.request_url, "raw_ref": ctx.raw_ref},
            likely_cause="the endpoint changed shape, or an error body was returned with HTTP 200",
            remediation="inspect the raw payload before changing any parsing code",
        )
    return obj


def require_list(obj: Any, ctx: PayloadContext, *, what: str = "payload") -> list:
    if not isinstance(obj, list):
        raise SchemaError(
            f"{what} is {type(obj).__name__}, expected an array",
            component=ctx.component, operation="require_list",
            context={"endpoint": ctx.endpoint, "url": ctx.request_url, "raw_ref": ctx.raw_ref,
                     "keys": sorted(obj)[:10] if isinstance(obj, dict) else None},
            likely_cause="an error object was returned with HTTP 200 (CoinDCX does this for some "
                         "invalid parameters), or the endpoint changed shape",
            remediation="inspect the raw payload; check for an errorCode field",
        )
    return obj


def require_fields(row: dict, fields: tuple[str, ...], ctx: PayloadContext, *, where: str) -> None:
    missing = [f for f in fields if f not in row]
    if missing:
        raise SchemaError(
            f"{where} is missing required field(s): {', '.join(missing)}",
            component=ctx.component, operation="require_fields",
            context={"missing": missing, "present": sorted(row)[:20], "url": ctx.request_url},
            likely_cause="a schema-incomplete row (verified real for B-NKN_USDT), or a renamed field",
            remediation="reject the row rather than defaulting it; a coerced value is "
                        "indistinguishable from a real one downstream (specs/11 §4.2)",
        )


def dec(row: dict, key: str, ctx: PayloadContext) -> Decimal:
    return to_decimal(row[key], field=key, component=ctx.component, operation="decimal")


def dec_opt(row: dict, key: str, ctx: PayloadContext) -> Decimal | None:
    v = row.get(key)
    return None if v is None else to_decimal(v, field=key, component=ctx.component, operation="decimal")


def as_int(row: dict, key: str, ctx: PayloadContext) -> int:
    v = row.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, Decimal, float, str)):
        raise SchemaError(
            f"{key} is not an integer: {v!r}",
            component=ctx.component, operation="as_int",
            context={"field": key, "value": repr(v)[:60], "url": ctx.request_url},
            likely_cause="a null or a string where a timestamp was expected",
            remediation="reject the row; do not substitute a local clock reading",
        )
    try:
        return int(Decimal(str(v)))
    except Exception as e:  # noqa: BLE001 - re-raised as a classified error below
        raise SchemaError(
            f"{key} cannot be read as an integer: {v!r}",
            component=ctx.component, operation="as_int",
            context={"field": key, "value": repr(v)[:60]}, cause=e,
        ) from e


def canonical_bool(value: Any, ctx: PayloadContext, *, field: str) -> bool:
    """Hazard #7: the aggressor flag is ``bool`` in spot REST, ``0``/``1`` in spot WS and ``bool``
    in futures. One canonical form, and anything else is rejected rather than guessed."""
    if isinstance(value, bool):
        return value
    if value in (0, 1, "0", "1", Decimal(0), Decimal(1)):
        return bool(int(value))
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise SchemaError(
        f"{field} is not a recognised boolean encoding: {value!r}",
        component=ctx.component, operation="canonical_bool",
        context={"field": field, "value": repr(value)[:60]},
        likely_cause="hazard #7: the flag's encoding varies by endpoint",
        remediation="add the new encoding to canonical_bool only after confirming its meaning",
    )
