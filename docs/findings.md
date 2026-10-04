# Findings

Append-only record of verified facts. Each entry: UTC date, exact request, raw evidence,
resolved answer. **⚠ CORRECTION** marks an entry that contradicts `specs/00-capability-map.md`
(the spec has been updated to match, per CLAUDE.md working rules).

These entries come from **recorder development**, not from the formal probes in
`specs/01-verification.md`. Where one overlaps a probe, it is marked *preliminary*: the
probe still has to run in full.

---

## F-001 ⚠ CORRECTION — spot order book: `Depth` (capital D) is rejected; `depth` works

**Date:** 2026-09-18 ~12:38 UTC
**Requests** (identical results on `api.coindcx.com` and `public.coindcx.com`):

| Query | Status | Levels (bids/asks) |
|---|---|---|
| `/market_data/orderbook?pair=B-BTC_USDT` | 200 | 50 / 50 |
| `...&Depth=50` | **422** | — |
| `...&Depth=20` | **422** | — |
| `...&Depth=200` | **422** | — |
| `...&depth=50` | 200 | 50 / 50 |
| `...&depth=20` | 200 | 20 / 20 |

422 body: `{"code":422,"errorCode":"BFF-SO-004","message":"Depth is not allowed","status":"error"}`
(fixture `tests/fixtures/spot_book_Depth_422_2026-09-18T1237Z.json`).

**Answer:** the depth parameter is lowercase `depth`. With no parameter, the response has
50 levels. Capability map §3 and hazard §5 #8 claimed the opposite, and the old
hazard would have produced a 422 on every spot-book poll. Whether `depth=100/200`
works is still open and belongs to **P14**.

**Nearby things that may also be wrong:** the documented depth set {1,5,10,20,50,100,200}
is unverified above 50.

## F-002 — spot `/market_data/*` is served on both hosts

**Date:** 2026-09-18 ~12:38 UTC. `GET {api,public}.coindcx.com/market_data/candles?pair=B-BTC_USDT&interval=1h&limit=3`
returned the same bars on both hosts. The only difference was the volume of the still-open
bar (351.67586 vs 351.80731, fetched ~1s apart). The order book behaves the same on both.
The recorder uses `api.coindcx.com`, as in the capability map.

## F-003 — number formatting: re-serializing parsed JSON is not verbatim

**Date:** 2026-09-18. `current_prices/futures/rt` sends `"fr":0.00007903`. Python's
`json.loads` then `json.dumps` produces `7.903e-05`. The value is the same, but the bytes differ.
**Consequence:** the recorder does not re-serialize payloads. It slices the exact source
text out of the response (`cdx_http/jsonutil.py`). This is covered by
`test_float_roundtrip_would_not_be_verbatim` and `test_futures_rt_stores_three_verbatim_rows`.

## F-004 ⚠ CORRECTION — `skw` is NOT a per-row health flag

**Date:** 2026-09-18, 12:30–12:56 UTC.
**Request:** `GET https://public.coindcx.com/market_data/v3/current_prices/futures/rt`,
sampled repeatedly (recorder smoke runs plus 6 samples 8 s apart).

**Evidence:**

| Time (UTC) | fresh rows (`btST` < 60 s old) | `skw` across all fresh rows |
|---|---|---|
| 12:37:25 (fixture) | — | BTC/ETH/SOL all `0` |
| 12:50–12:54 (recorder) | — | BTC/ETH/SOL moved together `1 → 3 → 10 → 33` |
| 12:55:03 | 496 | **all 496 = 40** |
| 12:55:20 – 12:55:54 (5 samples) | 488–497 | all = 40 |
| 12:56:03 | 495 | **all 495 = 24** |

Recorded live row, B-BTC_USDT, `btST` 0.7 s old: `"skw":10`.
At 12:55:03 the stale rows (btST ≥ 60 s) showed mixed values: 40, 4, 10, 1, 0, 36, 46, 2, −4, −20, 21, 64.

**Answer:** `skw` is a single value that the server stamps on every row it refreshes in a
snapshot. It drifts over time (seen 0…40, and negatives on frozen rows). Stale rows
keep whatever value was current when they froze. That is why the original verification
saw non-zero values only on frozen rows: at that moment the live value happened to be 0.
The name suggests a clock skew or latency figure, but the exact meaning is **unknown**.

**Impact:**
- Capability map §4 said "`skw` 0 = live, non-zero = frozen row". **Wrong.** Updated.
- **CLAUDE.md hard invariant #3 ("`skw != 0` means reject the row") is wrong.** Applied
  as written, it would reject every live BTC/ETH/SOL row whenever the shared value is
  non-zero, which was most of the time during these samples. Freshness has to be judged
  by `btST`/`bmST` age (invariant #2) alone. **CLAUDE.md is not yet edited. That decision is
  the project owner's.** See Open questions.
- The recorder is unaffected: it stores rows raw and never rejects on `skw`. Its P13
  quality rollup records `skw_min`/`skw_max` per hour, which will show how `skw` behaves
  over the long run.

## F-005 — response shapes and units observed while building the recorder

**Date:** 2026-09-18 ~12:37 UTC. Fixtures are in `tests/fixtures/` (see its README).

- `current_prices/futures/rt`: `{"ts": <ms>, "vs": <int>, "prices": {<pair>: {...}}}`, **539 rows**
  (the map said ~450). The stale, schema-incomplete `B-NKN_USDT` row is still present:
  `{fr, mp, efr, bmST, cmRT, skw}`, `bmST` = 2026-02-16.
- `/exchange/ticker`: array of **997** rows. Target rows use `market` = `BTCUSDT`/`ETHUSDT`/`SOLUSDT`.
  All values are strings. `timestamp` is **epoch seconds** (e.g. `1789735075`).
- `/exchange/v1/markets_details`: BTC/ETH/SOL vs USDT each appear once, as
  `pair=B-{X}_USDT`, `ecode=B`, `status=active`. *Preliminary for P3*: the full ecode
  survey has not been done.
- Spot order book: `{"timestamp": <ms>, "bids": {...}, "asks": {...}}`.
- Futures order book `/market_data/v3/orderbook/B-BTC_USDT-futures/50`: `{"ts": <ms>, "vs": <int>, "asks": {...}, "bids": {...}}`.
  Price keys have inconsistent precision (`"78058"`, `"78057.5"`). Key order is not sorted.
- Futures candlesticks (`resolution=60`, 3h window): `{"s":"ok","data":[...]}`, **ascending**,
  `time` in **ms** even though `from`/`to` are in seconds. *Preliminary for P4* (one sample only).
  BTC 1h volume ≈ 2,600–5,900, so probably **base (BTC)**. *Preliminary for P6*: not yet cross-checked.
- Spot candles `limit=3`: descending, ms bar-open times. Consistent with the map.
- Instrument (`B-BTC_USDT`): `taker_fee 0.059`, `maker_fee 0.0236`, `min_notional 60.0`,
  `price_increment 0.1`, `quantity_increment 0.001`. Consistent with the map. Note that
  `max_leverage_long`/`_short` = **20.0** at the top level, while the map lists a 100x tier. Not investigated.

## F-006 — recorder smoke test (not the 72 h acceptance run)

**Date:** 2026-09-18 12:46–12:54 UTC, scratch data directory, live API.
- Run 1, 150 s: 35 tasks, 13 streams, every stream recorded, **0 errors, 0 non-2xx**.
  Latency 60–100 ms per request.
- Run 2 was killed with `taskkill /F` (the Windows equivalent of kill -9) after 75 s and restarted 90 s later:
  prior data intact (0 unparseable lines), `prior_shutdown_clean:false` recorded, and every
  task resumed from its last record. The outage (~108 s for S1) was under the 2×interval
  threshold, so no gap record was expected. Gap records are covered by unit tests
  (`test_restart_records_startup_gap_and_prior_unclean`, `test_sleep_gap_reason_unknown_and_error_streak`).
- Run 4 was stopped with a simulated Ctrl-C: exit 0, `shutdown` marker written, partial-hour quality rollup flushed.
- Futures-book skew (source `ts` − local request midpoint): mean −73 ms, range −134…−32 ms
  (a few samples only; P15 needs ~200).

**Not yet demonstrated:** the ≥72 h run, a 10-minute network block, laptop sleep/wake,
a live gap record, and measured monthly disk growth (acceptance criteria 1, 3, 4, 7).
Those are now covered in F-020 onwards.

---

# Phase 1 probes (formal runs, 2026-09-19)

Every probe writes its full result, including the exact requests, to
`data/probe_results/<probe>_<UTC stamp>.json`. Probes that capture raw responses keep them under
`data/raw/probe/<probe>/<stamp>/`. The code is in `src/probes/`. The numbers below are quoted
from those files.

## F-007 — P3: spot markets for BTC / ETH / SOL ✅

**Run:** 2026-09-19 10:29 UTC, `GET https://api.coindcx.com/exchange/v1/markets_details`
(995 markets, ecodes `B`, `G`, `I`, `KC`). Result: `p03_20260919T102907Z.json`.

| pair | ecode | status | min_notional | step | base prec. | target prec. | min_quantity |
|---|---|---|---|---|---|---|---|
| `B-BTC_USDT` | B | active | 5 | 0.00001 | 2 | 5 | 0.00001 |
| `B-ETH_USDT` | B | active | 5 | 0.0001 | 2 | 4 | 0.0001 |
| `B-SOL_USDT` | B | active | 5 | 0.001 | 2 | 3 | 0.001 |

Other ecode variants of these assets: `I-BTC_INR`, `I-ETH_INR`, `I-SOL_INR` (all ecode I,
INR-quoted), plus `B-ETH_BTC`. **There are no non-B USDT markets.** The ticker `market`
names are `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, so the recorder's provisional config is now confirmed.
**P9 is only possible via INR:** `I-X_INR ÷ I-USDT_INR` compared against `B-X_USDT` (see F-015).

## F-008 ⚠ CORRECTION — P4 / P4b: futures candles are ascending, and serve many more resolutions than documented

**Run:** 2026-09-19 10:29 UTC. Results: `p04_p05_20260919T102908Z.json`, `p04b_20260919T102949Z.json`.

- **P4:** a 24h `resolution=60` window returned 25 bars, **ascending**, times in **ms**, `s:"ok"`.
  - A future window and a pre-listing (2010) window both return `200` with `{"s":"no_data","data":[]}`.
  - `from > to` returns **400** `{"code":400,"message":"Invalid Request.","status":"error"}`.
  - `from == to` returns the single bar that contains that instant, which is the still-open bar.
- **P4b resolution survey** (B-BTC_USDT, 20 bars each):
  - **Futures serve `1, 5, 15, 30, 60, 240, 480, 1D, 3D, 1M`**, all with exact spacing and UTC alignment.
  - `3, 120, 360, 720, 1W` return **400**.
  - The capability map (§3, §9) listed only `1, 5, 60, 1D`. **Futures 4h (`240`) is native**, so it needs no aggregation.
  - **Spot serves only `1m, 15m, 1h, 1d`.** Everything else returns 422 `"interval must be one of [1m, 15m, 1h, 1d]"`. That matches the map.
  - Spot 4h still has to be aggregated from 1h. P12 shows that aggregation is exact.

## F-009 ⚠ NEW HAZARD — P5: futures candlesticks silently truncate at 30,000 bars and keep the OLDEST

**Run:** 2026-09-19 10:29 UTC, `resolution=1`, B-BTC_USDT, windows ending 10:00Z.

| window | expected | returned | kept |
|---|---|---|---|
| 1h | 61 | 61 | all |
| 1d | 1,441 | 1,441 | all |
| 3d | 4,321 | 4,321 | all |
| 1w | 10,081 | 10,081 | all |
| 1 month | 43,201 | **30,000** | **oldest** (2026-08-20T10:00 → 2026-09-10T05:59) |

The truncation is **silent**: HTTP 200, `s:"ok"`, and nothing to indicate it. Because the oldest end is kept,
a naive "request everything since X" returns what looks like complete history that simply stops
9 days early. **Chunk futures requests well under 30,000 bars.** The recorder uses 500 and the
backfill 10,000.

## F-010 — P6: futures candle volume is BASE-denominated ✅

**Run:** 2026-09-19 10:29 UTC. The bar tested is 08:00Z 1h; `p06_20260919T102916Z.json`.
Σ(volume × close) over 24 futures 1h bars ÷ rt `v` (quote, 24h) = **0.966 (BTC), 0.952 (ETH), 0.976 (SOL)**.
The ratios are ≈1, so volume is base. They fall slightly short of 1 because the rt 24h window is rolling
and the close is only a proxy for the trade price. Example: the BTC bar has volume 2,972.412, which at
≈81,238 is ≈241.5M USDT, while spot BTC for the same hour is 342.3 BTC (base, verified).
**All three candle volume fields are therefore base; only rt `v` is quote.** Hazard §5 #4 is resolved.

## F-011 — P7: futures `data/trades` is a fixed 30-row snapshot; `limit` is ignored ✅

**Run:** 2026-09-19 10:29 UTC. Every variant (`limit` absent, `limit=5`, `limit=1000`) returned the
**same 30 rows**, spanning **7.06 s**, **oldest-first**, with keys `is_maker, price, quantity, timestamp`
and `is_maker` a boolean (15/15 split). **The tape cannot be used to reconstruct per-bar aggressor
volume at any bar size;** 30 BTC trades cover about 7 seconds. That would need continuous polling
several times a second, which is out of scope at this horizon.

## F-012 — P11: all 1h and 1d bars are aligned to UTC, on both products ✅

**Run:** 2026-09-19 10:29 UTC. 400 bars × {spot 1h, spot 1d, futures 60, futures 1D} × 3 pairs =
4,800 bars. **Every open time modulo 3,600,000 ms / 86,400,000 ms is 0.** There is no IST offset.
P4b also confirms UTC alignment for futures 15/30/240/480.

## F-013 — P12: aggregation reproduces native bars exactly ✅

**Run:** 2026-09-19 10:39 UTC over 2026-09-09T04:00Z → 2026-09-19T04:00Z. That covers 10 days,
including 10 overnight low-volume periods. `p12_20260919T103905Z.json`.

| test | windows per pair | exact match (BTC / ETH / SOL) |
|---|---|---|
| spot 15 × 1m → native 15m | 960 | 100% / 100% / 100% |
| futures 60 × 1m → native 60 | 240 | 99.58% (one bar per pair differs in **volume only**; max relative diff 1.8×10⁻⁵; OHLC 100%) |
| futures 3 × 5m → native 15 | 960 | 100% / 100% / 100% |
| futures 4 × 60 → native 240 | 60 | 100% / 100% / 100% |
| spot 4 × 1h vs futures 240 (shape only) | 59 | return correlation 0.9999; mean \|close diff\| 4.5–4.7 bps |

- **Continuity:** 14,400 of 14,400 expected 1m bars present on both products for all three
  pairs, with zero zero-volume bars. The failure mode the spec feared (empty minutes omitted)
  did not occur in this window. The backfill gap counts (F-018) test this over the full history.
- **Spec error:** 01-verification §P12 said "aggregate 12 × 5m → compare against spot native 15m".
  12 × 5m is one hour. The test used 3 × 5m, and compared it with native *futures* 15m (F-008) for exact equality.
- **Decision fed:** aggregated 4h (spot) and native 4h (futures) are both trustworthy, so the
  timeframe stack does not need to shrink.

## F-014 ⚠ NEW HAZARD — P14 (live part): order-book depth parameters

**Run:** 2026-09-19 10:35 UTC, `p14_depth_20260919T103558Z.json`.

- **Spot** `depth` (lowercase) accepts exactly **{1, 5, 10, 20, 50, 100, 200}**, and each value returns
  that many levels per side. `depth=500` returns 422 `"depth must be one of [1, 5, 10, 20, 50, 100, 200]"`.
  This refines F-001: the documented set was right, but the parameter name was wrong.
- **Futures** `/orderbook/{pair}-futures/{depth}`: 10, 20 and 50 return that many levels. **5 and 100 return
  HTTP 200 with an EMPTY book** (`bids:{}`, `asks:{}`). The failure is silent. Any consumer must treat
  an empty futures book as invalid, never as "no liquidity".
- The spread and depth distributions for P14 come from the recorder's 5-minute snapshots; see F-021.

## F-016 — P17: spot trade tape aggressor flag ✅ (populated), semantics consistent with upstream

**Run:** 2026-09-19 10:29 UTC. 500 trades (the maximum), spanning **24.6 s**, **newest-first**. Keys are
`T, m, p, q, s`, where `s` is the upstream symbol `BTCUSDT`. `m` is always a JSON boolean:
**191 true / 309 false**. It is populated and not constant.

The cross-check against the book is only indicative, because the book was fetched after the tape and
the price moved during those 24 s. Trades above the mid were 86 false / 57 true; trades below were
134 true / 223 false. The skew points the expected way: `m=true` means the buyer was the maker, so the
aggressor sold. It is not clean enough to call verified. At 500 trades per ~25 s, per-bar aggressor
volume would need continuous polling. **Deprioritized, as the spec allows.**

## F-017 — P1 (binary search): history depth, preliminary

**Run:** 2026-09-19 10:30 UTC, `p01_20260919T103037Z.json`.

The earliest bar was found by binary search on "a 499-bar window contains data". An out-of-range
request (2010) returns `[]` on spot and `s:"no_data"` on futures, never recent data.

| pair | spot 1d | spot 1h | spot 15m | spot 1m | fut 1D | fut 60 | fut 1 | fut 5/15/30/240/480 |
|---|---|---|---|---|---|---|---|---|
| BTC | 2017-08-17 | 2018-08-23 | 2018-08-23 | 2017-12-12 | 2021-01-01 | 2022-01-19 | 2021-01-01 | 2023-01-11 |
| ETH | 2017-08-17 | 2018-09-21 | 2018-09-21 | 2018-09-21 | 2022-07-01 | 2022-01-19 | 2022-08-11 | 2023-01-23 |
| SOL | 2020-08-11 | 2021-02-11 | 2021-02-27 | 2021-03-04 | 2022-07-01 | 2022-06-30 | 2022-06-30 | 2023-01-23 |

**These results are internally inconsistent, so they are treated as preliminary.** For example, spot BTC
1m supposedly starts before BTC 15m/1h, and ETH futures 60 before ETH futures 1D. The binary search
assumes no internal hole longer than one 499-bar window, and these patterns suggest such holes exist.
**The backfill resolves this.** It starts every series of a product/pair at the earliest bar found at
*any* resolution, scans forward in full, and reports the true first bar and every hole (F-018).
Even on these preliminary numbers, the answer to the spec's question is clear: **1h history is at least
3.6 years on futures and 5.6–8 years on spot.** That is ample for walk-forward validation at the
hourly/daily horizon.

## F-019 ⚠ CORRECTION — P8: positioning (`data/stats`) is PUBLIC; the documented POST is 404 ✅ (part A)

**Run:** 2026-09-19 10:56–11:00 UTC. `p08_20260919T*.json`; the fake-key run is labelled `_DUMMY_CREDENTIALS`.

| request | result |
|---|---|
| GET `…/api/v1/derivatives/futures/data/stats?pair=B-BTC_USDT`, **no auth headers** | **200**, full payload |
| same, with a *fake* key + signature (ms or s, pair in query or body) | 200, identical payload; the credentials are not checked |
| POST (any variant, with or without the fake signature) | **404** `{"status":"error","message":"not_found","code":404}` |
| GET without `pair`, or `pair=B-NOPE_USDT` | **200 with empty objects** `{"price_change_percent":{},"high_and_low":{},"position":{}}` ⚠ silent |

Payload example (BTC, 10:57 UTC):
`"position":{"count_percent":{"long":54.92,"short":45.08},"value_percent":{"long":31.65,"short":68.35}}`.
Both percentages are populated for all three pairs, and each long/short pair sums to 100.
There are also `price_change_percent` {1H,1D,1W,1M} and `high_and_low` {1D,1W}.

**Answer:** crowd positioning **is available**, and needs no credentials. Class 1 keeps its most
distinctive input. **Positioning history started accumulating at 2026-09-19 11:00:45 UTC** (S2
enabled, `auth = "none"`, 15-minute polls). The recorder flags any 200 with an empty `position`
as an `empty_position` error, but still records the response.

**Contradicts:**
- The capability map's §3 and §8, which said `data/stats` is signed and documented as POST. Both are updated.
- **CLAUDE.md invariant 12** ("Only two endpoints need authentication: `data/stats` and `conversions`").
  This is **not edited; it is the owner's decision** (see Q5).

**Part B, still pending:** whether a *valid* signed request returns anything more or different. The probe
runs this comparison automatically once `.env` has values: `python -m probes.p08_positioning`.

## F-020 — Recorder acceptance 3: 10-minute network outage ✅ PASS

**Run:** 2026-09-19 10:39–10:58 UTC, `reliability_outage_20260919T105804Z.json`. A separate recorder
instance was run through a local proxy controlled by the test. The production recorder was untouched.

Timeline: up 3 min → **blackhole 5 min** (connections accepted, never answered, so every request times out)
→ **refuse 5 min** (connections dropped) → up 6 min.

- The process stayed alive throughout. There were 74 transport errors, all logged to `_meta/errors.jsonl`.
- **No hot loop:** at most **10 attempts per task in 600 s**. S1 would normally make 10 polls in that
  time, and the books only 2; the backoff capped retries at the 60 s transport ceiling.
- **Automatic recovery for every stream:** the first record after the outage came 0.8 s (futures book)
  to 37.5 s (spot ticker) after connectivity returned.
- **Gap records:** 8 were written, all with `reason:"error_streak"` and `recoverable:false` (S1, S3, and the
  three S5 and three S6 tasks), with durations of 686–790 s.
- **Candle gap-fill was pulled forward** by the unrecoverable gap and ran 1.3–8.0 s after recovery.
  All 8 candle streams recorded again without waiting for the hourly schedule.

A shortened 60 s dry run (`…_DRYRUN_scale0.1.json`) validated the harness first. It was too short to
produce gap records, which is expected.

## F-021 — Operational fixes found during testing

1. **A detached recorder could not be stopped cleanly.** On Windows it has no console, so it cannot
   receive Ctrl-C. The fix: create `data/raw/_meta/STOP` and the recorder shuts down cleanly within
   about a second. A stale STOP file found at startup is removed.
2. **A probe launched through `cmd.exe` died after one sample** from a console Ctrl-C (the first P9/P10
   sampler, 10:32 UTC). Long-running jobs are now launched without the wrapper, and have not been interrupted since.
3. **The production recorder was restarted at 11:00:45 UTC** to run the code that ships: S2 enabled,
   plus the stop file. The old process was hard-killed, which doubled as another live crash test.
   On restart it recorded `prior_shutdown_clean:false` and resumed every task from its last record.
   **The 72 h acceptance window is 2026-09-19 11:00:45 → 2026-09-22 11:00:45 UTC.**

## F-022 — P2: rate limits. No 429 up to 10 req/s; `api.coindcx.com` advertises 5,000 per 60 s ✅

**Run:** 2026-09-19 11:03–11:15 UTC, `p02_*.json`. Each endpoint was ramped separately at 1 → 2 → 5 → 10 req/s,
15 s per step (270 requests per endpoint), with 30 s pauses between endpoints. The production recorder and
the P9/P10 sampler were running at the same time (~0.17 req/s combined); **neither saw a single error.**

| endpoint | result | latency p50 at 10 req/s |
|---|---|---|
| `/exchange/ticker` | no 429 up to 10 req/s | 78 ms |
| `/market_data/candles` | no 429 up to 10 req/s | 73 ms |
| `/market_data/orderbook` | no 429 up to 10 req/s | 81 ms |
| `/market_data/trade_history` | no 429 up to 10 req/s | 76 ms |
| `public…/current_prices/futures/rt` | no 429 up to 10 req/s | 65 ms |
| `public…/candlesticks` | no 429 up to 10 req/s | 47 ms |

**Undocumented rate-limit headers (IETF RateLimit draft) on every `api.coindcx.com` response:**
`ratelimit-policy: 5000;w=60` and `ratelimit: limit=5000, remaining=4868, reset=47`. That is a
budget of **5,000 requests per 60 s window** (~83 req/s), and the counter is shared across that
host's endpoints. `public.coindcx.com` sends no rate-limit headers and did not throttle.
There is no `Retry-After`, because no 429 occurred.

**Decision fed:** the recorder's ~230 requests/hour is ~0.08% of the advertised budget. The backfill runs at
5 req/s per host (6% of it). The test deliberately stopped at 10 req/s, so the real trigger point is unmeasured.

## F-018 — Backfill complete + P1 final: history depth, with holes located ✅

**Run:** 2026-09-19 11:13–12:31 UTC, 5 req/s per host, 14,900 requests, 0 failures, and **no interference**:
0 recorder errors, 0 gap records, and 0 sampler errors during the run. The 3D series were re-pulled at 12:33 after
the gap-accounting fix. The files are `data/backfill/{summary,verify}.json` plus 42 parquet files; the data dir is 1.3 GB.

`python -m backfill --verify` re-reads every parquet independently. **All 42 series are sorted and unique,
and every served resolution is present.** Spot pages of 1,000 bars were verified live (`limit=1000` works).

**P1 answer (true continuous start = after the last hole of 7 days or more):**

| series | first bar | continuous from | completeness after that | worst remaining hole |
|---|---|---|---|---|
| spot BTC 1m | 2017-08-17 | **2018-02-10 06:15** ⚠ (see below) | 99.69% | 2,010 min, 2018-02-08 |
| spot BTC 15m / 1h | 2017-08-17 | 2018-08-23 | 99.82% / 99.87% | 10 h, 2019-05-15 (exchange outage) |
| spot ETH 1m / 15m / 1h | 2017-08-17 | 2018-09-21 | 99.71% / 99.80% / 99.85% | 15.7 h, 2024-06-20 |
| spot SOL 1m / 15m / 1h | 2020-08-11 | 2021-02/03 | 99.76% / 99.86% / 99.89% | 15.8 h, 2024-06-20 |
| spot BTC/ETH 1d | 2017-08-17 | 2017-08-17 | **100%** | — |
| spot SOL 1d | 2020-08-11 | 2020-08-11 | **100%** | — |
| futures BTC 1 / 1D | 2021-01-01 | 2021-01-01 | 99.99% / 100% | 186 min, 2024-09-22 |
| futures BTC/ETH 60 | 2022-01-19 | 2022-01-19 | 99.99% | 4 h |
| futures SOL 60 | 2022-06-30 | 2022-06-30 | 99.99% | 2 h |
| futures ETH 1 | 2022-07-06 | 2022-08-11 | 99.70% | 19 h |
| futures SOL 1 | 2022-06-30 | 2022-10-07 | **98.61%** | 7,495 min (5.2 d), 2022-10-07 |
| futures 5/15/30/240/480 (all) | 2023-01-11/23 | same | 99.91–100% | 2023-01-23 short outage |
| futures ETH/SOL 1D | 2022-07-01 | same | 100% | — |

- **Why P1's binary search looked inconsistent:** each spot series starts with a stub of a few hours on its
  listing day, followed by a **months-long hole** (BTC 1m: 2017-08-17 20:39 → 2017-12-12; ETH 1m → 2018-09-21;
  SOL 1m → 2021-03-04; BTC 1h: 2017-09-28 → 2018-08-23). The binary search had actually found the start of
  continuous history.
- **⚠ NEW HAZARD: spot BTC 1m bars from 2017-12-12 to 2018-02-10 open at :20 or :14 seconds past the minute**
  (9,637 bars). Aggregating that range to coarser bars would be wrong. **Use spot BTC 1m only from
  2018-02-10T06:15Z**, or restrict it to hourly and daily data. Everything else is exactly aligned.
- **3D bars do not sit on a fixed grid:** there is one re-anchoring in August 2023 (steps of 1 and 2 days). They
  are reported as `irregular_short_steps: 3`, with no data missing. 1M bars are calendar months.
- Residual holes line up with real upstream outages (for example 2019-05-15 and 2024-06-20, which affect every
  series at once). They are not failures of the API or the pipeline.
- **Decision fed (backtest window):** hourly history is continuous from **2018-08/09 on spot** (≈8 years) and
  **2022-01 on futures** (≈4.7 years), at ≥99.8% completeness. Daily is complete from **2017-08** (spot) and
  **2021-01** (BTC futures). That is enough for walk-forward validation at the hourly/daily horizon.

## F-023 — P10 basis ✅ and P9 ecode divergence ✅ (6 h synchronous sampler)

**Run:** 2026-09-19 10:39–16:47 UTC, 578 paired samples (rt + ticker fetched concurrently),
1 fetch error. `p09_p10_20260919T103951Z.json`. Local receive times differed by a median of 15 ms.

**P10 — basis (mark vs spot last), valid samples only (both timestamps within 2 s):**

| pair | valid / attempts | discard | mean bps | σ bps | min | max |
|---|---|---|---|---|---|---|
| B-BTC_USDT | 561 / 578 | 2.9% | **−3.85** | 0.70 | −6.58 | +0.26 |
| B-ETH_USDT | 565 / 578 | 2.3% | **−4.14** | 0.71 | −9.12 | −0.83 |
| B-SOL_USDT | 552 / 578 | 4.5% | **−2.00** | 1.01 | −8.96 | +2.78 |

**Answer:** basis is **measurable, persistent and small**. The mean is ≈5σ, so it is not
measurement noise — the P10 branch in `specs/02` §1 ("if σ is dominated by noise, cut Family 6 to
funding only") does **not** trigger; basis features are retained. **But the magnitude is ~4 bps
against a verified 11.8 bps futures round trip**, so basis convergence is not tradable on its own
and must never be presented as an opportunity. It stays what `specs/03` §1 already made it: a
consistency check for C1-A, not a confirmation input.

The perp trades at a small **discount** to spot throughout, which is consistent with `fr` sitting
at its 0.0001 baseline for the entire window. **Funding never moved off baseline in 6 h**, so
`corr(basis, fr)` is unmeasurable (zero variance) and C1-A's mechanism was never observed live.
Recording more is the only fix; this is the Tier B problem in miniature.

**P9 — ecode divergence.** Per F-007 there is **no non-B USDT market**, so the only available
cross is CoinDCX-internal INR: `I-X_INR ÷ I-USDT_INR` versus `B-X_USDT`, all three rows taken from
the same ticker response (timestamp disagreement 0 s by construction).

| asset | median | mean | σ | min | max |
|---|---|---|---|---|---|
| BTC | −13.1 bps | −18.4 | 15.9 | −86.5 | +3.1 |
| ETH | −10.5 bps | −18.4 | 23.8 | −89.6 | +22.0 |
| SOL | −2.9 bps | −11.5 | 26.6 | −80.6 | +31.4 |

**Answer:** CoinDCX-internal INR markets **do** diverge materially from the relayed `B-` markets,
by more than the futures round-trip cost at times. This does **not** create a strategy here: it is
an INR-rail/USDT-INR pricing effect requiring instruments outside this system's set (`B-*` USDT
only), and the spread is dominated by the USDT/INR leg rather than by BTC/ETH/SOL pricing.
**Capability map §7 stands: `B-*` basis is the upstream venue's basis.** P9 is resolved as
"applicable but not exploitable within this instrument set".

## F-024 — P13 continuity baseline: the recorder is correct, the host is not ⚠

**Window:** 2026-09-19 11:00 UTC → 2026-09-23 17:28 UTC (103 h wall clock), single recorder
process, no crash. Analysis: `recorder_analysis_*.json`, `storage_check_*.json`.

- **Measured uptime ≈ 12.1 h of 103 h.** No records at all on 2026-09-22. The laptop slept or was
  off; the process was gone from 2026-09-23 17:28 until restarted on 2026-09-27 13:03 UTC.
- **250 gap records**: 184 `unknown` (sleep/wake) and 66 `error_streak`; longest 49.96 h.
  99 transport errors, no HTTP error at all (`429`/`503` never seen).
- **Snapshot streams lost that time permanently** — as designed, with every gap recorded.
- **Candle gap-fill worked exactly as intended:** `futures_candles_1` holds 6,168 bars per pair
  from 2026-09-19T10:25 to 2026-09-23T17:12 with **0 missing and 0 misaligned bars** across four
  days containing ~91 h of downtime. The same is true of every other candle stream.
- Duplicate `source_ts` records: 0. Out-of-order arrivals: 0.

**Verdict:** the recorder's resume, gap-recording and backfill design is **validated**; the
**72 h-unattended criterion is not met**, and cannot be on a laptop that sleeps. This is exactly
the argument `specs/18` §2 makes for a 24/7 host, now with a measured number attached.
**Consequence for Tier B:** `earliest_tier_b_validation` must be measured from *continuous*
recording, not from the first start date, or it will overstate the evidence base.

## F-025 — P14 spread and depth: books are one tick wide and very shallow in price terms

**Run:** 2026-09-19 12:35–21:57 UTC, 7 rounds across 4–5 distinct UTC hours (the spec asks for
~20; the sampler was interrupted by the same host downtime as F-024 — **P14 is partially met**).
`p14_books_20260919T123535Z.json`, plus the depth-parameter result in F-014.

| book | levels | median spread | median reach from mid | median depth inside reach |
|---|---|---|---|---|
| futures BTC (50) | 50 | **0.012 bps** | ±0.76 bps | 1.19M USDT |
| futures ETH (50) | 50 | 0.038 bps | ±1.88 bps | 1.39M USDT |
| futures SOL (50) | 50 | 0.895 bps | ±44.3 bps | 17.1M USDT |
| spot BTC (200) | 200 | **0.001 bps** | ±3.64 bps | 3.83M USDT |
| spot ETH (200) | 200 | 0.038 bps | ±9.68 bps | 3.02M USDT |
| spot SOL (200) | 200 | 0.895 bps | ±178.5 bps | 9.95M USDT |

Two structural facts:

1. **The spread is exactly one tick, always** (0.012 bps = 0.1/81,000 on BTC; 0.895 bps = 0.01/112
   on SOL). Every sample, every hour. So `LIQ_SPREAD` is near-constant per asset, and the
   `specs/08` §8 Tier-3 `SPREAD` veto (half-spread > 10% of `R`) will effectively never bind.
2. **"Depth within 10/25/50 bps" is not measurable for BTC or ETH at any available depth.** Even
   200 spot levels span only ±3.6 bps on BTC. The bands that *are* measurable are 1, 2 and 5 bps,
   which is what the analysis now reports; wider bands are returned as `None`, never as a
   truncated number. The `specs/08` §7.1 slippage model must therefore be built on the 1–5 bps
   bands and is only valid for sizes inside ~1M USDT on BTC futures. For retail size this is ample;
   it must not be extrapolated.

## F-026 — P15 server clock skew, and a stale spot-book timestamp

Same window as F-024; skew = source timestamp − local request midpoint, net of RTT (median RTT
69–88 ms).

| source | n | median skew | range of hourly means |
|---|---|---|---|
| `futures_rt` envelope `ts` | 692 | +1,000 ms | −544 … +1,654 ms |
| futures book `ts` | 440 | +1,669 ms | −68 … +3,491 ms |
| spot book `timestamp` | 434 | +1,403 ms | **−631,990** … +1,879 ms |
| spot ticker `timestamp` (1 s resolution) | 692 | +624 ms | −931 … +1,386 ms |

**Answer:** skew sits within roughly **±2 s**, so the 2-second basis tolerance
(`specs/02` Family 6) is the right order of magnitude and P10's 2.9% discard rate confirms it is
workable. `DQ_CLOCK`'s tolerance should be set no tighter than **±3 s**, and these are *payload
generation* times rather than pure clock offsets — the futures-book figure includes the exchange's
own snapshot latency.

**⚠ One anomaly worth its own gate:** a spot order-book response once carried a `timestamp`
**10.5 minutes stale** while returning HTTP 200. A single freshness check on the spot book is
therefore mandatory, not optional — this is the same class of failure as the stale rows in
`current_prices/futures/rt`, on a different endpoint.

## F-027 — P16 spot liquidity: thinner in volume, comparable in book depth

| asset | spot 24 h quote volume | spot / futures volume | spot / futures book depth (median) |
|---|---|---|---|
| BTC | 2.03B | **0.109** | 0.45 |
| ETH | 1.00B | **0.080** | 0.40 |
| SOL | 0.44B | **0.146** | 0.28 |

**Answer:** spot turnover is **8–15% of futures**, an order of magnitude thinner, while resting
book depth is 28–45% of futures. So spot is not unusable, but futures is unambiguously the primary
venue. **Decision fed:** keep spot as a tradable product only for small size, and treat it
primarily as the cross-reference leg for basis (`BAS_BPS`) and for C1-C's relative-value legs. Any
spot suggestion must carry the size caveat, per the P16 question in `specs/01`.

## F-028 — Storage growth and gzip compression, measured ✅

`storage_check_*.json`, over 2026-09-19 → 2026-09-27.

- **42 completed day files compressed, every sha256 verified against the pre-compression bytes;
  0 mismatches, 0 leftover `.tmp` files, 0 unlogged files.** The 14 day files left plain when the
  process died on 09-23 were compressed at the 09-27 restart, which exercised the startup path.
- **Measured compression ratio: 5.3×** (JSONL of repetitive market data).
- **Steady-state projection from measured record sizes at full cadence: ~379 MB/month raw →
  ~71 MB/month on disk.** Breakdown: `futures_rt` 65, `spot_ticker` 56, `spot_book` 57,
  `futures_book` 55, candles ~37 (inflated by catch-up refetches after downtime, so this is an
  upper bound), `positioning` 4, `instrument_meta` <1.
- That lands **inside** `specs/01b`'s "well under 100 MB/month" target after compression, which
  resolves Q3 empirically: the spec's figure was right on disk and wrong pre-compression.

## F-029 — All seven `specs/02` §1 PENDING branches resolved (PHASE-01)

**Date:** 2026-09-27. Each branch is decided by recorded Phase-1 evidence; the taken branch and its
consequence are below. No branch is left open, and none was decided by preference.

| Probe | Branch taken | Evidence | Consequence for the build |
|---|---|---|---|
| **P1** history depth | **"≥ 3 years" → full walk-forward protocol** (`specs/09` §2.3) | F-018: 1h continuous history from 2018-08/09 on spot (≈8 y) and 2022-01 on futures (≈4.7 y), ≥99.8% complete | Full protocol, ~24+ folds. 1d also fittable on spot (3,320 daily bars) but stays **context/gating only** per `specs/02` §3. **Window boundaries must start at each series' `continuous_from`, not its first bar** |
| **P11** UTC alignment | **Aligned — no redefinition needed** | F-012: 4,800 bars across both products, every `open_time % 3600000 / 86400000 == 0`; F-008 confirms the same for futures 15/30/240/480 | Daily features, prior-day levels and the 4h grid use plain UTC boundaries. No IST offset anywhere |
| **P12** aggregation exactness | **Aggregation is safe — 4h and futures 15m retained** | F-013: spot 15×1m→15m and futures 3×5m→15, 4×60→240 all **100% exact** over 960/60 windows per pair; 14,400/14,400 1m bars present, **zero** zero-volume bars omitted | The feared failure mode was not observed. **`known_absent_bars` is still built** (PHASE-07) as the guard, because absence of evidence over 10 days is not proof, and `specs/11` §7.2 requires aggregation to refuse across an unconfirmed gap |
| **P6** futures volume units | **Base-denominated — no conversion** | F-010: Σ(volume×close) over 24 h ÷ rt `v` = 0.966 / 0.952 / 0.976 | All three candle volume fields are base; only `current_prices` `v` is quote. Hazard §5 #4 closed |
| **P3** spot markets | **All three exist** | F-007: `B-BTC_USDT`, `B-ETH_USDT`, `B-SOL_USDT`, ecode B, active, `min_notional` 5 | Class 1 coverage is symmetric; the spot leg exists for all three. **But F-027**: spot turnover is 8–15% of futures, so spot is small-size / cross-reference only |
| **P8** `data/stats` | **Reachable — Family 7 and C2-C retained** | F-019: public unauthenticated GET, both `count_percent` and `value_percent` populated and summing to 100 | `POS_*` features and C2-C survive. They remain **Tier B** (no history), so `validated: false` until `earliest_tier_b_validation` |
| **P10** basis signal-to-noise | **Basis retained, but as a consistency check only** | F-023: mean −3.85/−4.14/−2.00 bps at σ 0.70/0.71/1.01 → ≈5σ, so **not** noise-dominated; the `specs/02` §1 cut-to-funding-only branch does **not** trigger | `BAS_BPS`/`BAS_Z` are implemented. **Magnitude (~4 bps) is well below the 11.8 bps round trip**, so basis may never be presented as a tradable dislocation — exactly the C1-A condition-3 data-quality use `specs/03` §1 already specifies |

**Two consequences that change later phases, recorded here so they are not rediscovered:**

1. **P1's answer is per-series.** `specs/09` §2's fold boundaries must be computed from the
   `continuous_from` dates in `data/backfill/verify.json`, not from the first bar in each file. Using
   the first bar would put the earliest folds inside a months-long hole (F-018).
2. **P10 leaves C1-A's mechanism unobserved.** Funding sat at its 0.0001 baseline for the whole
   6-hour sample, so no funding extreme has ever been seen live by this system. C1-A cannot be
   evaluated at all until the recorder captures one — which makes continuous 24/7 recording a
   prerequisite for Class 1's Tier B member, not just a nice-to-have.

## F-030 — `specs/03` amendment applied: C1-C condition 7

**Date:** 2026-09-27. `specs/04` §7 identified a genuine specification gap: C1-C (fade a
cross-asset residual) and C2-B (follow the factor trend) could both fire on the same residual in
opposite directions, because they shared no partition.

**Applied to `specs/03` §5:** added **condition 7 — `MR_VR(1h) < 1` on the dislocated asset.**

This restores the same mutual-exclusivity partition that separates C1-B from C2-A, and it is now
one of the four partitions that must be asserted under test (`specs/13` §15). The interim
`CLASS_CONFLICT` suppression rule from `specs/04` §7 remains the fallback for any *other* opposing
pair, and is superseded in general by the `specs/07` §4 hierarchy.

## F-031 — the variance-ratio formula in `specs/02` divides by `k` twice

`specs/02` Family 4 states the Lo-MacKinlay variance ratio as

```
sigma2_k = Sum (lnP_t - lnP_{t-k} - k*mu)^2 / m,   m = k(nk - k + 1)(1 - k/(nk))
VR(k)    = sigma2_k / (k * sigma2_1)
```

`m` already contains the factor `k`, so `sigma2_k` is an estimate of the **per-period** variance of
k-period returns. Dividing by `k` again biases every value down by a factor of `k`.

Measured on simulated series (3,000 observations, `numpy` default RNG, seed 7):

| Series | | k=2 | k=4 | k=8 |
|---|---|---|---|---|
| Random walk | literal `specs/02` | 0.509 | 0.264 | 0.140 |
| Random walk | `sigma2_k / sigma2_1` | **1.017** | **1.058** | **1.117** |
| AR(1), phi = -0.4 | literal | 0.295 | — | — |
| AR(1), phi = -0.4 | `sigma2_k / sigma2_1` | **0.591** | — | — |

The literal formula contradicts the interpretation `specs/02` gives two lines later — "`VR > 1` ->
trending, `VR < 1` -> mean-reverting, `VR approx 1` -> random walk" — because under it a random walk
reads 0.5, 0.26 and 0.14 rather than ~1.

**Why this matters beyond tidiness.** `MR_VR(1h)` is the **P axis of the regime model** (`specs/02`
§6) and appears as a hard condition in C1-B (2), C1-C (7, added by F-030) and C2-A (3). With the
literal formula every asset reads mean-reverting at all times: the `MR_VR < 1` conditions would pass
permanently, the `MR_VR > 1` condition in C2-A would never pass, and the regime model's second axis
would carry almost no information. The failure is silent — the numbers look plausible.

**Resolution applied:** `src/features/numerics.py` implements `VR(k) = sigma2_k / sigma2_1`, the
standard Lo-MacKinlay ratio and the one consistent with the spec's stated interpretation. The
docstring records the deviation. `tests/test_features_numerics.py` asserts ~1 on a random walk, > 1
on a positively autocorrelated series and < 1 on a mean-reverting one — the test that would have
caught this.

**Owner decision needed (Q7):** amend `specs/02` Family 4 to drop the extra `k`, or state a
different intent.

## F-032 — the order book is far shallower than the depth vetoes assume; futures is capped at 50 levels

Measured 2026-09-28 against the live endpoints, BTC:

| Endpoint | Requested depth | HTTP | Levels returned | Reach from mid |
|---|---|---|---|---|
| `orderbook/B-BTC_USDT-futures/{d}` | 50 | 200 | 50 / 50 | **±0.74 bps** |
| | 100 | **200** | **0 / 0 (empty)** | — |
| | 200 | **200** | **0 / 0 (empty)** | — |
| | 500 | **200** | **0 / 0 (empty)** | — |
| `orderbook?pair=B-BTC_USDT&depth={d}` | 50 | 200 | 50 / 50 | ±1.01 bps |
| | 100 | 200 | 100 / 100 | **±1.79 bps** |
| | 500 | 422 | — | — |

Two separate facts:

**1. The futures book cannot be fetched deeper than 50 levels.** Depths 100, 200 and 500 return
HTTP **200 with an empty book** — the F-014 trap, now confirmed for every depth above 50. A caller
that treats 200-with-empty-book as "no liquidity" would conclude the market had vanished. The
normalizer already rejects an empty book as invalid (`normalize/canonical.py`), so this surfaces as a
rejection rather than as zero depth.

**2. No depth band `specs/08` §7.1 relies on is observable.** The futures book spans ±0.74 bps and
the spot book ±1.79 bps at its maximum usable depth. The spec's veto is stated over **25 bps**, which
is 14× wider than anything visible, and F-025's earlier ±3.6 bps measurement was also well short of
it. Even the 1 bps band is marginal on futures.

**Consequences, implemented:**

- `LIQ_DEPTH` reports the notional of the **entire visible book** as its scalar, paired with
  `visible_reach_bps`, and keeps the 1/2/5/10/25/50 bps bands in its detail with `None` where the book
  does not reach. A `None` band means *unknown*, never zero. Labelling ±0.75 bps of book as a "25 bps"
  figure would understate true depth and read identically for 10, 25 and 50 bps.
- `LIQ_IMBAL` measures over the visible book when 1 bps is not covered, and states the band it used.
- The risk engine (PHASE-21) **cannot implement the 25 bps depth veto as written.** It must either
  size against visible depth with an explicit unmeasurability flag, or refuse sizes it cannot justify.
  This is a data-availability limit at this venue, not an implementation shortcut.
- The recorder polls depth 50 for both books. Spot could usefully be raised to 100 (it works and
  nearly doubles reach); futures cannot be improved. That is an owner decision (Q8) because it changes
  the irreplaceable stream's shape.

## F-033 — the regime model passes separation and occupancy but **fails persistence on BTC and SOL**

Fitted 2026-09-28 on 4,001 stored 1h futures bars per asset (~5.5 months), axes `VOL_RATIO(24/720)`
and `MR_VR_K2(24)`, trailing quantile lookback 720, per `specs/02` §6.

### The forward horizon decides the separation test, so it was fixed from the specs

`specs/02` §6 requires forward return distributions to "differ measurably across cells" but does not
say over what horizon. It turns out to decide the answer:

| Asset | Grid | Kruskal-Wallis p at fwd = 1 bar | 6 bars | 24 bars |
|---|---|---|---|---|
| BTC | 3×3 | 0.889 | 0.399 | **0.000** |
| BTC | 2×2 | 0.925 | 0.160 | **0.000** |
| ETH | 3×3 | 0.657 | 0.001 | **0.000** |
| ETH | 2×2 | 0.549 | 0.032 | **0.000** |
| SOL | 3×3 | 0.361 | 0.000 | **0.000** |
| SOL | 2×2 | 0.550 | 0.130 | **0.000** |

A one-bar test says there is no structure anywhere; a 24-bar test says there is structure everywhere.
Choosing the horizon that passes would be precisely the curve-fitting the spec's trailing-quantile rule
exists to prevent, so it is anchored to the horizon the **strategies** operate on: `specs/03` §5 expires
an untriggered setup after "3 funding intervals (24h)" and `specs/06` §4 says holding periods "land in
days". `SEPARATION_FORWARD_BARS = 24`, recorded in the artifact as `separation_horizon_bars`, and a
separation verdict is never reported without it.

### The outcome at 24 bars

| Asset | Occupancy (min cell share) | Persistence (median dwell) | Separation (p) | Verdict |
|---|---|---|---|---|
| **BTC** | PASS (0.221) | **FAIL — 2.0 bars** (need ≥ 3) | PASS (5.1e-06) | **degraded** |
| **ETH** | PASS (0.235) | PASS — 3.0 bars | PASS (2.6e-10) | **usable, 2×2** |
| **SOL** | PASS (0.214) | **FAIL — 2.0 bars** | PASS (2.2e-05) | **degraded** |

3×3 fails persistence on all three assets (median dwell 2.0 everywhere), so the escalation collapses to
2×2 as `specs/12` §10 prescribes; ETH then passes all three and BTC and SOL do not.

### What this means

The axes **do** carry information about the next day — separation is significant at p < 1e-4 on every
asset, which is a positive result and worth stating plainly. What fails is the *label's stability*: on
BTC and SOL the cell changes every two bars, and `specs/02` §6 is explicit that "a label that flips
every bar is not a regime, it is a noisy indicator with a fancy name".

So the system runs **per-asset mixed mode**, which the degraded design already supports:

- **ETH** uses the full model: cell gating, dwell stability (C1-B 3, C2-A 7, C3-B 7) and
  `regime_persistence_prob` are all available.
- **BTC and SOL** run degraded: every raw-axis threshold still applies (`MR_VR < 1` in C1-B 2 / C1-C 7,
  `MR_VR > 1` in C2-A 3, the `VOL_RATIO` percentile in C3-A 1), the dwell-stability conditions are
  dropped, and C3-B publishes `regime_persistence_prob: null` with a reason. Reduced selectivity, not
  broken strategies.
- The dashboard must show which assets are degraded, since two of three are.

### Caveats on this fit

- 4,001 bars is ~5.5 months. The quantile lookback consumes 720 of them, and `VOL_RATIO` needs 721 bars
  to exist at all, so only 3,281 bars are classifiable. The fit should be repeated once a longer window
  is available; `specs/09`'s walk-forward protocol will re-fit per fold regardless.
- Persistence is measured on runs of the cell label. Because the cut points are trailing quantiles
  recomputed every bar, a value sitting near a boundary flips the label without the market changing.
  That is inherent to the method the spec prescribes, and it is the most likely reason dwell is short.
- **This is a substantive finding about this market at this horizon**, which is what `specs/12` §10 asks
  to be recorded either way.

Artifacts: `config/regime_model_B-{BTC,ETH,SOL}_USDT.json`, summary in
`data/probe_results/regime_fit.json`.

## F-034 — failure drill: 15 deliberate failures, all classified and actionable

Run 2026-09-29 (`python -m probes.failure_drill`, result in
`data/probe_results/failure_drill.json`). Each drill breaks one thing for real and records what the
system says. The question is not "does it fail" but **"would the message tell an operator what to do
at 3 a.m.?"** — which means naming the component, the likely cause, the remediation, and above all the
**problem class**, since on Azure that is what decides whether to look at the code, the configuration,
the VM or the venue.

| drill | what was broken | code | problem class |
|---|---|---|---|
| missing database | pointed the API at a database that does not exist | `STORE_FAILURE` | **infrastructure** |
| corrupt database | wrote text into a .db file and queried it | `STORE_FAILURE` | **infrastructure** |
| missing regime artifact | removed regime_model_*.json | `CONFIG_INVALID` | **configuration** |
| corrupt regime artifact | truncated regime_model_*.json mid-object | `CONFIG_INVALID` | **configuration** |
| missing parameter file | pointed the registry at a path with no parameters.toml | `CONFIG_INVALID` | **configuration** |
| parameter budget | nothing (checking the guard exists) | `CONFIG_PARAMETER_INVALID` | **configuration** |
| public bind, no token | asked the API to bind 0.0.0.0 with no auth token | `STARTUP_FAILED` | **configuration** |
| shallow snapshot request | asked for 50 bars where the registry needs 721 | `STARTUP_FAILED` | **configuration** |
| clock behind data | set the clock five hours behind the newest stored bar | `CLOCK_SKEWED` | **infrastructure** |
| stale Tier B | futures_rt is 9,000 s old against a 60 s window | `DQ_FAILED` | **data_quality** |
| store failure behind the API | the database connection was closed under a live request | `STORE_FAILURE` | **infrastructure** |
| network failure | a request to a host that does not resolve | `EXTERNAL_TRANSPORT` | **external_dependency** |
| aggregation across a gap | aggregated 4h from three of four hourly bars | `DQ_GAP` | **data_quality** |
| risk engine mutation | altered a published stop after the strategy set it | `RISK_LEVELS_MUTATED` | **application_bug** |
| control strategy live | evaluated C4-X1 outside backtest mode | `STRATEGY_FAILURE` | **application_bug** |

**Result: 15 of 15 actionable**, 0 silent. "Actionable" is asserted, not asserted-by-eye: the drill
requires a problem class, a likely cause and a remediation on every failure, and fails its own exit
code otherwise.

### Two real defects the drill found, both fixed

1. **A missing `config/parameters.toml` raised a bare `FileNotFoundError`.** The single most likely
   deployment mistake — running from the wrong directory, or not shipping the file — produced an
   unclassified traceback that named the path and nothing else. `load_registry` now raises
   `ConfigError` for a missing file, an unreadable one and malformed TOML, each with its own
   remediation. This is exactly the class of startup failure the local-first instruction was meant to
   surface before Azure, and it would have cost an hour on a first deploy.

2. **A `sqlite3` connection was shared across threads.** FastAPI runs a sync handler in a worker
   thread, and a connection belongs to the thread that created it — so the dashboard returned HTTP 500
   on its first request while the engine carried on writing happily. Found by `tests/test_api.py`
   before the drill; fixed with `store/reader.py`, one **read-only connection per thread**, with
   read-only enforced by SQLite's `mode=ro` URI rather than by convention.

### What the drill confirms about the design

- A **stale Tier B source** gates the family (`DQ_FAILED` with the age and the window) rather than
  publishing a stale number.
- A **store failure behind the API** renders `DOWN` with a correlation id and keeps the payload's
  shape — it never renders as an empty market, which `specs/14` §10 calls the most dangerous thing
  this API can do.
- **Aggregating across an unconfirmed gap** is refused with the reason, not computed from what is
  present.
- The **risk engine mutating a published level** raises `RISK_LEVELS_MUTATED` at CRITICAL, and a
  **control strategy evaluated outside backtest** refuses — both classified `application_bug`,
  because both mean the code is wrong rather than the market.

---

## F-035 — A dead feed reported itself as "warming": the series-state check tested depth before staleness ✅

**Date:** 2026-10-03. **Found by:** running `python -m app --once` after a two-week break and reading
the `snapshot_incomplete` payload rather than the summary line.

The report said, for every alpha series:

```
'reason': 'WARMING', 'detail': 'need 1440 1h bars, have 1105 (335.0h more to warm)'
```

Both halves were true. Ingestion had also been stopped since 2026-09-19, so the newest stored 1h bar
was **14 days old** — and nothing said so. `SnapshotBuilder._series` checked the three failure reasons
in the order `EMPTY -> WARMING -> EDGE_BEHIND`, and a window that is short *and* stale hits `WARMING`
first and returns.

The two messages are not interchangeable:

| Reason | What an operator does |
|---|---|
| `WARMING` | nothing — wait for history to accumulate |
| `EDGE_BEHIND` | restart ingestion; the series describes the past |

Reporting the reassuring one for a dead feed is how a stopped process goes unnoticed for a fortnight.

**Fix.** Staleness is now checked **before** depth, and when both hold the shortfall is named as a
consequence rather than as a separate problem: *"the window is also 331 bars short, but that is a
consequence of the stale edge, not a separate problem."* Covered by
`tests/test_snapshot.py::test_a_window_that_is_both_short_and_stale_reports_the_stale_edge_not_the_warm_up`
and by its converse, so "give it time" still wins when the edge *is* current.

**Related process gap, fixed the same day.** There was no runnable process that kept the SQLite tables
current. `python -m recorder` appends raw JSONL (hard invariant 8), and `store.import_raw` bridges
JSONL to SQLite for the **Tier B streams only** — it has no candle importer. `ingest/pipeline.py` held
the candle polling but had no `__main__`, so nothing ever ran it. Running the recorder alone therefore
leaves `bars` frozen wherever ingest last ran. `python -m ingest` now exists (`--check` / `--once` /
`--futures-only`), with a startup assertion that the candle set includes futures 15m and 1m, because
without those ACT arms and then waits forever without reporting a failure.

---

## F-036 — 332 of 1,440 1h bars were present, correct, and invisible: history pulled once never finalizes ✅

**Date:** 2026-10-03. **Found by:** `detect_gaps` reporting **zero** missing bars on a window the
snapshot builder said was 331 bars short. Two correct answers to what looked like the same question.

The difference is `final_only`. `detect_gaps` passes `final_only=False`; `SnapshotBuilder` passes
`final_only=True`. Counting both ways:

```
B-BTC_USDT futures, last 1440 h   is_final=0: 332   is_final=1: 1108
                       4h                      80            280
                       1d                      10             50
                      15m                    1339           4421
                       1m                   20122          66278
```

A bar becomes final only when a **second** fetch returns it unchanged (specs/11 §6.2, the two-fetch
rule). `poll_candles` re-fetches `refetch_bars` back from the newest stored bar, so a catch-up that
pulls two weeks of history in one request leaves every one of those bars non-final, and the next pass
starts from the new edge and never looks at them again.

The result is the failure mode this project treats as worst: data that is **present and correct in the
database and completely unusable**, reported indefinitely as `WARMING` while the row count says it is
there. Nothing is logged, because nothing is wrong.

**Fix.** `SqliteStore.non_final_bar_times(..., settled_before_ms=, limit=)` finds them, oldest first,
excluding bars close enough to now that the venue may still be revising them legitimately. One
`settle:` task per series (`IngestPipeline.poll_settle`) re-requests the oldest contiguous window, at
most `SETTLE_BATCH_BARS = 300` per pass, at priority **P2** — below the live polls, because settling
old history matters and never matters more than knowing the current price. A pass with nothing to
settle makes no request at all, which is what lets the cadence be brisk.

Measured on the first real pass:

```
4h  requested=79  returned=80   remaining_non_final=0
1d  requested=9   returned=10   remaining_non_final=0
1h  requested=300 returned=301  remaining_non_final=30
15m requested=300 returned=301  remaining_non_final=1036
1m  requested=300 returned=301  remaining_non_final=19819
```

A bar the venue no longer returns is left non-final rather than promoted: an unconfirmable bar is
exactly what the rule exists to exclude. A bar that comes back **different** still raises a
`BAR_MUTATION`, handled identically to the live path.

---

## F-037 — `content_hash` compared Decimal strings, so `105` and `105.0` were a mutation ✅

**Date:** 2026-10-03. **Found by:** writing the F-036 regression test and watching five identical bars
refuse to finalize.

`content_hash` built its payload from `str(Decimal)`:

```python
payload = "|".join(str(x) for x in (bar.open, bar.high, bar.low, bar.close, bar.volume_base))
```

`str(Decimal("105"))` is `"105"` and `str(Decimal("105.0"))` is `"105.0"`. Same price, different hash.

Two consequences, in opposite directions:

1. **A false `BAR_MUTATION`** — logged at ERROR, recorded as an `external_dependency` DQ event, and
   marking every signal at that `as_of` non-replayable. The loudest false alarm the store can raise,
   and a flood of them would train an operator to ignore the one alert that means history really did
   change.
2. **The two-fetch rule never fires**, so the F-036 sweep would re-request the same window forever and
   settle nothing.

This is not hypothetical: hard invariant 6 already records the venue sending inconsistent precision on
order books — `"11570.67000000"` beside `"2001"`.

**Fix.** `canonical_decimal()` returns `format(Decimal(v).normalize(), "f")`, which is equal whenever
the *value* is equal (`'f'` because `normalize()` turns `Decimal("100")` into `1E+2`). `content_hash`
uses it, and `same_content(row, bar)` compares the stored columns **by value** as the authority, with
the hash kept as the fast path — so rows written before the change do not all read as mutations the
first time they are re-fetched. Four tests cover it, including that a genuinely changed price is still
a mutation.

---

## F-038 — θ calibrated structurally at 0.75; the two criteria disagree, and the lookbacks are why ✅

**Date:** 2026-10-03. **Probe:** `python -m probes.swing_calibration`, grid at
`data/probe_results/swing_calibration.json`. **Window:** all stored futures history
(BTC 1h from 2022-01-19, ETH 1h from 2022-01-19, SOL 1h from 2022-06-30), final bars only.

`swing_atr_multiple` (θ, registry parameter 10) shipped `unset` with `value = 0.0`, which made every
Family 3 feature report `PARAMETER_UNSET` and silently disabled the structural conditions in C2-A/B/C,
C3-B and C3-C. It was the last uncalibrated parameter. It is now **0.75, `provisional`**, and the
registry has **zero** `unset` entries at exactly 30 of 30.

### Calibrated from structure only — no returns, no performance, no P&L

θ decides what counts as a swing pivot, which decides what C2-A's stop references, which decides every
`R` downstream. Choosing it by what made money on the training window would fit the structure
*definition* to one price path, and every strategy reading `STR_SWING` would inherit the fit with
nothing looking wrong. Three criteria, each tied to what a consumer actually needs:

| Criterion | What it measures | Why it is the shape it is |
|---|---|---|
| **availability** | fraction of evaluation points where the consumer's feature is *defined* | failure mode is **silence** — the condition never evaluates, which looks like "no setups today" |
| **spacing** | median bars between consecutive pivots vs `lookback_short / 2` | pivots alternate high and low, so one swing cycle is two spacings |
| **dispersion** | cross-asset CV of pivot density | one shared θ is only defensible if it means the same thing on BTC, ETH and SOL |

Availability is required **per pair**, because the requirement differs: `1h/short` is read for
`last_swing_high` / `last_swing_low` and needs **one pivot of each kind**; `1h/medium` feeds `STR_SEQ`
through `structure_intact` and needs **two of each**. One shared rule would either excuse a useless θ
or reject a usable one.

### The grid

```
 theta   avail   worst cell  spacing_err  dispersion  viable
  0.25   0.917     1h/short       0.6667      0.0191  yes
  0.50   0.917     1h/short       0.6528      0.0164  yes
  0.60   0.917     1h/short       0.6528      0.0128  yes
  0.75   0.883     1h/short       0.6250      0.0128  yes   <- chosen
  0.90   0.850     1h/short       0.6250      0.0210  yes
  1.00   0.842     1h/short       0.6250      0.0221  yes
  1.25   0.700     1h/short       0.6250      0.0238  no
  1.50   0.450     1h/short       0.5833      0.0294  no
  2.00   0.092     1h/short       0.4583      0.0407  no
  3.00   0.000     1h/short       0.8021      0.1190  no
```

Availability falls monotonically in θ, as it must — θ is a significance threshold, so raising it can
only remove pivots. The floor (0.80) is crossed between 1.00 and 1.25. Among viable candidates the
spacing error ties at 0.625 for 0.75 / 0.90 / 1.00, and **dispersion breaks the tie at 0.75** (0.0128
against 0.0210 and 0.0221), which also happens to have the highest availability of the three. The
optimum is flat — θ between 0.5 and 1.0 changes little — and that flatness is itself worth recording:
θ is a scale, not a sensitive knob.

### ⚠ The two criteria disagree, and the probe says so rather than averaging over it

Availability caps θ at **1.0**; spacing is best at **2.0**, where availability has collapsed to 0.09.
No single θ satisfies both, and the cause is **not** θ:

```
1h/medium median spacing by theta:  4.0  5.0  5.0  6.0  10.0  (theta 0.25 -> 2.0)
1h/short  median spacing by theta:  4.0  4.0  4.0  4.0   3.0
```

On `1h/short` the median is pinned at 4 bars across the whole usable grid, because with `k = 3`
confirmation two confirmable pivots cannot sit much closer than `k + 1` bars — the **window's
geometry** decides it, not θ. And `lookback_1h_short = 24` with `k = 3` simply cannot host the 12-bar
swings the trading horizon implies: by the time θ is large enough for a swing to be meaningful, a
24-bar window contains no confirmed pivot of each kind and C2-A's structural stop has nothing to
reference.

That is a statement about the **shared lookbacks**, not about θ. Changing `lookback_1h_short` would
alter every feature on the 1h timeframe, so it is raised as **Q11** rather than decided here. θ = 0.75
is the availability-respecting end of the conflict, which is a large part of why it is `provisional`
and not `fixed`. `probes.swing_calibration.tension()` computes and prints the gap on every run, so it
cannot quietly stop being true.

### Two mistakes the probe caught by refusing to answer

Both runs returned **"NO VIABLE THETA"** before the method was right, and both times the probe was
correct to decline:

1. **Measuring an impossibility.** The first run scored every (timeframe, horizon) pair, including
   `4h/short` (6 bars) and `1d/short` (7 bars) — fewer than the `2k + 2 = 8` bars a pivot needs to
   exist. Availability was 0.0 for every candidate. `STR_SWING` and `STR_SEQ` now declare
   `min_lookback = MIN_PIVOT_BARS`, so the engine **skips** those four combinations instead of
   computing and failing them on every evaluation; `undefined_combinations` went from 1 to 5. Before
   this, an arithmetic impossibility was being reported once per bar as a data-quality failure,
   indistinguishable from a real one.
2. **Deriving the spacing target per window instead of per timeframe.** Keying the target by horizon
   gave `1h/medium` a target of 84 bars (168 / 2). Wrong: pivot spacing is a property of the price
   process and θ, so looking back 168 bars reveals *more* swings, not longer ones. Both horizons on a
   timeframe share one target, derived from that timeframe's **short** lookback.

A calibration that always produces a number is indistinguishable from a guess.
`tests/test_swing_calibration.py` holds 13 tests, and the ones that matter most assert the refusals:
the probe raises `ConfigError` when no pair can hold a pivot, returns `chosen: null` when nothing
clears the floor, and the CLI exits non-zero so an operator cannot read "calibrated" from a run that
calibrated nothing.

---

## F-039 — The backtest window's start is derived from coverage, and coverage must be read per tier ✅

**Date:** 2026-10-03. **Found by:** `python -m backtest --window train --coverage` on the first
pre-registered window, which reported **0% usable** over 1,246 steps.

Two separate problems, and the first one hid the second.

### The reported number was useless, not wrong

`BacktestDriver.coverage()` counted a step as unusable if the snapshot had **any** problem. Over a
historical window every step has one, because **Tier B has zero history**: funding, basis,
positioning and the book are point-in-time with no archive before the recorder started in 2026-09.
`specs/09` §13 already anticipates this with a separate Tier B timeline — but a single
`usable_fraction` of 0.0 makes a perfectly sound Tier A backtest look impossible.

Worse, the ranked `reasons` list was truncated to its top 12 for readability, and with 640 Tier B
entries at one per source per asset, the truncation showed **only Tier B** and hid the handful of
Tier A reasons that were actually actionable.

Coverage now reports three readings, and the Tier A reasons in full:

```
steps                      1246
usable_fraction             0.000   <- strict: the only reading a Tier B strategy can be judged on
tier_a_usable_fraction      0.568   <- the one that matters for a Tier A backtest
tier_b_only_fraction        0.568   <- expected to be ~100% over history; if not, something is wrong
tier_a_reasons   {EMPTY: 2978, WARMING: 951, EDGE_BEHIND: 21}
```

### The window itself was wrong, and the fix is a derivation rather than a choice

With the Tier A number visible, 56.8% was the real problem. Measured per series on stored futures
history:

| series | first bar |
|---|---|
| 1h | 2022-01-19 (BTC, ETH), 2022-06-30 (SOL) |
| 1m | 2021-01-01 (BTC), 2022-06-30 (SOL), 2022-07-06 (ETH) |
| 1d | 2021-01-01 (BTC), 2022-07-01 (ETH, SOL) |
| **15m** | **2023-01-11 (BTC), 2023-01-23 (ETH, SOL)** |
| **4h** | **2023-01-11 (BTC), 2023-01-23 (ETH, SOL)** |

15m and 4h simply do not exist before 2023-01-23 — the venue's own history, not a gap in ours. The
pre-registered window started 2022-02-01, so for its first eleven months the snapshot could not build
two of the five required series, and ACT could not size a trigger at all.

Adding the warm-up on top (the 1d series needs 180 bars beyond the first) puts the earliest fully
warm `as_of` in late July 2023. The window was amended to start **2023-08-01**, which is a derivation
from coverage rather than a preference:

```
tier_a_usable_fraction 0.568 -> 0.914   (700 steps on a daily grid)
```

The amendment was made **before any result was seen**, so it did not burn the holdout — which is
exactly the situation `specs/09` §1.3 contemplates, and the reason the protocol was locked and then
coverage checked, in that order.

2023-08-01 → 2026-09-30 is **38 months**: `specs/09` §2.3's full protocol, with ~26 walk-forward folds
on the 32 months before the holdout at §2.2's 6-month train / 1-month test / 1-month step.

### What this leaves open

Two of twelve strategies — **C1-A and C2-C** — read Tier B exclusively and therefore **cannot be
backtested at all**. Not weakly: there is no data. They would reach shadow with no historical evidence
whatsoever, which is recorded as an open question rather than resolved by running them anyway.







---
---
---
---


---

## Open questions

| # | Question | Blocking | Decision it leaves open |
|---|---|---|---|
| Q1 | ~~Should CLAUDE.md invariant #3 be rewritten?~~ **Resolved 2026-09-19.** The owner approved the change: "`skw` is not a health flag; judge freshness by `btST`/`bmST` age only." CLAUDE.md is updated. | — | — |
| Q2 | What does `skw` actually mean? | Needs longer observation (quality.jsonl `skw_min`/`skw_max`) | Whether it is usable as a data-quality metric |
| Q3 | ~~Disk growth ~270 MB/month~~ **Resolved 2026-09-19.** The owner accepted it and does not want polling reduced. Completed day files are now gzip-compressed losslessly (sha256-verified); the measured effect is in F-023. | — | — |
| Q4 | ~~Positioning (S2) is wired but disabled.~~ **Resolved 2026-09-19.** The endpoint is public (F-019), S2 is enabled, and history has been accumulating since 11:00:45 UTC. Only P8 part B (signed vs unsigned comparison) waits for `.env`. | — | — |
| Q5 | **CLAUDE.md invariant 12** says `data/stats` needs authentication. F-019 shows it does not. Proposed wording: "Only `conversions` is documented as signed (unused). `data/stats` is public: call it unauthenticated. Keep signed-request code isolated in `cdx_http/signed.py`." | Owner decision | Whether S2 should stay unauthenticated (current default) or be switched to `auth = "signed"` once a key exists |
| Q6 | Futures also serve 15/30/240/480/3D/1M (F-008). S7 gap-fill records only the spec set (1/5/60/1D). Adding 15/30/240/480 costs +12 requests/hour. | Owner decision (spec expansion) | Whether native futures 4h is recorded continuously, or only via the backfill |
| Q7 | ~~`specs/02` Family 4 variance ratio divides by `k` twice (F-031).~~ **RESOLVED 2026-10-03.** The owner confirmed the corrected standard Lo–MacKinlay form and the spec is amended at `specs/02` §5 Family 4 with the measurements that justified it. Tracked briefly as Q-J in `latest-updates.md`; that id is merged into this one. | — | — |
| Q8 | **Raise the recorder's spot book depth from 50 to 100?** F-032 measured 100 levels working and reaching ±1.79 bps versus ±1.01 bps at 50, for one extra request per poll. Futures cannot be improved (>50 returns an empty book). | Nothing; it changes the recorded stream's shape going forward | A recording-policy decision about the irreplaceable dataset |
| Q9 | ~~Which `k` does a bare `MR_VR(1h)` mean?~~ **RESOLVED 2026-10-03.** The owner confirmed **k = 2** as canonical and the spec now states it explicitly; `MR_VR_K4`/`MR_VR_K8` must be named where wanted. | — | — |
| Q10 | ~~Confirm `risk_per_trade` and `portfolio_vol_target`.~~ **RESOLVED 2026-10-03.** The owner confirmed **`risk_per_trade = 0.5%`** and **`portfolio_vol_target = 15%` annualised**. Both remain `status = "user"` in the registry: they are risk appetite, not market properties, so they are never swept (`specs/08` §11). | — | — |
| Q11 | **`lookback_1h_short = 24` cannot host swings at the horizon's own scale.** F-038 measured the conflict: pivot availability on a 24-bar window caps θ at ~1.0, but the median swing spacing only reaches the derived 12-bar target around θ ≈ 2.0, where availability is 0.09. With `k = 3` confirmation, 24 bars is too short for 12-bar swings, so C2-A's structural stop necessarily references smaller swings than its own premise describes. Options: (a) accept it — θ = 0.75 is the availability-respecting end, and this is why θ is `provisional`; (b) read `STR_SWING` at `1h/medium` for the structural stop instead of `1h/short`, which costs no parameter and whose spacing does respond to θ; (c) change `lookback_1h_short`, which alters **every** 1h feature and the regime axes with it. | Nothing — (a) is in force and the system runs | Whether C2-A's structural stop should read the medium window, and whether the shared short lookback is right for structure at all |
