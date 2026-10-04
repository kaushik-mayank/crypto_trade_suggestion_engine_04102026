"""Recorder: storage, candle windows, and the scheduler against real captured payloads."""
import base64
import json
import logging
from pathlib import Path

import pytest

from cdx_http.client import HttpResult, TransportError
from recorder.config import load_config
from recorder.runner import Recorder
from recorder.storage import JsonlStore, envelope_line, iter_records, utc_day
from recorder.tasks import (
    CandleTask,
    FuturesRtTask,
    SpotTickerTask,
    build_tasks,
    closed_until,
    futures_book_task,
    plan_windows,
    spot_book_task,
)

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
RT = (FIX / "futures_rt_2026-09-18T1237Z.json").read_bytes()
TICKER = (FIX / "spot_ticker_2026-09-18T1237Z.json").read_bytes()
FBOOK = (FIX / "futures_book_B-BTC_USDT_50_2026-09-18T1237Z.json").read_bytes()

T0 = 1789735045753          # envelope ts of the RT fixture (2026-09-18T12:37:25.753Z)
MIN = 60_000
HOUR = 3_600_000


# ---- storage ---------------------------------------------------------------------

def test_envelope_line_exact():
    line = envelope_line("futures_rt", "B-BTC_USDT", 1000, 1780, 200, 1500, "https://x/y?a=1", '{"fr":0.00007903}')
    assert line == ('{"stream":"futures_rt","pair":"B-BTC_USDT","sent_at_ms":1000,"received_at_ms":1780,'
                    '"http_status":200,"source_ts_ms":1500,"request_url":"https://x/y?a=1","payload":{"fr":0.00007903}}')


def test_utc_day_boundaries():
    assert utc_day(1789689599999) == "2026-09-17"   # 23:59:59.999Z
    assert utc_day(1789689600000) == "2026-09-18"   # 00:00:00.000Z


def test_torn_last_line_is_isolated(tmp_path):
    f = tmp_path / "s" / "2026-09-18.jsonl"
    f.parent.mkdir()
    f.write_bytes(b'{"a":1}\n{"a":2,"torn')        # simulated kill -9 mid-write
    store = JsonlStore(tmp_path)
    store.append("s", 1789735045753, '{"a":3}')
    store.close()
    assert f.read_bytes() == b'{"a":1}\n{"a":2,"torn\n{"a":3}\n'
    assert [r["a"] for r in iter_records(f)] == [1, 3]
    assert store.repaired == [f]


# ---- candle helpers -----------------------------------------------------------------

def test_plan_windows_share_boundary_bar():
    # 1h bars, 3 bars per chunk -> span 2h; 0..5h
    assert plan_windows(0, 5 * HOUR, HOUR, 3) == [(0, 2 * HOUR), (2 * HOUR, 4 * HOUR), (4 * HOUR, 5 * HOUR)]
    assert plan_windows(0, 30 * MIN, HOUR, 500) == [(0, 30 * MIN)]


def test_closed_until_excludes_in_progress_bar():
    # bars open at 10:00, 11:00, 12:00; response at 12:37 -> 12:00 bar still open
    ten = 1789725600000
    assert closed_until([ten, ten + HOUR, ten + 2 * HOUR], HOUR, ten + 2 * HOUR + 37 * MIN) == ten + HOUR
    assert closed_until([ten], HOUR, ten + HOUR) == ten        # closes exactly at receive time
    assert closed_until([], HOUR, ten) is None


# ---- fake transport --------------------------------------------------------------------

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeHttp:
    """Routes by URL prefix. A route value is bytes (200), (status, bytes), an Exception,
    or a callable(url) returning one of those."""

    def __init__(self, clock, routes):
        self.clock = clock
        self.routes = routes
        self.calls = []

    def request(self, method, url, params=None, headers=None, body=None):
        from cdx_http.client import build_url
        full = build_url(url, params)
        self.calls.append(full)
        for prefix, v in self.routes.items():
            if full.startswith(prefix):
                if callable(v) and not isinstance(v, (bytes, Exception)):
                    v = v(full)
                if isinstance(v, Exception):
                    raise TransportError(method, full, self.clock(), self.clock(), v)
                status, content = (200, v) if isinstance(v, bytes) else v
                return HttpResult(method, full, status, {}, content, self.clock() - 780, self.clock())
        raise AssertionError(f"unrouted {full}")


RT_URL = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"
TICKER_URL = "https://api.coindcx.com/exchange/ticker"
PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]


def rt_task():
    return FuturesRtTask("futures_rt", None, "public.coindcx.com", 60, pairs=list(PAIRS))


def make(tmp_path, tasks, routes, t=T0 + 780):
    clock = Clock(t)
    http = FakeHttp(clock, routes)
    rec = Recorder(tasks, http, JsonlStore(tmp_path), clock_ms=clock, sleep=lambda s: None, secrets=("SECRETKEY",))
    return rec, http, clock


def lines(tmp_path, stream):
    out = []
    for f in sorted((tmp_path / stream).glob("*.jsonl")):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l]
    return out


def meta(tmp_path, name):
    p = tmp_path / "_meta" / f"{name}.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ---- S1 --------------------------------------------------------------------------------

def test_futures_rt_stores_three_verbatim_rows(tmp_path):
    rec, _, _ = make(tmp_path, [rt_task()], {RT_URL: RT})
    rec.startup()
    rec.tick()
    raw = (tmp_path / "futures_rt" / "2026-09-18.jsonl").read_text()
    recs = [json.loads(l) for l in raw.splitlines()]
    assert [r["pair"] for r in recs] == PAIRS
    assert all(r["source_ts_ms"] == T0 and r["http_status"] == 200 for r in recs)
    assert recs[0]["sent_at_ms"] == T0 and recs[0]["received_at_ms"] == T0 + 780
    # byte-verbatim: the stored text contains the exact source slices
    assert '"payload":{"ts":1789735045753,"vs":360059847,"row":{"fr":0.00007903,"h":78443,' in raw
    assert '"efr":0.00003739' in raw
    assert recs[2]["payload"]["row"]["mp"] == 105.9857814
    assert "B-NKN_USDT" not in raw                                   # only target rows stored


def test_futures_rt_dedup_on_same_source_ts(tmp_path):
    rec, _, clock = make(tmp_path, [rt_task()], {RT_URL: RT})
    rec.startup()
    rec.tick()
    clock.t += 60_000
    rec.tick()                                  # same fixture again -> same envelope ts
    assert len(lines(tmp_path, "futures_rt")) == 3


def test_futures_rt_quality_counts_stale_and_incomplete_rows(tmp_path):
    t = FuturesRtTask("futures_rt", None, "public.coindcx.com", 60, pairs=["B-BTC_USDT", "B-NKN_USDT", "B-XXX_USDT"])
    rec, _, _ = make(tmp_path, [t], {RT_URL: RT})
    rec.startup()
    rec.tick()
    rec.shutdown()
    q = [r for r in meta(tmp_path, "quality") if r["stream"] == "futures_rt"][0]
    assert (q["skw_min"], q["skw_max"]) == (0, 1)   # BTC skw=0, NKN skw=1 in this fixture
    assert q["rows_missing_fields"] == 1         # NKN lacks h/l/v/ls/pc/mkt/btST/ctRT
    assert q["pairs_missing"] == 1               # B-XXX_USDT
    assert q["max_btst_age_ms"] == (T0 + 780) - 1789735045102   # BTC row's btST
    assert q["records"] == 2
    assert any(e["kind"] == "pair_missing" for e in meta(tmp_path, "errors"))


# ---- S3 --------------------------------------------------------------------------------

def test_spot_ticker_filters_and_converts_timestamp(tmp_path):
    t = SpotTickerTask("spot_ticker", None, "api.coindcx.com", 60,
                       markets={"BTCUSDT": "B-BTC_USDT", "ETHUSDT": "B-ETH_USDT", "SOLUSDT": "B-SOL_USDT"})
    rec, _, _ = make(tmp_path, [t], {TICKER_URL: TICKER})
    rec.startup()
    rec.tick()
    recs = lines(tmp_path, "spot_ticker")
    assert sorted(r["pair"] for r in recs) == PAIRS
    btc = [r for r in recs if r["pair"] == "B-BTC_USDT"][0]
    assert btc["source_ts_ms"] == 1789735075000
    assert btc["payload"]["last_price"] == "78094.01000000"


# ---- S5 / S6 ------------------------------------------------------------------------------

def test_books_whole_body_and_lowercase_depth(tmp_path):
    fb = futures_book_task("B-BTC_USDT", 300, 50)
    sb = spot_book_task("B-BTC_USDT", 300, 50)
    rec, http, _ = make(tmp_path, [fb, sb], {
        "https://public.coindcx.com/market_data/v3/orderbook/B-BTC_USDT-futures/50": FBOOK,
        "https://api.coindcx.com/market_data/orderbook?pair=B-BTC_USDT&depth=50": (FIX / "spot_book_B-BTC_USDT_depth50_2026-09-18T1238Z.json").read_bytes(),
    })
    rec.startup()
    rec.tick()
    assert "https://api.coindcx.com/market_data/orderbook?pair=B-BTC_USDT&depth=50" in http.calls
    f = lines(tmp_path, "futures_book")[0]
    assert f["source_ts_ms"] == 1789735077647 and f["payload"]["asks"]["78057.5"] == "0.363"
    assert FBOOK.decode() in (tmp_path / "futures_book" / "2026-09-18.jsonl").read_text()


# ---- errors / isolation ---------------------------------------------------------------------

def test_one_failing_stream_does_not_stop_others(tmp_path):
    fb = futures_book_task("B-BTC_USDT", 300, 50)
    rec, _, clock = make(tmp_path, [rt_task(), fb], {
        RT_URL: (500, b"oops"),
        "https://public.coindcx.com/market_data/v3/orderbook/": FBOOK,
    })
    rec.startup()
    rec.tick()
    assert len(lines(tmp_path, "futures_book")) == 1
    st = rec.state["futures_rt"]
    assert st.fail_streak == 1 and st.next_due_ms == clock.t + 5_000      # 500 -> 5s
    errs = meta(tmp_path, "errors")
    assert errs[0]["http_status"] == 500 and errs[0]["body"] == "oops"


def test_401_stops_stream_permanently(tmp_path):
    rec, http, clock = make(tmp_path, [rt_task()], {RT_URL: (401, b'{"message":"bad key SECRETKEY"}')})
    rec.startup()
    rec.tick()
    assert rec.state["futures_rt"].stopped
    clock.t += 3_600_000
    rec.tick()
    assert len(http.calls) == 1
    text = (tmp_path / "_meta" / "errors.jsonl").read_text()
    assert "SECRETKEY" not in text and "<redacted>" in text


def test_503_maintenance_backoff_and_429_host_slowdown(tmp_path):
    rec, _, clock = make(tmp_path, [rt_task()], {RT_URL: (503, b"")})
    rec.startup()
    rec.tick()
    assert rec.state["futures_rt"].next_due_ms == clock.t + 30_000
    rec.http.routes[RT_URL] = (429, b"")
    clock.t += 30_000
    rec.tick()
    assert rec.state["futures_rt"].next_due_ms == clock.t + 2_000          # 429, streak 2 -> 2s
    rec.http.routes[RT_URL] = RT
    clock.t += 2_000
    rec.tick()
    assert rec.state["futures_rt"].next_due_ms == clock.t + 120_000        # host halved: 60s -> 120s


def test_malformed_json_is_logged_raw_and_survived(tmp_path):
    rec, _, _ = make(tmp_path, [rt_task()], {RT_URL: b'{"ts":1,"prices":{'})
    rec.startup()
    rec.tick()
    e = meta(tmp_path, "errors")[0]
    assert e["kind"] == "malformed_json"
    assert base64.b64decode(e["body_b64"]) == b'{"ts":1,"prices":{'
    assert not (tmp_path / "futures_rt").exists()


def test_transport_error_backoff(tmp_path):
    rec, _, clock = make(tmp_path, [rt_task()], {RT_URL: ConnectionError("down")})
    rec.startup()
    rec.tick()
    assert rec.state["futures_rt"].next_due_ms == clock.t + 2_000
    assert meta(tmp_path, "errors")[0]["kind"] == "transport"


# ---- gaps / resume -------------------------------------------------------------------------

def test_restart_records_startup_gap_and_prior_unclean(tmp_path):
    rec, _, clock = make(tmp_path, [rt_task()], {RT_URL: RT})
    rec.startup()
    rec.tick()
    first = clock.t
    rec.store.close()                          # simulate kill -9: no shutdown marker
    rec2, _, clock2 = make(tmp_path, [rt_task()], {RT_URL: RT}, t=first + 10 * MIN)
    rec2.startup()
    assert rec2.prior_shutdown_clean is False
    rec2.tick()
    gaps = [g for g in meta(tmp_path, "gaps") if g.get("reason")]
    assert gaps == [{"stream": "futures_rt", "pair": None, "gap_start_ms": first, "gap_end_ms": first + 10 * MIN,
                     "duration_s": 600.0, "reason": "startup", "recoverable": False}]


def test_clean_shutdown_marker(tmp_path):
    rec, _, _ = make(tmp_path, [rt_task()], {RT_URL: RT})
    rec.startup()
    rec.shutdown()
    rec2, _, _ = make(tmp_path, [rt_task()], {RT_URL: RT})
    rec2.startup()
    assert rec2.prior_shutdown_clean is True


def test_sleep_gap_reason_unknown_and_error_streak(tmp_path):
    rec, _, clock = make(tmp_path, [rt_task()], {RT_URL: RT})
    rec.startup()
    rec.tick()
    clock.t += 3 * HOUR                         # laptop slept
    rec.tick()
    rec.http.routes[RT_URL] = (500, b"")
    clock.t += MIN
    rec.tick()
    clock.t += 5 * MIN
    rec.http.routes[RT_URL] = RT
    rec.tick()
    reasons = [(g["reason"], g["duration_s"]) for g in meta(tmp_path, "gaps") if g.get("reason")]
    assert reasons == [("unknown", 10800.0), ("error_streak", 360.0)]


# ---- S7 candles ------------------------------------------------------------------------------

def spot_candles_body(times, iv=HOUR):
    return json.dumps([{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 0.5, "time": t}
                       for t in sorted(times, reverse=True)]).encode()


def test_candle_first_fetch_chunks_and_coverage(tmp_path):
    now = 1789735045753                                   # 12:37:25Z
    h12 = now - now % HOUR                                # 12:00Z
    t = CandleTask("spot_candles_1h", "B-BTC_USDT", "api.coindcx.com", 3600, product="spot",
                   resolution="1h", interval_ms=HOUR, chunk_bars=3, initial_bars=5, max_gap_fill_days=7)

    def route(url):
        from urllib.parse import parse_qs, urlsplit
        q = {k: int(v[0]) for k, v in parse_qs(urlsplit(url).query).items() if k in ("startTime", "endTime")}
        return spot_candles_body([x for x in range(h12 - 5 * HOUR, h12 + 1, HOUR) if q["startTime"] <= x <= q["endTime"]])

    rec, http, clock = make(tmp_path, [t], {"https://api.coindcx.com/market_data/candles": route}, t=now)
    rec.startup()
    rec.tick()
    # start = now - 5h, span per chunk = 2h -> windows [now-5h, now-3h], [now-3h, now-1h], [now-1h, now]
    assert [u.split("startTime=")[1].split("&")[0] for u in http.calls] == [
        str(now - 5 * HOUR), str(now - 3 * HOUR), str(now - HOUR)]
    assert "limit=3" in http.calls[0] and "interval=1h" in http.calls[0]
    assert t.coverage_ms == h12 - HOUR                    # 12:00 bar still open at 12:37
    assert len(lines(tmp_path, "spot_candles_1h")) == 3

    # restart: coverage reloaded from disk; next fetch starts at 11:00Z
    t2 = CandleTask("spot_candles_1h", "B-BTC_USDT", "api.coindcx.com", 3600, product="spot",
                    resolution="1h", interval_ms=HOUR, chunk_bars=3, initial_bars=5, max_gap_fill_days=7)
    rec.store.close()
    rec2, http2, _ = make(tmp_path, [t2], {"https://api.coindcx.com/market_data/candles": route}, t=now + 2 * HOUR)
    rec2.startup()
    assert t2.coverage_ms == h12 - HOUR
    rec2.tick()
    assert f"startTime={h12 - HOUR}" in http2.calls[0]


def test_futures_candle_params_in_seconds(tmp_path):
    now = 1789735078000
    t = CandleTask("futures_candles_60", "B-BTC_USDT", "public.coindcx.com", 3600, product="futures",
                   resolution="60", interval_ms=HOUR, chunk_bars=500, initial_bars=3, max_gap_fill_days=7)
    body = (FIX / "futures_candles_B-BTC_USDT_60_2026-09-18T1237Z.json").read_bytes()
    rec, http, _ = make(tmp_path, [t], {"https://public.coindcx.com/market_data/candlesticks": body}, t=now)
    rec.startup()
    rec.tick()
    assert http.calls == ["https://public.coindcx.com/market_data/candlesticks?pair=B-BTC_USDT"
                          "&from=1789724278&to=1789735078&resolution=60&pcode=f"]
    # fixture bars open 10:00, 11:00, 12:00Z; received 12:37:58 -> 11:00Z is newest closed
    assert t.coverage_ms == 1789729200000


def test_candle_outage_beyond_window_logged(tmp_path):
    now = T0
    t = CandleTask("spot_candles_1h", "B-BTC_USDT", "api.coindcx.com", 3600, product="spot", resolution="1h",
                   interval_ms=HOUR, chunk_bars=500, initial_bars=500, max_gap_fill_days=7,
                   coverage_ms=now - 30 * 86_400_000)
    rec, _, _ = make(tmp_path, [t], {"https://api.coindcx.com/market_data/candles": b"[]"}, t=now)
    rec.startup()
    rec.tick()
    ev = [g for g in meta(tmp_path, "gaps") if g.get("event") == "candle_gap_exceeds_fill_window"][0]
    # floor = now - max(7d, 500h) = now - 500h
    assert ev["gap_end_ms"] == now - 500 * HOUR and ev["recoverable"] is False


def test_unrecoverable_gap_pulls_candles_forward(tmp_path):
    c = CandleTask("spot_candles_1h", "B-BTC_USDT", "api.coindcx.com", 3600, product="spot", resolution="1h",
                   interval_ms=HOUR, initial_bars=10)
    rec, _, clock = make(tmp_path, [rt_task(), c], {RT_URL: RT, "https://api.coindcx.com/market_data/candles": b"[]"})
    rec.startup()
    rec.tick()
    clock.t += 3 * HOUR
    rec.state[c.key].next_due_ms = clock.t + HOUR        # pretend it was not yet due
    rec.tick()                                           # RT gap detected -> candles pulled to now
    assert rec.state[c.key].next_due_ms == clock.t
    rec.tick()
    assert sum("candles" in u for u in rec.http.calls) == 2


# ---- config / wiring ---------------------------------------------------------------------------

def test_shipped_config_builds_expected_tasks(caplog):
    cfg = load_config(ROOT / "recorder.toml")
    with caplog.at_level(logging.WARNING):
        tasks = build_tasks(cfg, None, logging.getLogger("t"))
    streams = {}
    for t in tasks:
        streams[t.stream] = streams.get(t.stream, 0) + 1
    assert streams["futures_rt"] == 1 and streams["spot_ticker"] == 1
    assert streams["instrument_meta"] == 3 and streams["futures_book"] == 3 and streams["spot_book"] == 3
    assert sum(v for k, v in streams.items() if "candles" in k) == 24
    assert streams["positioning"] == 3                     # public GET, no credentials needed (F-019)
    assert all(t.auth == "none" and t.creds is None for t in tasks if t.stream == "positioning")
    # requests/hour at steady state (spec budget: ~230)
    per_hour = sum(3600 / t.interval_s for t in tasks)
    assert per_hour == pytest.approx(60 + 12 + 60 + 0.5 + 36 + 36 + 24)


STATS_URL = "https://api.coindcx.com/api/v1/derivatives/futures/data/stats"
STATS_BODY = (b'{"price_change_percent":{"1H":-0.05},"high_and_low":{},'
              b'"position":{"count_percent":{"long":54.92,"short":45.08},"value_percent":{"long":31.65,"short":68.35}}}')


class HeaderSpy(FakeHttp):
    def request(self, method, url, params=None, headers=None, body=None):
        self.seen = (method, headers, body)
        return super().request(method, url, params, headers, body)


def test_positioning_unauthenticated_get(tmp_path):
    from recorder.tasks import PositioningTask
    t = PositioningTask("positioning", "B-BTC_USDT", "api.coindcx.com", 900)
    clock = Clock(T0)
    http = HeaderSpy(clock, {STATS_URL: STATS_BODY})
    rec = Recorder([t], http, JsonlStore(tmp_path), clock_ms=clock, sleep=lambda s: None)
    rec.startup()
    rec.tick()
    assert http.seen == ("GET", None, None)                # no headers, no body, no credentials
    r = lines(tmp_path, "positioning")[0]
    assert r["request_url"] == STATS_URL + "?pair=B-BTC_USDT"
    assert r["payload"]["position"]["value_percent"]["short"] == 68.35
    assert STATS_BODY.decode() in (tmp_path / "positioning" / "2026-09-18.jsonl").read_text()


def test_positioning_empty_position_is_recorded_and_flagged(tmp_path):
    from recorder.tasks import PositioningTask
    t = PositioningTask("positioning", "B-NOPE_USDT", "api.coindcx.com", 900)
    rec, _, _ = make(tmp_path, [t], {STATS_URL: b'{"price_change_percent":{},"high_and_low":{},"position":{}}'})
    rec.startup()
    rec.tick()
    assert len(lines(tmp_path, "positioning")) == 1         # raw-first: still stored
    assert meta(tmp_path, "errors")[0]["kind"] == "empty_position"
