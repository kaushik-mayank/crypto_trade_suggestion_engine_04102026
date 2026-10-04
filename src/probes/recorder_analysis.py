"""P13 / P14 / P15 / P16 — analyses over the recorder's own data (``data/raw``).

    python -m probes.recorder_analysis                 # all four, whole recording
    python -m probes.recorder_analysis --since 2026-09-19T10:26:00Z
    python -m probes.recorder_analysis --depth-param   # P14 live: which depth values work

Reads plain and gzipped day files. Nothing here modifies the recorded data.

P13  per stream per UTC day: records, polls missed vs schedule, duplicate source
     timestamps, out-of-order arrivals, S1 rows failing freshness (btST/bmST age >
     FRESH_MS) or schema (missing fields), HTTP errors by code, worst contiguous gap;
     candles: missing bars across the union of recorded pages.
P14  spread (bps) and cumulative depth within 10/25/50 bps of mid, per product/pair;
     levels returned vs requested.
P15  server clock skew: source timestamp minus local request midpoint, and its drift.
P16  spot vs futures: 24h quote volume ratio and depth ratio.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from recorder.storage import DAY_RE, iter_records

from .common import DATA, FUTURES_BOOK, PAIRS, SPOT_BOOK, ProbeRun, iso, setup_logging

RAW = DATA / "raw"
FRESH_MS = 60_000
BANDS = (1, 2, 5, 10, 25, 50)   # bps around mid; a band beyond the book's reach is None
S1_FIELDS = ("fr", "efr", "mp", "ls", "h", "l", "v", "pc", "mkt", "btST", "bmST", "ctRT", "cmRT", "skw")
INTERVAL_S = {"futures_rt": 60, "spot_ticker": 60, "instrument_meta": 21600, "futures_book": 300, "spot_book": 300,
              "positioning": 900}
CANDLE_IV = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000, "1d": 86_400_000,
             "1": 60_000, "5": 300_000, "60": 3_600_000, "1D": 86_400_000}


def _parse_since(s: str | None) -> int:
    if not s:
        return 0
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def records(stream: str, since: int = 0):
    d = RAW / stream
    if not d.is_dir():
        return
    files = sorted((p for p in d.iterdir() if DAY_RE.match(p.name)), key=lambda p: (p.name[:10], p.suffix == ".gz"))
    for f in files:
        for r in iter_records(f):
            if r.get("received_at_ms", 0) >= since:
                yield r


def meta(name: str, since: int = 0) -> list[dict]:
    p = RAW / "_meta" / f"{name}.jsonl"
    if not p.exists():
        return []
    return [r for r in iter_records(p) if r.get("at_ms", r.get("gap_end_ms", r.get("hour_start_ms", 0))) >= since]


def _stats(xs):
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * (len(xs) - 1)))]  # noqa: E731
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 3), "min": round(xs[0], 3), "p05": round(q(.05), 3),
            "median": round(q(.5), 3), "p95": round(q(.95), 3), "max": round(xs[-1], 3)}


def _day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


# ---- P13 ------------------------------------------------------------------------------

def p13(since: int) -> dict:
    out = {"fresh_window_ms": FRESH_MS, "streams": {}, "candles": {}}
    for stream, iv_s in INTERVAL_S.items():
        by_key = defaultdict(list)
        for r in records(stream, since):
            by_key[r.get("pair")].append(r)
        if not by_key:
            continue
        per_day = defaultdict(lambda: Counter())
        worst = {"gap_s": 0}
        for pair, recs in by_key.items():
            last_src = None
            prev_recv = None
            for r in recs:
                day = _day(r["received_at_ms"])
                c = per_day[day]
                c["records"] += 1
                if r.get("http_status") != 200:
                    c["non_200_records"] += 1
                if prev_recv is not None:
                    if r["received_at_ms"] < prev_recv:
                        c["out_of_order"] += 1
                    gap = (r["received_at_ms"] - prev_recv) / 1000
                    if gap > 2 * iv_s:
                        c["gaps_over_2x_interval"] += 1
                        c["missed_polls"] += int(gap // iv_s) - 1
                    if gap > worst["gap_s"]:
                        worst = {"gap_s": gap, "pair": pair, "from": iso(prev_recv), "to": iso(r["received_at_ms"])}
                src = r.get("source_ts_ms")
                if src is not None and src == last_src:
                    c["duplicate_source_ts"] += 1
                last_src = src
                prev_recv = r["received_at_ms"]
                if stream == "futures_rt":
                    row = (r.get("payload") or {}).get("row") or {}
                    ts = row.get("btST", row.get("bmST"))
                    if ts is None or r["received_at_ms"] - ts > FRESH_MS:
                        c["rows_failing_freshness"] += 1
                    if any(f not in row for f in S1_FIELDS):
                        c["rows_failing_schema"] += 1
        n_keys = len(by_key)
        out["streams"][stream] = {"pairs": sorted(str(k) for k in by_key), "per_day": {d: dict(c) for d, c in sorted(per_day.items())},
                                  "expected_polls_per_day_per_pair": 86400 // iv_s, "pairs_count": n_keys,
                                  "worst_gap": worst}
    errors = meta("errors", since)
    out["errors_by_stream_kind_status"] = dict(Counter(f"{e.get('stream')}|{e.get('kind')}|{e.get('http_status')}"
                                                       for e in errors))
    q = meta("quality", since)
    fails = defaultdict(Counter)
    attempts = Counter()
    for row in q:
        attempts[row["stream"]] += row.get("attempts", 0)
        for k, v in (row.get("failures") or {}).items():
            fails[row["stream"]][k] += v
    out["http_error_rate_by_stream"] = {s: {"attempts": attempts[s], "failures": dict(fails[s]),
                                            "failure_rate": round(sum(fails[s].values()) / attempts[s], 5) if attempts[s] else None}
                                        for s in attempts}
    out["gap_records"] = [g for g in meta("gaps", since) if "reason" in g or g.get("event") == "candle_gap_exceeds_fill_window"]
    out["startups"] = [g for g in meta("gaps", since) if g.get("event") in ("startup", "shutdown")]
    # candles: union of all recorded bars per series
    for d in sorted(RAW.iterdir()) if RAW.is_dir() else []:
        if "_candles_" not in d.name:
            continue
        res = d.name.split("_candles_")[1]
        iv = CANDLE_IV.get(res)
        bars = defaultdict(set)
        for r in records(d.name, since):
            p = r.get("payload")
            rows = p if isinstance(p, list) else (p.get("data") if isinstance(p, dict) else None) or []
            for b in rows:
                if isinstance(b, dict) and isinstance(b.get("time"), int) and b["time"] + iv <= r["received_at_ms"]:
                    bars[r["pair"]].add(b["time"])
        for pair, ts in bars.items():
            ts = sorted(ts)
            missing = sum((b - a) // iv - 1 for a, b in zip(ts, ts[1:]) if b - a != iv)
            out["candles"][f"{d.name}:{pair}"] = {"bars": len(ts), "first": iso(ts[0]) if ts else None,
                                                   "last": iso(ts[-1]) if ts else None, "missing_bars": missing,
                                                   "misaligned": sum(1 for t in ts if t % iv)}
    return out


# ---- P14 / P16 ---------------------------------------------------------------------------

def _book_metrics(payload) -> dict | None:
    bids = {Decimal(k): Decimal(v) for k, v in (payload.get("bids") or {}).items()}
    asks = {Decimal(k): Decimal(v) for k, v in (payload.get("asks") or {}).items()}
    if not bids or not asks:
        return None
    bb, ba = max(bids), min(asks)
    mid = (bb + ba) / 2
    m = {"levels_bid": len(bids), "levels_ask": len(asks), "spread_bps": float((ba - bb) / mid * 10000),
         "crossed": ba <= bb}
    # How far the returned levels reach from mid on each side. A band wider than that reach
    # would silently measure "the whole returned book", so it is reported as None instead.
    reach = float(min(mid - min(bids), max(asks) - mid) / mid * 10000)
    m["reach_bps"] = reach
    for band in BANDS:
        lo, hi = mid * (1 - Decimal(band) / 10000), mid * (1 + Decimal(band) / 10000)
        m[f"depth_usdt_{band}bps"] = (float(sum(p * q for p, q in bids.items() if p >= lo) +
                                            sum(p * q for p, q in asks.items() if p <= hi))
                                      if reach >= band else None)
    m["depth_usdt_whole_book"] = float(sum(p * q for p, q in bids.items()) + sum(p * q for p, q in asks.items()))
    m["book_span_bps"] = float((max(asks) - min(bids)) / mid * 10000)
    return m


def p14(since: int) -> dict:
    out = {}
    for stream in ("futures_book", "spot_book"):
        per_pair = defaultdict(list)
        for r in records(stream, since):
            mm = _book_metrics(r.get("payload") or {})
            if mm:
                mm["hour"] = datetime.fromtimestamp(r["received_at_ms"] / 1000, tz=UTC).hour
                per_pair[r["pair"]].append(mm)
        out[stream] = {}
        for pair, ms in per_pair.items():
            out[stream][pair] = {
                "snapshots": len(ms), "distinct_utc_hours": len({m["hour"] for m in ms}),
                "levels_bid": _stats([m["levels_bid"] for m in ms]), "levels_ask": _stats([m["levels_ask"] for m in ms]),
                "spread_bps": _stats([m["spread_bps"] for m in ms]), "crossed_books": sum(m["crossed"] for m in ms),
                "book_span_bps": _stats([m["book_span_bps"] for m in ms]),
                "reach_bps": _stats([m["reach_bps"] for m in ms]),
                "depth_usdt_whole_book": _stats([m["depth_usdt_whole_book"] for m in ms]),
                **{f"depth_usdt_{b}bps": _stats([m[f"depth_usdt_{b}bps"] for m in ms if m[f"depth_usdt_{b}bps"] is not None])
                   for b in BANDS},
                **{f"band_{b}bps_covered_fraction": round(sum(m[f"depth_usdt_{b}bps"] is not None for m in ms) / len(ms), 3)
                   for b in BANDS},
            }
    return out


def p14_depth_param() -> dict:
    run = ProbeRun("p14_depth")
    out = {"spot": {}, "futures": {}}
    for depth in (1, 5, 10, 20, 50, 100, 200, 500):
        r = run.get(SPOT_BOOK, {"pair": "B-BTC_USDT", "depth": depth}, retry=False)
        b = run.parse(r) if r.ok else {}
        out["spot"][depth] = {"status": r.status, "bids": len(b.get("bids", {})), "asks": len(b.get("asks", {})),
                              "body": None if r.ok else r.body[:200].decode(errors="replace")}
    for depth in (5, 10, 20, 50, 100):
        r = run.get(FUTURES_BOOK.format(pair="B-BTC_USDT", depth=depth), retry=False)
        b = run.parse(r) if r.ok else {}
        out["futures"][depth] = {"status": r.status, "bids": len(b.get("bids", {})), "asks": len(b.get("asks", {})),
                                 "body": None if r.ok else r.body[:200].decode(errors="replace")}
    run.save(out)
    return out


def p16(since: int, p14_out: dict) -> dict:
    last_spot, last_fut = {}, {}
    for r in records("spot_ticker", since):
        last_spot[r["pair"]] = r
    for r in records("futures_rt", since):
        last_fut[r["pair"]] = r
    out = {}
    for pair in PAIRS:
        s, f = last_spot.get(pair), last_fut.get(pair)
        if not s or not f:
            continue
        sv = Decimal(str(s["payload"]["volume"]))      # ticker volume: quote (see findings)
        fv = Decimal(str(f["payload"]["row"].get("v", 0)))
        d = {"spot_24h_volume_ticker": str(sv), "futures_24h_v_quote": str(fv),
             "spot_over_futures_volume": round(float(sv / fv), 4) if fv else None,
             "as_of": iso(s["received_at_ms"])}
        sw = p14_out.get("spot_book", {}).get(pair, {}).get("depth_usdt_whole_book", {}).get("median")
        fw = p14_out.get("futures_book", {}).get(pair, {}).get("depth_usdt_whole_book", {}).get("median")
        d["median_whole_book_depth_spot_over_futures"] = round(sw / fw, 4) if sw and fw else None
        d["note"] = "band ratios are None where either book does not reach that band (50 levels)"
        for b in BANDS:
            sp = p14_out.get("spot_book", {}).get(pair, {}).get(f"depth_usdt_{b}bps", {}).get("median")
            fu = p14_out.get("futures_book", {}).get(pair, {}).get(f"depth_usdt_{b}bps", {}).get("median")
            d[f"median_depth_{b}bps_spot_over_futures"] = round(sp / fu, 4) if sp and fu else None
        out[pair] = d
    return out


# ---- P15 -----------------------------------------------------------------------------------

def p15(since: int) -> dict:
    out = {}
    for stream in ("futures_rt", "futures_book", "spot_book", "spot_ticker"):
        skews, by_hour, rtts = [], defaultdict(list), []
        seen = set()
        for r in records(stream, since):
            src = r.get("source_ts_ms")
            if src is None or (src, r.get("received_at_ms")) in seen:
                continue
            seen.add((src, r["received_at_ms"]))
            mid = (r["sent_at_ms"] + r["received_at_ms"]) / 2
            skews.append(src - mid)
            rtts.append(r["received_at_ms"] - r["sent_at_ms"])
            by_hour[r["received_at_ms"] // 3_600_000].append(src - mid)
        hourly = [statistics.fmean(v) for _, v in sorted(by_hour.items())]
        out[stream] = {"skew_ms": _stats(skews), "rtt_ms": _stats(rtts),
                       "hourly_mean_skew_first_last": [round(hourly[0], 1), round(hourly[-1], 1)] if hourly else None,
                       "hourly_mean_skew_range": [round(min(hourly), 1), round(max(hourly), 1)] if hourly else None,
                       "note": "spot_ticker timestamp has 1 s resolution" if stream == "spot_ticker" else
                               "futures_rt ts is the envelope ts (payload-generation time), not a per-row tick time"
                               if stream == "futures_rt" else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO UTC start, e.g. 2026-09-19T10:26:00Z")
    ap.add_argument("--depth-param", action="store_true")
    args = ap.parse_args()
    setup_logging()
    if args.depth_param:
        p14_depth_param()
        return 0
    since = _parse_since(args.since)
    run = ProbeRun("recorder_analysis", capture=False)
    r14 = p14(since)
    run.save({"since": iso(since) if since else None, "P13": p13(since), "P14": r14, "P15": p15(since),
              "P16": p16(since, r14)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
