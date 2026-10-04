"""PHASE-13 — Tier B families 6 (funding/basis), 7 (positioning) and 8 (liquidity).

Tier B has **no history before recording started**, so most of these tests are about refusing
honestly: a z-score over an afternoon, a cumulative funding figure over intervals nobody observed, a
depth band the book does not reach. Each has a plausible-looking wrong answer available, and each is
the kind of number that would look fine on a dashboard and be meaningless.
"""
from decimal import Decimal

import pytest

from config.parameters import load_registry
from features.engine import FeatureEngine
from features.f6_funding import FUNDING_CAP, FUNDING_INTERVAL_MS
from features.f8_liquidity import BANDS_BPS, IMBAL_BAND_BPS
from features.registry import BY_NAME, FEATURES, tier_a_names
from features.value import FeatureStatus
from normalize.canonical import BookSnapshot, FuturesRT, Positioning, Product, SpotTicker, Timeframe
from snapshot.models import MarketSnapshot, SeriesKey, SeriesState

H = 3_600_000
DAY = 86_400_000
T0 = 1767225600000
ASSET = "B-BTC_USDT"
PARAMS = {p.name: p.value for p in load_registry().parameters}


def rt(ts, *, fr="0.0001", efr="0.0002", mark="85000", last="84990"):
    return FuturesRT(asset=ASSET, ts_ms=ts, funding_rate=fr, predicted_funding_rate=efr,
                     mark_price=mark, last_price=last, tpe_tick_send_ms=ts, tpe_mark_send_ms=ts,
                     received_at_ms=ts + 200)


def ticker(ts, *, last="84980"):
    return SpotTicker(asset=ASSET, ts_ms=ts, last_price=last, received_at_ms=ts + 100)


def positioning(ts, *, long_cnt="60", long_val="55"):
    return Positioning(asset=ASSET, ts_ms=ts, long_count_pct=long_cnt,
                       short_count_pct=str(100 - Decimal(long_cnt)),
                       long_value_pct=long_val, short_value_pct=str(100 - Decimal(long_val)))


def book(ts, *, product=Product.FUTURES, mid=85000.0, levels=50, step_bps=0.02, qty=0.1):
    """A synthetic book whose reach is controlled: ``levels * step_bps`` bps either side."""
    bids, asks = [], []
    for i in range(levels):
        off = mid * (step_bps * (i + 1)) / 10_000
        bids.append((Decimal(str(round(mid - off, 2))), Decimal(str(qty))))
        asks.append((Decimal(str(round(mid + off, 2))), Decimal(str(qty))))
    return BookSnapshot(asset=ASSET, product=product, ts_ms=ts, bids=tuple(bids), asks=tuple(asks),
                        fetched_at_ms=ts)


def snapshot(*, as_of=T0 + 100 * H, rt_now=None, rt_hist=(), tk_now=None, tk_hist=(),
             pos_now=None, pos_hist=(), fbook=None, sbook=None, history=True):
    """A snapshot carrying only Tier B — enough for these families, which read no bars."""
    from snapshot.models import PointInTimeValue

    def pv(source, value, ok=True, reason=None, max_age=60_000):
        ts = None
        if value is not None:
            ts = getattr(value, "source_ts_ms", None) or getattr(value, "ts_ms", None)
        return PointInTimeValue(source=source, asset=ASSET, value=value, source_ts_ms=ts,
                                age_ms=(as_of - ts) if ts else None, max_age_ms=max_age,
                                ok=ok and value is not None,
                                reason=reason or (None if value is not None else f"no {source} record"))

    pit = {("futures_rt", ASSET): pv("futures_rt", rt_now),
           ("spot_ticker", ASSET): pv("spot_ticker", tk_now, max_age=120_000),
           ("positioning", ASSET): pv("positioning", pos_now, max_age=2_700_000),
           ("futures_book", ASSET): pv("futures_book", fbook, max_age=600_000),
           ("spot_book", ASSET): pv("spot_book", sbook, max_age=600_000)}
    hist = {}
    if history:
        hist = {("futures_rt", ASSET): tuple(rt_hist), ("spot_ticker", ASSET): tuple(tk_hist),
                ("positioning", ASSET): tuple(pos_hist), ("futures_book", ASSET): (),
                ("spot_book", ASSET): ()}
    # one bar series so the engine has something to key on; Tier B does not read it
    key = SeriesKey(ASSET, Product.FUTURES, Timeframe.H1)
    series = {key: SeriesState(key=key, bars=(), requested=1, min_required=1, ok=False,
                               reason="EMPTY", detail="not needed for Tier B")}
    return MarketSnapshot(as_of_ms=as_of, code_version="test", parameter_hash="ph", series=series,
                          point_in_time=pit, point_in_time_history=hist)


def compute(snap, **kw):
    engine = FeatureEngine(PARAMS, parameter_hash="ph", **kw)
    return engine.compute(snap, ASSET, Product.FUTURES)


# ---- Family 6: funding -------------------------------------------------------------------

def test_funding_values_are_reported_as_given_with_their_units():
    fs = compute(snapshot(rt_now=rt(T0 + 100 * H - 10_000)))
    assert fs.value("FND_RATE") == pytest.approx(0.0001)
    assert fs.value("FND_PRED") == pytest.approx(0.0002)
    assert fs.value("FND_TENSION") == pytest.approx(0.0001)
    assert "per 8h interval" in fs.get("FND_RATE").detail["units"]
    assert fs.get("FND_RATE").detail["interval_hours"] == 8


def test_cap_distance_is_one_at_zero_and_zero_at_the_cap():
    at_zero = compute(snapshot(rt_now=rt(T0, efr="0"))).value("FND_CAPDIST")
    at_cap = compute(snapshot(rt_now=rt(T0, efr=str(FUNDING_CAP)))).value("FND_CAPDIST")
    assert at_zero == pytest.approx(1.0)
    assert at_cap == pytest.approx(0.0)


def test_a_rate_beyond_the_documented_cap_is_refused_rather_than_clipped():
    """Beyond the venue's own cap means the contract or our units are wrong; a clipped 0.0 would
    present a contract violation as an extreme but valid reading."""
    fv = compute(snapshot(rt_now=rt(T0, efr="0.05"))).get("FND_CAPDIST")
    assert fv.status is FeatureStatus.NUMERICAL_FAILURE
    assert fv.detail["guard"] == "CAP_EXCEEDED"
    assert "0.02" in fv.reason


def test_a_stale_funding_row_disables_the_whole_family_rather_than_ageing_quietly():
    from snapshot.models import PointInTimeValue
    snap = snapshot(rt_now=rt(T0))
    stale = PointInTimeValue(source="futures_rt", asset=ASSET, value=rt(T0), source_ts_ms=T0,
                             age_ms=9_000_000, max_age_ms=60_000, ok=False,
                             reason="9000.0s old, window is 60s")
    snap = MarketSnapshot(as_of_ms=snap.as_of_ms, code_version="t", parameter_hash="p",
                          series=snap.series,
                          point_in_time={**dict(snap.point_in_time), ("futures_rt", ASSET): stale},
                          point_in_time_history=dict(snap.point_in_time_history))
    fs = compute(snap)
    for name in ("FND_RATE", "FND_PRED", "FND_TENSION", "FND_CAPDIST"):
        fv = fs.get(name)
        assert fv.status is FeatureStatus.DQ_FAILED
        assert "9000.0s old" in fv.reason
        assert fv.value is None


def test_a_funding_z_score_needs_a_sample_and_says_how_little_it_has():
    hist = [rt(T0 + i * 60_000, fr="0.0001") for i in range(5)]
    fv = compute(snapshot(rt_now=hist[-1], rt_hist=hist)).get("FND_Z")
    assert fv.status is FeatureStatus.INSUFFICIENT_DATA
    assert "5 observation(s)" in fv.reason and "no history before recording started" in fv.reason


def test_a_constant_funding_rate_fails_the_z_score_rather_than_reporting_zero():
    """Measured on real recorded data: BTC funding sat at exactly 0.0001 for the whole window, so the
    trailing std is 1e-20. A z-score of a constant is undefined, not 0."""
    hist = [rt(T0 + i * 60_000, fr="0.0001") for i in range(60)]
    fv = compute(snapshot(rt_now=hist[-1], rt_hist=hist)).get("FND_Z")
    assert fv.status is FeatureStatus.NUMERICAL_FAILURE
    assert fv.detail["guard"] == "ZERO_STDDEV"


def test_a_funding_z_score_reports_how_much_history_it_used():
    hist = [rt(T0 + i * 60_000, fr=f"0.000{1 + i % 5}") for i in range(120)]
    fv = compute(snapshot(rt_now=hist[-1], rt_hist=hist)).get("FND_Z")
    assert fv.status is FeatureStatus.OK
    assert fv.detail["observations"] == 120
    assert fv.detail["history_hours"] == pytest.approx(119 / 60, abs=0.02)
    assert "only as long as recording has run" in fv.detail["caveat"]


def test_cumulative_funding_is_labelled_an_approximation_everywhere():
    as_of = T0 + 10 * DAY
    hist = [rt(as_of - 8 * DAY + i * 3_600_000, fr="0.0001") for i in range(8 * 24)]
    fs = compute(snapshot(as_of=as_of, rt_now=hist[-1], rt_hist=hist))
    fv = fs.get("FND_CUM_1D")
    assert fv.status is FeatureStatus.OK
    assert fv.value == pytest.approx(3 * 0.0001)             # three 8h intervals
    assert "approximation of unknown error" in fv.detail["approximation"]
    assert len(fv.detail["per_interval"]) == 3
    assert fv.detail["days"] == 1.0
    assert fs.get("FND_CUM_3D").detail["days"] == 3.0


def test_cumulative_funding_refuses_intervals_recording_does_not_reach():
    as_of = T0 + 10 * DAY
    hist = [rt(as_of - 12 * 3_600_000 + i * 3_600_000, fr="0.0001") for i in range(12)]
    fs = compute(snapshot(as_of=as_of, rt_now=hist[-1], rt_hist=hist))
    fv = fs.get("FND_CUM_1W")
    assert fv.status is FeatureStatus.INSUFFICIENT_DATA
    assert "21 interval(s)" in fv.reason and "does not reach back" in fv.reason
    assert FUNDING_INTERVAL_MS == 8 * 3_600_000


# ---- Family 6: basis ---------------------------------------------------------------------

def test_basis_is_computed_when_timestamps_align():
    ts = T0 + 100 * H - 5_000
    fs = compute(snapshot(rt_now=rt(ts, mark="85000"), tk_now=ticker(ts, last="84990")))
    fv = fs.get("BAS_BPS")
    assert fv.status is FeatureStatus.OK
    assert fv.value == pytest.approx((85000 / 84990 - 1) * 10_000)
    assert fv.detail["timestamp_skew_ms"] == 0
    assert "upstream venue basis" in fv.detail["scope"]


def test_basis_is_refused_outside_the_two_second_tolerance():
    """The F-023 lesson: a ~120-point apparent gap was entirely a 6.5-minute fetch gap. Outside
    tolerance this measures elapsed price movement, not basis."""
    ts = T0 + 100 * H - 5_000
    fs = compute(snapshot(rt_now=rt(ts), tk_now=ticker(ts - 400_000)))
    fv = fs.get("BAS_BPS")
    assert fv.status is FeatureStatus.INSUFFICIENT_DATA
    assert "400.0s apart" in fv.reason
    assert "measures elapsed price movement, not basis" in fv.reason


def test_the_basis_z_score_reports_its_discard_rate():
    ts = T0 + 100 * H
    rt_hist, tk_hist = [], []
    for i in range(80):
        t = ts - (80 - i) * 60_000
        rt_hist.append(rt(t, mark=str(85000 + i)))
        # every third observation is deliberately unaligned
        tk_hist.append(ticker(t - (300_000 if i % 3 == 0 else 0), last=str(84990 + i)))
    fs = compute(snapshot(as_of=ts, rt_now=rt_hist[-1], rt_hist=rt_hist,
                          tk_now=tk_hist[-1], tk_hist=tk_hist))
    fv = fs.get("BAS_Z")
    assert fv.status is FeatureStatus.OK, fv.reason
    assert fv.detail["discarded_unaligned"] > 20
    assert 0.2 < fv.detail["discard_fraction"] < 0.45
    assert fv.detail["aligned"] + fv.detail["discarded_unaligned"] == 80


def test_the_basis_z_score_refuses_when_too_few_pairs_align():
    ts = T0 + 100 * H
    rt_hist = [rt(ts - (60 - i) * 60_000) for i in range(60)]
    # offset by half a poll interval: with a whole-multiple offset every row would find an exact
    # match ten rows back, which is the opposite of what this test means to set up
    tk_hist = [ticker(ts - (60 - i) * 60_000 - 30_000) for i in range(60)]
    fv = compute(snapshot(as_of=ts, rt_now=rt_hist[-1], rt_hist=rt_hist,
                          tk_now=tk_hist[-1], tk_hist=tk_hist)).get("BAS_Z")
    assert fv.status is FeatureStatus.INSUFFICIENT_DATA
    assert "0 of 60" in fv.reason


def test_a_request_without_tier_b_history_fails_at_startup_not_mid_pass():
    """"No history was requested" is a configuration mistake, so it must surface when the app starts.
    Discovering it as a per-feature status an hour later would train the operator to ignore the
    status that also means "the venue is down"."""
    from core.errors import StartupError
    from snapshot import SeriesRequest, SnapshotRequest

    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    request = SnapshotRequest(
        series=(SeriesRequest(SeriesKey(ASSET, Product.FUTURES, Timeframe.H1), bars=1440),),
        tier_b=(("futures_rt", ASSET),),        # present, but no history and no book/positioning
        tier_b_history_ms=0)
    with pytest.raises(StartupError) as ei:
        engine.assert_request_sufficient(request)
    problems = " | ".join(ei.value.context["problems"])
    assert "tier_b_history_ms=0" in problems          # FND_Z etc. need a trailing series
    assert "tier_b_history_ms" in ei.value.remediation


def test_the_standard_request_satisfies_every_registered_tier_b_feature():
    from snapshot import SnapshotRequest
    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    engine.assert_request_sufficient(SnapshotRequest.standard([ASSET]))      # must not raise


def test_a_missing_tier_b_source_is_named_at_startup():
    from core.errors import StartupError
    from snapshot import SeriesRequest, SnapshotRequest

    engine = FeatureEngine(PARAMS, parameter_hash="ph")
    # every bar series present, so the only problems left are the Tier B ones
    request = SnapshotRequest(
        series=tuple(SeriesRequest(SeriesKey(ASSET, Product.FUTURES, tf),
                                   bars=engine.required_bars(tf))
                     for tf in (Timeframe.H1, Timeframe.H4, Timeframe.D1)),
        tier_b=(("futures_rt", ASSET),), tier_b_history_ms=86_400_000)
    with pytest.raises(StartupError) as ei:
        engine.assert_request_sufficient(request)
    problems = " | ".join(ei.value.context["problems"])
    assert "futures_book" in problems and "positioning" in problems
    assert "does not list in tier_b" in problems


# ---- Family 7: positioning ---------------------------------------------------------------

def test_positioning_divergence_separates_participation_from_size():
    fs = compute(snapshot(pos_now=positioning(T0 + 100 * H - 60_000, long_cnt="70", long_val="45")))
    assert fs.value("POS_LONG_CNT") == pytest.approx(70.0)
    assert fs.value("POS_LONG_VAL") == pytest.approx(45.0)
    fv = fs.get("POS_DIVERGENCE")
    assert fv.value == pytest.approx(25.0)
    assert fv.detail["interpretation"] == "many small longs"

    other = compute(snapshot(pos_now=positioning(T0, long_cnt="40", long_val="65")))
    assert other.get("POS_DIVERGENCE").detail["interpretation"] == "concentrated large longs"


def test_every_positioning_value_carries_the_population_caveat():
    fs = compute(snapshot(pos_now=positioning(T0 + 100 * H - 60_000)))
    for name in ("POS_LONG_CNT", "POS_LONG_VAL", "POS_DIVERGENCE"):
        assert fs.get(name).detail["population"] == "CoinDCX users only, not the market"


def test_a_positioning_z_score_needs_months_and_says_so():
    hist = [positioning(T0 + i * 900_000) for i in range(10)]
    fv = compute(snapshot(pos_now=hist[-1], pos_hist=hist)).get("POS_Z_CNT")
    assert fv.status is FeatureStatus.INSUFFICIENT_DATA
    assert "10 snapshot(s)" in fv.reason


def test_a_positioning_z_score_flags_its_own_unreliability_when_it_does_compute():
    hist = [positioning(T0 + i * 900_000, long_cnt=str(55 + (i % 7))) for i in range(60)]
    fv = compute(snapshot(as_of=T0 + 60 * 900_000, pos_now=hist[-1], pos_hist=hist)).get("POS_Z_CNT")
    assert fv.status is FeatureStatus.OK
    assert "unreliable until months" in fv.detail["caveat"]
    assert fv.detail["observations"] == 60


def test_rate_of_change_refuses_when_history_does_not_reach_back():
    hist = [positioning(T0 + i * 900_000) for i in range(20)]      # ~5 hours
    fv = compute(snapshot(as_of=T0 + 20 * 900_000, pos_now=hist[-1], pos_hist=hist)).get("POS_ROC_3D")
    assert fv.status is FeatureStatus.INSUFFICIENT_DATA
    assert "3 day(s) before" in fv.reason and "reaches back 0.2" in fv.reason


def test_rate_of_change_reports_the_span_it_actually_measured():
    """A "1 day" change measured over 1.3 days is not wrong, but it must say 1.3."""
    hist = [positioning(T0 + i * DAY // 2, long_cnt=str(50 + i)) for i in range(6)]
    as_of = T0 + 3 * DAY
    fv = compute(snapshot(as_of=as_of, pos_now=hist[-1], pos_hist=hist)).get("POS_ROC_1D")
    assert fv.status is FeatureStatus.OK
    assert fv.detail["days"] == 1
    assert fv.detail["actual_span_days"] >= 1.0
    assert fv.value == pytest.approx(float(hist[-1].long_count_pct - hist[-3].long_count_pct))


# ---- Family 8: liquidity -----------------------------------------------------------------

def test_the_spread_is_hand_computable_from_l1():
    b = book(T0 + 100 * H - 60_000, mid=85000.0, levels=5, step_bps=1.0)
    fs = compute(snapshot(fbook=b))
    expected = float((b.asks[0][0] - b.bids[0][0]) / b.mid * Decimal(10_000))
    assert fs.value("LIQ_SPREAD") == pytest.approx(expected)
    assert fs.get("LIQ_SPREAD").detail["levels"] == 10


def test_bands_the_book_does_not_reach_report_none_not_zero():
    """The whole point of F-032: a shallow book must not be summed and labelled 25 bps."""
    shallow = book(T0 + 100 * H - 60_000, levels=50, step_bps=0.02)      # reach ~1 bps
    fv = compute(snapshot(fbook=shallow)).get("LIQ_DEPTH")
    assert fv.status is FeatureStatus.OK
    bands = fv.detail["bands"]
    assert bands["25bps"] is None and bands["50bps"] is None
    assert "25bps" in fv.detail["unmeasurable"]
    assert fv.value == pytest.approx(fv.detail["visible_notional"])
    assert fv.detail["visible_reach_bps"]["bid"] < 2.0
    assert "not observable at this venue" in fv.detail["note"]


def test_a_deep_book_measures_the_bands_it_covers():
    deep = book(T0 + 100 * H - 60_000, levels=60, step_bps=1.0)          # reach ~60 bps
    fv = compute(snapshot(fbook=deep)).get("LIQ_DEPTH")
    bands = fv.detail["bands"]
    assert all(bands[f"{b}bps"] is not None for b in BANDS_BPS)
    assert bands["1bps"] < bands["5bps"] < bands["25bps"] <= bands["50bps"]
    assert fv.detail["widest_measured_bps"] == 50
    assert fv.detail["unmeasurable"] == []


def test_imbalance_is_marked_forbidden_as_alpha_and_states_its_band():
    b = book(T0 + 100 * H - 60_000, levels=50, step_bps=0.02)
    fv = compute(snapshot(fbook=b)).get("LIQ_IMBAL")
    assert fv.status is FeatureStatus.OK
    assert "forbidden" in fv.detail["alpha_use"]
    assert fv.detail["consumer"] == "risk engine and health only"
    assert fv.detail["measured_over"] == "+/-1 bps"          # the book reaches exactly 1 bps here
    assert fv.detail["requested_band_bps"] == IMBAL_BAND_BPS
    # near zero, not exactly: the synthetic levels round to 2 decimals, so the bid side carries
    # marginally less notional at the same quantity -- which is how a real book behaves too
    assert fv.value == pytest.approx(0.0, abs=1e-3)

    # narrower than 1 bps: the whole visible book is used, and the detail says so rather than
    # implying a 1 bps measurement
    tiny = compute(snapshot(fbook=book(T0 + 100 * H - 60_000, levels=10, step_bps=0.02)))
    assert tiny.get("LIQ_IMBAL").detail["measured_over"] == "the entire visible book"


def test_an_absent_book_disables_liquidity_without_disabling_funding():
    fs = compute(snapshot(rt_now=rt(T0 + 100 * H - 10_000), fbook=None))
    assert fs.get("LIQ_SPREAD").status is FeatureStatus.DQ_FAILED
    assert fs.get("LIQ_DEPTH").status is FeatureStatus.DQ_FAILED
    assert fs.get("FND_RATE").status is FeatureStatus.OK          # families fail independently


def test_the_spot_and_futures_spreads_are_separate_features():
    fs = compute(snapshot(fbook=book(T0, levels=5, step_bps=1.0),
                          sbook=book(T0, product=Product.SPOT, levels=5, step_bps=3.0)))
    assert fs.value("LIQ_SPREAD") < fs.value("LIQ_SPREAD_SPOT")


# ---- the tier boundary -------------------------------------------------------------------

def test_tier_b_features_have_no_timeframe_and_no_window():
    for spec in FEATURES:
        if spec.tier == "B":
            assert spec.timeframes == (None,), spec.name
            assert spec.windowless is True, spec.name
    fs = compute(snapshot(rt_now=rt(T0 + 100 * H - 10_000)))
    fv = fs.get("FND_RATE")
    assert fv.timeframe is None and fv.bars_used == 0 and fv.lookback == 0
    assert str(fv.key) == "FND_RATE@pit"


def test_no_tier_a_feature_depends_on_a_tier_b_one():
    """specs/02 §2: that dependency would make a Tier A feature unbacktestable without saying so."""
    tier_a = set(tier_a_names())
    for spec in FEATURES:
        if spec.tier != "A":
            continue
        for dep_name, _ in spec.depends_on:
            assert dep_name in tier_a, f"{spec.name} (Tier A) depends on Tier B {dep_name}"


def test_tier_b_features_are_reachable_without_any_bar_series():
    """A snapshot whose only bar series is unusable must still yield funding and the book: the
    families are independent, and a candle outage must not blank the funding panel."""
    fs = compute(snapshot(rt_now=rt(T0 + 100 * H - 10_000),
                          fbook=book(T0 + 100 * H - 60_000, levels=50, step_bps=0.02)))
    assert fs.is_ok("FND_RATE") and fs.is_ok("LIQ_SPREAD")
    assert not fs.is_ok("TRD_TSTAT", Timeframe.H1, "short")


def test_every_tier_b_spec_is_registered_exactly_once_with_a_version():
    names = [s.name for s in FEATURES if s.tier == "B"]
    assert len(names) == len(set(names))
    assert len(names) == 21
    for n in names:
        assert BY_NAME[n].version == "1.0.0"
