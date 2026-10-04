"""Candle normalizers — spot and futures (hazards #1, #3, #4).

* **Spot** ``/market_data/candles`` returns bars **descending** by time `[VERIFIED]`. Getting this
  wrong inverts time, which is the single most damaging silent bug available here, so the output is
  asserted ascending and the newest bar is asserted last.
* **Futures** ``/market_data/candlesticks`` wraps rows in ``{"s": "ok", "data": [...]}``, returns
  them **ascending**, and uses **milliseconds** in ``time`` even though the request's ``from``/``to``
  are in seconds (F-008). ``s: "no_data"`` is an empty result, not an error.
* Volume is **base**-denominated on both products (F-010), so no conversion is applied — and none
  may be added without re-running P6.
"""
from __future__ import annotations

from core.errors import CryptoTraderError, SchemaError
from normalize.base import PayloadContext, as_int, dec, parse_json, require_fields, require_list, require_mapping
from normalize.canonical import Bar, Product, Timeframe
from normalize.errors import Accepted, NormalizeOutcome, Rejected

BAR_FIELDS = ("open", "high", "low", "close", "volume", "time")


def _bars_from_rows(rows: list, ctx: PayloadContext, *, source: str) -> tuple[Bar, ...]:
    if ctx.asset is None or ctx.product is None or ctx.timeframe is None:
        raise SchemaError(
            "candle normalization needs asset, product and timeframe in the payload context",
            component=ctx.component, operation="normalize",
            context={"asset": ctx.asset, "product": str(ctx.product), "timeframe": str(ctx.timeframe)},
            likely_cause="the ingest adapter did not populate PayloadContext",
            remediation="pass the context the adapter already knows; never infer it from the payload",
        )
    bars: list[Bar] = []
    for i, row in enumerate(rows):
        require_mapping(row, ctx, what=f"bar[{i}]")
        require_fields(row, BAR_FIELDS, ctx, where=f"bar[{i}]")
        try:
            bars.append(Bar(
                asset=ctx.asset, product=ctx.product, timeframe=ctx.timeframe,
                open_time_ms=as_int(row, "time", ctx),
                open=dec(row, "open", ctx), high=dec(row, "high", ctx),
                low=dec(row, "low", ctx), close=dec(row, "close", ctx),
                volume_base=dec(row, "volume", ctx),
                source=source, fetched_at_ms=ctx.received_at_ms,
            ))
        except ValueError as e:      # pydantic validation: OHLC consistency, alignment, float prices
            raise SchemaError(
                f"bar[{i}] failed validation: {e}",
                component=ctx.component, operation="normalize",
                context={"row": {k: str(v) for k, v in row.items()}, "url": ctx.request_url},
                likely_cause="invalid OHLC ordering, a misaligned boundary, or a changed field type",
                remediation="inspect the raw payload; invalid OHLC must never reach the feature layer, "
                            "where it surfaces as a negative Garman-Klass variance",
                cause=e,
            ) from e

    bars.sort(key=lambda b: b.open_time_ms)
    times = [b.open_time_ms for b in bars]
    if len(set(times)) != len(times):
        dupes = sorted({t for t in times if times.count(t) > 1})
        raise SchemaError(
            f"duplicate bar open times in one page: {dupes[:5]}",
            component=ctx.component, operation="normalize",
            context={"duplicates": dupes[:5], "count": len(times), "url": ctx.request_url},
            likely_cause="overlapping pagination windows merged incorrectly, or the endpoint repeated a bar",
            remediation="deduplicate at the page boundary in the adapter, not by silently dropping rows here",
        )
    return tuple(bars)


def normalize_spot(raw: bytes, ctx: PayloadContext) -> NormalizeOutcome:
    """Spot candles: a bare JSON array, **descending**, ms open times, base volume."""
    try:
        rows = require_list(parse_json(raw, ctx), ctx, what="spot candles")
        bars = _bars_from_rows(rows, ctx, source="spot_candles")
    except CryptoTraderError as e:
        # A classified rejection is an outcome, not a crash. Anything unclassified propagates:
        # an unexpected exception here is a bug in this normalizer and must not be absorbed.
        return Rejected(error=e, raw_ref=ctx.raw_ref)
    return Accepted(records=bars, raw_ref=ctx.raw_ref)


def normalize_futures(raw: bytes, ctx: PayloadContext) -> NormalizeOutcome:
    """Futures candlesticks: ``{"s": "ok"|"no_data", "data": [...]}``, ascending, ms ``time``."""
    try:
        obj = require_mapping(parse_json(raw, ctx), ctx, what="futures candlesticks")
        status = obj.get("s")
        if status == "no_data":
            return Accepted(records=(), raw_ref=ctx.raw_ref, warnings=("s=no_data: empty window",))
        if status != "ok":
            raise SchemaError(
                f"futures candlesticks returned s={status!r}",
                component=ctx.component, operation="normalize",
                context={"s": str(status), "url": ctx.request_url, "keys": sorted(obj)},
                likely_cause="an invalid resolution or an out-of-range window",
                remediation="check the resolution against the verified served set (F-008)",
            )
        rows = require_list(obj.get("data"), ctx, what="futures candlesticks data")
        bars = _bars_from_rows(rows, ctx, source="futures_candles")
    except CryptoTraderError as e:
        return Rejected(error=e, raw_ref=ctx.raw_ref)
    return Accepted(records=bars, raw_ref=ctx.raw_ref)


def assert_ascending(bars: tuple[Bar, ...]) -> None:
    """The hazard #3 guard, callable from anywhere bars are consumed."""
    times = [b.open_time_ms for b in bars]
    if times != sorted(times):
        raise SchemaError(
            "bars are not ascending after normalization",
            component="normalize.candles", operation="assert_ascending",
            context={"first": times[:3], "last": times[-3:]},
            likely_cause="a caller re-sorted or concatenated pages without re-sorting",
            remediation="normalization is the only place ordering is established (specs/10 §5)",
        )


def timeframe_for(product: Product, api_resolution: str) -> Timeframe:
    """Map an API resolution string to the canonical timeframe (F-008 for the futures set)."""
    spot = {"1m": Timeframe.M1, "15m": Timeframe.M15, "1h": Timeframe.H1, "1d": Timeframe.D1}
    futures = {"1": Timeframe.M1, "5": Timeframe.M5, "15": Timeframe.M15, "30": Timeframe.M30,
               "60": Timeframe.H1, "240": Timeframe.H4, "1D": Timeframe.D1}
    table = spot if product is Product.SPOT else futures
    if api_resolution not in table:
        raise SchemaError(
            f"{product.value} does not serve resolution {api_resolution!r}",
            component="normalize.candles", operation="timeframe_for",
            context={"product": product.value, "resolution": api_resolution, "served": sorted(table)},
            likely_cause="spot serves only 1m/15m/1h/1d (422 otherwise); futures serve the F-008 set",
            remediation="use a served resolution, or aggregate from a finer one (exact per F-013)",
        )
    return table[api_resolution]
