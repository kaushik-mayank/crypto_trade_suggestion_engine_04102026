"""P2 — what request rate triggers 429 on each market-data endpoint?

Controlled ramp, one endpoint at a time: RATES req/s for STEP_S seconds each; stop that
endpoint at the FIRST 429, then measure recovery (one request every 2 s until a 200).
Ceiling is deliberately 10 req/s: ~140x the recorder's rate and 5x the backfill's,
enough to set a budget without hammering the exchange. No 429 at the ceiling is
recorded as "no limit observed up to 10 req/s", not as "no limit".

Every response's headers are scanned for anything rate-limit-like (undocumented).
"""
from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

from .common import (
    FUTURES_CANDLES,
    FUTURES_RT,
    SPOT_BOOK,
    SPOT_CANDLES,
    SPOT_TICKER,
    SPOT_TRADES,
    ProbeRun,
    iso,
    now_ms,
    setup_logging,
)

RATES = [1, 2, 5, 10]
STEP_S = 15
HDR_HINTS = ("rate", "limit", "retry", "quota", "remaining", "throttl")


def endpoints():
    now_s = now_ms() // 1000
    return {
        "spot_ticker": (SPOT_TICKER, None),
        "spot_candles": (SPOT_CANDLES, {"pair": "B-BTC_USDT", "interval": "1m", "limit": 10}),
        "spot_orderbook": (SPOT_BOOK, {"pair": "B-BTC_USDT"}),
        "spot_trade_history": (SPOT_TRADES, {"pair": "B-BTC_USDT", "limit": 10}),
        "futures_rt": (FUTURES_RT, None),
        "futures_candlesticks": (FUTURES_CANDLES, {"pair": "B-BTC_USDT", "from": now_s - 3600, "to": now_s,
                                                   "resolution": "1", "pcode": "f"}),
    }


_local = threading.local()


def _session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers["User-Agent"] = "crypto-trader-phase1/0.1 (+research; read-only; rate-limit probe P2)"
    return _local.s


def _one(url, params):
    t = now_ms()
    try:
        r = _session().get(url, params=params, timeout=20)
        hints = {k: v for k, v in r.headers.items() if any(h in k.lower() for h in HDR_HINTS)}
        return {"t": t, "status": r.status_code, "hints": hints,
                "body": r.text[:300] if r.status_code >= 400 else None, "latency_ms": now_ms() - t}
    except requests.RequestException as e:
        return {"t": t, "status": None, "error": type(e).__name__, "latency_ms": now_ms() - t}


def ramp(name, url, params) -> dict:
    out = {"endpoint": name, "url": url, "steps": [], "first_429": None, "recovery": None}
    header_hints = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for rate in RATES:
            futures = []
            t_start = time.monotonic()
            for i in range(rate * STEP_S):
                target = t_start + i / rate
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                futures.append(ex.submit(_one, url, params))
                done429 = [f.result() for f in futures if f.done() and f.result()["status"] == 429]
                if done429:
                    break
            results = [f.result() for f in futures]
            for r in results:
                header_hints.update(r.get("hints") or {})
            statuses = Counter(str(r["status"]) for r in results)
            lat = sorted(r["latency_ms"] for r in results)
            step = {"rate_req_s": rate, "sent": len(results), "statuses": dict(statuses),
                    "latency_ms_p50": lat[len(lat) // 2], "latency_ms_max": lat[-1],
                    "achieved_req_s": round(len(results) / max(1e-9, time.monotonic() - t_start), 2)}
            out["steps"].append(step)
            first = next((r for r in sorted(results, key=lambda r: r["t"]) if r["status"] == 429), None)
            if first:
                idx = sorted(results, key=lambda r: r["t"]).index(first)
                out["first_429"] = {"rate_req_s": rate, "request_index_in_step": idx, "at": iso(first["t"]),
                                    "hints": first.get("hints"), "body": first.get("body")}
                t429 = first["t"]
                for _ in range(150):                       # up to 5 minutes
                    time.sleep(2)
                    r = _one(url, params)
                    if r["status"] == 200:
                        out["recovery"] = {"seconds_after_first_429": round((r["t"] - t429) / 1000, 1)}
                        break
                else:
                    out["recovery"] = {"seconds_after_first_429": ">300"}
                break
            time.sleep(5)
    out["rate_limit_like_headers_seen"] = header_hints
    out["verdict"] = (f"429 at {out['first_429']['rate_req_s']} req/s" if out["first_429"]
                      else f"no 429 up to {RATES[-1]} req/s (ceiling of this test)")
    return out


def main() -> int:
    setup_logging()
    run = ProbeRun("p02", capture=False)
    results = []
    for name, (url, params) in endpoints().items():
        r = ramp(name, url, params)
        print(name, r["verdict"], r["steps"][-1], flush=True)
        results.append(r)
        time.sleep(30)                                     # let any window reset between endpoints
    run.requests_note = "requests issued directly (thread pool), counted in steps"
    run.save({"question": "What request rate triggers 429 per market-data endpoint?",
              "note": "the production recorder was running concurrently at ~0.07 req/s",
              "endpoints": results})
    return 0


if __name__ == "__main__":
    sys.exit(main())
