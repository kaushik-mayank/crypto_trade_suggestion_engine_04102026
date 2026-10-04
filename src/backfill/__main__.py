"""One-time historical candle backfill (specs/01b-recorder.md, "One-time backfill").

    python -m backfill                       # everything, depth from the latest P1 result
    python -m backfill --only futures:B-BTC_USDT:60 --force

For each product x pair x served resolution, pulls the full available history:

* raw pages, verbatim, to ``data/backfill/raw/{product}/{pair}/{res}.jsonl.gz``
  (one envelope line per page: request URL, local send/receive times, body);
* bars to ``data/backfill/{product}/{pair}/{res}.parquet``: ``time_ms`` int64 and
  open/high/low/close/volume as ``decimal128(38,18)`` (parsed from the JSON text as
  Decimal; a value that does not fit raises instead of rounding);
* a per-series summary (bar count, first/last bar, internal gaps) to
  ``data/backfill/summary.json``.

Chunking: spot pages are ``SPOT_LIMIT`` bars (500 verified default, 1000 documented max,
re-checked at start-up); futures pages are ``FUTURES_CHUNK`` bars, far below the silent
30,000-bar cap found by P5. Pages overlap by one bar. Only bars closed at fetch time are
kept. Each series is written to temporary files and renamed when complete, so an
interrupted run never leaves a partial series that looks complete.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import logging
import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from cdx_http.client import HttpClient, HttpResult
from cdx_http.jsonutil import loads_decimal
from probes.common import DATA, FUTURES_CANDLES, PAIRS, SPOT_CANDLES, iso, now_ms

log = logging.getLogger("backfill")

M = 60_000
SPOT_RES = {"1m": M, "15m": 15 * M, "1h": 60 * M, "1d": 1440 * M}
FUT_RES = {"1": M, "5": 5 * M, "15": 15 * M, "30": 30 * M, "60": 60 * M, "240": 240 * M, "480": 480 * M,
           "1D": 1440 * M, "3D": 3 * 1440 * M, "1M": None}
FLOOR = 1262304000000          # 2010-01-01
FUTURES_CHUNK = 10_000
DEC = pa.decimal128(38, 18)
SCHEMA = pa.schema([("time_ms", pa.int64()), ("open", DEC), ("high", DEC), ("low", DEC), ("close", DEC),
                    ("volume", DEC)])
OUT = DATA / "backfill"


class Fetcher:
    def __init__(self, rate: float):
        self.http = HttpClient(host_rates={"api.coindcx.com": rate, "public.coindcx.com": rate})
        self.requests = 0

    def get(self, url, params) -> HttpResult:
        self.requests += 1
        res = self.http.request_with_retry("GET", url, params=params, max_attempts=8)
        if not res.ok:
            raise RuntimeError(f"HTTP {res.status} for {res.url}: {res.body[:200]!r}")
        return res


def spot_limit(f: Fetcher) -> int:
    """Use 1000 per page only if the endpoint really returns 1000 bars for a 1000-bar window."""
    end = now_ms() - 3 * 3600_000
    end -= end % M
    start = end - 999 * M
    rows = loads_decimal(f.get(SPOT_CANDLES, {"pair": "B-BTC_USDT", "interval": "1m", "startTime": start,
                                              "endTime": end, "limit": 1000}).body)
    got = sum(1 for r in rows if start <= int(r["time"]) <= end)
    log.info("spot limit check: asked 1000 bars, got %d in window", got)
    return 1000 if got == 1000 else 500


def p1_earliest() -> dict:
    files = sorted(glob.glob(str(DATA / "probe_results" / "p01_*.json")))
    if not files:
        raise SystemExit("no P1 result in data/probe_results - run `python -m probes.p01_history_depth` first")
    doc = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    out = {(s["product"], s["pair"], s["resolution"]): s["earliest_bar_ms"] for s in doc["series"]
           if "earliest_bar_ms" in s}
    log.info("using P1 result %s (%d series)", files[-1], len(out))
    return out


def backfill_series(f: Fetcher, product: str, pair: str, res: str, iv: int | None, earliest: int,
                    spot_page: int) -> dict:
    d = OUT / product / pair
    rawd = OUT / "raw" / product / pair
    d.mkdir(parents=True, exist_ok=True)
    rawd.mkdir(parents=True, exist_ok=True)
    pq_path, raw_path = d / f"{res}.parquet", rawd / f"{res}.jsonl.gz"
    pq_tmp, raw_tmp = pq_path.with_suffix(".parquet.tmp"), raw_path.with_suffix(".gz.tmp")

    now = now_ms()
    if iv is None:                                          # 1M: one request for everything
        windows = [(earliest, now)]
    else:
        page = spot_page if product == "spot" else FUTURES_CHUNK
        span = (page - 1) * iv
        windows, s = [], earliest
        while s < now:
            e = min(s + span, now)
            windows.append((s, e))
            s = e
            if e >= now:
                break

    stats = {"bars": 0, "first_ms": None, "last_ms": None, "gaps": 0, "missing_bars": 0,
             "largest_gap_bars": 0, "largest_gap_at": None, "duplicates_dropped": 0, "pages": 0,
             "out_of_order_pages": 0}
    last_t = None
    writer = pq.ParquetWriter(pq_tmp, SCHEMA, compression="zstd")
    try:
        with gzip.open(raw_tmp, "wt", encoding="utf-8") as raw:
            for s, e in windows:
                if product == "spot":
                    params = {"pair": pair, "interval": res, "startTime": s, "endTime": e, "limit": spot_page}
                    r = f.get(SPOT_CANDLES, params)
                    rows = loads_decimal(r.body)
                else:
                    params = {"pair": pair, "from": s // 1000, "to": e // 1000, "resolution": res, "pcode": "f"}
                    r = f.get(FUTURES_CANDLES, params)
                    rows = loads_decimal(r.body).get("data", [])
                raw.write(json.dumps({"request_url": r.url, "sent_at_ms": r.sent_at_ms,
                                      "received_at_ms": r.received_at_ms, "http_status": r.status},
                                     separators=(",", ":"))[:-1] + ',"body":' + r.body.decode("utf-8") + "}\n")
                stats["pages"] += 1
                times = [int(x["time"]) for x in rows]
                if times and times != sorted(times) and times != sorted(times, reverse=True):
                    stats["out_of_order_pages"] += 1
                batch = []
                for x in sorted(rows, key=lambda x: int(x["time"])):
                    t = int(x["time"])
                    closed = (t + iv <= r.received_at_ms) if iv else t != max(times)   # newest month is open
                    if not closed:
                        continue
                    if last_t is not None and t <= last_t:
                        stats["duplicates_dropped"] += 1
                        continue
                    if iv and last_t is not None and t - last_t < iv:
                        stats["irregular_short_steps"] = stats.get("irregular_short_steps", 0) + 1
                    elif iv and last_t is not None and t - last_t != iv:
                        missing = (t - last_t) // iv - 1
                        stats["gaps"] += 1
                        stats["missing_bars"] += missing
                        if missing > stats["largest_gap_bars"]:
                            stats["largest_gap_bars"], stats["largest_gap_at"] = missing, iso(last_t)
                    batch.append(x)
                    last_t = t
                if batch:
                    writer.write_table(pa.table({
                        "time_ms": pa.array([int(x["time"]) for x in batch], pa.int64()),
                        **{k: pa.array([x[k] for x in batch], DEC) for k in ("open", "high", "low", "close", "volume")},
                    }, schema=SCHEMA))
                    stats["bars"] += len(batch)
                    stats["first_ms"] = stats["first_ms"] or int(batch[0]["time"])
                    stats["last_ms"] = int(batch[-1]["time"])
    finally:
        writer.close()
    os.replace(pq_tmp, pq_path)
    os.replace(raw_tmp, raw_path)
    stats.update({"first": iso(stats["first_ms"]), "last": iso(stats["last_ms"]),
                  "parquet": str(pq_path.relative_to(DATA.parent)), "parquet_bytes": pq_path.stat().st_size,
                  "raw_bytes_gz": raw_path.stat().st_size})
    if iv:
        stats["expected_bars_first_to_last"] = (stats["last_ms"] - stats["first_ms"]) // iv + 1 if stats["bars"] else 0
    return stats


STRUCTURAL_GAP_MS = 7 * 86_400_000


def verify_series(path: Path, iv: int | None) -> dict:
    """Re-read a parquet and check it independently of the fetch-time bookkeeping."""
    t = pq.read_table(path, columns=["time_ms"]).column(0).to_pylist()
    out = {"bars": len(t), "first": iso(t[0]) if t else None, "last": iso(t[-1]) if t else None,
           "sorted_unique": t == sorted(set(t))}
    if iv is None or not t:
        return out
    grid = iv if iv <= 86_400_000 else None          # 3D has no fixed UTC grid (F-024)
    out["misaligned"] = sum(1 for x in t if grid and x % grid) if grid else None
    short = [(a, b) for a, b in zip(t, t[1:]) if b - a < iv]
    gaps = [(a, b) for a, b in zip(t, t[1:]) if b - a > iv]
    structural = [(a, b) for a, b in gaps if b - a >= STRUCTURAL_GAP_MS]
    cont_start = structural[-1][1] if structural else t[0]
    after = [(a, b) for a, b in gaps if a >= cont_start]
    missing_after = sum((b - a) // iv - 1 for a, b in after)
    expected_after = (t[-1] - cont_start) // iv + 1
    worst = max(after, key=lambda g: g[1] - g[0], default=None)
    out.update({
        "irregular_short_steps": len(short),
        "structural_gaps_ge_7d": [{"from": iso(a), "to": iso(b), "missing_bars": (b - a) // iv - 1} for a, b in structural],
        "continuous_from": iso(cont_start),
        "gaps_after_continuous_start": len(after),
        "missing_bars_after_continuous_start": missing_after,
        "completeness_after_continuous_start": round(1 - missing_after / expected_after, 6) if expected_after else None,
        "largest_gap_after_continuous_start": {"from": iso(worst[0]), "to": iso(worst[1]),
                                               "missing_bars": (worst[1] - worst[0]) // iv - 1} if worst else None,
    })
    return out


def verify_all() -> dict:
    out = {}
    for pair in PAIRS:
        for product, table in (("spot", SPOT_RES), ("futures", FUT_RES)):
            for res, iv in table.items():
                path = OUT / product / pair / f"{res}.parquet"
                out[f"{product}:{pair}:{res}"] = verify_series(path, iv) if path.exists() else {"error": "missing parquet"}
    (OUT / "verify.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backfill")
    ap.add_argument("--only", action="append", help="product:pair:res (repeatable)")
    ap.add_argument("--force", action="store_true", help="re-pull series that already have a parquet file")
    ap.add_argument("--rate", type=float, default=5.0,
                    help="requests/s per host (P2: api.coindcx.com allows 5000/60s; 5/s is 6%% of it)")
    ap.add_argument("--log", type=Path, help="also log to this file (for detached runs)")
    ap.add_argument("--verify", action="store_true", help="re-check every parquet; write data/backfill/verify.json")
    args = ap.parse_args(argv)
    if args.verify:
        for k, v in verify_all().items():
            print(k, {x: v.get(x) for x in ("bars", "sorted_unique", "misaligned", "continuous_from",
                                             "completeness_after_continuous_start", "irregular_short_steps", "error")})
        return 0
    handlers = [logging.StreamHandler()] if sys.stderr else []
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    earliest = p1_earliest()
    f = Fetcher(args.rate)
    page = spot_limit(f)
    series = [(p, pair, r, iv) for pair in PAIRS for p, table in (("spot", SPOT_RES), ("futures", FUT_RES))
              for r, iv in table.items()]
    if args.only:
        wanted = {tuple(o.split(":")) for o in args.only}
        series = [s for s in series if s[:3] in wanted]
    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary["_spot_page_bars"] = page
    for product, pair, res, iv in series:
        key = f"{product}:{pair}:{res}"
        if (OUT / product / pair / f"{res}.parquet").exists() and not args.force:
            log.info("skip %s (exists; --force to re-pull)", key)
            continue
        # Start every series at the earliest bar P1 found at ANY resolution of this
        # product/pair. P1's binary search can stop after an internal hole (it found spot
        # BTC 1m starting months before 15m/1h, which cannot be the true first bar), so a
        # full scan from the common floor is what establishes the real first bar and
        # counts every hole. Empty leading pages cost requests, nothing else.
        floor = [v for (p, pr, _), v in earliest.items() if p == product and pr == pair]
        start = min(floor) if floor else None
        if iv is None and start is None:
            start = FLOOR
        if start is None:
            log.error("no P1 depth for %s - skipped", key)
            summary[key] = {"error": "no P1 depth"}
            continue
        t0 = time.time()
        log.info("backfill %s from %s", key, iso(start))
        try:
            st = backfill_series(f, product, pair, res, iv, start, page)
        except Exception as e:  # keep going with the other series
            log.exception("failed %s", key)
            st = {"error": f"{type(e).__name__}: {e}"}
        st["seconds"] = round(time.time() - t0, 1)
        st["completed_utc"] = iso(now_ms())
        summary[key] = st
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info("done %s: %s bars, %s gaps (%s missing), %.0fs", key, st.get("bars"), st.get("gaps"),
                 st.get("missing_bars"), st["seconds"])
    log.info("total requests: %d", f.requests)
    return 0


if __name__ == "__main__":
    sys.exit(main())
