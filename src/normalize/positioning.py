"""Positioning normalizer — futures ``data/stats`` (F-019).

Public, unauthenticated GET. Two verified traps:

* an **unknown or missing pair returns HTTP 200 with empty objects**
  (``{"price_change_percent":{},"high_and_low":{},"position":{}}``) — silently. That is rejected
  here rather than stored as "no positioning";
* the payload carries **no timestamp of its own**, so the local receive time is used and labelled as
  such. Every consumer must treat this series' age as poll-cadence-bounded, not source-stamped.
"""
from __future__ import annotations

from core.errors import CryptoTraderError, SchemaError
from normalize.base import PayloadContext, dec, parse_json, require_fields, require_mapping
from normalize.canonical import Positioning
from normalize.errors import Accepted, NormalizeOutcome, Rejected


def normalize(raw: bytes, ctx: PayloadContext) -> NormalizeOutcome:
    try:
        obj = require_mapping(parse_json(raw, ctx), ctx, what="positioning")
        position = obj.get("position")
        if not isinstance(position, dict) or not position:
            raise SchemaError(
                "position object is empty — the pair is unknown to the endpoint",
                component=ctx.component, operation="normalize",
                context={"asset": ctx.asset, "url": ctx.request_url, "top_level": sorted(obj)},
                likely_cause="a missing or misspelled pair parameter returns 200 with empty objects (F-019)",
                remediation="check the pair string; an empty payload must never be stored as zero positioning",
            )
        counts = require_mapping(position.get("count_percent", {}), ctx, what="count_percent")
        values = require_mapping(position.get("value_percent", {}), ctx, what="value_percent")
        require_fields(counts, ("long", "short"), ctx, where="count_percent")
        require_fields(values, ("long", "short"), ctx, where="value_percent")
        rec = Positioning(
            asset=ctx.asset or "",
            ts_ms=ctx.received_at_ms,          # the payload carries no timestamp of its own
            long_count_pct=dec(counts, "long", ctx),
            short_count_pct=dec(counts, "short", ctx),
            long_value_pct=dec(values, "long", ctx),
            short_value_pct=dec(values, "short", ctx),
        )
    except CryptoTraderError as e:
        return Rejected(error=e, raw_ref=ctx.raw_ref)
    except ValueError as e:
        return Rejected(
            error=SchemaError(
                f"positioning failed validation: {e}",
                component=ctx.component, operation="normalize",
                context={"asset": ctx.asset},
                likely_cause="long and short percentages did not sum to 100",
                remediation="reject it; a partial split would silently distort POS_DIVERGENCE",
                cause=e,
            ),
            raw_ref=ctx.raw_ref,
        )
    return Accepted(records=(rec,), raw_ref=ctx.raw_ref,
                    warnings=("ts_ms is the local receive time: this endpoint carries no timestamp",))
