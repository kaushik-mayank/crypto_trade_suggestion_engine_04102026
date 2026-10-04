"""Order-book normalizers — spot and futures (hazards #5, #6; F-014, F-026).

The book arrives as a **JSON object keyed by price string** with no guaranteed order and
inconsistent precision (``"11570.67000000"`` next to ``"2001"``). Ordering is established **here,
once**: keys become ``Decimal``, bids sort descending, asks ascending, and a ``"0"`` quantity means
*delete this level* so it is dropped.

Two verified traps:

* **an empty book is invalid, not "no liquidity"** — futures depth 5 or 100 returns HTTP 200 with
  ``{}`` on both sides (F-014);
* **a spot book timestamp can be minutes stale** while the response is 200 (F-026), so the caller
  passes ``max_age_ms`` and a stale snapshot is rejected rather than trusted.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from core.errors import CryptoTraderError, SchemaError, StaleDataError
from normalize.base import PayloadContext, as_int, parse_json, require_mapping
from normalize.canonical import BookSnapshot, Product
from normalize.errors import Accepted, NormalizeOutcome, Rejected


def _levels(obj: dict, ctx: PayloadContext, *, side: str) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    dropped_zero = 0
    for price_s, qty_s in obj.items():
        try:
            price, qty = Decimal(str(price_s)), Decimal(str(qty_s))
        except (InvalidOperation, ValueError) as e:
            raise SchemaError(
                f"{side} level has a non-numeric price or quantity: {price_s!r}: {qty_s!r}",
                component=ctx.component, operation="levels",
                context={"side": side, "price": repr(price_s)[:40], "qty": repr(qty_s)[:40]},
                likely_cause="the book payload shape changed",
                remediation="inspect the raw payload before adjusting parsing",
                cause=e,
            ) from e
        if qty == 0:
            dropped_zero += 1            # hazard #6: "0" means delete this level
            continue
        if qty < 0 or price <= 0:
            raise SchemaError(
                f"{side} level is nonsensical: price={price} qty={qty}",
                component=ctx.component, operation="levels",
                context={"side": side, "price": str(price), "qty": str(qty)},
                likely_cause="a sign or unit change in the book payload",
                remediation="reject the snapshot; never sort a negative quantity into the book",
            )
        out.append((price, qty))
    out.sort(key=lambda lv: lv[0], reverse=(side == "bids"))
    return out


def _normalize(raw: bytes, ctx: PayloadContext, *, product: Product, ts_field: str,
               version_field: str | None, max_age_ms: int | None) -> NormalizeOutcome:
    try:
        obj = require_mapping(parse_json(raw, ctx), ctx, what=f"{product.value} book")
        if "code" in obj and "message" in obj:      # CoinDCX returns an error object with some params
            raise SchemaError(
                f"book endpoint returned an error object: {obj.get('message')!r}",
                component=ctx.component, operation="normalize",
                context={"code": str(obj.get("code")), "errorCode": str(obj.get("errorCode")),
                         "url": ctx.request_url},
                likely_cause="an unsupported depth value, or capital-D 'Depth' (hazard #8 / F-001)",
                remediation="spot accepts lowercase depth in {1,5,10,20,50,100,200}; "
                            "futures accepts only {10,20,50} (F-014)",
            )
        bids_raw = require_mapping(obj.get("bids", {}), ctx, what="bids")
        asks_raw = require_mapping(obj.get("asks", {}), ctx, what="asks")
        if not bids_raw or not asks_raw:
            raise SchemaError(
                "book has an empty side — HTTP 200 with no levels is invalid, not 'no liquidity'",
                component=ctx.component, operation="normalize",
                context={"bid_levels": len(bids_raw), "ask_levels": len(asks_raw), "url": ctx.request_url},
                likely_cause="futures depth 5 or 100 returns 200 with an empty book (F-014)",
                remediation="request futures depth 10, 20 or 50 only",
            )
        ts_ms = as_int(obj, ts_field, ctx)
        if max_age_ms is not None and ctx.received_at_ms - ts_ms > max_age_ms:
            raise StaleDataError(
                f"book snapshot is {(ctx.received_at_ms - ts_ms) / 1000:.1f}s old",
                component=ctx.component, operation="normalize",
                context={"ts_ms": ts_ms, "received_at_ms": ctx.received_at_ms, "max_age_ms": max_age_ms},
                likely_cause="a spot book was observed 10.5 minutes stale while returning 200 (F-026)",
                remediation="treat as DQ_STALE: the risk engine must not size against a stale book",
            )
        snap = BookSnapshot(
            asset=ctx.asset or "",
            product=product,
            ts_ms=ts_ms,
            bids=tuple(_levels(bids_raw, ctx, side="bids")),
            asks=tuple(_levels(asks_raw, ctx, side="asks")),
            version=as_int(obj, version_field, ctx) if version_field and version_field in obj else None,
            fetched_at_ms=ctx.received_at_ms,
        )
    except CryptoTraderError as e:
        return Rejected(error=e, raw_ref=ctx.raw_ref)
    except ValueError as e:               # pydantic: crossed book, unsorted, empty side
        return Rejected(
            error=SchemaError(
                f"book failed validation: {e}",
                component=ctx.component, operation="normalize",
                context={"url": ctx.request_url},
                likely_cause="a crossed or unordered book, which indicates a partial or mixed snapshot",
                remediation="reject it; a crossed book would produce a negative spread downstream",
                cause=e,
            ),
            raw_ref=ctx.raw_ref,
        )
    return Accepted(records=(snap,), raw_ref=ctx.raw_ref)


def normalize_spot(raw: bytes, ctx: PayloadContext, *, max_age_ms: int | None = None) -> NormalizeOutcome:
    """Spot: ``{"timestamp": ms, "bids": {...}, "asks": {...}}``."""
    return _normalize(raw, ctx, product=Product.SPOT, ts_field="timestamp", version_field=None,
                      max_age_ms=max_age_ms)


def normalize_futures(raw: bytes, ctx: PayloadContext, *, max_age_ms: int | None = None) -> NormalizeOutcome:
    """Futures: ``{"ts": ms, "vs": version, "bids": {...}, "asks": {...}}``."""
    return _normalize(raw, ctx, product=Product.FUTURES, ts_field="ts", version_field="vs",
                      max_age_ms=max_age_ms)
