"""Stream tasks S1–S7 (specs/01b-recorder.md, Streams).

A task is the unit of scheduling and of error state: one task per (stream, pair)
where the endpoint is per-pair, one task per stream where a single response covers
all pairs. Tasks record raw slices; they do not normalize or judge. The only parsing
done here is to locate the slices, read the source timestamp, and count quality
statistics for probe P13 — none of it alters what is stored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cdx_http.jsonutil import raw_array_elements, raw_object_members
from cdx_http.signed import Credentials, signed_request

if TYPE_CHECKING:
    from .runner import TaskContext

API = "https://api.coindcx.com"
PUBLIC = "https://public.coindcx.com"

FUTURES_RT_URL = f"{PUBLIC}/market_data/v3/current_prices/futures/rt"
SPOT_TICKER_URL = f"{API}/exchange/ticker"
INSTRUMENT_URL = f"{API}/exchange/v1/derivatives/futures/data/instrument"
FUTURES_BOOK_URL = PUBLIC + "/market_data/v3/orderbook/{pair}-futures/{depth}"
SPOT_BOOK_URL = f"{API}/market_data/orderbook"
STATS_URL = f"{API}/api/v1/derivatives/futures/data/stats"
SPOT_CANDLES_URL = f"{API}/market_data/candles"
FUTURES_CANDLES_URL = f"{PUBLIC}/market_data/candlesticks"

# Fields a complete futures_rt row carries (B-NKN_USDT was observed missing h/l/v/ls/pc/mkt).
FUTURES_RT_FIELDS = ("fr", "efr", "mp", "ls", "h", "l", "v", "pc", "mkt", "btST", "bmST", "ctRT", "cmRT", "skw")


def _int_or_none(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


@dataclass
class Task:
    stream: str
    pair: str | None
    host: str
    interval_s: float

    #: gaps in this stream can be refilled later from REST (candles only)
    recoverable = False

    @property
    def key(self) -> str:
        return f"{self.stream}:{self.pair}" if self.pair else self.stream

    def run(self, ctx: TaskContext) -> None:
        raise NotImplementedError


# ---- S1 ------------------------------------------------------------------------

@dataclass
class FuturesRtTask(Task):
    pairs: list[str] = field(default_factory=list)

    def run(self, ctx):
        res, text, obj = ctx.get(FUTURES_RT_URL)
        top = raw_object_members(text)
        prices_raw = raw_object_members(top["prices"]) if "prices" in top else {}
        prices = obj.get("prices") if isinstance(obj, dict) else None
        source_ts = _int_or_none(obj.get("ts")) if isinstance(obj, dict) else None
        for pair in self.pairs:
            if pair not in prices_raw:
                ctx.error("pair_missing", res, detail=f"{pair} absent from prices")
                ctx.observe("pairs_missing", 1)
                continue
            payload = '{"ts":%s,"vs":%s,"row":%s}' % (top.get("ts", "null"), top.get("vs", "null"), prices_raw[pair])
            ctx.write(self.stream, pair, res, source_ts, payload)
            row = prices.get(pair) if isinstance(prices, dict) else None
            if isinstance(row, dict):
                # `skw` is NOT a per-row health flag (docs/findings.md F-004): it is one value
                # shared by every fresh row and it drifts over time. Record its range only.
                skw = row.get("skw")
                if isinstance(skw, (int, float)) and not isinstance(skw, bool):
                    ctx.observe("skw_min", skw, mode="min")
                    ctx.observe("skw_max", skw, mode="max")
                if any(f not in row for f in FUTURES_RT_FIELDS):
                    ctx.observe("rows_missing_fields", 1)
                btst = _int_or_none(row.get("btST"))
                if btst is not None:
                    ctx.observe("max_btst_age_ms", res.received_at_ms - btst, mode="max")


# ---- S2 ------------------------------------------------------------------------

@dataclass
class PositioningTask(Task):
    """Futures ``data/stats``. P8 (findings F-019): a plain unauthenticated GET returns the
    full payload, POST returns 404. ``auth="none"`` is the default so no credential is
    sent to an endpoint that does not need one; ``auth="signed"`` keeps the documented form."""
    auth: str = "none"
    creds: Credentials | None = None
    method: str = "GET"
    timestamp_unit: str = "ms"

    def run(self, ctx):
        if self.auth == "signed":
            body, headers = signed_request(self.creds, None, self.timestamp_unit)
        else:
            body, headers = None, None
        res, text, obj = ctx.get(STATS_URL, params={"pair": self.pair}, method=self.method, headers=headers, body=body)
        ctx.write(self.stream, self.pair, res, None, text)
        # An unknown pair returns 200 with empty objects (F-019); count it, never drop it.
        pos = obj.get("position") if isinstance(obj, dict) else None
        if not pos:
            ctx.observe("empty_position", 1)
            ctx.error("empty_position", res, detail="200 with empty position object")


# ---- S3 ------------------------------------------------------------------------

@dataclass
class SpotTickerTask(Task):
    markets: dict[str, str] = field(default_factory=dict)   # ticker `market` -> pair

    def run(self, ctx):
        res, text, obj = ctx.get(SPOT_TICKER_URL)
        if not isinstance(obj, list):
            ctx.error("unexpected_shape", res, detail="ticker body is not an array", body=res.body)
            return
        seen = set()
        for raw, row in zip(raw_array_elements(text), obj):
            market = row.get("market") if isinstance(row, dict) else None
            if market in self.markets:
                seen.add(market)
                ts = _int_or_none(row.get("timestamp"))
                # Ticker `timestamp` is epoch SECONDS (observed 2026-09-18); envelope field is ms.
                ctx.write(self.stream, self.markets[market], res, None if ts is None else ts * 1000, raw)
        for market in self.markets.keys() - seen:
            ctx.error("pair_missing", res, detail=f"{market} absent from ticker")
            ctx.observe("pairs_missing", 1)


# ---- S4 / S5 / S6: one request, whole body stored -----------------------------

@dataclass
class WholeBodyTask(Task):
    url: str = ""
    params: dict = field(default_factory=dict)
    source_ts_field: str | None = None

    def run(self, ctx):
        res, text, obj = ctx.get(self.url, params=self.params)
        ts = None
        if self.source_ts_field and isinstance(obj, dict):
            ts = _int_or_none(obj.get(self.source_ts_field))
        ctx.write(self.stream, self.pair, res, ts, text)


def instrument_task(pair: str, interval_s: float) -> WholeBodyTask:
    return WholeBodyTask("instrument_meta", pair, "api.coindcx.com", interval_s,
                         url=INSTRUMENT_URL, params={"pair": pair, "margin_currency_short_name": "USDT"})


def futures_book_task(pair: str, interval_s: float, depth: int) -> WholeBodyTask:
    return WholeBodyTask("futures_book", pair, "public.coindcx.com", interval_s,
                         url=FUTURES_BOOK_URL.format(pair=pair, depth=depth), source_ts_field="ts")


def spot_book_task(pair: str, interval_s: float, depth: int) -> WholeBodyTask:
    # lowercase `depth`: the documented capital `Depth` is rejected with 422 (verified 2026-09-18)
    return WholeBodyTask("spot_book", pair, "api.coindcx.com", interval_s,
                         url=SPOT_BOOK_URL, params={"pair": pair, "depth": depth}, source_ts_field="timestamp")


# ---- S7 ------------------------------------------------------------------------

def closed_until(bar_times: list[int], interval_ms: int, received_at_ms: int) -> int | None:
    """Open time of the newest bar that had fully closed when the response arrived."""
    closed = [t for t in bar_times if t + interval_ms <= received_at_ms]
    return max(closed) if closed else None


def plan_windows(start_ms: int, end_ms: int, interval_ms: int, chunk_bars: int) -> list[tuple[int, int]]:
    """Inclusive [start, end] windows of at most ``chunk_bars`` bar-opens each.

    Consecutive windows share one boundary bar, so an inclusive/exclusive ambiguity in
    the endpoint can only cause a duplicate, never a hole.
    """
    span = (chunk_bars - 1) * interval_ms
    out = []
    s = start_ms
    while True:
        e = min(s + span, end_ms)
        out.append((s, e))
        if e >= end_ms:
            return out
        s = e


def bar_times(product: str, obj) -> list[int]:
    rows = obj if product == "spot" else (obj.get("data") if isinstance(obj, dict) else None)
    if not isinstance(rows, list):
        return []
    return [t for r in rows if isinstance(r, dict) and (t := _int_or_none(r.get("time"))) is not None]


@dataclass
class CandleTask(Task):
    product: str = "spot"            # "spot" | "futures"
    resolution: str = "1h"           # spot interval or futures resolution, as sent
    interval_ms: int = 3_600_000
    chunk_bars: int = 500
    initial_bars: int = 500
    max_gap_fill_days: float = 7
    coverage_ms: int | None = None   # open time of newest closed bar already recorded

    recoverable = True

    def run(self, ctx):
        now = ctx.now_ms()
        floor = now - max(int(self.max_gap_fill_days * 86_400_000), self.chunk_bars * self.interval_ms)
        start = now - self.initial_bars * self.interval_ms if self.coverage_ms is None else self.coverage_ms
        if start < floor:
            ctx.meta_gap_event("candle_gap_exceeds_fill_window", self, start, floor)
            start = floor
        for s, e in plan_windows(start, now, self.interval_ms, self.chunk_bars):
            if self.product == "spot":
                params = {"pair": self.pair, "interval": self.resolution,
                          "startTime": s, "endTime": e, "limit": self.chunk_bars}
                url = SPOT_CANDLES_URL
            else:
                params = {"pair": self.pair, "from": s // 1000, "to": (e + 999) // 1000,
                          "resolution": self.resolution, "pcode": "f"}
                url = FUTURES_CANDLES_URL
            res, text, obj = ctx.get(url, params=params)
            ctx.write(self.stream, self.pair, res, None, text)
            times = bar_times(self.product, obj)
            ctx.observe("bars", len(times))
            c = closed_until(times, self.interval_ms, res.received_at_ms)
            if c is not None and (self.coverage_ms is None or c > self.coverage_ms):
                self.coverage_ms = c

    def load_coverage(self, records) -> None:
        """Set coverage from previously written records of this (stream, pair)."""
        for rec in records:
            if rec.get("pair") != self.pair or rec.get("http_status") != 200:
                continue
            c = closed_until(bar_times(self.product, rec.get("payload")), self.interval_ms, rec.get("received_at_ms", 0))
            if c is not None and (self.coverage_ms is None or c > self.coverage_ms):
                self.coverage_ms = c


def candle_stream_name(product: str, resolution: str) -> str:
    return f"{product}_candles_{resolution}"


# ---- construction ----------------------------------------------------------------

def build_tasks(cfg, creds: Credentials | None, log) -> list[Task]:
    from .config import FUTURES_RESOLUTION_MS, SPOT_INTERVAL_MS

    tasks: list[Task] = []
    s = cfg.stream("futures_rt")
    if s.enabled:
        tasks.append(FuturesRtTask("futures_rt", None, "public.coindcx.com", s.interval_s, pairs=list(cfg.futures_pairs)))

    s = cfg.stream("positioning")
    auth = s.options.get("auth", "none")
    if not s.enabled:
        log.warning("S2 positioning is DISABLED - positioning history is NOT accumulating")
    elif auth == "signed" and creds is None:
        log.error("S2 positioning auth=signed but no credentials in environment/.env - positioning history is NOT accumulating")
    else:
        for pair in cfg.futures_pairs:
            tasks.append(PositioningTask("positioning", pair, "api.coindcx.com", s.interval_s, auth=auth,
                                         creds=creds if auth == "signed" else None,
                                         method=s.options.get("method", "GET"),
                                         timestamp_unit=s.options.get("timestamp_unit", "ms")))

    s = cfg.stream("spot_ticker")
    if s.enabled and cfg.spot:
        tasks.append(SpotTickerTask("spot_ticker", None, "api.coindcx.com", s.interval_s,
                                    markets={m.ticker_market: m.pair for m in cfg.spot}))

    s = cfg.stream("instrument_meta")
    if s.enabled:
        tasks += [instrument_task(p, s.interval_s) for p in cfg.futures_pairs]

    s = cfg.stream("futures_book")
    if s.enabled:
        tasks += [futures_book_task(p, s.interval_s, int(s.options.get("depth", 50))) for p in cfg.futures_pairs]

    s = cfg.stream("spot_book")
    if s.enabled:
        tasks += [spot_book_task(m.pair, s.interval_s, int(s.options.get("depth", 50))) for m in cfg.spot]

    s = cfg.stream("candles")
    if s.enabled:
        o = s.options
        common = {"chunk_bars": int(o.get("chunk_bars", 500)), "initial_bars": int(o.get("initial_bars", 500)),
                  "max_gap_fill_days": float(o.get("max_gap_fill_days", 7))}
        for m in cfg.spot:
            for iv in o.get("spot_intervals", []):
                tasks.append(CandleTask(candle_stream_name("spot", iv), m.pair, "api.coindcx.com", s.interval_s,
                                        product="spot", resolution=iv, interval_ms=SPOT_INTERVAL_MS[iv], **common))
        for pair in cfg.futures_pairs:
            for r in o.get("futures_resolutions", []):
                tasks.append(CandleTask(candle_stream_name("futures", r), pair, "public.coindcx.com", s.interval_s,
                                        product="futures", resolution=r, interval_ms=FUTURES_RESOLUTION_MS[r], **common))
    return tasks
