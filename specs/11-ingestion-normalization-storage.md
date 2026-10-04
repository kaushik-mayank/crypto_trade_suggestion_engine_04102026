# 11 — Ingestion, Normalization & Storage

**Depends on:** `specs/00` (hazards, verified metadata), `specs/01` (HTTP client, probes),
`specs/01b` (recorder), `specs/10` (canonical forms, storage choices).
**Character:** this is an implementation spec. It should be buildable without further design
decisions.

---

## 1. Scope, and two revisions to the Phase 0 plan

**In scope:** REST polling for spot and futures, the normalization layer, bar finalization, gap
detection and backfill, storage, and ingest-time data-quality computation.

### Revision 1 — no WebSocket, so no Socket.IO work

The original Phase 0 plan listed Socket.IO clients, ping-keepalive, and depth sequence-gap
recovery. All were removed by the Phase 0 revision: at an hourly-to-daily horizon streaming buys
nothing and costs the documented Socket.IO version contradiction, an undocumented depth-resync
protocol, and reconnect-storm handling. **Do not build any of it.**

### Revision 2 — the recorder does not retire

Phase 0 said the recorder would be "superseded by this layer after parallel-run proves
equivalence." **That is now wrong and is revised here.**

The recorder's value is precisely its **independence**. If the app crashes, deadlocks, or ships
a bad deploy, the recorder keeps capturing the unrecoverable Tier B series. Retiring it would
trade permanent data loss risk against a saving of roughly 60 requests per hour — a bad trade at
any plausible rate limit.

> **The recorder runs permanently, alongside the app, as the independent raw-capture layer.**

The two poll the same endpoints independently and do not share state. Coupling them (having the
app tail the recorder's JSONL) was considered and **rejected**: it reintroduces the single point
of failure the separation exists to remove. The duplicated request cost is trivial.

---

## 2. Polling schedule and request budget

### 2.1 Principle: prefer all-symbol endpoints

`/exchange/ticker` and `current_prices/futures/rt` return **every symbol in one request**. At
three assets that is a 3× saving on the highest-frequency polls. Use them for price freshness;
use per-symbol endpoints only where no aggregate exists.

### 2.2 Schedule

| Source | Cadence | Requests/hr | Priority |
|---|---|---|---|
| `current_prices/futures/rt` | 15 s | 240 | **P0** — live price, funding, mark |
| `/exchange/ticker` | 30 s | 120 | **P0** — live spot price (all markets) |
| 1m candles (3 assets × 2 products) | 60 s, offset +5 s past bar close | 360 | **P0** — trigger evaluation |
| 15m candles | on bar close +10 s | 24 | P1 |
| 1h candles | on bar close +15 s | 6 | P1 |
| 1d candles | on bar close +30 s | 0.25 | P1 |
| `data/stats` × 3 | 15 min | 12 | P2 — Tier B positioning |
| Order book (spot + futures) × 3 | 5 min | 72 | **P3** — first to be dropped |
| Instrument metadata × 6 | 6 h | 1 | P2 |
| **Total** | | **≈ 835/hr ≈ 0.23 req/s** | |

Plus the recorder's independent ~230/hr. Combined ≈ 0.30 req/s, which should sit comfortably
inside any plausible limit — **but confirm against probe P2's measurement before enabling P3
polling.**

### 2.3 Bar-close offsets

Candles are polled **after** the bar closes, with a small offset, because an immediately-fetched
bar may not be final. Finalization is handled properly in §6; the offset merely reduces churn.

---

## 3. Ingest layer

### 3.1 Adapters

One adapter per endpoint. Each returns raw bytes plus request metadata and **never parses**
(the raw-first invariant, `specs/01b` §design principle 1).

```python
class IngestResult:
    endpoint: str
    params: dict
    raw: bytes
    http_status: int
    response_headers: dict
    sent_at_ms: int          # local clock
    received_at_ms: int      # local clock
```

### 3.2 Adaptive rate limiting

Production form of `specs/01` §0:

- **Per-host token bucket.** Initial rate from probe P2's measurement; if P2 is incomplete, a
  deliberately conservative default with a log line stating the assumption.
- **On `429`:** halve the rate for that host for 10 minutes, exponential backoff from 1 s capped
  at 60 s. Log at WARN.
- **On `503`:** backoff from 30 s capped at 15 minutes, with a **distinct log line** — this is
  documented maintenance (`specs/00` §8), not our fault, and should not trigger rate reduction.
- **Recovery:** restore rate gradually after sustained success, never in one step.

### 3.3 Priority preemption

When the bucket is constrained, **P0 polls preempt lower priorities and P3 is dropped first.**
Losing an order-book snapshot costs a risk-engine input that is refreshed every 5 minutes anyway;
losing 1m candles costs trigger evaluation, which is the system's actual job.

Dropped polls are recorded in `dq_events`, not silently skipped.

---

## 4. Normalization

### 4.1 Structure

One normalizer per endpoint. **Not a generic normalizer** — the hazards are endpoint-specific and
a generic layer would have to guess.

```python
def normalize(raw: bytes, meta: IngestResult) -> NormalizeOutcome
# NormalizeOutcome = Accepted(records) | Rejected(reason, raw_ref) | Partial(records, rejected_rows)
```

### 4.2 The governing rule

> **Reject, do not coerce.**

If a payload does not match expectations, emit a rejection with the raw bytes referenced — never
guess, never substitute a default, never fill forward. A coerced value is indistinguishable from
a real one three layers downstream, which is how a malformed payload becomes a trade.

### 4.3 Canonical types

Pydantic models enforcing `specs/10` §5. Prices and quantities are `Decimal`; timestamps are
UTC milliseconds as `int`; fees are the `Fee` value object.

```
Bar(asset, product, timeframe, open_time_ms, open, high, low, close, volume_base,
    is_final, source, fetched_at_ms)
Trade(asset, product, ts_ms, price, qty, is_buyer_maker, source)
BookSnapshot(asset, product, ts_ms, bids: list[(Decimal, Decimal)], asks: [...], version)
FuturesRT(asset, ts_ms, funding_rate, predicted_funding_rate, mark_price, last_price,
          high_24h, low_24h, volume_quote_24h, price_change_pct, skw,
          tpe_tick_send_ms, tpe_mark_send_ms)
Positioning(asset, ts_ms, long_count_pct, short_count_pct, long_value_pct, short_value_pct)
InstrumentMeta(asset, product, tick, qty_increment, min_notional, maker_fee: Fee,
               taker_fee: Fee, multiplier_up, multiplier_down, funding_frequency_h,
               leverage_tiers, margin_tiers, status, fetched_at_ms)
```

Book levels are `list[tuple]` **sorted numerically**, never a dict — the JSON object arrives
unordered (`specs/00` §5 hazard 5) and the ordering must be established here, once.

---

## 5. The eleven hazards — transformation and test

Each hazard from `specs/00` §5 gets an explicit transformation and a targeted test using a
**real captured payload** as fixture.

| # | Hazard | Transformation | Test |
|---|---|---|---|
| 1 | Mixed time units | Futures candlestick `from`/`to` in **seconds** → ×1000. Futures WS `open_time` seconds; `close_time` fractional seconds | Assert every stored `open_time_ms` is a plausible ms epoch (13 digits, within ±2y of now) |
| 2 | Auth timestamp contradiction | Resolved by probe P8; encode the answer in one place | Signed request succeeds |
| 3 | Mixed sort order | Spot candles arrive **descending** `[VERIFIED]` → reverse. Futures ⧗ P4 | **Assert ascending after normalization and that the last row is newest.** This inverts time if wrong |
| 4 | Volume units | Spot candles **base** `[VERIFIED]`. `current_prices` `v` **quote** `[VERIFIED]` → divide by typical price. Futures candles ⧗ P6 | Cross-check a known hour: spot base volume × price ≈ quote magnitude |
| 5 | Book is a JSON object | Parse keys as `Decimal`, sort numerically, emit ordered list | Feed a payload with keys out of order; assert sorted output |
| 6 | `"0"` quantity = delete | Not applicable without WS depth updates. Guard anyway: drop zero-qty levels from snapshots | Payload with a zero level |
| 7 | `m` flag type varies | bool / `0`/`1` / bool → canonical `is_buyer_maker: bool` | All three encodings in one test |
| 8 | `Depth` capital-D param | Encode once in the adapter | Request returns the requested depth |
| 9 | GET-with-signed-body | Resolved by P8; isolated in `authenticated.py` | Live call succeeds |
| 10 | Double-slash URL | Not used (no order endpoints) | — |
| 11 | Socket.IO contradiction | Moot — no WebSocket | — |

### 5.1 Row-level validation for `current_prices/futures/rt`

Empirically verified in Phase 1: this payload contains **stale rows and schema-incomplete rows**
under a fresh envelope timestamp (`specs/00` §4).

Per row, in order:

1. `skw != 0` → **reject row**, reason `SKW_NONZERO`
2. Missing any of `ls`, `mp`, `fr`, `efr` → **reject row**, reason `SCHEMA_INCOMPLETE`
3. `now − btST` or `now − bmST` beyond the freshness window → **reject row**, reason `STALE_ROW`
4. Otherwise accept

**The envelope `ts` is never used for freshness.** Fixtures must include the real payload
containing `B-NKN_USDT` (which returns only `{fr, mp, efr, bmST, cmRT, skw}`) and at least one
row with a months-old `btST`.

---

## 6. Bar finalization and mutation detection

A bar fetched immediately after its close may not be final. If we store it and the exchange later
revises it, every feature computed from it is wrong and nothing fails.

### 6.1 The two-fetch rule

> **A bar is `is_final = True` only when two consecutive fetches return it identically.**

Self-validating, and it introduces no parameter. Each poll re-fetches the most recent **5 bars**
(cheap, since `limit` is supported) and reconciles.

### 6.2 Mutation detection

If a bar already marked final returns different values on a later fetch:

- Log at **ERROR** with both versions
- Write a `dq_events` row of type `BAR_MUTATION`
- Update the stored bar to the new value
- **Flag every signal whose audit record references that `as_of`** — their replay will no longer
  reproduce, and that must be visible rather than discovered later

This should be rare. If it is not rare, it is a finding about the exchange that belongs in
`docs/findings.md` and changes how much the backtest can be trusted.

### 6.3 Features consume final bars only

`MarketSnapshot.closed_bars` contains **only** bars with `is_final = True`. A non-final bar is
neither closed nor forming — it is excluded entirely.

---

## 7. Gaps, backfill, and known-absent bars

### 7.1 Detection

On startup, and every hour:

For each `(asset, product, timeframe)`: take the latest stored `open_time_ms`, enumerate expected
bar boundaries up to `now − one bar`, and diff against what is stored.

### 7.2 The zero-volume ambiguity

⧗ PENDING P12. If the exchange **omits** zero-volume bars rather than returning them with zero
volume, a missing bar is ambiguous: did we miss it, or does it not exist?

**Resolution:** request a narrow window that brackets the missing bar. If the response contains
the surrounding bars but not the missing one, **the bar does not exist**. Record it in
`known_absent_bars` so it is never re-requested.

```
known_absent_bars(asset, product, timeframe, open_time_ms, confirmed_at_ms, confirmations)
```

Require **two** confirmations before treating absence as settled, for the same reason as §6.1.

**This distinction is load-bearing:** `specs/02` §1 branches the entire timeframe stack on P12,
because aggregating 15 one-minute bars into a 15m bar when only 13 were returned produces a
silently wrong bar. Aggregation must consult `known_absent_bars` and **refuse to aggregate**
across an unconfirmed gap.

### 7.3 Backfill

Chunked per probe P1's depth findings and P5's truncation findings, rate-limited at **P3
priority** so backfill never starves live polling. Resumable: record progress so a restart
continues rather than restarting.

---

## 8. Storage

### 8.1 One source of truth

> **SQLite is the source of truth for everything. Parquet is a derived artifact, regenerated
> from SQLite, never written directly.**

This eliminates any possibility of the live and backtest paths diverging — a risk `specs/10` §1
ranks second only to correctness. Parquet exists solely so DuckDB can run analytical queries
efficiently during backtest and analysis.

Rejected: writing bars to Parquet directly (two write paths, divergence possible); a hot/cold
split (two sources of truth). At 3 assets × 2 products × 1m resolution the volume is ~260k rows
per month total — SQLite handles this without strain.

### 8.2 Layout

```
data/
├── raw/                    recorder JSONL (independent, immutable)
├── state.db                SQLite — SOURCE OF TRUTH
├── parquet/                derived, regenerated hourly + on demand
│   └── {product}/{asset}/{tf}/{year}-{month}.parquet
├── holdout/                PHYSICALLY SEPARATE (specs/09 §1.2)
└── backtest/               run outputs keyed by protocol hash
```

### 8.3 Core tables

| Table | Key | Notes |
|---|---|---|
| `bars` | `(asset, product, timeframe, open_time_ms)` | Upsert; `is_final`, `fetch_count` |
| `known_absent_bars` | same | §7.2 |
| `futures_rt` | `(asset, ts_ms)` | Accepted rows only; rejects go to `dq_events` |
| `positioning` | `(asset, ts_ms)` | Tier B |
| `book_snapshots` | `(asset, product, ts_ms)` | Levels as JSON; retention-capped |
| `trades` | `(asset, product, ts_ms, price, qty)` | **Dedup is imperfect** — spot REST has no trade ID. Document, do not pretend otherwise |
| `instrument_meta` | `(asset, product, fetched_at_ms)` | Versioned — a fee change is an event worth keeping |
| `dq_events` | auto | Rejections, gaps, mutations, dropped polls, rate-limit episodes |

### 8.4 Storage interface

```python
class StorageInterface(Protocol):
    def get_bars(asset, product, tf, start_ms, end_ms, final_only=True) -> list[Bar]: ...
    def upsert_bars(bars: list[Bar]) -> UpsertResult: ...
    def get_point_in_time(asset, source, as_of_ms) -> Record | None: ...
    def latest_bar_time(asset, product, tf) -> int | None: ...
    def record_dq_event(event) -> None: ...
```

**`get_point_in_time` must never return a record with `ts_ms > as_of_ms`.** This is the storage
layer's contribution to the look-ahead boundary, and it is asserted in tests rather than trusted.

Swapping SQLite for Postgres later must touch only `src/store/` — no caller may depend on SQL
dialect or on SQLite specifics.

---

## 9. Data-quality computation at ingest

Family 10 features are computed **here**, not in the feature layer, because they describe
ingestion health rather than market state.

| Metric | Computation |
|---|---|
| `DQ_AGE` | `now − source_timestamp`, per source |
| `DQ_SKW` | Row-level, §5.1 |
| `DQ_COMPLETE` | Expected vs received bars, consulting `known_absent_bars` |
| `DQ_LATENCY` | `received_at_ms − btST` — the measurable TPE relay latency |
| `DQ_CLOCK` | Server timestamp vs local, net of round-trip (probe P15) |
| `DQ_DISAGREE` | Spot last vs futures mark, **only on timestamp-aligned pairs within 2 s** |

`DQ_DISAGREE` inherits the hard tolerance from `specs/02` Family 6, for the reason established
in Phase 1: an unsynchronised comparison measures elapsed time, not disagreement.

---

## 10. Module layout

```
src/ingest/
├── client.py            adaptive rate limiter, backoff, raw capture, redaction
├── scheduler.py         asyncio tasks, priorities, catch-up on restart
├── authenticated.py     ← ONLY module reading the secret
├── adapters/            one per endpoint, returns IngestResult
└── dq.py                §9

src/normalize/
├── canonical.py         Pydantic models, Fee type, Decimal enforcement
├── errors.py            NormalizeOutcome types
└── {endpoint}.py        one normalizer per endpoint

src/store/
├── interface.py         Protocol
├── sqlite_store.py      source of truth
├── parquet_export.py    derived, regenerated
└── migrations/

src/pipeline/
├── finalization.py      §6
├── gaps.py              §7
└── backfill.py          §7.3
```

---

## 11. Tests

Beyond the per-hazard tests in §5:

| Test | Asserts |
|---|---|
| **Fee 100× error** | Construct a `Fee` from the raw API value; assert any fee entering the cost model is `< 0.01`. **Deliberately build the error and prove it is caught** (`specs/10` criterion 4) |
| **Sort inversion** | Feed a real descending spot payload; assert ascending output and newest-last |
| **Stale row rejection** | Real payload with `B-NKN_USDT` and a stale `btST`; assert both rejected with correct reasons and that valid rows still pass |
| **Finalization** | A bar is not final on first fetch; becomes final on identical second fetch |
| **Mutation** | Fetch, alter, re-fetch; assert ERROR log, `dq_events` row, and affected-signal flagging |
| **Gap vs absent** | Simulate an omitted bar; assert `known_absent_bars` after two confirmations and no further requests |
| **Aggregation refusal** | Attempt 1m→15m aggregation across an unconfirmed gap; assert refusal |
| **`as_of` boundary** | `get_point_in_time` never returns a record newer than `as_of_ms` |
| **Priority preemption** | Under a constrained bucket, P0 polls succeed and P3 is dropped and logged |
| **Restart catch-up** | Kill mid-poll, restart; assert gap detected, backfilled, and no duplicate rows |

---

## 12. Parameters — zero

Running total: **30 → 30. Unchanged.**

Polling cadences, priority levels, the 5-bar re-fetch window, the two-confirmation rules and
retention caps are **operational configuration**, recorded in the settings file but not model
parameters — they cannot change a signal's content, only its timeliness.

---

## 13. Acceptance criteria for Phase 11

1. No Socket.IO, WebSocket, or depth-resync code exists anywhere.
2. The recorder runs independently and is **not** retired; a test kills the app and confirms the
   recorder continues capturing.
3. Every hazard in `specs/00` §5 has a passing test backed by a **real captured payload**, not a
   synthetic one.
4. The `B-NKN_USDT` stale/incomplete-row fixture is committed and its test passes.
5. Bars become final only after two identical fetches; features consume final bars only.
6. Bar mutation is detected, logged at ERROR, and flags affected signals.
7. `known_absent_bars` distinguishes missing from non-existent, and aggregation refuses to span
   an unconfirmed gap.
8. SQLite is the sole write path; Parquet is regenerated and never written directly. A test
   asserts both return identical data for any overlapping range.
9. `get_point_in_time` respects `as_of_ms` under test.
10. Rate limiting backs off on `429`, handles `503` distinctly without reducing rate, and drops
    P3 before P0.
11. Measured request rate is recorded and compared against probe P2's limits in
    `docs/findings.md`.
12. Only `src/ingest/authenticated.py` references the secret.
