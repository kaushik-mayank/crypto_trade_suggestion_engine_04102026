"""P1 — candle history depth per (product, pair, resolution).

Binary search on the window start ``t``: the predicate is "a window of W bars starting
at t contains at least one bar inside it". Bars outside the requested window are
ignored, so an endpoint that answers an out-of-range request with recent data cannot
fool the search. W stays well under the futures 30,000-bar silent cap (P5).

Assumes no internal hole longer than W; the backfill counts internal gaps and would
expose one.
"""
from __future__ import annotations

import sys

from .common import FUTURES_CANDLES, PAIRS, SPOT_CANDLES, ProbeRun, iso, now_ms, setup_logging

M = 60_000
SPOT = {"1m": M, "15m": 15 * M, "1h": 60 * M, "1d": 1440 * M}
FUT = {"1": M, "5": 5 * M, "15": 15 * M, "30": 30 * M, "60": 60 * M, "240": 240 * M, "480": 480 * M,
       "1D": 1440 * M, "3D": 3 * 1440 * M}          # 1M has no fixed interval; the backfill takes it whole
FLOOR = 1262304000000           # 2010-01-01: clearly before any of these listings
W_BARS = 499


class Series:
    def __init__(self, run, product, pair, res, iv):
        self.run, self.product, self.pair, self.res, self.iv = run, product, pair, res, iv

    def bars(self, start, end) -> tuple[int, list[int]]:
        if self.product == "spot":
            r = self.run.get(SPOT_CANDLES, {"pair": self.pair, "interval": self.res, "startTime": start,
                                            "endTime": end, "limit": W_BARS + 1})
            rows = self.run.parse(r) if r.ok else []
        else:
            r = self.run.get(FUTURES_CANDLES, {"pair": self.pair, "from": start // 1000, "to": end // 1000,
                                               "resolution": self.res, "pcode": "f"})
            obj = self.run.parse(r) if r.ok else {}
            rows = obj.get("data", []) if isinstance(obj, dict) else []
        return r.status, [int(x["time"]) for x in rows if isinstance(x, dict) and "time" in x]

    def in_window(self, start) -> list[int]:
        end = start + W_BARS * self.iv
        status, times = self.bars(start, end)
        if status != 200:
            raise RuntimeError(f"{self.product} {self.pair} {self.res}: HTTP {status}")
        return [t for t in times if start <= t <= end]


def probe_series(s: Series, now: int) -> dict:
    W = W_BARS * s.iv
    # out-of-range behaviour, recorded as evidence
    status, raw = s.bars(FLOOR, FLOOR + W)
    oor = {"status": status, "returned": len(raw), "returned_outside_window": sum(1 for t in raw if t > FLOOR + W),
           "newest_returned": iso(max(raw)) if raw else None}
    lo, hi = FLOOR, now - W
    if s.in_window(lo):
        return {"error": "data exists at the 2010 floor; widen the search", "out_of_range_request": oor}
    if not s.in_window(hi):
        return {"error": "no data in the most recent window", "out_of_range_request": oor}
    steps = 0
    while hi - lo > s.iv:
        mid = lo + (hi - lo) // 2
        mid -= mid % s.iv
        if mid <= lo:
            break
        steps += 1
        if s.in_window(mid):
            hi = mid
        else:
            lo = mid
    earliest = min(s.in_window(hi))
    return {"earliest_bar": iso(earliest), "earliest_bar_ms": earliest,
            "expected_bars_to_now": (now - earliest) // s.iv + 1,
            "span_days": round((now - earliest) / 86_400_000, 1), "search_steps": steps,
            "out_of_range_request": oor}


def main() -> int:
    setup_logging()
    run = ProbeRun("p01", rate=2.0, capture=False)   # ~1,300 requests; results carry the evidence
    now = now_ms()
    out = []
    for pair in PAIRS:
        for product, table in (("spot", SPOT), ("futures", FUT)):
            for res, iv in table.items():
                try:
                    r = probe_series(Series(run, product, pair, res, iv), now)
                except Exception as e:
                    r = {"error": f"{type(e).__name__}: {e}"}
                print(product, pair, res, r.get("earliest_bar"), r.get("span_days"), r.get("error", ""), flush=True)
                out.append({"product": product, "pair": pair, "resolution": res, **r})
    run.save({"question": "How far back does each endpoint serve, per resolution?", "series": out})
    return 0


if __name__ == "__main__":
    sys.exit(main())
