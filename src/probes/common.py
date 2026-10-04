"""Shared probe scaffolding (specs/01-verification.md).

Every probe:
* makes requests through ``ProbeRun`` so each request/response is captured verbatim
  (secrets redacted) under ``data/raw/probe/<probe_id>/<run_stamp>/``;
* parses with Decimal (``ProbeRun.parse``);
* saves a JSON result to ``data/probe_results/<probe_id>_<run_stamp>.json``;
  the human-readable conclusion goes into ``docs/findings.md`` by hand, quoting it.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cdx_http.client import HttpClient, HttpResult
from cdx_http.jsonutil import loads_decimal

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

API = "https://api.coindcx.com"
PUBLIC = "https://public.coindcx.com"
FUTURES_RT = f"{PUBLIC}/market_data/v3/current_prices/futures/rt"
SPOT_TICKER = f"{API}/exchange/ticker"
MARKETS_DETAILS = f"{API}/exchange/v1/markets_details"
SPOT_CANDLES = f"{API}/market_data/candles"
SPOT_BOOK = f"{API}/market_data/orderbook"
SPOT_TRADES = f"{API}/market_data/trade_history"
FUTURES_CANDLES = f"{PUBLIC}/market_data/candlesticks"
FUTURES_TRADES = f"{API}/exchange/v1/derivatives/futures/data/trades"
FUTURES_BOOK = PUBLIC + "/market_data/v3/orderbook/{pair}-futures/{depth}"
FUTURES_INSTRUMENT = f"{API}/exchange/v1/derivatives/futures/data/instrument"
FUTURES_STATS = f"{API}/api/v1/derivatives/futures/data/stats"

PAIRS = ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"]
SPOT_INTERVAL_MS = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000, "1d": 86_400_000}
FUTURES_RES_MS = {"1": 60_000, "5": 300_000, "60": 3_600_000, "1D": 86_400_000}

log = logging.getLogger("probe")


def utc_stamp(ms: int | None = None) -> str:
    t = datetime.now(UTC) if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC)
    return t.strftime("%Y%m%dT%H%M%SZ")


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, (set, tuple)):
            return list(o)
        if isinstance(o, Path):
            return str(o)
        return super().default(o)


class ProbeRun:
    def __init__(self, probe_id: str, rate: float = 2.0, capture: bool = True):
        self.probe_id = probe_id
        self.stamp = utc_stamp()
        self.capture_dir = DATA / "raw" / "probe" / probe_id / self.stamp if capture else None
        self.http = HttpClient(host_rates={"api.coindcx.com": rate, "public.coindcx.com": rate},
                               capture_dir=self.capture_dir)
        self.started_ms = now_ms()
        self.n_requests = 0

    def get(self, url: str, params: dict | None = None, retry: bool = True, **kw) -> HttpResult:
        self.n_requests += 1
        if retry:
            return self.http.request_with_retry("GET", url, params=params, max_attempts=4, **kw)
        return self.http.request("GET", url, params=params, **kw)

    @staticmethod
    def parse(res: HttpResult):
        return loads_decimal(res.body)

    def save(self, result: dict) -> Path:
        out = DATA / "probe_results" / f"{self.probe_id}_{self.stamp}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "probe": self.probe_id,
            "run_started_utc": iso(self.started_ms),
            "run_finished_utc": iso(now_ms()),
            "requests": self.n_requests,
            "raw_capture_dir": str(self.capture_dir.relative_to(ROOT)) if self.capture_dir else None,
            **result,
        }
        out.write_text(json.dumps(doc, indent=2, cls=_Enc), encoding="utf-8")
        print(json.dumps(doc, indent=2, cls=_Enc)[:6000])
        print(f"\nresult written to {out}")
        return out


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def bar_times_spot(rows) -> list[int]:
    return [int(r["time"]) for r in rows] if isinstance(rows, list) else []


def bar_rows_futures(obj) -> list[dict]:
    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        return obj["data"]
    return []


def run_logged(name: str, main) -> int:
    """Run a long-lived probe with a file log that records every exit, including
    KeyboardInterrupt/SystemExit, so a detached process can never die without evidence."""
    import traceback
    log_path = ROOT / "logs" / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def note(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{iso(now_ms())} {msg}" + chr(10))

    import os
    note(f"start pid={os.getpid()} argv={sys.argv[1:]}")
    try:
        rc = main()
        note(f"exit rc={rc}")
        return rc
    except BaseException as e:  # noqa: BLE001 - log absolutely everything, then re-raise
        note(f"DIED {type(e).__name__}: {e}" + chr(10) + traceback.format_exc())
        raise
