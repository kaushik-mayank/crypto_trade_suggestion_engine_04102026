"""Instrument metadata normalizer (invariant 1, specs/08 §6).

Fees arrive as **percent** and become :class:`Fee` objects here — the single conversion point for the
100× error. Margin tiers come from ``dynamic_safety_margin_details`` and are **never** hardcoded:
without them, liquidation distance cannot be computed and no signal may be published (``DQ_META``).
"""
from __future__ import annotations

from decimal import Decimal

from core.errors import CryptoTraderError, SchemaError
from normalize.base import PayloadContext, as_int, dec, dec_opt, parse_json, require_fields, require_mapping
from normalize.canonical import Fee, InstrumentMeta, MarginTier, Product
from normalize.errors import Accepted, NormalizeOutcome, Rejected

REQUIRED = ("pair", "price_increment", "quantity_increment", "min_notional", "maker_fee", "taker_fee")


def _margin_tiers(node: object, ctx: PayloadContext) -> tuple[MarginTier, ...]:
    """``dynamic_safety_margin_details`` maps a notional ceiling to a maintenance-margin percent."""
    if not isinstance(node, dict) or not node:
        return ()
    tiers: list[MarginTier] = []
    for ceiling, rate in node.items():
        try:
            max_notional = Decimal(str(ceiling))
            pct = Decimal(str(rate))
        except Exception as e:  # noqa: BLE001 - reclassified immediately
            raise SchemaError(
                f"margin tier {ceiling!r}: {rate!r} is not numeric",
                component=ctx.component, operation="margin_tiers",
                context={"ceiling": repr(ceiling)[:40], "rate": repr(rate)[:40]},
                likely_cause="the tier table changed shape",
                remediation="inspect the instrument payload; sizing must not fall back to a default",
                cause=e,
            ) from e
        # Verified: BTC returns 0.5 at the 20,000 tier, meaning 0.5 PERCENT maintenance margin.
        tiers.append(MarginTier(max_notional=max_notional, maintenance_margin_rate=pct / Decimal(100)))
    tiers.sort(key=lambda t: t.max_notional)
    return tuple(tiers)


def normalize(raw: bytes, ctx: PayloadContext) -> NormalizeOutcome:
    try:
        obj = require_mapping(parse_json(raw, ctx), ctx, what="instrument")
        inst = require_mapping(obj.get("instrument", obj), ctx, what="instrument.instrument")
        require_fields(inst, REQUIRED, ctx, where="instrument")
        leverage = dec_opt(inst, "max_leverage_long", ctx) or dec_opt(inst, "max_leverage", ctx)
        meta = InstrumentMeta(
            asset=str(inst["pair"]),
            product=ctx.product or Product.FUTURES,
            tick=dec(inst, "price_increment", ctx),
            qty_increment=dec(inst, "quantity_increment", ctx),
            min_notional=dec(inst, "min_notional", ctx),
            maker_fee=Fee.from_api_percent(inst["maker_fee"], component=ctx.component, operation="maker_fee"),
            taker_fee=Fee.from_api_percent(inst["taker_fee"], component=ctx.component, operation="taker_fee"),
            multiplier_up=dec_opt(inst, "multiplier_up", ctx),
            multiplier_down=dec_opt(inst, "multiplier_down", ctx),
            funding_frequency_h=as_int(inst, "funding_frequency", ctx) if "funding_frequency" in inst else None,
            max_leverage=leverage,
            margin_tiers=_margin_tiers(inst.get("dynamic_safety_margin_details"), ctx),
            status=str(inst.get("status", "unknown")),
            fetched_at_ms=ctx.received_at_ms,
        )
    except CryptoTraderError as e:
        return Rejected(error=e, raw_ref=ctx.raw_ref)
    except ValueError as e:
        return Rejected(
            error=SchemaError(
                f"instrument failed validation: {e}",
                component=ctx.component, operation="normalize",
                context={"url": ctx.request_url},
                likely_cause="a non-positive tick, increment or min_notional",
                remediation="reject it: tick rounding and sizing both divide by these",
                cause=e,
            ),
            raw_ref=ctx.raw_ref,
        )

    warnings: list[str] = []
    if not meta.margin_tiers:
        warnings.append("no margin tiers: liquidation distance is uncomputable, so DQ_META will veto signals")
    if meta.status != "active":
        warnings.append(f"instrument status is {meta.status!r}")
    return Accepted(records=(meta,), raw_ref=ctx.raw_ref, warnings=tuple(warnings))
