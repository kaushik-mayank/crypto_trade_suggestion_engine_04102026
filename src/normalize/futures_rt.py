"""``current_prices/futures/rt`` normalizer — row-level validation (specs/11 §5.1, invariants 2-4).

This payload is the one the project has verified to be hostile:

* a **fresh envelope ``ts``** sits above rows that can be **months stale** (invariant 2), so the
  envelope timestamp is carried for provenance and **never** used for freshness;
* rows are **not schema-uniform** — ``B-NKN_USDT`` really returns only
  ``{fr, mp, efr, bmST, cmRT, skw}`` (invariant 4), so every field is checked for presence;
* **``skw`` is not a health flag** (F-004, invariant 3). Every fresh row in a snapshot carries the
  same value and it drifts over time. `specs/11` §5.1 step 1 says to reject on ``skw != 0``; that
  text predates F-004 and is **deliberately not implemented** — doing so would discard every live
  row. The value is recorded so its behaviour stays observable.

Rejection reasons are stable strings so they can be counted in ``dq_events``:
``SCHEMA_INCOMPLETE``, ``STALE_ROW``, ``NO_ROW_TIMESTAMP``, ``PAIR_ABSENT``, ``VALIDATION``.
"""
from __future__ import annotations

from core.errors import CryptoTraderError, SchemaError
from normalize.base import PayloadContext, as_int, dec, dec_opt, parse_json, require_fields, require_mapping
from normalize.canonical import FuturesRT
from normalize.errors import Accepted, NormalizeOutcome, Partial, Rejected, RejectedRow

#: Without these a row cannot support any Tier B feature, so it is rejected rather than defaulted.
REQUIRED = ("fr", "efr", "mp", "ls")


def normalize(
    raw: bytes,
    ctx: PayloadContext,
    *,
    assets: tuple[str, ...],
    max_age_ms: int,
) -> NormalizeOutcome:
    """Normalize the rows for ``assets`` only (the payload holds ~540 pairs).

    ``max_age_ms`` is the freshness window, judged against the row's own ``btST``/``bmST``.
    """
    try:
        obj = require_mapping(parse_json(raw, ctx), ctx, what="futures rt")
        prices = require_mapping(obj.get("prices", {}), ctx, what="futures rt prices")
        envelope_ts = as_int(obj, "ts", ctx)
    except CryptoTraderError as e:
        return Rejected(error=e, raw_ref=ctx.raw_ref)

    accepted: list[FuturesRT] = []
    rejected: list[RejectedRow] = []
    for asset in assets:
        row = prices.get(asset)
        if not isinstance(row, dict):
            rejected.append(RejectedRow(asset, "PAIR_ABSENT", "pair not present in the prices object",
                                        {"pairs_in_payload": len(prices)}))
            continue
        try:
            require_fields(row, REQUIRED, ctx, where=f"row[{asset}]")
        except CryptoTraderError as e:
            rejected.append(RejectedRow(asset, "SCHEMA_INCOMPLETE", e.message,
                                        {"present": sorted(row), "missing": e.context.get("missing")}))
            continue

        tick_ms = as_int(row, "btST", ctx) if "btST" in row else None
        mark_ms = as_int(row, "bmST", ctx) if "bmST" in row else None
        source_ts = tick_ms if tick_ms is not None else mark_ms
        if source_ts is None:
            rejected.append(RejectedRow(asset, "NO_ROW_TIMESTAMP",
                                        "row carries neither btST nor bmST, so its age is unknowable",
                                        {"present": sorted(row)}))
            continue
        age_ms = ctx.received_at_ms - source_ts
        if age_ms > max_age_ms:
            rejected.append(RejectedRow(
                asset, "STALE_ROW",
                f"row is {age_ms / 1000:.1f}s old, beyond the {max_age_ms / 1000:.0f}s freshness window",
                {"age_ms": age_ms, "row_ts_ms": source_ts, "envelope_ts_ms": envelope_ts,
                 "note": "the envelope ts is fresh; the row is not (invariant 2)"},
            ))
            continue

        try:
            accepted.append(FuturesRT(
                asset=asset, ts_ms=envelope_ts,
                funding_rate=dec(row, "fr", ctx), predicted_funding_rate=dec(row, "efr", ctx),
                mark_price=dec(row, "mp", ctx), last_price=dec(row, "ls", ctx),
                high_24h=dec_opt(row, "h", ctx), low_24h=dec_opt(row, "l", ctx),
                volume_quote_24h=dec_opt(row, "v", ctx),          # quote-denominated (hazard #4)
                price_change_pct=dec_opt(row, "pc", ctx),
                skw=int(row["skw"]) if isinstance(row.get("skw"), int) else None,
                tpe_tick_send_ms=tick_ms, tpe_mark_send_ms=mark_ms,
                received_at_ms=ctx.received_at_ms,
            ))
        except (ValueError, CryptoTraderError) as e:
            rejected.append(RejectedRow(asset, "VALIDATION", str(e)))

    if not accepted:
        return Rejected(
            error=SchemaError(
                f"no tracked pair produced an acceptable row ({len(rejected)} rejected)",
                component=ctx.component, operation="normalize",
                context={"reasons": sorted({r.reason for r in rejected}), "assets": list(assets)},
                likely_cause="the whole feed is stale or the payload shape changed",
                remediation="inspect the raw payload; if rows are stale, this is an EXTERNAL problem "
                            "and the system should go quiet rather than emit",
            ),
            raw_ref=ctx.raw_ref,
        )
    if rejected:
        return Partial(records=tuple(accepted), rejected=tuple(rejected), raw_ref=ctx.raw_ref)
    return Accepted(records=tuple(accepted), raw_ref=ctx.raw_ref)
