"""P11 — do 1h and 1d bars align to UTC boundaries on both products?"""
from __future__ import annotations

import sys
from collections import Counter

from .common import FUTURES_CANDLES, PAIRS, SPOT_CANDLES, ProbeRun, iso, now_ms, setup_logging

H, D = 3_600_000, 86_400_000


def main() -> int:
    setup_logging()
    run = ProbeRun("p11")
    now = now_ms()
    out = []
    for pair in PAIRS:
        for product, res, iv, span in (("spot", "1h", H, 400 * H), ("spot", "1d", D, 400 * D),
                                       ("futures", "60", H, 400 * H), ("futures", "1D", D, 400 * D)):
            if product == "spot":
                rows = run.parse(run.get(SPOT_CANDLES, {"pair": pair, "interval": res, "startTime": now - span,
                                                        "endTime": now, "limit": 500}))
            else:
                rows = run.parse(run.get(FUTURES_CANDLES, {"pair": pair, "from": (now - span) // 1000,
                                                           "to": now // 1000, "resolution": res, "pcode": "f"}))
                rows = rows.get("data", [])
            times = [int(r["time"]) for r in rows]
            rem = Counter(t % iv for t in times)
            out.append({"pair": pair, "product": product, "resolution": res, "bars": len(times),
                        "remainders_ms": {str(k): v for k, v in rem.most_common(5)},
                        "all_aligned_utc": set(rem) == {0},
                        "offset_hours_if_misaligned": sorted({(t % iv) / H for t in times})[:5],
                        "sample_open": iso(times[0]) if times else None})
    run.save({"question": "Are 1h/1d bars aligned to UTC boundaries?", "series": out,
              "all_aligned": all(s["all_aligned_utc"] for s in out)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
