"""P12 — does aggregating fine bars reproduce native coarse bars exactly?

Tests (all over ~10 days, closed bars only):
  spot     15 x 1m -> native 15m      (spec test 1)
  futures  60 x 1m -> native 60       (spec test 2, exact)
  futures   3 x 5m -> native 15       (the spec says 12 x 5m -> spot 15m; 12 x 5m is an hour,
                                       and P4b found futures serve 15 natively, so exact equality is testable)
  futures   4 x 60 -> native 240      (futures 4h is native, P4b)
  spot      4 x 1h -> futures 240     (shape only: different books; spot 4h is not served)
Also: are empty (zero-trade) minutes omitted or returned with zero volume?

Rule: open=first open, high=max, low=min, close=last close, volume=sum. Decimal throughout.
"""
from __future__ import annotations

import sys
from decimal import Decimal

from .common import FUTURES_CANDLES, PAIRS, SPOT_CANDLES, ProbeRun, iso, now_ms, setup_logging

M = 60_000
DAYS = 10
CHUNK = 499


def fetch(run, product, pair, res, iv, start, end) -> dict[int, dict]:
    out = {}
    s = start
    while s < end:
        e = min(s + (CHUNK - 1) * iv, end)
        if product == "spot":
            rows = run.parse(run.get(SPOT_CANDLES, {"pair": pair, "interval": res, "startTime": s, "endTime": e,
                                                    "limit": CHUNK}))
        else:
            rows = run.parse(run.get(FUTURES_CANDLES, {"pair": pair, "from": s // 1000, "to": e // 1000,
                                                       "resolution": res, "pcode": "f"})).get("data", [])
        for r in rows:
            t = int(r["time"])
            if start <= t < end:
                out[t] = {k: Decimal(str(r[k])) for k in ("open", "high", "low", "close", "volume")}
        s = e + iv
    return out


def aggregate(fine: dict[int, dict], start: int, iv: int, n: int) -> tuple[dict | None, int]:
    parts = [fine[start + i * iv] for i in range(n) if start + i * iv in fine]
    if not parts:
        return None, n
    return {"open": parts[0]["open"], "high": max(p["high"] for p in parts), "low": min(p["low"] for p in parts),
            "close": parts[-1]["close"], "volume": sum(p["volume"] for p in parts)}, n - len(parts)


def compare(fine, coarse, fine_iv, n) -> dict:
    res = {"windows": 0, "exact_match": 0, "ohlc_match": 0, "windows_with_missing_fine_bars": 0,
           "field_mismatch_counts": dict.fromkeys(("open", "high", "low", "close", "volume"), 0),
           "max_volume_rel_diff": Decimal(0), "examples": []}
    for t, c in sorted(coarse.items()):
        agg, missing = aggregate(fine, t, fine_iv, n)
        if agg is None:
            continue
        res["windows"] += 1
        if missing:
            res["windows_with_missing_fine_bars"] += 1
        diff = [k for k in agg if agg[k] != c[k]]
        for k in diff:
            res["field_mismatch_counts"][k] += 1
        if c["volume"]:
            res["max_volume_rel_diff"] = max(res["max_volume_rel_diff"], abs(agg["volume"] - c["volume"]) / c["volume"])
        if not diff:
            res["exact_match"] += 1
        if not [k for k in diff if k != "volume"]:
            res["ohlc_match"] += 1
        if diff and len(res["examples"]) < 5:
            res["examples"].append({"window": iso(t), "missing_fine": missing, "native": c, "aggregated": agg})
    res["exact_match_rate"] = round(res["exact_match"] / res["windows"], 4) if res["windows"] else None
    res["ohlc_match_rate"] = round(res["ohlc_match"] / res["windows"], 4) if res["windows"] else None
    return res


def shape(a: dict, b: dict, iv: int) -> dict:
    """Correlation of close-to-close returns between two coarse series."""
    ts = sorted(set(a) & set(b))
    ra = [float(a[y]["close"] / a[x]["close"] - 1) for x, y in zip(ts, ts[1:]) if y - x == iv]
    rb = [float(b[y]["close"] / b[x]["close"] - 1) for x, y in zip(ts, ts[1:]) if y - x == iv]
    n = len(ra)
    if n < 3:
        return {"n": n}
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return {"n": n, "return_correlation": round(cov / (va * vb), 5) if va and vb else None,
            "mean_abs_close_diff_bps": round(sum(abs(float(a[t]["close"] / b[t]["close"] - 1)) for t in ts) / len(ts) * 1e4, 3)}


def missing_minutes(fine: dict, start: int, end: int, iv: int) -> dict:
    expected = (end - start) // iv
    zero_vol = sum(1 for v in fine.values() if v["volume"] == 0)
    gaps = [t for t in range(start, end, iv) if t not in fine]
    return {"expected": expected, "present": len(fine), "missing": len(gaps), "zero_volume_bars_present": zero_vol,
            "first_missing": [iso(t) for t in gaps[:5]]}


def main() -> int:
    setup_logging()
    run = ProbeRun("p12", capture=False)
    now = now_ms()
    end = now - now % (240 * M) - 240 * M           # closed 4h boundary, one full 4h back for safety
    start = end - DAYS * 1440 * M
    out = {"window": [iso(start), iso(end)], "pairs": {}}
    for pair in PAIRS:
        s1m = fetch(run, "spot", pair, "1m", M, start, end)
        s15 = fetch(run, "spot", pair, "15m", 15 * M, start, end)
        s1h = fetch(run, "spot", pair, "1h", 60 * M, start, end)
        f1 = fetch(run, "futures", pair, "1", M, start, end)
        f5 = fetch(run, "futures", pair, "5", 5 * M, start, end)
        f15 = fetch(run, "futures", pair, "15", 15 * M, start, end)
        f60 = fetch(run, "futures", pair, "60", 60 * M, start, end)
        f240 = fetch(run, "futures", pair, "240", 240 * M, start, end)
        spot4h = {t: aggregate(s1h, t, 60 * M, 4)[0] for t in range(start, end, 240 * M)}
        spot4h = {t: v for t, v in spot4h.items() if v}
        out["pairs"][pair] = {
            "spot_15x1m_vs_15m": compare(s1m, s15, M, 15),
            "futures_60x1m_vs_60": compare(f1, f60, M, 60),
            "futures_3x5m_vs_15": compare(f5, f15, 5 * M, 3),
            "futures_4x60_vs_240": compare(f60, f240, 60 * M, 4),
            "spot_4x1h_vs_futures_240_shape": shape(spot4h, f240, 240 * M),
            "spot_1m_continuity": missing_minutes(s1m, start, end, M),
            "futures_1m_continuity": missing_minutes(f1, start, end, M),
        }
        print(pair, {k: (v.get("exact_match_rate"), v.get("windows")) for k, v in out["pairs"][pair].items()
                     if "windows" in v}, flush=True)
    run.save(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
