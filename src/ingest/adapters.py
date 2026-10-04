"""One adapter per endpoint (specs/11 §3.1).

Each adapter knows the URL, the parameter spelling and the unit conventions — and nothing else. It
returns raw bytes; normalization happens downstream where it can be re-run against captured payloads.

The verified request-side traps live here, once:

* spot order book takes **lowercase ``depth``** in {1,5,10,20,50,100,200}; capital ``Depth`` is a 422 (F-001);
* futures order book takes depth **10, 20 or 50** only — 5 and 100 return 200 with an empty book (F-014);
* futures candlesticks take ``from``/``to`` in **seconds** while returning ``time`` in milliseconds (F-008);
* spot candles take ``startTime``/``endTime`` in **milliseconds** and a ``limit`` (verified 1000 works);
* futures candlesticks silently truncate to the **oldest 30,000 bars** (F-009), so chunks stay far below that.
"""
from __future__ import annotations

from core.errors import ConfigError
from ingest.client import IngestClient, IngestResult
from normalize.canonical import Product

API = "https://api.coindcx.com"
PUBLIC = "https://public.coindcx.com"

SPOT_CANDLES = f"{API}/market_data/candles"
SPOT_TICKER = f"{API}/exchange/ticker"
SPOT_BOOK = f"{API}/market_data/orderbook"
SPOT_TRADES = f"{API}/market_data/trade_history"
MARKETS_DETAILS = f"{API}/exchange/v1/markets_details"
FUTURES_CANDLES = f"{PUBLIC}/market_data/candlesticks"
FUTURES_RT = f"{PUBLIC}/market_data/v3/current_prices/futures/rt"
FUTURES_BOOK = PUBLIC + "/market_data/v3/orderbook/{pair}-futures/{depth}"
FUTURES_INSTRUMENT = f"{API}/exchange/v1/derivatives/futures/data/instrument"
FUTURES_TRADES = f"{API}/exchange/v1/derivatives/futures/data/trades"
FUTURES_STATS = f"{API}/api/v1/derivatives/futures/data/stats"

SPOT_DEPTHS = (1, 5, 10, 20, 50, 100, 200)
FUTURES_DEPTHS = (10, 20, 50)
SPOT_MAX_LIMIT = 1000
FUTURES_MAX_BARS = 30_000
FUTURES_SAFE_CHUNK = 10_000


def _bad(param: str, value: object, allowed: object, note: str) -> ConfigError:
    return ConfigError(
        f"{param}={value!r} is not accepted by this endpoint",
        component="ingest.adapters", operation="validate",
        context={"param": param, "value": repr(value), "allowed": allowed},
        likely_cause=note,
        remediation=f"use one of {allowed}",
    )


def spot_candles(client: IngestClient, *, pair: str, interval: str, start_ms: int, end_ms: int,
                 limit: int = 500) -> IngestResult:
    if limit > SPOT_MAX_LIMIT:
        raise _bad("limit", limit, f"<= {SPOT_MAX_LIMIT}", "spot candles cap the page size")
    if start_ms > end_ms:
        raise _bad("startTime", start_ms, f"<= endTime ({end_ms})", "an inverted window")
    return client.get("spot_candles", SPOT_CANDLES,
                      {"pair": pair, "interval": interval, "startTime": start_ms, "endTime": end_ms,
                       "limit": limit})


def futures_candles(client: IngestClient, *, pair: str, resolution: str, start_ms: int,
                    end_ms: int) -> IngestResult:
    """``from``/``to`` are **seconds** here (hazard #1). Chunks must stay under 30,000 bars (F-009)."""
    if start_ms > end_ms:
        raise _bad("from", start_ms, f"<= to ({end_ms})", "an inverted window returns HTTP 400")
    return client.get("futures_candles", FUTURES_CANDLES,
                      {"pair": pair, "from": start_ms // 1000, "to": (end_ms + 999) // 1000,
                       "resolution": resolution, "pcode": "f"})


def spot_ticker(client: IngestClient) -> IngestResult:
    """All markets in one request — a 3x saving at three assets (specs/11 §2.1)."""
    return client.get("spot_ticker", SPOT_TICKER)


def futures_rt(client: IngestClient) -> IngestResult:
    """All pairs in one request: funding, predicted funding, mark and last price."""
    return client.get("futures_rt", FUTURES_RT)


def spot_book(client: IngestClient, *, pair: str, depth: int = 50) -> IngestResult:
    if depth not in SPOT_DEPTHS:
        raise _bad("depth", depth, SPOT_DEPTHS, "spot rejects other values with 422 (F-014)")
    # lowercase `depth`: capital `Depth` is a 422 (F-001)
    return client.get("spot_book", SPOT_BOOK, {"pair": pair, "depth": depth})


def futures_book(client: IngestClient, *, pair: str, depth: int = 50) -> IngestResult:
    if depth not in FUTURES_DEPTHS:
        raise _bad("depth", depth, FUTURES_DEPTHS,
                   "futures depth 5 and 100 return HTTP 200 with an EMPTY book (F-014)")
    return client.get("futures_book", FUTURES_BOOK.format(pair=pair, depth=depth))


def instrument(client: IngestClient, *, pair: str, margin_currency: str = "USDT") -> IngestResult:
    return client.get("instrument", FUTURES_INSTRUMENT,
                      {"pair": pair, "margin_currency_short_name": margin_currency})


def positioning(client: IngestClient, *, pair: str) -> IngestResult:
    """Public, unauthenticated GET; POST is a 404 (F-019)."""
    return client.get("positioning", FUTURES_STATS, {"pair": pair})


def spot_trades(client: IngestClient, *, pair: str, limit: int = 500) -> IngestResult:
    return client.get("spot_trades", SPOT_TRADES, {"pair": pair, "limit": limit})


def futures_trades(client: IngestClient, *, pair: str) -> IngestResult:
    """``limit`` is ignored: the response is a fixed 30 rows (~7 s) (F-011)."""
    return client.get("futures_trades", FUTURES_TRADES, {"pair": pair})


def markets_details(client: IngestClient) -> IngestResult:
    return client.get("markets_details", MARKETS_DETAILS)


def candle_chunks(product: Product, timeframe_ms: int, start_ms: int, end_ms: int,
                  *, spot_page: int = 1000) -> list[tuple[int, int]]:
    """Inclusive [start, end] windows sized for the endpoint.

    Consecutive windows share one boundary bar, so an inclusive/exclusive ambiguity can only produce
    a duplicate (which the store deduplicates), never a hole.
    """
    bars = spot_page if product is Product.SPOT else FUTURES_SAFE_CHUNK
    if bars >= FUTURES_MAX_BARS:
        raise _bad("chunk_bars", bars, f"< {FUTURES_MAX_BARS}",
                   "futures candlesticks truncate silently to the OLDEST 30,000 bars (F-009)")
    span = (bars - 1) * timeframe_ms
    out: list[tuple[int, int]] = []
    s = start_ms
    while True:
        e = min(s + span, end_ms)
        out.append((s, e))
        if e >= end_ms:
            return out
        s = e
