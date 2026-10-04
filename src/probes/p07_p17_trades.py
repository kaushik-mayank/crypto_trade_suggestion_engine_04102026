"""P7 — futures ``data/trades``: row cap, span, ordering, does ``limit`` work?
P17 — spot ``trade_history`` aggressor flag ``m``: populated and meaningful?
"""
from __future__ import annotations

import sys
from collections import Counter

from .common import FUTURES_TRADES, SPOT_BOOK, SPOT_TRADES, ProbeRun, iso, setup_logging


def _order(ts):
    if len(ts) < 2:
        return "n/a"
    if all(a >= b for a, b in zip(ts, ts[1:])):
        return "newest_first"
    if all(a <= b for a, b in zip(ts, ts[1:])):
        return "oldest_first"
    return "unordered"


def main() -> int:
    setup_logging()
    run = ProbeRun("p07_p17")
    # ---- P7 ----
    p7 = {}
    for label, params in (("default", {"pair": "B-BTC_USDT"}), ("limit_5", {"pair": "B-BTC_USDT", "limit": 5}),
                          ("limit_1000", {"pair": "B-BTC_USDT", "limit": 1000})):
        r = run.get(FUTURES_TRADES, params)
        rows = run.parse(r) if r.ok else []
        ts = [int(x["timestamp"]) for x in rows if isinstance(x, dict) and "timestamp" in x]
        p7[label] = {"status": r.status, "rows": len(rows), "order": _order(ts),
                     "span_s": (max(ts) - min(ts)) / 1000 if ts else None,
                     "newest": iso(max(ts)) if ts else None, "oldest": iso(min(ts)) if ts else None,
                     "keys": sorted(rows[0]) if rows else None,
                     "is_maker_values": dict(Counter(str(x.get("is_maker")) for x in rows))}
    p7["limit_param_works"] = p7["limit_5"]["rows"] == 5 or p7["limit_1000"]["rows"] not in (p7["default"]["rows"],)

    # ---- P17 ----
    r = run.get(SPOT_TRADES, {"pair": "B-BTC_USDT", "limit": 500})
    trades = run.parse(r)
    book = run.parse(run.get(SPOT_BOOK, {"pair": "B-BTC_USDT", "depth": 50}))
    best_ask = min(book["asks"], key=lambda k: float(k)) if book.get("asks") else None
    best_bid = max(book["bids"], key=lambda k: float(k)) if book.get("bids") else None
    ts = [int(t["T"]) for t in trades]
    m = Counter(repr(t.get("m")) for t in trades)
    p17 = {
        "status": r.status, "rows": len(trades), "order": _order(ts), "keys": sorted(trades[0]) if trades else None,
        "span_s": (max(ts) - min(ts)) / 1000 if ts else None,
        "m_distribution": dict(m),
        "m_types": sorted({type(t.get("m")).__name__ for t in trades}),
        "s_values_sample": sorted({str(t.get("s")) for t in trades})[:5],
        "best_bid_at_probe": best_bid, "best_ask_at_probe": best_ask,
        "note": "book snapshot is taken after the tape; the ask/bid cross-check is indicative only",
        "m_by_side_of_book": None,
    }
    if best_ask and best_bid:
        mid = (float(best_ask) + float(best_bid)) / 2
        above = Counter(repr(t.get("m")) for t in trades if float(t["p"]) > mid)
        below = Counter(repr(t.get("m")) for t in trades if float(t["p"]) < mid)
        p17["m_by_side_of_book"] = {"price_above_mid": dict(above), "price_below_mid": dict(below)}
    run.save({"P7": p7, "P17": p17})
    return 0


if __name__ == "__main__":
    sys.exit(main())
