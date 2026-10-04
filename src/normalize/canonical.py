"""Canonical internal representation (specs/10 §5, specs/11 §4.3).

Declared once, enforced by types at the normalization boundary. Every hazard in
`specs/00` §5 has its canonical answer here:

| Quantity | Canonical form |
|---|---|
| Timestamps | UTC **milliseconds**, ``int`` |
| Bar arrays | **ascending** by open time |
| Prices, quantities | ``Decimal`` — **never** ``float`` |
| Volume | **base**-denominated |
| Fees | ``Fee`` — a fraction internally, converted from the API's percent exactly once |
| Aggressor flag | ``bool`` ``is_buyer_maker`` |

Two structural defences worth naming:

* **``Fee`` cannot be built by accident.** The API returns ``taker_fee: 0.059`` meaning *percent*.
  Treating it as a fraction understates costs 100×, every gate passes, and no test fails
  (specs/10 §5). ``Fee`` has no public constructor that takes a bare number: you must say
  ``from_api_percent`` or ``from_fraction``, and any fee ≥ 1% is rejected as a unit error.
* **Price fields reject ``float``.** Passing a float would silently introduce binary rounding into
  a value that becomes a published stop. The model raises instead (invariant 5).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator, model_validator

from core.errors import SchemaError, UnitError

#: Epoch bounds for a plausible millisecond timestamp. The lower bound catches a seconds value
#: passed as milliseconds (10 digits) — hazard #1, which silently moves a bar 50 years.
MIN_PLAUSIBLE_MS = 1_262_304_000_000          # 2010-01-01
MAX_FUTURE_SKEW_MS = 86_400_000               # one day ahead: clock skew, never real data

#: Any fee at or above this fraction means the percent→fraction conversion was skipped.
FEE_SANITY_LIMIT = Decimal("0.01")


class Product(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    H8 = "8h"
    D1 = "1d"

    @property
    def ms(self) -> int:
        return {
            "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
            "1h": 3_600_000, "4h": 14_400_000, "8h": 28_800_000, "1d": 86_400_000,
        }[self.value]


def _no_float(v: Any) -> Any:
    """Reject ``float`` for any price/quantity. str, int and Decimal are accepted."""
    if isinstance(v, float):
        raise ValueError(
            "float is not permitted for a price or quantity: pass str/int/Decimal. "
            "Binary floats cannot represent exchange tick sizes exactly (invariant 5)"
        )
    return v


Money = Annotated[Decimal, BeforeValidator(_no_float)]


def to_decimal(value: Any, *, field: str, component: str, operation: str) -> Decimal:
    """Parse a Decimal or raise a :class:`SchemaError` naming the field and the offending value."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise SchemaError(
            f"{field} is not a number: {value!r}",
            component=component, operation=operation,
            context={"field": field, "value": repr(value)[:80]},
            likely_cause="the endpoint changed a field's type, or a null reached a numeric field",
            remediation="inspect the captured raw payload; add it as a fixture and update the normalizer",
            cause=e,
        ) from e


class Fee:
    """A fee as an internal **fraction** (0.00059), constructed explicitly from a stated unit.

    ``Fee(0.059)`` is impossible on purpose. The only ways in are
    :meth:`from_api_percent` (what CoinDCX returns) and :meth:`from_fraction`.
    """

    __slots__ = ("_fraction",)

    def __init__(self, fraction: Decimal, *, _internal: bool = False) -> None:
        if not _internal:
            raise TypeError(
                "construct a Fee with Fee.from_api_percent(...) or Fee.from_fraction(...); "
                "a bare number is ambiguous and that ambiguity is a 100x cost error (specs/10 §5)"
            )
        self._fraction = fraction

    @classmethod
    def from_api_percent(cls, value: Any, *, component: str = "normalize", operation: str = "fee") -> Fee:
        """CoinDCX returns percent: ``0.059`` means 0.059%. Divide by 100, exactly once."""
        pct = to_decimal(value, field="fee_percent", component=component, operation=operation)
        return cls._checked(pct / Decimal(100), component=component, operation=operation, source_percent=pct)

    @classmethod
    def from_fraction(cls, value: Any, *, component: str = "normalize", operation: str = "fee") -> Fee:
        frac = to_decimal(value, field="fee_fraction", component=component, operation=operation)
        return cls._checked(frac, component=component, operation=operation, source_percent=None)

    @classmethod
    def _checked(cls, fraction: Decimal, *, component: str, operation: str, source_percent: Decimal | None) -> Fee:
        if fraction < 0:
            raise UnitError(
                f"negative fee fraction {fraction}",
                component=component, operation=operation, context={"fraction": str(fraction)},
                likely_cause="a rebate was parsed as a fee, or the field changed meaning",
                remediation="check the instrument payload; negative maker rebates need an explicit decision",
            )
        if fraction >= FEE_SANITY_LIMIT:
            raise UnitError(
                f"fee fraction {fraction} is >= {FEE_SANITY_LIMIT} (1%), which is implausible for this venue",
                component=component, operation=operation,
                context={"fraction": str(fraction), "source_percent": str(source_percent)},
                likely_cause="an API percent value (e.g. 0.059 meaning 0.059%) was treated as a fraction — "
                             "the 100x cost understatement of specs/10 §5",
                remediation="use Fee.from_api_percent() for values taken from the instrument endpoint",
            )
        return cls(fraction, _internal=True)

    @property
    def fraction(self) -> Decimal:
        """The canonical form: multiply a notional by this."""
        return self._fraction

    @property
    def percent(self) -> Decimal:
        """For display only. Never feed this to the cost model."""
        return self._fraction * Decimal(100)

    def on(self, notional: Decimal) -> Decimal:
        """Cost of one side on ``notional``."""
        return notional * self._fraction

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Fee) and other._fraction == self._fraction

    def __hash__(self) -> int:
        return hash(("Fee", self._fraction))

    def __repr__(self) -> str:
        return f"Fee(fraction={self._fraction}, percent={self.percent})"


def validate_ms(value: int, *, field: str, component: str, operation: str, now_ms: int | None = None) -> int:
    """Reject an implausible millisecond timestamp (hazard #1)."""
    import time
    now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"{field} is not an int: {value!r}", component=component, operation=operation,
                          context={"field": field, "value": repr(value)[:60]})
    if value < MIN_PLAUSIBLE_MS:
        raise SchemaError(
            f"{field}={value} is before 2010 — it looks like SECONDS passed as milliseconds",
            component=component, operation=operation, context={"field": field, "value": value},
            likely_cause="futures candlesticks use seconds for from/to (hazard #1); multiply by 1000",
            remediation="convert at the normalizer boundary, not at the call site",
        )
    if value > now + MAX_FUTURE_SKEW_MS:
        raise SchemaError(
            f"{field}={value} is more than a day in the future",
            component=component, operation=operation, context={"field": field, "value": value, "now_ms": now},
            likely_cause="a local clock jump, or a microsecond timestamp passed as milliseconds",
            remediation="check NTP/chrony on the host; verify the field's unit",
        )
    return value


class CanonicalModel(BaseModel):
    """Immutable, strict: unknown fields are a contract change and must be noticed, not absorbed."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)


class Bar(CanonicalModel):
    asset: str
    product: Product
    timeframe: Timeframe
    open_time_ms: int
    open: Money
    high: Money
    low: Money
    close: Money
    volume_base: Money
    is_final: bool = False
    source: str = ""
    fetched_at_ms: int = 0
    synthesised: bool = False          # a confirmed-absent 1m bar (specs/12 §6.2)

    @field_validator("open_time_ms")
    @classmethod
    def _plausible(cls, v: int) -> int:
        return validate_ms(v, field="open_time_ms", component="normalize.bar", operation="validate")

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Bar:
        """Invalid OHLC must be rejected here. If it reaches the feature layer, Garman-Klass
        produces a negative variance, which specs/12 §4.1 treats as a data-integrity alarm."""
        top, bottom = max(self.open, self.close), min(self.open, self.close)
        if self.high < top or self.low > bottom or self.high < self.low:
            raise ValueError(
                f"inconsistent OHLC: O={self.open} H={self.high} L={self.low} C={self.close} "
                "(requires L <= min(O,C) <= max(O,C) <= H)"
            )
        if self.volume_base < 0:
            raise ValueError(f"negative volume {self.volume_base}")
        if self.open_time_ms % self.timeframe.ms and self.timeframe in (Timeframe.H1, Timeframe.D1):
            # 1h/1d are verified UTC-aligned (F-012). Spot BTC 1m has a real misaligned era (F-018),
            # so alignment is asserted only where it was verified.
            raise ValueError(f"{self.timeframe.value} bar open {self.open_time_ms} is not aligned to a UTC boundary")
        return self

    @property
    def close_time_ms(self) -> int:
        return self.open_time_ms + self.timeframe.ms

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / 3


class Trade(CanonicalModel):
    asset: str
    product: Product
    ts_ms: int
    price: Money
    qty: Money
    is_buyer_maker: bool
    source: str = ""


class BookSnapshot(CanonicalModel):
    """Levels are **numerically sorted lists**, never the JSON object the API returns (hazard #5)."""

    asset: str
    product: Product
    ts_ms: int
    bids: tuple[tuple[Decimal, Decimal], ...]     # descending by price
    asks: tuple[tuple[Decimal, Decimal], ...]     # ascending by price
    version: int | None = None
    fetched_at_ms: int = 0

    @model_validator(mode="after")
    def _ordered_and_sane(self) -> BookSnapshot:
        if not self.bids or not self.asks:
            raise ValueError("empty book side: futures depth 5/100 returns 200 with an empty book (F-014); "
                             "an empty book is invalid, not 'no liquidity'")
        if any(a[0] <= b[0] for a, b in zip(self.bids, self.bids[1:])):
            raise ValueError("bids are not strictly descending")
        if any(a[0] >= b[0] for a, b in zip(self.asks, self.asks[1:])):
            raise ValueError("asks are not strictly ascending")
        if any(q <= 0 for _, q in self.bids + self.asks):
            raise ValueError("zero or negative quantity level (hazard #6: '0' means delete)")
        if self.bids[0][0] >= self.asks[0][0]:
            raise ValueError(f"crossed book: best bid {self.bids[0][0]} >= best ask {self.asks[0][0]}")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.asks[0][0] - self.bids[0][0]) / self.mid * Decimal(10_000)


class FuturesRT(CanonicalModel):
    """One accepted row of ``current_prices/futures/rt``.

    ``skw`` is carried but is **not** a health flag (F-004): every fresh row in a snapshot shares
    one value and it drifts. Freshness comes from ``tpe_tick_send_ms`` / ``tpe_mark_send_ms``.
    """

    asset: str
    ts_ms: int                        # envelope ts — provenance only, NEVER freshness (invariant 2)
    funding_rate: Money
    predicted_funding_rate: Money
    mark_price: Money
    last_price: Money
    high_24h: Money | None = None
    low_24h: Money | None = None
    volume_quote_24h: Money | None = None
    price_change_pct: Money | None = None
    skw: int | None = None
    tpe_tick_send_ms: int | None = None
    tpe_mark_send_ms: int | None = None
    received_at_ms: int = 0

    @property
    def source_ts_ms(self) -> int | None:
        """The row's own time, for freshness. Prefers the tick send time."""
        return self.tpe_tick_send_ms if self.tpe_tick_send_ms is not None else self.tpe_mark_send_ms

    def age_ms(self, now_ms: int) -> int | None:
        ts = self.source_ts_ms
        return None if ts is None else now_ms - ts


class SpotTicker(CanonicalModel):
    """One row of ``/exchange/ticker``.

    ``timestamp`` arrives in **seconds** with 1-second resolution (F-005) and is converted to ms
    here, once. ``volume`` is **quote**-denominated (F-027), so it is named to say so — comparing it
    with a candle's base volume without conversion is hazard #4.
    """

    asset: str
    ts_ms: int
    last_price: Money
    bid: Money | None = None
    ask: Money | None = None
    high_24h: Money | None = None
    low_24h: Money | None = None
    volume_quote_24h: Money | None = None
    change_pct_24h: Money | None = None
    received_at_ms: int = 0

    @model_validator(mode="after")
    def _quotes_ordered(self) -> SpotTicker:
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(f"crossed ticker quote: bid {self.bid} > ask {self.ask}")
        return self


class Positioning(CanonicalModel):
    asset: str
    ts_ms: int
    long_count_pct: Money
    short_count_pct: Money
    long_value_pct: Money
    short_value_pct: Money

    @model_validator(mode="after")
    def _percentages_sum(self) -> Positioning:
        for a, b, label in ((self.long_count_pct, self.short_count_pct, "count"),
                            (self.long_value_pct, self.short_value_pct, "value")):
            if abs((a + b) - Decimal(100)) > Decimal("0.05"):
                raise ValueError(f"{label} percentages sum to {a + b}, not 100")
        return self

    @property
    def divergence(self) -> Decimal:
        """``POS_DIVERGENCE``: count-weighted minus value-weighted long share."""
        return self.long_count_pct - self.long_value_pct


class MarginTier(CanonicalModel):
    max_notional: Decimal
    maintenance_margin_rate: Decimal


class InstrumentMeta(CanonicalModel):
    """Fetched live, never hardcoded (invariant 1). The doc's sample values are wrong for BTC."""

    asset: str
    product: Product
    tick: Money
    qty_increment: Money
    min_notional: Money
    maker_fee: Fee
    taker_fee: Fee
    multiplier_up: Money | None = None
    multiplier_down: Money | None = None
    funding_frequency_h: int | None = None
    max_leverage: Money | None = None
    margin_tiers: tuple[MarginTier, ...] = ()
    status: str = "unknown"
    fetched_at_ms: int = 0

    @field_validator("tick", "qty_increment", "min_notional")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError(f"must be positive, got {v}")
        return v

    def maintenance_margin_for(self, notional: Decimal) -> Decimal:
        """Rate from the tier matching this notional — never hardcoded (specs/08 §6)."""
        if not self.margin_tiers:
            raise UnitError(
                "no margin tiers available, so liquidation distance cannot be computed",
                component="normalize.instrument", operation="maintenance_margin_for",
                context={"asset": self.asset, "notional": str(notional)},
                likely_cause="instrument metadata was fetched without dynamic_safety_margin_details",
                remediation="refetch the instrument; a signal must not be published without it (DQ_META)",
            )
        for tier in self.margin_tiers:
            if notional <= tier.max_notional:
                return tier.maintenance_margin_rate
        return self.margin_tiers[-1].maintenance_margin_rate
