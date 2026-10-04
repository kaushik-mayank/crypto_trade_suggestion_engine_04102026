# 10 — System Architecture & Technology Selection

**Depends on:** all prior specs. **Governs:** Phases 11–15 implementation.

---

## 1. Design priorities, in order

This system's engineering problem is **not** throughput and **not** latency. Three assets, two
products, a handful of timeframes, hourly-to-daily decisions — the compute load is trivial. The
hard problems are elsewhere, and the architecture must optimise for them in this order:

1. **Correctness.** Eleven documented normalisation hazards (`specs/00` §5), unit mismatches
   across endpoints, and look-ahead risk everywhere. A wrong number here is worse than no number.
2. **Backtest/live parity.** If the two paths diverge, every Phase 9 result is invalid and
   nothing fails visibly.
3. **Reliability under a laptop that sleeps.** Restart-safe, gap-aware, resumable.
4. **Debuggability.** Every signal must be replayable from its audit record.

**Explicitly not priorities:** latency, horizontal scalability, throughput, multi-user support,
high availability. Optimising for any of these would trade away the four above, and the Phase 0
risk register named "premature distribution" as the principal architectural danger here.

---

## 2. The central decision: `MarketSnapshot` as a structural look-ahead boundary

`specs/09` §9 specifies look-ahead *audits*. Audits catch bugs. Architecture can prevent them.

> **The strategy engine is a pure function of an immutable `MarketSnapshot`. It has no access to
> storage, no clock, and no knowledge of whether it is running live or in a backtest.**

```
MarketSnapshot(as_of) ──► StrategyEngine ──► RawSignal[]
                             (pure)
```

Because the engine literally cannot reach data outside the snapshot, look-ahead becomes an
**impossibility rather than a bug class**. The audits in `specs/09` §9 remain, as a backstop
against the snapshot *builder* being wrong.

### 2.1 What a snapshot contains

```python
MarketSnapshot:
    as_of_ms: int                      # UTC milliseconds
    closed_bars: {(asset, product, tf): Bars}   # ascending, all with close_time <= as_of
    forming_bar: {(asset, product, tf): Bar | None}   # EXPLICITLY MARKED
    point_in_time: {...}               # Tier B values with their own source timestamps
    instruments: {(asset, product): InstrumentMeta}
    dq: {source: DQState}
    snapshot_id: str                   # hash of (as_of, code_version, param_hash)
```

`forming_bar` is separated from `closed_bars` in the type system, not by convention. Only the
trigger logic (`specs/02` §3 — the 1m/15m role) may read it; feature computation takes
`closed_bars` only. **A feature function that accepts `forming_bar` is a defect**, and the
signature should make that visible at review.

### 2.2 Same engine, two drivers

| Driver | Builds snapshots from |
|---|---|
| `LiveDriver` | Current store state, `as_of = now` |
| `BacktestDriver` | Historical store, `as_of` stepping through time |

**The engine, arbitration, risk and cost modules are shared verbatim.** This is the structural
form of `specs/08` criterion 10 and `specs/09` criterion 4 — parity by construction rather than
by discipline.

---

## 3. Process topology

**Two long-running processes plus offline entry points. Not more.**

| Process | Contents | Why separate |
|---|---|---|
| **`recorder`** | `specs/01b` poller | Its data is **unrecoverable**. If the app crashes, recording must not. This is the only justified process split. |
| **`app`** | Snapshot builder, features, regime, strategies, arbitration, risk, API, web | Crashing loses nothing permanent — state rebuilds from the store. |
| *offline* | `backtest`, `probes`, `backfill`, `analysis` | Run on demand, same modules, different entry point. |

### Why not more

Splitting further on a single laptop would add failure domains, serialisation boundaries and
deployment complexity in exchange for nothing. **Explicitly rejected:** microservices, a message
broker (Kafka/RabbitMQ/Redis Streams), a separate feature service, a separate strategy service,
container orchestration. Revisit only if hosting later demands it — and the module boundaries in
§4 are clean enough that splitting remains possible without a rewrite.

---

## 4. Module decomposition and contracts

```
src/
├── config/        parameters (30), settings, secrets loading
├── http/          shared client: adaptive rate limit, backoff, raw capture, redaction
├── ingest/        one adapter per endpoint → raw bytes + metadata
│   └── authenticated.py   ← THE ONLY module that reads the API secret
├── normalize/     the hazard layer: units, ordering, Decimal, schema validation
├── store/         StorageInterface + Parquet / SQLite / DuckDB implementations
├── snapshot/      MarketSnapshot builder — the look-ahead boundary
├── features/      Tier A families 1-5,9 · Tier B families 6-8 · family 10 DQ
├── regime/        two-axis model, validity tests, transition matrix
├── strategies/
│   ├── act.py     ← ONE implementation, imported by 4 consumers
│   └── c1a.py c1b.py c1c.py c2a.py … c4x2.py
├── arbitration/   Phase 7 pipeline, lifecycle state machine
├── risk/          Phase 8 veto ladder, sizing, cost model
├── audit/         record construction, persistence, replay
├── api/           FastAPI routes
├── web/           Jinja2 templates, HTMX fragments, static
├── backtest/      BacktestDriver + fill model + metrics + protocol lock
└── recorder/      separate process
```

### Contracts

| Boundary | Passes | Invariant |
|---|---|---|
| `ingest → normalize` | Raw bytes + request metadata | **Ingest never parses.** Raw-first (`specs/01b`) |
| `normalize → store` | Validated typed records | All units canonical (§5). Rejects rather than coerces |
| `store → snapshot` | Query by `(asset, product, tf, as_of)` | Store never returns data after `as_of` |
| `snapshot → features` | Immutable `MarketSnapshot` | The look-ahead boundary |
| `features → strategies` | `FeatureSet` | Every value present, or **NaN with an explicit reason**. Never silently absent |
| `strategies → arbitration` | `RawSignal[]` | Levels immutable from here on |
| `arbitration → risk` | Ranked, deduplicated `SignalSet` | Order is meaningful (`specs/08` §4.3) |
| `risk → api` | `Signal[]` with all vetoes attached | **Levels byte-identical to what strategies produced** (`specs/08` criterion 1) |
| everything `→ audit` | Append-only records | Never mutated |

---

## 5. Canonical internal representation

Given eleven documented hazards, the internal representation is declared once and enforced by
types at the `normalize` boundary.

| Quantity | Canonical form | Converted from |
|---|---|---|
| Timestamps | **UTC milliseconds, int** | Futures candlesticks use **seconds**; futures WS candles use seconds with fractions |
| Bar arrays | **Ascending by time** | Spot candles arrive **descending** `[VERIFIED]` |
| Prices, quantities | **`Decimal`** | Strings with inconsistent precision |
| Volume | **Base-denominated** | `current_prices` `v` is **quote** `[VERIFIED]`; futures candles ⧗ PENDING P6 |
| Funding rates | **Per-interval decimal fraction** | As reported (already fractions) |
| Aggressor flag | **bool `is_buyer_maker`** | bool in spot REST, `0/1` in spot WS, bool in futures |

### ⚠ The fee unit trap — a silent 100× error

The API returns `taker_fee: 0.059` meaning **0.059 percent**. The canonical internal form is a
**decimal fraction: 0.00059**.

> **Treating the API value as a fraction understates all costs by 100×.** Every net-edge gate
> then passes, every R:R looks excellent, and **no test fails.** The entire Phase 9 validation
> would be invalid while appearing healthy.

Enforce with a distinct type (`Percent` vs `Fraction`, or a `Fee` value object) so the conversion
cannot be skipped, plus an assertion that any fee entering the cost model is `< 0.01`. This is
the highest-consequence unit error available in this system.

---

## 6. Technology selection

Each choice states its reason and what it beat.

| Layer | Choice | Rationale | Rejected |
|---|---|---|---|
| **Language** | **Python 3.11+** | Numerical ecosystem (OLS, variance ratio, Wilson intervals); compute load is trivial so development speed dominates; strong agent support | Rust/Go — execution speed irrelevant here, weaker stats ecosystem, slower iteration. TS backend — weak numerics |
| **Dataframes** | **Polars** | Strict semantics, **no implicit index alignment** — pandas' silent alignment is a real source of subtle bugs in exactly this work. Explicit nulls | pandas — familiar but has genuine footguns (chained assignment, NaN/None ambiguity, index surprises). Acceptable fallback if the agent hits real friction; **do not mix** |
| **Numerics/stats** | **numpy + scipy + statsmodels** | OLS with standard errors for `TRD_TSTAT`; Wilson intervals; hypothesis tests | Hand-rolled — needless risk on standard estimators |
| **Validation** | **Pydantic v2** | Turns §5's canonical forms into **enforced types at every boundary**. This is a correctness tool here, not convenience | dataclasses — no validation. attrs — weaker ecosystem fit |
| **Bar storage** | **Parquet**, partitioned by `(product, asset, timeframe)` | Columnar, compressed, portable, no server, immutable-friendly | CSV — no types, no compression |
| **Raw recorder** | **JSONL append-only** | Raw-first, crash-safe, human-inspectable (`specs/01b`) | Binary — defeats inspectability |
| **Operational state** | **SQLite** | ACID, single file, zero-ops, SQL-queryable, stdlib | Postgres — a service to keep alive on a laptop for no benefit at this volume |
| **Analytics** | **DuckDB** | SQL directly over Parquet, excellent for backtest analysis | Loading everything into memory — works now, breaks later |
| **Web/API** | **FastAPI** | Async-native, Pydantic-integrated, auto OpenAPI | Flask — no async, weaker validation. Django — far too heavy |
| **Scheduling** | **asyncio tasks with per-task last-run state** | Each task catches up on restart; no external dependency; fits the sleep/resume model | Celery — needs a broker, a whole failure domain for nothing. cron — fights the persistent-state model |
| **Templates/UI** | **Jinja2 + HTMX** | Dashboard is read-mostly with periodic refresh. **No build step, no npm, no bundler**, no client state to desync | React/Next — build toolchain and state management for a read-mostly display; doubles the agent's maintenance surface. Streamlit — full-rerun model unsuitable for live data, poor layout control |
| **Charts** | **lightweight-charts** | Purpose-built for financial charts, small, plain JS, level overlays | Plotly — heavy. Chart.js — not financial-native |
| **Logging** | **structlog** → JSON | Logs must be queryable. **Logs are not the audit trail** (§8) | stdlib alone — unstructured |
| **Testing** | **pytest + hypothesis** | Property-based testing fits this domain unusually well — see §6.1 | pytest alone — misses the property class |
| **Config** | **Pydantic Settings + TOML parameter file** | Parameters versioned and hashable for `parameter_set_hash` | Scattered constants — defeats the 30-parameter budget |
| **Packaging** | **uv or pip + pyproject.toml** | Reproducible, lockfile | Conda — heavier than needed |
| **Containerisation** | **Dockerfile provided, not required locally** | Portability for later hosting without imposing container workflow on laptop dev | Docker-required — friction now for a benefit deferred |

### 6.1 Why property-based testing earns its place

Several specifications are stated as *properties*, and properties are exactly what Hypothesis
tests well:

- ACT's `E(t)` is **monotone non-increasing** for a LONG (`specs/03` §2)
- Trailing stops **never loosen** (`specs/04` acceptance 6)
- Stops round **away** from entry; targets round **toward** (`specs/08` §10)
- `k_net` after rounding is never greater than before
- Signed correlation sizing treats a long/short pair as hedged (`specs/08` criterion 4)
- Normalised bars are always ascending, regardless of source ordering

Each is a universally-quantified claim, which example-based tests can only sample.

---

## 7. Data model and storage layout

```
data/                              # gitignored
├── raw/                           # recorder, JSONL, append-only (specs/01b)
├── bars/                          # Parquet, immutable
│   └── {product}/{asset}/{tf}/{year}/{month}.parquet
├── holdout/                       # ← PHYSICALLY SEPARATE (specs/09 §1.2)
├── state.db                       # SQLite: signals, lifecycle, audit
└── backtest/                      # run outputs, keyed by protocol hash
```

### SQLite schema (operational)

| Table | Contents |
|---|---|
| `signals` | Current state of every signal, one row per signal |
| `signal_events` | Append-only state transitions (`specs/07` §8) with timestamps |
| `signal_audit` | One row per signal per evaluation — the full decision trace |
| `rejections` | Signals that never emitted, with the failing gate and its inputs |
| `arbitration_events` | Conflicts, merges, suppressions, handoffs |
| `dq_events` | Gate failures, gaps, staleness episodes |
| `runs` | Process starts, code version, parameter hash, config hash |

`rejections` is not optional. `specs/07` and `specs/08` both require that vetoed and suppressed
signals be recorded — **what the system chose not to show is as diagnostic as what it showed.**

---

## 8. Audit and replayability

### Logs ≠ audit trail

**Logs** are operational diagnostics: unstructured-ish, rotated, disposable.
**The audit trail** is a first-class persisted data structure, never rotated away, and is the
thing that makes a signal defensible. Conflating them is a common and costly mistake.

### Replay by reconstruction, not by copying

Storing a full snapshot per signal would be wasteful. Instead, exploit immutability:

> **Bars are immutable and append-only. Therefore `(as_of, code_version, parameter_hash)`
> deterministically reconstructs the snapshot.**

Each `signal_audit` row stores:

```
signal_id, strategy_id, strategy_version,
as_of_ms, snapshot_id, code_version, parameter_hash,
feature_values: {...}            # what was actually used — small
conditions: [{name, passed, value, threshold}],   # ALL, passed and failed
regime_cell, regime_dwell,
act_state: {x_star_history, e_t},
levels_raw / levels_rounded,     # both, so rounding is visible
fees_used, cost_breakdown,
dq_states: {...},
vetoes: [{tier, name, passed, inputs}],   # ALL, no short-circuit
arbitration: {conflicts, corroborations, suppressions}
```

`replay(signal_id)` rebuilds the snapshot from the bar store and re-runs the engine. **A signal
whose replay does not reproduce the original output is a bug** — a property worth asserting in CI
against a sample of stored signals.

---

## 9. Security architecture

Following directly from the Phase 0 finding that **only `data/stats` requires credentials**:

1. **One module reads the secret.** `src/ingest/authenticated.py`. Everything else runs
   unauthenticated. Enforce by review — an import of the settings' secret field anywhere else is
   a defect.
2. **`.env` gitignored before the first commit.** `.env.example` with placeholders committed.
3. **Redaction at two layers:** the HTTP client redacts headers in raw capture (`specs/01`), and
   a structlog processor redacts any value matching the secret before emission.
4. **No trading, no withdrawal permission** on the key. Nothing in the codebase may call an
   order, cancel, transfer or leverage endpoint.
5. **No balance reading.** No `users/balances`, no `futures/wallets` (`specs/08` §2.3).
6. **The dashboard binds to `127.0.0.1` by default.** It displays trading suggestions and
   account-shaped information; exposing it requires an explicit flag that logs a warning. Do not
   default to `0.0.0.0` — that decision belongs to the hosting phase, made deliberately.
7. **Parameter and code version recorded on every signal**, so a bad deploy is attributable
   rather than mysterious.

---

## 10. Failure domain map

| Failure | Blast radius | Handling |
|---|---|---|
| Recorder crash | **Unrecoverable Tier B gap** | Separate process; supervised restart; gap written to `_meta/gaps.jsonl` |
| App crash | Signals stop; nothing permanent lost | Restart; state rebuilt from store |
| `429` | Degraded polling | Adaptive backoff, halve rate for that host 10 min |
| `503` (documented maintenance) | Extended outage | Long capped backoff; `DQ_STALE` vetoes everything downstream — **the system goes quiet rather than emitting stale signals** |
| Laptop sleep | Gaps in both processes | Startup gap detection + candle backfill |
| Disk full | Writes fail | Free-space check in health endpoint; recorder alerts before the app |
| Corrupt Parquet | Feature computation fails | Checksums; rebuild from immutable raw JSONL |
| Clock drift | Freshness wrong; signed requests rejected | P15 monitoring; surfaced in health |
| Bad parameter deploy | Wrong signals | `parameter_hash` on every signal makes it attributable and reversible |
| Feature NaN | Strategy gate fails | Explicit NaN-with-reason; strategy declines rather than computing on garbage |

**Governing principle:** when in doubt, the system **emits nothing**. A quiet dashboard is a
correct response to degraded data. `NO VALID TRADE` is a first-class output (the
no-forced-signal principle), and that extends to infrastructure failure.

---

## 11. Explicitly rejected architecture

Recorded so they are not re-proposed:

- **Microservices** — failure domains and serialisation cost for no benefit at this scale
- **Message broker** (Kafka, RabbitMQ, Redis Streams) — nothing here needs durable queuing
- **Redis / separate cache** — the whole working set fits in memory
- **Postgres / TimescaleDB / InfluxDB** — operational overhead unjustified; SQLite + Parquet
  covers it, behind an interface that permits migration later
- **Kubernetes / orchestration** — two processes
- **WebSocket ingestion** — rejected in Phase 0's revision; at an hourly horizon it buys nothing
  and costs the Socket.IO version contradiction, depth-resync design, and reconnect handling
- **React/Next frontend** — build toolchain for a read-mostly dashboard
- **Prometheus + Grafana** — deferred to the hosting phase; the health endpoint and status panel
  suffice for a single-user local system

Every one of these becomes reasonable at a different scale. None is reasonable at this one.

---

## 12. Parameters — zero

Running total: **30 → 30. Unchanged.** Architecture is not a model. All 30 live in one versioned
TOML file whose hash is recorded on every signal.

---

## 13. Acceptance criteria for Phase 10

1. `MarketSnapshot` is immutable, and the strategy engine has **no** storage handle, no clock,
   and no I/O. Verify by inspection of imports in `src/strategies/`.
2. `forming_bar` is type-separated from `closed_bars`; no feature function accepts it.
3. `LiveDriver` and `BacktestDriver` produce structurally identical snapshots, with a test
   feeding the same `as_of` through both and asserting equality.
4. The `Fee` type makes the percent→fraction conversion unskippable, with an assertion that any
   fee entering the cost model is `< 0.01`. **Test the 100× error explicitly** — construct it and
   prove it is caught.
5. Normalisation converts every quantity in §5 to canonical form and **rejects** malformed input
   rather than coercing it.
6. `replay(signal_id)` reproduces the original signal exactly, asserted in CI over a sample.
7. `rejections` and `arbitration_events` are populated — vetoed and suppressed signals persist.
8. Only `src/ingest/authenticated.py` references the secret. The dashboard binds to localhost by
   default.
9. The recorder runs as a separate process and survives an app crash, demonstrated.
10. Property-based tests exist for every §6.1 property.
11. Storage sits behind an interface such that swapping SQLite for Postgres touches only
    `src/store/`.
