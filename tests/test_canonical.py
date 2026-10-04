"""Canonical types (PHASE-02). Hand-computed values; failure paths first.

The most important test in this file is the deliberate construction of the 100x fee error
(specs/10 criterion 4) — the bug that would invalidate every validation result while every test
passed and every gate looked healthy.
"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.errors import SchemaError, UnitError
from normalize.canonical import (
    Bar,
    BookSnapshot,
    Fee,
    FuturesRT,
    InstrumentMeta,
    MarginTier,
    Positioning,
    Product,
    Timeframe,
    Trade,
    validate_ms,
)
from normalize.errors import Accepted, Partial, Rejected, RejectedRow

H1 = Timeframe.H1
T0 = 1789725600000          # 2026-09-18T10:00:00Z, aligned to the hour


def bar(**kw):
    base = {"asset": "B-BTC_USDT", "product": Product.FUTURES, "timeframe": H1, "open_time_ms": T0,
            "open": "100", "high": "110", "low": "90", "close": "105", "volume_base": "12.5"}
    return Bar(**{**base, **kw})


# ---- Fee: the 100x error ------------------------------------------------------------------

def test_the_api_percent_becomes_a_fraction_exactly_once():
    # Verified live: BTC taker_fee = 0.059 meaning 0.059 percent (F-005 / specs/00 §2).
    f = Fee.from_api_percent("0.059")
    assert f.fraction == Decimal("0.00059")          # hand-computed: 0.059 / 100
    assert f.percent == Decimal("0.059")
    # round-trip cost on a 10,000 USDT notional, taker both sides: 2 * 5.9 = 11.8 bps
    assert f.on(Decimal(10_000)) == Decimal("5.90000")


def test_fee_cannot_be_constructed_from_a_bare_number():
    with pytest.raises(TypeError, match="from_api_percent"):
        Fee(Decimal("0.059"))                        # ambiguous unit -> refused


def test_treating_an_api_percent_as_a_fraction_is_caught_as_a_unit_error():
    """THE 100x error, constructed deliberately."""
    with pytest.raises(UnitError) as ei:
        Fee.from_fraction("0.059")                   # 5.9% — implausible; the conversion was skipped
    err = ei.value
    assert err.code == "NORMALIZE_UNIT"
    assert err.problem_class.value == "application_bug"
    assert "100x" in err.likely_cause
    assert "from_api_percent" in err.remediation


def test_fee_rejects_negative_and_accepts_a_plausible_fraction():
    with pytest.raises(UnitError, match="negative"):
        Fee.from_fraction("-0.0001")
    assert Fee.from_fraction("0.00059").fraction == Decimal("0.00059")


def test_fee_equality_and_hashing_are_by_value():
    assert Fee.from_api_percent("0.059") == Fee.from_fraction("0.00059")
    assert len({Fee.from_api_percent("0.059"), Fee.from_fraction("0.00059")}) == 1


def test_fee_rejects_a_non_numeric_value_with_a_named_field():
    with pytest.raises(SchemaError) as ei:
        Fee.from_api_percent(None)
    assert ei.value.context["field"] == "fee_percent"


# ---- timestamps: hazard #1 ----------------------------------------------------------------

def test_seconds_passed_as_milliseconds_is_rejected_with_the_cause_named():
    with pytest.raises(SchemaError) as ei:
        validate_ms(1789725600, field="open_time_ms", component="t", operation="t")   # 10 digits
    assert "SECONDS" in ei.value.message
    assert "hazard #1" in ei.value.likely_cause


def test_a_timestamp_far_in_the_future_is_rejected():
    with pytest.raises(SchemaError, match="future"):
        validate_ms(9_999_999_999_999, field="ts", component="t", operation="t", now_ms=T0)


def test_historical_timestamps_are_accepted_because_backfill_reaches_2017():
    assert validate_ms(1502928000000, field="ts", component="t", operation="t") == 1502928000000


# ---- Bar ----------------------------------------------------------------------------------

def test_bar_rejects_float_prices_so_decimal_is_structural():
    with pytest.raises(ValidationError, match="float is not permitted"):
        bar(close=105.0)


def test_bar_rejects_inconsistent_ohlc_before_it_can_reach_garman_klass():
    with pytest.raises(ValidationError, match="inconsistent OHLC"):
        bar(high="104", close="105")                 # close above high
    with pytest.raises(ValidationError, match="inconsistent OHLC"):
        bar(low="101")                               # low above open
    with pytest.raises(ValidationError, match="negative volume"):
        bar(volume_base="-1")


def test_hourly_bar_must_be_utc_aligned_as_verified_by_p11():
    with pytest.raises(ValidationError, match="not aligned"):
        bar(open_time_ms=T0 + 1234)
    # 1m is NOT alignment-checked: spot BTC 1m has a real misaligned era (F-018)
    Bar(asset="a", product=Product.SPOT, timeframe=Timeframe.M1, open_time_ms=T0 + 20_000,
        open="1", high="1", low="1", close="1", volume_base="0")


def test_bar_is_immutable_and_derives_close_time():
    b = bar()
    assert b.close_time_ms == T0 + 3_600_000
    assert b.typical_price == (Decimal(110) + Decimal(90) + Decimal(105)) / 3
    with pytest.raises(ValidationError):
        b.close = Decimal(1)                         # frozen


def test_unknown_fields_are_a_contract_change_and_are_rejected():
    with pytest.raises(ValidationError, match="extra"):
        bar(unexpected_field=1)


# ---- BookSnapshot: hazards #5, #6 and F-014 ----------------------------------------------

def book(**kw):
    base = {"asset": "B-BTC_USDT", "product": Product.SPOT, "ts_ms": T0,
            "bids": ((Decimal("99.9"), Decimal("2")), (Decimal("99.8"), Decimal("1"))),
            "asks": ((Decimal("100.1"), Decimal("3")), (Decimal("100.3"), Decimal("1")))}
    return BookSnapshot(**{**base, **kw})


def test_book_metrics_are_hand_computable():
    b = book()
    assert b.mid == Decimal("100.0")
    assert b.spread_bps == pytest.approx(Decimal("20.0"))     # 0.2 / 100 * 10000


def test_empty_book_side_is_invalid_not_zero_liquidity():
    with pytest.raises(ValidationError, match="empty book side"):
        book(asks=())


def test_book_rejects_unsorted_sides_zero_levels_and_crossed_quotes():
    with pytest.raises(ValidationError, match="descending"):
        book(bids=((Decimal("99.8"), Decimal("1")), (Decimal("99.9"), Decimal("2"))))
    with pytest.raises(ValidationError, match="ascending"):
        book(asks=((Decimal("100.3"), Decimal("1")), (Decimal("100.1"), Decimal("3"))))
    with pytest.raises(ValidationError, match="zero or negative quantity"):
        book(bids=((Decimal("99.9"), Decimal("0")),))
    with pytest.raises(ValidationError, match="crossed book"):
        book(bids=((Decimal("100.5"), Decimal("1")),))


# ---- FuturesRT: invariants 2, 3, 4 -------------------------------------------------------

def test_futures_rt_freshness_comes_from_the_row_not_the_envelope():
    row = FuturesRT(asset="B-BTC_USDT", ts_ms=T0 + 60_000, funding_rate="0.00007903",
                    predicted_funding_rate="0.0001", mark_price="78083.9", last_price="78083.9",
                    skw=40, tpe_tick_send_ms=T0, tpe_mark_send_ms=T0 - 500)
    assert row.source_ts_ms == T0                     # the row's own tick time, not ts_ms
    assert row.age_ms(T0 + 5_000) == 5_000


def test_skw_is_carried_but_never_used_as_a_health_flag():
    # F-004: every fresh row shares one skw value; rejecting on it would discard live data.
    row = FuturesRT(asset="a", ts_ms=T0, funding_rate="0", predicted_funding_rate="0",
                    mark_price="1", last_price="1", skw=40, tpe_tick_send_ms=T0)
    assert row.skw == 40 and row.age_ms(T0) == 0


def test_futures_rt_requires_the_fields_a_schema_incomplete_row_lacks():
    with pytest.raises(ValidationError):
        FuturesRT(asset="B-NKN_USDT", ts_ms=T0, funding_rate="0.003", predicted_funding_rate="0",
                  mark_price="0.009")                 # no last_price: B-NKN_USDT's real shape


# ---- Positioning, Trade, InstrumentMeta -------------------------------------------------

def test_positioning_divergence_and_percentage_validation():
    p = Positioning(asset="a", ts_ms=T0, long_count_pct="54.92", short_count_pct="45.08",
                    long_value_pct="31.65", short_value_pct="68.35")
    assert p.divergence == Decimal("23.27")           # 54.92 - 31.65, hand-computed
    with pytest.raises(ValidationError, match="sum to"):
        Positioning(asset="a", ts_ms=T0, long_count_pct="54", short_count_pct="30",
                    long_value_pct="50", short_value_pct="50")


def test_trade_carries_the_canonical_aggressor_flag():
    t = Trade(asset="a", product=Product.SPOT, ts_ms=T0, price="100", qty="1", is_buyer_maker=True)
    assert t.is_buyer_maker is True


def test_instrument_margin_tier_lookup_is_never_hardcoded():
    inst = InstrumentMeta(
        asset="B-BTC_USDT", product=Product.FUTURES, tick="0.1", qty_increment="0.001",
        min_notional="60", maker_fee=Fee.from_api_percent("0.0236"), taker_fee=Fee.from_api_percent("0.059"),
        margin_tiers=(MarginTier(max_notional=Decimal(20_000), maintenance_margin_rate=Decimal("0.005")),
                      MarginTier(max_notional=Decimal(100_000), maintenance_margin_rate=Decimal("0.01"))))
    assert inst.maintenance_margin_for(Decimal(5_000)) == Decimal("0.005")
    assert inst.maintenance_margin_for(Decimal(50_000)) == Decimal("0.01")
    assert inst.maintenance_margin_for(Decimal(10_000_000)) == Decimal("0.01")   # beyond the table: last tier


def test_missing_margin_tiers_blocks_the_signal_rather_than_guessing():
    inst = InstrumentMeta(asset="a", product=Product.FUTURES, tick="0.1", qty_increment="0.001",
                          min_notional="60", maker_fee=Fee.from_api_percent("0"), taker_fee=Fee.from_api_percent("0"))
    with pytest.raises(UnitError) as ei:
        inst.maintenance_margin_for(Decimal(1000))
    assert "DQ_META" in ei.value.remediation


def test_instrument_rejects_non_positive_increments():
    with pytest.raises(ValidationError):
        InstrumentMeta(asset="a", product=Product.FUTURES, tick="0", qty_increment="0.001",
                       min_notional="60", maker_fee=Fee.from_api_percent("0"), taker_fee=Fee.from_api_percent("0"))


# ---- outcomes ---------------------------------------------------------------------------

def test_outcome_types_distinguish_total_and_partial_rejection():
    acc = Accepted(records=(1, 2))
    part = Partial(records=(1,), rejected=(RejectedRow("B-NKN_USDT", "SCHEMA_INCOMPLETE"),))
    rej = Rejected(error=SchemaError("bad", component="c", operation="o"))
    assert (acc.ok, len(acc)) == (True, 2)
    assert (part.ok, len(part), part.rejected[0].reason) == (True, 1, "SCHEMA_INCOMPLETE")
    assert (rej.ok, len(rej), rej.reason) == (False, 0, "NORMALIZE_SCHEMA")
