"""P4 — futures candlestick sort order and ``s`` status field.
P5 — futures candlestick max bars per request (silent truncation?).
"""
from __future__ import annotations

import sys

from .common import FUTURES_CANDLES, ProbeRun, iso, now_ms, setup_logging

H = 3600


def fetch(run, pair, frm, to, res):
    r = run.get(FUTURES_CANDLES, {"pair": pair, "from": frm, "to": to, "resolution": res, "pcode": "f"}, retry=True)
    body = r.body.decode("utf-8", errors="replace")
    try:
        obj = run.parse(r)
    except Exception:
        obj = None
    data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), list) else []
    return r.status, (obj.get("s") if isinstance(obj, dict) else None), data, body[:300]


def main() -> int:
    setup_logging()
    run = ProbeRun("p04_p05")
    now_s = now_ms() // 1000
    end = now_s - now_s % H        # last whole hour

    # ---- P4 ----
    st, s, data, _ = fetch(run, "B-BTC_USDT", end - 24 * H, end, "60")
    times = [int(d["time"]) for d in data]
    p4 = {
        "window": [iso((end - 24 * H) * 1000), iso(end * 1000)],
        "status": st, "s": s, "bars": len(times),
        "first_time": iso(times[0]) if times else None, "last_time": iso(times[-1]) if times else None,
        "direction": ("ascending" if times == sorted(times) else "descending" if times == sorted(times, reverse=True)
                      else "unordered") if len(times) > 1 else "n/a",
        "strictly_monotonic": all(a < b for a, b in zip(times, times[1:])) or all(a > b for a, b in zip(times, times[1:])),
        "time_unit": "ms" if times and times[0] > 10**12 else "s",
        "edge_cases": {},
    }
    for name, (frm, to, res) in {
        "future_window": (now_s + 10 * 24 * H, now_s + 11 * 24 * H, "60"),
        "from_after_to": (end, end - 24 * H, "60"),
        "pre_listing_2010": (1262304000, 1262304000 + 24 * H, "60"),
        "invalid_resolution_15": (end - 24 * H, end, "15"),
        "empty_zero_width": (end, end, "60"),
    }.items():
        st2, s2, d2, body2 = fetch(run, "B-BTC_USDT", frm, to, res)
        p4["edge_cases"][name] = {"status": st2, "s": s2, "bars": len(d2),
                                  "first_bar": iso(int(d2[0]["time"])) if d2 else None, "body_head": body2}

    # ---- P5 ----
    p5 = []
    for label, span in (("1h", H), ("1d", 24 * H), ("3d", 3 * 24 * H), ("1w", 7 * 24 * H), ("1mo", 30 * 24 * H)):
        frm = end - span
        st, s, data, _ = fetch(run, "B-BTC_USDT", frm, end, "1")
        times = sorted(int(d["time"]) for d in data)
        expected = span // 60
        p5.append({"window": label, "from": iso(frm * 1000), "to": iso(end * 1000), "status": st, "s": s,
                   "expected_bars_approx": expected, "returned": len(times),
                   "first": iso(times[0]) if times else None, "last": iso(times[-1]) if times else None,
                   "kept_end": "newest" if times and times[-1] >= (end - 120) * 1000 else
                               "oldest" if times and times[0] <= (frm + 120) * 1000 else "?"})
    run.save({"P4": p4, "P5": p5})
    return 0


if __name__ == "__main__":
    sys.exit(main())
