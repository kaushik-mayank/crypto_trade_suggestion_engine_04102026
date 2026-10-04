# 01b — Cold-Start Recorder

## Why this exists

Funding rate, predicted funding, mark price, long/short positioning, order-book depth and the
trade tape are **snapshot-only**. No endpoint serves their history. They cannot be backfilled
at any price. Every hour the recorder is not running is an hour of that data permanently lost.

At the hourly/daily swing horizon this is **much less urgent than it would be intraday** —
funding updates 3× per day, positioning at hourly resolution is ample, and the order book is
not an alpha input. So this is a modest poller, not a streaming service. But it should start
soon, because the clock on funding/positioning history only starts when it does.

**This is not the production pipeline.** It is a deliberately dumb, dependency-light process
whose only job is to never miss data and never corrupt it. The production ingestion layer
(Phase 11) will supersede it, and will be validated by parallel-running against it.

---

## Design principles

1. **Raw-first.** Store the unmodified response body plus local metadata. No parsing, no
   cleaning, no reshaping, no derived fields. If normalization is wrong, we re-run it
   downstream; if it is wrong *at ingest*, the data is gone.
2. **Append-only.** One JSON Lines file per stream per UTC day. Never rewrite a file.
3. **Crash-safe.** Flush after every write. A kill -9 mid-write may lose the last line;
   it must not corrupt earlier lines or the file.
4. **Resumable.** On start, detect the gap since the last recorded observation, log it
   explicitly, backfill whatever REST allows, and carry on. Assume the machine sleeps.
5. **Boring.** No framework, no database, no async complexity. A loop, an HTTP client,
   and a file writer. It should be readable in one sitting and run unattended for months.
6. **Identical on laptop and VPS.** No environment-specific paths or assumptions.

---

## Streams

Poll **only** `B-BTC_USDT`, `B-ETH_USDT`, `B-SOL_USDT` (plus the spot equivalents once P3
confirms their pair strings).

| # | Stream | Endpoint | Interval | Auth | Priority |
|---|---|---|---|---|---|
| S1 | Futures RT prices | `public.../v3/current_prices/futures/rt` | **60 s** | none | **critical** |
| S2 | Positioning | `/api/v1/.../data/stats?pair=` × 3 | **15 min** | signed | **critical** |
| S3 | Spot ticker | `/exchange/ticker` | 60 s | none | high |
| S4 | Futures instrument meta | `/derivatives/futures/data/instrument` × 3 | **6 h** | none | high |
| S5 | Futures order book | `public.../v3/orderbook/{pair}-futures/50` × 3 | 5 min | none | medium |
| S6 | Spot order book | `/market_data/orderbook?pair=&depth=50` × 3 *(lowercase — findings F-001)* | 5 min | none | medium |
| S7 | Candle gap-fill | `/market_data/candles`, `public.../candlesticks` | hourly | none | high |

**Request budget:** S1 60/hr + S2 12/hr + S3 60/hr + S4 0.5/hr + S5/S6 72/hr + S7 ~24/hr
≈ **230 requests/hour ≈ 0.064 req/s**. This is negligible, but confirm against P2's measured
limits before enabling S5/S6 — drop them first if anything is tight.

### Notes per stream

- **S1** is the one that matters most. It carries `fr`, `efr` and `mp` — the entire funding
  and basis dataset. 60s is far finer than needed for an 8h funding cycle; the redundancy is
  deliberate so that a sleeping laptop still catches most funding boundaries.
- **S1 filtering:** the response contains ~450 pairs. Store **only the three target rows**,
  plus the envelope `ts`/`vs`. Storing all 450 would be ~50× the volume for no benefit.
  Store the three rows **verbatim**, including `skw` and all timestamps.
- **S2** needs credentials and depends on probe P8 resolving how to call it. Until P8 passes,
  leave S2 disabled but wired, and log clearly that positioning history is not accumulating.
- **S4** exists because fees and tick sizes change. A change is itself a recordable event.
- **S5/S6** are low value at this horizon — they exist to build a spread/depth distribution
  for the Phase 8 risk model and Phase 9 slippage assumptions, not for alpha. Drop them
  before dropping anything else.
- **S7** closes candle gaps caused by downtime, and is the mechanism that makes laptop sleep
  survivable.

---

## Storage layout

```
data/raw/
├── futures_rt/2026-09-18.jsonl
├── positioning/2026-09-18.jsonl
├── spot_ticker/2026-09-18.jsonl
├── instrument_meta/2026-09-18.jsonl
├── futures_book/2026-09-18.jsonl
├── spot_book/2026-09-18.jsonl
└── _meta/
    ├── gaps.jsonl          # every detected downtime window
    ├── errors.jsonl        # every non-2xx, with code and body
    └── quality.jsonl       # hourly rollup for probe P13
```

### Record envelope

Every line, every stream, same shape:

```json
{
  "stream":        "futures_rt",
  "pair":          "B-BTC_USDT",
  "sent_at_ms":    1789670545000,
  "received_at_ms":1789670545780,
  "http_status":   200,
  "source_ts_ms":  1789670545780,
  "payload":       { "...verbatim slice, unmodified..." }
}
```

- `sent_at_ms` / `received_at_ms` are **local** clock. Together with `source_ts_ms` they give
  round-trip time and server skew for free (probe P15).
- `source_ts_ms` is the envelope timestamp from the response, **not** validated here.
  Validation is downstream. The recorder records; it does not judge.
- `payload` for S1 is the single pair's row object plus the envelope `ts` and `vs`.

### Deduplication

Dedupe key: `(stream, pair, source_ts_ms)`. If the same key is already the last written
record for that stream, skip the write. Do **not** scan the whole file — keep the last key
per stream in memory. Duplicate suppression is a nice-to-have; a duplicate is harmless,
a missing record is not. **When in doubt, write.**

---

## Gap handling

On startup and after any error streak:

1. Read the last `received_at_ms` per stream from today's and yesterday's files.
2. If the gap exceeds 2× that stream's poll interval, write a record to `_meta/gaps.jsonl`
   with `stream`, `gap_start_ms`, `gap_end_ms`, `duration_s`, and `reason`
   (`"startup" | "error_streak" | "unknown"`).
3. For candle-backed data, trigger S7 immediately to backfill the window.
4. For S1/S2/S5/S6 the gap is **unrecoverable**. Log it and move on. Do not interpolate.
   Do not synthesize. A gap in the record must remain visible as a gap forever, because
   downstream code has to be able to see it.

---

## Error handling

| Condition | Behaviour |
|---|---|
| `429` | Exponential backoff from 1s, cap 60s. Log. Halve the poll rate for that host for 10 min. |
| `503` | Backoff from 30s, cap 15 min. Distinct log line — this is documented maintenance, not our fault. |
| `500` | Backoff from 5s, cap 2 min. |
| `401` | **Stop that stream entirely**, log loudly. Do not retry a bad credential in a loop. |
| Connection / timeout | Backoff from 2s, cap 60s. |
| Malformed JSON | Write the raw bytes to `_meta/errors.jsonl` and continue. Never crash on a bad payload. |

**One stream failing must never stop the others.** Each stream runs on its own schedule with
its own error state.

---

## One-time backfill (run once, separately)

Not part of the loop. A standalone script, run after probe P1 establishes real history depth.

For each of the three pairs × {spot, futures} × every served resolution, pull the **entire**
available history into `data/backfill/{product}/{pair}/{resolution}.parquet`, chunked
according to P2's rate limits and P5's truncation findings.

Record in `docs/findings.md`: bar count, first and last timestamp, and the count of detected
internal gaps per series. **Re-run this whenever P1's depth answer changes.**

---

## Operational requirements

- Single command to start (`python -m recorder` or equivalent).
- Config in one file: pairs, intervals, enable/disable per stream, base data path.
- Log to stdout **and** a rotating file. Include stream, status, and latency on every poll at
  DEBUG; only errors and gaps at INFO.
- Print a one-line health summary every 15 minutes: per-stream records written, error count,
  last successful poll age.
- `Ctrl-C` performs a clean shutdown: flush, close files, write a final gap marker.

## Acceptance criteria

1. Runs unattended for ≥72 hours.
2. Survives `kill -9` mid-write: on restart, prior data is intact, the gap is recorded, and
   candle data is backfilled.
3. Survives a simulated network outage (block the hosts for 10 minutes): backs off, does not
   hot-loop, recovers automatically, records the gap.
4. Survives laptop sleep/wake without manual intervention.
5. `_meta/quality.jsonl` contains hourly rollups sufficient to answer probe P13.
6. Zero credentials appear anywhere in `data/`, logs, or committed files.
7. Total disk growth measured and reported — at these volumes it should be well under
   100 MB/month with S5/S6 enabled.
