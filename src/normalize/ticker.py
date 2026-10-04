"""Spot ticker normalizer — ``/exchange/ticker`` (hazard #1, #4).

One request returns **every** market (997 rows observed), so the normalizer filters to the markets
we track. ``timestamp`` is **seconds** (F-005) and is converted here, once. ``volume`` is **quote**
denominated (F-027) and is named to say so.
"""
from __future__ import annotations

from core.errors import CryptoTraderError, SchemaError
from normalize.base import (
    PayloadContext,
    as_int,
    dec,
    dec_opt,
    parse_json,
    require_fields,
    require_list,
)
from normalize.canonical import SpotTicker
from normalize.errors import Accepted, NormalizeOutcome, Partial, Rejected, RejectedRow

REQUIRED = ("market", "last_price", "timestamp")


def normalize(raw: bytes, ctx: PayloadContext, *, markets: dict[str, str]) -> NormalizeOutcome:
    """``markets`` maps the ticker's ``market`` name (``BTCUSDT``) to our asset id (``B-BTC_USDT``)."""
    try:
        rows = require_list(parse_json(raw, ctx), ctx, what="ticker")
    except CryptoTraderError as e:
        return Rejected(error=e, raw_ref=ctx.raw_ref)

    accepted: list[SpotTicker] = []
    rejected: list[RejectedRow] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        market = row.get("market")
        if market not in markets:
            continue
        seen.add(str(market))
        try:
            require_fields(row, REQUIRED, ctx, where=f"ticker[{market}]")
            ts_s = as_int(row, "timestamp", ctx)
            accepted.append(SpotTicker(
                asset=markets[str(market)],
                ts_ms=ts_s * 1000,                       # seconds -> canonical ms, exactly once
                last_price=dec(row, "last_price", ctx),
                bid=dec_opt(row, "bid", ctx), ask=dec_opt(row, "ask", ctx),
                high_24h=dec_opt(row, "high", ctx), low_24h=dec_opt(row, "low", ctx),
                volume_quote_24h=dec_opt(row, "volume", ctx),
                change_pct_24h=dec_opt(row, "change_24_hour", ctx),
                received_at_ms=ctx.received_at_ms,
            ))
        except CryptoTraderError as e:
            rejected.append(RejectedRow(str(market), e.code, e.message, e.context))
        except ValueError as e:
            rejected.append(RejectedRow(str(market), "VALIDATION", str(e)))

    for missing in sorted(set(markets) - seen):
        rejected.append(RejectedRow(
            missing, "MARKET_ABSENT",
            "the tracked market was not present in the ticker payload",
            {"tracked_markets": sorted(markets)},
        ))

    if not accepted and rejected:
        return Rejected(
            error=SchemaError(
                f"no tracked market could be normalized from {len(rows)} ticker rows",
                component=ctx.component, operation="normalize",
                context={"tracked": sorted(markets), "rejected": [r.reason for r in rejected][:5]},
                likely_cause="the market naming changed, or the payload is an error object",
                remediation="check /exchange/v1/markets_details for the current coindcx_name values",
            ),
            raw_ref=ctx.raw_ref,
        )
    if rejected:
        return Partial(records=tuple(accepted), rejected=tuple(rejected), raw_ref=ctx.raw_ref)
    return Accepted(records=tuple(accepted), raw_ref=ctx.raw_ref)
