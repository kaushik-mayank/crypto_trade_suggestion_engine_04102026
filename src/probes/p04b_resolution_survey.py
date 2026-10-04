"""P4b (added) — which candle resolutions are actually served? P4 found futures
``resolution=15`` returns real 15-minute bars although the docs list only 1/5/60/1D.
A resolution counts as served when it returns ≥3 bars whose spacing equals the
expected interval and whose open times are aligned to it."""
from __future__ import annotations

import sys
from collections import Counter

from .common import FUTURES_CANDLES, SPOT_CANDLES, ProbeRun, now_ms, setup_logging

M = 60_000
SPOT = {"1m": M, "3m": 3 * M, "5m": 5 * M, "15m": 15 * M, "30m": 30 * M, "1h": 60 * M, "2h": 120 * M,
        "4h": 240 * M, "6h": 360 * M, "8h": 480 * M, "12h": 720 * M, "1d": 1440 * M, "3d": 3 * 1440 * M,
        "1w": 7 * 1440 * M, "1M": None}
FUT = {"1": M, "3": 3 * M, "5": 5 * M, "15": 15 * M, "30": 30 * M, "60": 60 * M, "120": 120 * M,
       "240": 240 * M, "360": 360 * M, "480": 480 * M, "720": 720 * M, "1D": 1440 * M, "3D": 3 * 1440 * M,
       "1W": 7 * 1440 * M, "1M": None}


def verdict(times, iv):
    times = sorted(set(times))
    if len(times) < 3:
        return {"bars": len(times), "served": False}
    steps = Counter(b - a for a, b in zip(times, times[1:]))
    step = steps.most_common(1)[0][0]
    out = {"bars": len(times), "modal_step_min": step / M, "step_share": round(steps[step] / (len(times) - 1), 3)}
    if iv:
        out["served"] = step == iv
        out["aligned_utc"] = all(t % iv == 0 for t in times) if iv <= 1440 * M else None
    else:
        out["served"] = 28 * 1440 * M <= step <= 31 * 1440 * M
    return out


def main() -> int:
    setup_logging()
    run = ProbeRun("p04b")
    now = now_ms()
    spot, fut = {}, {}
    for name, iv in SPOT.items():
        span = (iv or 31 * 1440 * M) * 20
        r = run.get(SPOT_CANDLES, {"pair": "B-BTC_USDT", "interval": name, "startTime": now - span,
                                   "endTime": now, "limit": 20}, retry=False)
        rows = run.parse(r) if r.ok else None
        spot[name] = {"status": r.status, **(verdict([int(x["time"]) for x in rows], iv) if isinstance(rows, list)
                                             else {"body": r.body[:200].decode(errors="replace")})}
    for name, iv in FUT.items():
        span = (iv or 31 * 1440 * M) * 20
        r = run.get(FUTURES_CANDLES, {"pair": "B-BTC_USDT", "from": (now - span) // 1000, "to": now // 1000,
                                      "resolution": name, "pcode": "f"}, retry=False)
        obj = run.parse(r) if r.ok else None
        data = obj.get("data", []) if isinstance(obj, dict) else []
        fut[name] = {"status": r.status, "s": obj.get("s") if isinstance(obj, dict) else None,
                     **verdict([int(x["time"]) for x in data], iv)}
    run.save({"spot_intervals": spot, "futures_resolutions": fut,
              "spot_served": [k for k, v in spot.items() if v.get("served")],
              "futures_served": [k for k, v in fut.items() if v.get("served")]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
