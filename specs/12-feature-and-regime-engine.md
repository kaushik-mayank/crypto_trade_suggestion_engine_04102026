# 12 — Feature Engine & Regime Engine

**Implements:** `specs/02`. **Depends on:** `specs/10` (snapshot boundary), `specs/11` (storage,
gaps, finalization).
**Character:** implementation spec. Buildable without further design decisions.

---

## 1. The computation model: recompute from window, never accumulate

Two options existed. The choice matters more than it looks.

| | Stateful incremental | **Recompute from window (chosen)** |
|---|---|---|
| Live speed | Faster | Fast enough — one window per feature per update |
| Backtest speed | Faster | Adequate when vectorised (§7) |
| **Parity risk** | **High** — accumulator state, seeding differences and floating-point accumulation cause live and backtest to diverge | **None** — same function, same inputs, same output |

> **No feature holds accumulator state between calls.** Every value is computed from an explicit
> window of bars.

This is the engine-level form of the same reasoning that chose SMA over Wilder's smoothing for
ATR (`specs/02` §5): a recursive estimator's value depends on where it was seeded, so backtest
and live disagree for the same bar unless seeding is replicated exactly. At this data volume the
performance cost of recomputation is negligible and the correctness gain is total.

---

## 2. Feature contract

`specs/10` requires every feature to be present or **NaN with an explicit reason** — never
silently absent.

```python
class FeatureValue:
    name: str
    version: str                 # bumps when the formula changes
    value: float | None
    status: OK | WARMING | GAP_IN_WINDOW | NUMERICAL_FAILURE | DQ_FAILED | INSUFFICIENT_DATA
    reason: str | None           # human-readable, e.g. "need 720 bars, have 431"
    window_start_ms: int
    window_end_ms: int
    bars_used: int
    computed_at_ms: int
```

A strategy must be able to distinguish *"this feature is 0.3"* from *"this feature could not be
computed because the window contained a gap."* Conflating them is how a data outage becomes a
trade.

**`FeatureSet` is the immutable container passed to strategies.** Accessing a non-`OK` feature
raises rather than returning `None` — a strategy must check status explicitly, which makes the
handling visible at review.

---

## 3. The Decimal/float boundary

Prices are `Decimal` in storage and in published levels (`specs/10` §5). Statistics cannot be
computed in `Decimal` — numpy and scipy do not accept it, and the precision buys nothing for a
t-statistic.

> **Explicit, single-point conversion:**
> `Decimal → float64` on entry to feature computation.
> `float64 → Decimal → round to tick` on any value that becomes a published price.

Encode this as two named functions, not as scattered casts. Any feature output that flows into a
stop, target or entry level passes through the return conversion, and tick rounding follows
`specs/08` §10 (stops away from entry, targets toward).

---

## 4. Numerical stability guards

Each degenerate case returns `NUMERICAL_FAILURE` with a specific reason rather than a number.

| Feature | Degenerate case | Guard |
|---|---|---|
| `TRD_TSTAT` | `SE(β) → 0` on a perfectly straight series → t → ∞ | If `SE(β) < ε` relative to `|β|`, fail with `ZERO_RESIDUAL_VARIANCE` |
| `TRD_ER` | Denominator `Σ|ΔP| = 0` (no movement in window) | Fail with `NO_MOVEMENT` |
| `MR_VR` | `σ²(1) ≈ 0` | Fail with `ZERO_VARIANCE` |
| `MR_AC1` | Zero variance in returns | Fail with `ZERO_VARIANCE` |
| `DEV_Z`, `FND_Z`, `BAS_Z` | Trailing std `≈ 0` | Fail with `ZERO_STDDEV` |
| `VLM_ZROBUST` | `MAD = 0` (≥ half the window identical) | Fail with `ZERO_MAD` |
| `X_CORR`, `X_BETA` | Zero variance in either series | Fail with `ZERO_VARIANCE` |
| `VOL_GK` | Negative variance | **See §4.1 — this is a diagnostic, not a guard** |
| Any | `inf` or `nan` in output | Fail with `NON_FINITE`; log the inputs |

### 4.1 Garman–Klass negativity is a data-validity alarm

For the single-bar form specified in `specs/02` Family 2, with `r = ln(H/L)` and `c = ln(C/O)`:

```
σ²_GK = 0.5·r² − (2ln2 − 1)·c²
```

Valid OHLC guarantees `L ≤ min(O,C) ≤ max(O,C) ≤ H`, hence `|c| ≤ r`, hence
`σ²_GK ≥ (0.5 − 0.386)·r² > 0`.

> **Therefore a negative Garman–Klass variance is mathematically impossible on valid OHLC. If
> one appears, the bar is invalid and normalization failed to catch it.**

Do not clamp it to zero. Raise, log at ERROR, write a `dq_events` row of type `INVALID_OHLC`, and
add the OHLC ordering assertion to `specs/11`'s normalization tests if it is not already firing
there. Clamping would convert a live data-integrity alarm into a silently wrong volatility
estimate.

---

## 5. Warm-up gating

Each feature declares `min_bars_required`, derived from its lookback (`specs/02` §4, §9).

| Timeframe | Longest lookback | Required | Wall-clock |
|---|---|---|---|
| 1h | 720 | 1,440 | ~60 days |
| 4h | 180 | 360 | ~60 days |
| 1d | 90 | 180 | ~180 days |
| Regime | — | 2× the longest feature warm-up | — |

Below threshold: `status = WARMING`, `reason = "need N bars, have M"`.

The engine exposes warm-up state **per feature, per asset, per product, per timeframe** through
the health endpoint and the dashboard status panel. Per `specs/02` §12.4 it must **refuse
loudly** rather than emit from a half-warm feature — a strategy receiving `WARMING` declines and
records that as the failing condition.

---

## 6. Gaps inside a computation window

### 6.1 Unconfirmed gap → refuse

If the window contains a missing bar **not** present in `known_absent_bars` (`specs/11` §7.2),
return `GAP_IN_WINDOW`. We do not know whether data is missing or the bar never existed, and
computing either way would produce a number whose meaning is unknown.

### 6.2 Confirmed-absent bar → treat as a no-trade bar

A bar confirmed absent means **no trades occurred**, which is real information rather than
missing data. Represent it as `O = H = L = C = prior close`, `volume = 0`.

**Rule by timeframe:**

- **1h, 4h, 1d** — any absent bar in the window returns `GAP_IN_WINDOW` regardless of
  confirmation. At these horizons on BTC/ETH/SOL an absent bar indicates something wrong, not a
  quiet market, and computing through it would hide that.
- **1m** — synthesise the no-trade bar and continue.

### 6.3 Why the 1m synthesis is safe

Synthetic flat bars create artificial zero returns, which biases volatility estimates **downward**.

That bias is confined because `specs/02` §3 restricts 1m to **trigger evaluation and
execution-quality checks — never alpha.** No alpha feature is computed on 1m, so the bias cannot
reach a signal's content. Record the synthesised-bar count on any 1m-derived value so the
dependency is visible.

---

## 7. Vectorised and windowed paths must agree

Backtest needs every bar's value across a long history; live needs only the latest. Two
implementations are unavoidable for performance:

```python
compute_series(bars, params) -> list[FeatureValue]   # vectorised — backtest
compute_at(bars, as_of, params) -> FeatureValue      # single window — live
```

> **Required test, per feature: `compute_series` and `compute_at` agree on every bar of a
> fixture, to floating-point tolerance.**

This is the feature-level analogue of the backfill/live equivalence test in `specs/02` §8.7 and
`specs/09` §9, and it catches the same class of bug one layer lower. A feature without this test
passing is not considered implemented.

Vectorise with Polars rolling operations where the formula permits. `TRD_TSTAT` and `MR_VR`
require a windowed loop — write the loop, then optimise only if profiling shows it matters.

---

## 8. Caching and versioning

Features are pure functions of `(bars, params)`, so caching is safe and keyed by:

```
(series_id, feature_name, feature_version, as_of_ms, parameter_hash)
```

**Version bumps invalidate the cache automatically.** A formula change that does not bump the
version is a defect — it would serve stale values computed by the old formula, which is worse
than no cache.

`feature_version` is recorded on every `FeatureValue` and propagates into the audit record
(`specs/10` §8), so a signal's derivation names the exact formula that produced it.

---

## 9. Regime engine

### 9.1 Fitted offline, loaded live

The regime model is **fitted offline** on the training window and shipped as an artifact. The
live engine loads and applies it; it never refits.

```
regime_model.json:
  model_version, fitted_on_window, grid_shape,
  axis_definitions: { V: VOL_RATIO(1h), P: MR_VR(1h) },
  quantile_lookback,
  transition_matrix,          # for regime_persistence_prob (specs/05 §4)
  dwell_statistics,           # median dwell per cell
  occupancy,                  # per cell
  validity_test_results       # pass/fail for all three, with statistics
```

Refitting is a deliberate, versioned event recorded in `docs/findings.md`, not a background job.
A silently drifting regime model would make every regime-gated backtest result unreproducible.

### 9.2 Trailing quantiles only

Cut points come from **trailing** quantiles of each axis over `quantile_lookback` — never
full-sample. This means cell boundaries move over time, which is correct and must be recorded:
`RegimeState` carries the boundary values actually used, so the classification is replayable.

### 9.3 Runtime state

```python
class RegimeState:
    cell: tuple[int, int]
    cell_label: str                  # e.g. "expanding / trending"
    dwell_bars: int
    entered_at_ms: int
    axis_values: {vol_ratio, mr_vr}
    axis_boundaries: {...}           # the trailing quantiles used — for audit
    persistence_prob: float | None   # from the transition matrix; C3-B only
    model_version: str
```

`dwell_bars` is consumed by the stability conditions in C1-B (4), C2-A (7) and C3-A (see the
class specs). `persistence_prob` is labelled a **regime statistic, never a trade probability**
(`specs/05` §4).

---

## 10. ⚠ Contingency: what happens if the regime model fails its validity tests

`specs/02` §6 states that "no stable regime structure" is a legitimate result. It is also a
**project-threatening** one, because most strategies carry a regime gate. Design the fallback
now rather than improvising it under pressure.

### Escalation

1. Fit at 3×3. Run occupancy, persistence and separation.
2. Any test fails → collapse to **2×2**, refit, retest.
3. Still fails → **the discretised regime model is unusable.**

### The degraded mode

Inspect what actually depends on the *cell* versus the *raw axis*:

| Condition | Depends on | Survives failure? |
|---|---|---|
| C1-B cond 2 — `MR_VR < 1` | Raw axis | ✅ |
| C2-A cond 3 — `MR_VR > 1` | Raw axis | ✅ |
| C3-B cond 1 — `MR_VR` crossing | Raw axis | ✅ |
| C3-A cond 1 — `VOL_RATIO` percentile | Raw axis | ✅ |
| **"regime cell stable ≥ median dwell"** (C1-B 3, C2-A 7, C3-B 7) | **The cell** | ❌ |
| `regime_persistence_prob` (C3-B) | Transition matrix | ❌ |
| Regime-conditioned reporting (`specs/09` §6) | The cell | ❌ |

> **Degraded mode: drop the dwell-stability conditions, keep every raw-axis threshold.**

The system survives with **reduced selectivity**, not with broken strategies — the raw axis
measurements were always doing the substantive work, and the cell was providing a stability
filter on top. Consequences to record:

- C3-B publishes `regime_persistence_prob: null`, with `confidence_unavailable_reason` set.
- `specs/09` §6 regime-conditioned analysis is replaced by conditioning on **volatility tercile
  alone**, which is still informative and still tests hypothesis H8.
- The degradation is surfaced in the dashboard status panel. A user should be able to see the
  system is running with a weaker filter set.

**Record the outcome either way in `docs/findings.md`.** A regime model that fails its tests is
a substantive finding about this market at this horizon.

---

## 11. Redundancy protocol — an offline design input, not a runtime filter

`specs/02` §7 runs **offline** on the training window and produces:

```
feature_selection.json:
  spearman_matrix, clusters, retained_representatives,
  vif_report, selection_rationale   # stability-based, never performance-based
```

Its output does **not** filter features at runtime — the live engine computes everything, which
is cheap. It drives exactly two specification decisions, which must be recorded:

1. **C1-B condition 4** (`specs/03` §4): if `ρ(DEV_Z, STR_DIST_PDH) ≥ 0.8`, the condition is
   demoted from a **gate** to a **strength input only**.
2. **`er_min`** (`specs/04` §8): if `TRD_TSTAT` and `TRD_ER` collapse into one cluster,
   **C2-A condition 4 is deleted** and parameter 24 is never registered.

Hypothesis H9 (`specs/09` §11) predicts the clusters this should find. If it does not find them,
the protocol is broken and must be fixed before its output is acted on.

---

## 12. Module layout

```
src/features/
├── base.py            FeatureValue, FeatureSet, the compute_series/compute_at protocol
├── numeric.py         Decimal↔float boundary, stability guards, ε definitions
├── windows.py         window extraction, gap checks, warm-up accounting
├── trend.py           Family 1
├── volatility.py      Family 2
├── structure.py       Family 3
├── deviation.py       Family 4    (includes MR_VR — the Lo–MacKinlay estimator)
├── cross_asset.py     Family 5
├── funding.py         Family 6    (Tier B)
├── positioning.py     Family 7    (Tier B, gated on P8)
├── liquidity.py       Family 8    (risk inputs only, never alpha)
├── volume.py          Family 9
└── registry.py        name → implementation, version, min_bars_required, tier

src/regime/
├── model.py           RegimeState, classification against a loaded artifact
├── fit.py             offline fitting + the three validity tests
├── transitions.py     transition matrix, dwell statistics
└── degraded.py        §10 fallback

src/analysis/
└── redundancy.py      §11, offline
```

---

## 13. Tests

| Test | Asserts |
|---|---|
| **Hand-computed fixtures** | Every formula matches a value computed by hand on a small fixture. Required for all of Families 1–9 (`specs/02` §12.1) |
| **Series/window parity** | `compute_series` == `compute_at` on every bar, every feature (§7) |
| **No look-ahead** | For every feature at bar `t`, `window_end_ms <= close_time(t)`. Automated across the registry, not per feature |
| **Warm-up** | Status is `WARMING` below threshold and `OK` exactly at it |
| **Unconfirmed gap** | `GAP_IN_WINDOW` returned; nothing computed |
| **Confirmed absent, 1h** | `GAP_IN_WINDOW` (§6.2) |
| **Confirmed absent, 1m** | Synthetic flat bar; `synthesised_count` recorded |
| **Each numerical guard** | Construct the degenerate input; assert `NUMERICAL_FAILURE` with the right reason |
| **GK negativity** | Feed deliberately invalid OHLC; assert raise + `INVALID_OHLC` event, **not** a clamped zero |
| **Decimal boundary** | A feature-derived price level is `Decimal` and tick-aligned |
| **Cache invalidation** | Bumping `feature_version` invalidates cached values |
| **Regime trailing quantiles** | Classification at bar `t` uses no data after `t` |
| **Regime dwell** | Dwell resets on cell change and increments otherwise |
| **Degraded mode** | With a failed model, dwell conditions drop out and raw-axis conditions still evaluate |
| **Property (Hypothesis)** | Z-scores are scale-invariant; `TRD_ER ∈ [0,1]`; `MR_VR > 0`; correlation ∈ [−1,1] |

---

## 14. Parameters — zero

Running total: **30 → 30. Unchanged.**

`ε` thresholds for the stability guards, the quantile lookback, and cache sizes are **numerical
and operational configuration**. They can turn a value into a `NUMERICAL_FAILURE`, but they
cannot change a successfully-computed value — so they are not model parameters. Record them in
the settings file with their rationale.

---

## 15. Acceptance criteria for Phase 12

1. **No feature holds state between calls.** Verify by inspection — a module-level accumulator or
   an instance attribute persisting across calls is a defect.
2. `compute_series` and `compute_at` agree on every bar for every feature, under test.
3. Every feature has a hand-computed fixture test.
4. The automated no-look-ahead check runs across the whole registry and passes.
5. Warm-up state is exposed per feature / asset / product / timeframe and surfaced in the health
   endpoint.
6. Every numerical guard has a test constructing its degenerate case.
7. Negative Garman–Klass **raises and alarms**; it is never clamped.
8. Unconfirmed gaps produce `GAP_IN_WINDOW`; confirmed-absent handling differs by timeframe per
   §6.2; the 1m synthesis records its count.
9. The regime model is fitted offline, versioned, and loaded — never refitted at runtime.
10. All three validity tests run and their results are recorded in `docs/findings.md`, **including
    a failure**, with the degraded mode engaged and visible in the UI if they fail.
11. The redundancy protocol has run, and both dependent decisions (C1-B condition 4,
    `er_min`) are recorded with their outcome.
12. Hypothesis H9's predicted clusters are checked; a mismatch halts use of the protocol's output
    until resolved.
