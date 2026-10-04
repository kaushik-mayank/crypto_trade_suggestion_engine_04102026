"""PHASE-03 — every `specs/00` §5 hazard, on **real captured payloads** (specs/11 acceptance 3).

Fixture provenance is in `tests/fixtures/README.md`. Synthetic bytes appear only where the point is
a *corruption* that the live API did not hand us (truncation, a renamed field, a stale timestamp).
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from normalize import book, candles, futures_rt, instrument, positioning, ticker, trades
from normalize.base import PayloadContext
from normalize.canonical import Product, Timeframe
from normalize.errors import Accepted, Partial, Rejected

FIX = Path(__file__).parent / "fixtures"
RECV = 1789735100000            # just after the 2026-09-18T12:37Z fixture capture
MARKETS = {"BTCUSDT": "B-BTC_USDT", "ETHUSDT": "B-ETH_USDT", "SOLUSDT": "B-SOL_USDT"}


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def ctx(endpoint: str, **kw) -> PayloadContext:
    base = {"endpoint": endpoint, "received_at_ms": RECV, "sent_at_ms": RECV - 80,
            "request_url": f"https://example/{endpoint}", "raw_ref": f"fixtures/{endpoint}"}
    return PayloadContext(**{**base, **kw})


# ---- hazard #3: spot candles arrive DESCENDING ------------------------------------------

def test_hazard3_spot_candles_are_reversed_to_ascending_with_newest_last():
    body = raw("spot_candles_B-BTC_USDT_1h_2026-09-18T1237Z.json")
    source_times = [int(r["time"]) for r in json.loads(body)]
    assert source_times == sorted(source_times, reverse=True), "fixture must be the real descending payload"

    out = candles.normalize_spot(body, ctx("spot_candles", asset="B-BTC_USDT",
                                          product=Product.SPOT, timeframe=Timeframe.H1))
    assert isinstance(out, Accepted)
    times = [b.open_time_ms for b in out.records]
    assert times == sorted(times)                       # ascending
    assert times[-1] == max(source_times)               # newest last — inverting time is the killer bug
    candles.assert_ascending(out.records)


def test_hazard4_spot_candle_volume_is_base_and_untouched():
    body = raw("spot_candles_B-BTC_USDT_1h_2026-09-18T1237Z.json")
    newest = max(json.loads(body), key=lambda r: int(r["time"]))
    out = candles.normalize_spot(body, ctx("spot_candles", asset="B-BTC_USDT",
                                           product=Product.SPOT, timeframe=Timeframe.H1))
    bar = out.records[-1]
    assert bar.volume_base == Decimal(str(newest["volume"]))     # base, no conversion applied


# ---- hazard #1: futures uses seconds for from/to but ms for `time` ---------------------

def test_hazard1_futures_candle_times_are_already_ms_and_ascending():
    body = raw("futures_candles_B-BTC_USDT_60_2026-09-18T1237Z.json")
    out = candles.normalize_futures(body, ctx("futures_candles", asset="B-BTC_USDT",
                                              product=Product.FUTURES, timeframe=Timeframe.H1))
    assert isinstance(out, Accepted)
    times = [b.open_time_ms for b in out.records]
    assert times == sorted(times)
    assert all(t > 1_000_000_000_000 for t in times)              # 13-digit ms, not seconds
    assert all(t % Timeframe.H1.ms == 0 for t in times)           # UTC-aligned (F-012)


def test_futures_no_data_is_an_empty_result_not_an_error():
    out = candles.normalize_futures(b'{"s":"no_data","data":[]}',
                                    ctx("futures_candles", asset="a", product=Product.FUTURES,
                                        timeframe=Timeframe.H1))
    assert isinstance(out, Accepted) and len(out) == 0 and out.warnings


def test_futures_unexpected_status_is_rejected_with_the_resolution_named():
    out = candles.normalize_futures(b'{"s":"error","data":[]}',
                                    ctx("futures_candles", asset="a", product=Product.FUTURES,
                                        timeframe=Timeframe.H1))
    assert isinstance(out, Rejected)
    assert "s='error'" in out.error.message
    assert "F-008" in out.error.remediation


def test_a_seconds_timestamp_in_a_bar_is_rejected_not_silently_shifted():
    body = json.dumps([{"open": "1", "high": "1", "low": "1", "close": "1",
                        "volume": "1", "time": 1789725600}]).encode()          # seconds!
    out = candles.normalize_spot(body, ctx("spot_candles", asset="a", product=Product.SPOT,
                                          timeframe=Timeframe.H1))
    assert isinstance(out, Rejected)
    assert "SECONDS" in out.error.message or "SECONDS" in str(out.error)


def test_truncated_body_is_a_malformed_payload_naming_the_raw_reference():
    out = candles.normalize_spot(b'[{"open":1,', ctx("spot_candles", asset="a", product=Product.SPOT,
                                                     timeframe=Timeframe.H1))
    assert isinstance(out, Rejected)
    assert out.error.code == "EXTERNAL_MALFORMED_PAYLOAD"
    assert out.error.context["raw_ref"] == "fixtures/spot_candles"
    assert out.error.problem_class.value == "external_dependency"


def test_missing_context_is_an_application_error_not_a_guess():
    out = candles.normalize_spot(b"[]", ctx("spot_candles"))          # no asset/product/timeframe
    assert isinstance(out, Rejected)
    assert "needs asset, product and timeframe" in out.error.message


def test_duplicate_bar_times_in_one_page_are_rejected():
    row = {"open": "1", "high": "1", "low": "1", "close": "1", "volume": "1", "time": 1789725600000}
    out = candles.normalize_spot(json.dumps([row, row]).encode(),
                                 ctx("spot_candles", asset="a", product=Product.SPOT, timeframe=Timeframe.H1))
    assert isinstance(out, Rejected) and "duplicate bar open times" in out.error.message


def test_unserved_resolution_is_refused_with_the_served_set():
    with pytest.raises(Exception) as ei:
        candles.timeframe_for(Product.SPOT, "5m")            # spot 422s on 5m (F-008)
    assert "does not serve resolution" in str(ei.value)
    assert candles.timeframe_for(Product.FUTURES, "240") is Timeframe.H4     # native 4h (F-008)


# ---- ticker: seconds -> ms, quote volume, absent market -------------------------------

def test_ticker_timestamp_seconds_become_ms_and_volume_is_labelled_quote():
    body = raw("spot_ticker_2026-09-18T1237Z.json")
    btc_src = next(r for r in json.loads(body) if r["market"] == "BTCUSDT")
    out = ticker.normalize(body, ctx("spot_ticker"), markets=MARKETS)
    assert isinstance(out, Accepted) and len(out) == 3
    btc = next(r for r in out.records if r.asset == "B-BTC_USDT")
    assert btc.ts_ms == int(btc_src["timestamp"]) * 1000
    assert btc.last_price == Decimal(btc_src["last_price"])
    assert btc.volume_quote_24h == Decimal(btc_src["volume"])


def test_a_tracked_market_missing_from_the_ticker_is_reported_not_ignored():
    body = raw("spot_ticker_2026-09-18T1237Z.json")
    out = ticker.normalize(body, ctx("spot_ticker"), markets={**MARKETS, "NOPEUSDT": "B-NOPE_USDT"})
    assert isinstance(out, Partial)
    assert [r.reason for r in out.rejected] == ["MARKET_ABSENT"]
    assert len(out.records) == 3                       # the healthy markets still pass


def test_ticker_error_object_returned_with_http_200_is_rejected():
    out = ticker.normalize(b'{"code":422,"message":"bad"}', ctx("spot_ticker"), markets=MARKETS)
    assert isinstance(out, Rejected)
    assert "expected an array" in out.error.message


# ---- futures rt: invariants 2, 3, 4 ---------------------------------------------------

def test_futures_rt_accepts_fresh_rows_and_rejects_the_stale_incomplete_one():
    """The real payload: three healthy pairs plus B-NKN_USDT, which is both stale and incomplete."""
    body = raw("futures_rt_2026-09-18T1237Z.json")
    out = futures_rt.normalize(body, ctx("futures_rt"),
                               assets=("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-NKN_USDT"),
                               max_age_ms=60_000)
    assert isinstance(out, Partial)
    assert {r.asset for r in out.records} == {"B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"}
    nkn = next(r for r in out.rejected if r.identifier == "B-NKN_USDT")
    # B-NKN_USDT returns only {fr, mp, efr, bmST, cmRT, skw}: it fails on schema before staleness
    assert nkn.reason == "SCHEMA_INCOMPLETE"
    assert "ls" in nkn.context["missing"]


def test_a_fresh_envelope_does_not_rescue_a_stale_row():
    """Invariant 2, constructed: envelope ts is now, the row's btST is months old."""
    body = json.dumps({
        "ts": RECV, "vs": 1,
        "prices": {"B-OLD_USDT": {"fr": "0.0001", "efr": "0.0001", "mp": "1", "ls": "1",
                                  "btST": RECV - 40 * 86_400_000, "bmST": RECV - 40 * 86_400_000, "skw": 12}},
    }).encode()
    out = futures_rt.normalize(body, ctx("futures_rt"), assets=("B-OLD_USDT",), max_age_ms=60_000)
    assert isinstance(out, Rejected)                       # nothing acceptable remained
    assert "stale" in out.error.message.lower() or "no tracked pair" in out.error.message


def test_stale_row_rejection_records_the_age_and_the_envelope_for_contrast():
    body = json.dumps({
        "ts": RECV, "vs": 1,
        "prices": {
            "B-GOOD_USDT": {"fr": "0.0001", "efr": "0.0001", "mp": "1", "ls": "1", "btST": RECV - 500, "skw": 40},
            "B-OLD_USDT": {"fr": "0.0001", "efr": "0.0001", "mp": "1", "ls": "1", "btST": RECV - 600_000, "skw": 40},
        },
    }).encode()
    out = futures_rt.normalize(body, ctx("futures_rt"), assets=("B-GOOD_USDT", "B-OLD_USDT"), max_age_ms=60_000)
    assert isinstance(out, Partial)
    stale = out.rejected[0]
    assert stale.reason == "STALE_ROW" and stale.context["age_ms"] == 600_000
    assert stale.context["envelope_ts_ms"] == RECV        # proves the envelope was fresh


def test_skw_nonzero_is_carried_and_never_rejected(capsys):
    """F-004 / invariant 3: rejecting on skw would discard every live row."""
    body = raw("futures_rt_2026-09-18T1237Z.json")
    payload = json.loads(body)
    payload["prices"]["B-BTC_USDT"]["skw"] = 40          # the value seen on ALL fresh rows live
    payload["prices"]["B-BTC_USDT"]["btST"] = RECV - 500
    out = futures_rt.normalize(json.dumps(payload).encode(), ctx("futures_rt"),
                               assets=("B-BTC_USDT",), max_age_ms=60_000)
    assert isinstance(out, Accepted)
    assert out.records[0].skw == 40


def test_row_without_any_timestamp_is_rejected_because_its_age_is_unknowable():
    body = json.dumps({"ts": RECV, "prices": {"B-X_USDT": {"fr": "0", "efr": "0", "mp": "1", "ls": "1"}}}).encode()
    out = futures_rt.normalize(body, ctx("futures_rt"), assets=("B-X_USDT",), max_age_ms=60_000)
    assert isinstance(out, Rejected)


def test_absent_pair_is_reported_separately_from_a_stale_one():
    body = json.dumps({"ts": RECV, "prices": {
        "B-BTC_USDT": {"fr": "0", "efr": "0", "mp": "1", "ls": "1", "btST": RECV - 100}}}).encode()
    out = futures_rt.normalize(body, ctx("futures_rt"), assets=("B-BTC_USDT", "B-GONE_USDT"), max_age_ms=60_000)
    assert isinstance(out, Partial)
    assert out.rejected[0].reason == "PAIR_ABSENT"


# ---- hazards #5, #6, #8 and F-014: books ---------------------------------------------

def test_hazard5_book_object_becomes_sorted_lists():
    body = raw("futures_book_B-BTC_USDT_50_2026-09-18T1237Z.json")
    src = json.loads(body)
    assert list(src["asks"])[:3] != sorted(src["asks"], key=float)[:3], "fixture must be unordered"

    out = book.normalize_futures(body, ctx("futures_book", asset="B-BTC_USDT"))
    assert isinstance(out, Accepted)
    snap = out.records[0]
    assert [p for p, _ in snap.bids] == sorted((p for p, _ in snap.bids), reverse=True)
    assert [p for p, _ in snap.asks] == sorted(p for p, _ in snap.asks)
    assert snap.bids[0][0] < snap.asks[0][0]
    assert snap.version == src["vs"]


def test_spot_book_normalizes_and_reports_a_one_tick_spread():
    body = raw("spot_book_B-BTC_USDT_depth50_2026-09-18T1238Z.json")
    out = book.normalize_spot(body, ctx("spot_book", asset="B-BTC_USDT"))
    snap = out.records[0]
    assert len(snap.bids) == 50 and len(snap.asks) == 50
    assert snap.spread_bps < Decimal("1")          # verified: books are one tick wide (F-025)


def test_hazard6_zero_quantity_levels_are_dropped():
    body = json.dumps({"timestamp": RECV, "bids": {"100": "1", "99": "0"}, "asks": {"101": "1"}}).encode()
    out = book.normalize_spot(body, ctx("spot_book", asset="a"))
    assert isinstance(out, Accepted)
    assert [str(p) for p, _ in out.records[0].bids] == ["100"]


def test_hazard8_capital_depth_error_object_is_rejected_with_the_fix_named():
    """The real 422 body for `Depth=50` (F-001), returned with an HTTP error the adapter surfaces."""
    out = book.normalize_spot(raw("spot_book_Depth_422_2026-09-18T1237Z.json"), ctx("spot_book", asset="a"))
    assert isinstance(out, Rejected)
    assert "lowercase depth" in out.error.remediation
    assert "hazard #8" in out.error.likely_cause


def test_empty_futures_book_is_invalid_not_no_liquidity():
    body = json.dumps({"ts": RECV, "vs": 1, "bids": {}, "asks": {}}).encode()
    out = book.normalize_futures(body, ctx("futures_book", asset="a"))
    assert isinstance(out, Rejected)
    assert "F-014" in out.error.likely_cause


def test_stale_book_is_rejected_as_a_data_quality_failure():
    body = json.dumps({"timestamp": RECV - 631_000, "bids": {"100": "1"}, "asks": {"101": "1"}}).encode()
    out = book.normalize_spot(body, ctx("spot_book", asset="a"), max_age_ms=600_000)
    assert isinstance(out, Rejected)
    assert out.error.code == "DQ_STALE"
    assert out.error.problem_class.value == "data_quality"
    assert "F-026" in out.error.likely_cause


def test_crossed_book_is_rejected():
    body = json.dumps({"timestamp": RECV, "bids": {"102": "1"}, "asks": {"101": "1"}}).encode()
    out = book.normalize_spot(body, ctx("spot_book", asset="a"))
    assert isinstance(out, Rejected) and "crossed" in out.error.message


# ---- instrument: the fee conversion point -------------------------------------------

def test_instrument_converts_percent_fees_and_builds_margin_tiers():
    out = instrument.normalize(raw("instrument_B-BTC_USDT_2026-09-18T1237Z.json"),
                               ctx("instrument", asset="B-BTC_USDT", product=Product.FUTURES))
    assert isinstance(out, Accepted)
    inst = out.records[0]
    # verified live values (specs/00 §2): taker 0.059 PERCENT -> 0.00059 fraction
    assert inst.taker_fee.fraction == Decimal("0.00059")
    assert inst.maker_fee.fraction == Decimal("0.000236")
    assert inst.tick == Decimal("0.1") and inst.qty_increment == Decimal("0.001")
    assert inst.min_notional == Decimal("60.0")
    assert inst.multiplier_up == Decimal("4.0")            # +/-4% limit-price band, verified
    # round-trip taker cost = 2 x 5.9 bps = 11.8 bps, the figure every gate uses
    assert inst.taker_fee.fraction * 2 * Decimal(10_000) == Decimal("11.80000")


def test_instrument_margin_tier_is_a_fraction_not_a_percent():
    out = instrument.normalize(raw("instrument_B-BTC_USDT_2026-09-18T1237Z.json"),
                               ctx("instrument", asset="B-BTC_USDT", product=Product.FUTURES))
    inst = out.records[0]
    if inst.margin_tiers:
        rate = inst.maintenance_margin_for(Decimal(10_000))
        assert Decimal("0.001") <= rate <= Decimal("0.05"), f"{rate} does not look like a fraction"
    else:
        assert any("DQ_META" in w for w in out.warnings)


def test_instrument_without_tiers_warns_that_signals_will_be_vetoed():
    body = json.dumps({"instrument": {"pair": "B-X_USDT", "price_increment": "0.1",
                                      "quantity_increment": "0.001", "min_notional": "60",
                                      "maker_fee": "0.02", "taker_fee": "0.05", "status": "active"}}).encode()
    out = instrument.normalize(body, ctx("instrument", asset="B-X_USDT", product=Product.FUTURES))
    assert isinstance(out, Accepted)
    assert any("DQ_META" in w for w in out.warnings)


def test_instrument_with_an_implausible_fee_is_rejected_as_a_unit_error():
    body = json.dumps({"instrument": {"pair": "B-X_USDT", "price_increment": "0.1",
                                      "quantity_increment": "0.001", "min_notional": "60",
                                      "maker_fee": "0.02", "taker_fee": "5.9"}}).encode()   # 5.9% !
    out = instrument.normalize(body, ctx("instrument", asset="B-X_USDT", product=Product.FUTURES))
    assert isinstance(out, Rejected)
    assert out.error.code == "NORMALIZE_UNIT"
    assert out.error.problem_class.value == "application_bug"


def test_instrument_missing_a_required_field_names_it():
    body = json.dumps({"instrument": {"pair": "B-X_USDT", "price_increment": "0.1"}}).encode()
    out = instrument.normalize(body, ctx("instrument", asset="B-X_USDT", product=Product.FUTURES))
    assert isinstance(out, Rejected)
    assert "min_notional" in out.error.message


# ---- positioning (F-019) ------------------------------------------------------------

def test_positioning_normalizes_the_real_payload_and_computes_divergence():
    out = positioning.normalize(raw("positioning_B-BTC_USDT_2026-09-19T1057Z.json"),
                                ctx("positioning", asset="B-BTC_USDT"))
    assert isinstance(out, Accepted)
    p = out.records[0]
    src = json.loads(raw("positioning_B-BTC_USDT_2026-09-19T1057Z.json"))["position"]
    assert p.long_count_pct == Decimal(str(src["count_percent"]["long"]))
    assert p.divergence == p.long_count_pct - p.long_value_pct
    assert p.ts_ms == RECV                       # local receive time, and the warning says so
    assert any("local receive time" in w for w in out.warnings)


def test_empty_positioning_object_is_rejected_rather_than_stored_as_zero():
    body = b'{"price_change_percent":{},"high_and_low":{},"position":{}}'      # the real unknown-pair reply
    out = positioning.normalize(body, ctx("positioning", asset="B-NOPE_USDT"))
    assert isinstance(out, Rejected)
    assert "F-019" in out.error.likely_cause


# ---- hazard #7: the aggressor flag -------------------------------------------------

def test_spot_tape_normalizes_500_real_trades_with_a_boolean_flag():
    out = trades.normalize_spot(raw("spot_trades_B-BTC_USDT_500_2026-09-19T1029Z.json"),
                                ctx("spot_trades", asset="B-BTC_USDT"))
    assert isinstance(out, Accepted) and len(out) == 500
    assert all(isinstance(t.is_buyer_maker, bool) for t in out.records)
    times = [t.ts_ms for t in out.records]
    assert times == sorted(times)                     # canonical ascending despite newest-first input


def test_futures_tape_is_thirty_rows_and_says_so():
    out = trades.normalize_futures(raw("futures_trades_B-BTC_USDT_2026-09-19T1029Z.json"),
                                   ctx("futures_trades", asset="B-BTC_USDT"))
    assert isinstance(out, Accepted) and len(out) == 30
    assert any("fixed 30-row snapshot" in w for w in out.warnings)


@pytest.mark.parametrize("encoded,expected", [(True, True), (False, False), (1, True), (0, False),
                                              ("1", True), ("0", False), ("true", True)])
def test_hazard7_every_aggressor_encoding_maps_to_one_canonical_bool(encoded, expected):
    body = json.dumps([{"p": "100", "q": "1", "T": RECV, "m": encoded}]).encode()
    out = trades.normalize_spot(body, ctx("spot_trades", asset="a"))
    assert isinstance(out, Accepted)
    assert out.records[0].is_buyer_maker is expected


def test_an_unknown_aggressor_encoding_is_rejected_not_guessed():
    body = json.dumps([{"p": "100", "q": "1", "T": RECV, "m": "maybe"}]).encode()
    out = trades.normalize_spot(body, ctx("spot_trades", asset="a"))
    assert isinstance(out, Rejected)
    assert "hazard #7" in out.error.likely_cause


# ---- hazards that are moot, recorded so the coverage claim is honest ---------------

def test_moot_hazards_are_documented_not_implemented():
    """#2 auth timestamp, #9 GET-with-signed-body: resolved by F-019 — the endpoint is public and
    unauthenticated, so neither contradiction can bite. #10 double-slash URL: that endpoint is an
    order endpoint and is never called. #11 Socket.IO: no WebSocket exists (specs/11 §1 Revision 1)."""
    import pathlib
    src = pathlib.Path(__file__).parents[1] / "src"
    assert not list(src.rglob("*socketio*")) and not list(src.rglob("*websocket*"))
    joined = " ".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py"))
    assert "socketio" not in joined.lower()
    for forbidden in ("/orders/create", "/orders/cancel", "/wallets", "users/balances"):
        assert forbidden not in joined, f"an order/balance endpoint appears in src/: {forbidden}"
