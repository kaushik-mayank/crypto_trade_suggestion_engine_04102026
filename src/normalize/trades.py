"""Trade-tape normalizers — spot and futures (hazard #7; F-011, F-016).

Both tapes are **snapshots**, and their limits are verified:

* **spot** ``/market_data/trade_history`` returns up to 500 trades spanning ~25 s, newest first,
  with ``m`` a JSON boolean;
* **futures** ``data/trades`` returns a **fixed 30 rows** spanning ~7 s, oldest first, and
  **ignores ``limit``** entirely.

Neither can reconstruct per-bar aggressor volume, so ``VLM_AGGR`` stays low-confidence by
construction. ``is_buyer_maker`` is the one canonical form of the aggressor flag (hazard #7).
"""
from __future__ import annotations

from core.errors import CryptoTraderError, SchemaError
from normalize.base import (
    PayloadContext,
    as_int,
    canonical_bool,
    dec,
    parse_json,
    require_fields,
    require_list,
    require_mapping,
)
from normalize.canonical import Product, Trade
from normalize.errors import Accepted, NormalizeOutcome, Rejected

#: (price, qty, timestamp, aggressor-flag) field names per product.
SPOT_FIELDS = ("p", "q", "T", "m")
FUTURES_FIELDS = ("price", "quantity", "timestamp", "is_maker")


def _normalize(raw: bytes, ctx: PayloadContext, *, product: Product,
               fields: tuple[str, str, str, str]) -> NormalizeOutcome:
    price_f, qty_f, ts_f, flag_f = fields
    try:
        rows = require_list(parse_json(raw, ctx), ctx, what=f"{product.value} trades")
        trades: list[Trade] = []
        for i, row in enumerate(rows):
            require_mapping(row, ctx, what=f"trade[{i}]")
            require_fields(row, fields, ctx, where=f"trade[{i}]")
            trades.append(Trade(
                asset=ctx.asset or "",
                product=product,
                ts_ms=as_int(row, ts_f, ctx),
                price=dec(row, price_f, ctx),
                qty=dec(row, qty_f, ctx),
                is_buyer_maker=canonical_bool(row[flag_f], ctx, field=flag_f),
                source=f"{product.value}_trades",
            ))
    except CryptoTraderError as e:
        return Rejected(error=e, raw_ref=ctx.raw_ref)
    except ValueError as e:
        return Rejected(
            error=SchemaError(f"trade failed validation: {e}", component=ctx.component,
                              operation="normalize", context={"asset": ctx.asset}, cause=e),
            raw_ref=ctx.raw_ref,
        )

    trades.sort(key=lambda t: t.ts_ms)                      # canonical ascending, whatever the source order
    warnings = []
    if product is Product.FUTURES and len(trades) == 30:
        warnings.append("futures tape is a fixed 30-row snapshot (~7 s) and ignores limit (F-011)")
    if trades:
        span_s = (trades[-1].ts_ms - trades[0].ts_ms) / 1000
        warnings.append(f"tape covers {span_s:.1f}s: a snapshot, not a complete record")
    return Accepted(records=tuple(trades), raw_ref=ctx.raw_ref, warnings=tuple(warnings))


def normalize_spot(raw: bytes, ctx: PayloadContext) -> NormalizeOutcome:
    return _normalize(raw, ctx, product=Product.SPOT, fields=SPOT_FIELDS)


def normalize_futures(raw: bytes, ctx: PayloadContext) -> NormalizeOutcome:
    return _normalize(raw, ctx, product=Product.FUTURES, fields=FUTURES_FIELDS)
