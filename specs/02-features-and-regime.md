# 02 — Feature Layer & Regime Model

**Depends on:** `specs/00-capability-map.md`, Phase 1 probe results.
**Status:** written *before* Phase 1 probes have run. Every dependency on an unresolved probe
is marked **⧗ PENDING Pn** with the branch to take for each possible outcome. Nothing here is
assumed to be true where it has not been verified.

---

## 1. Blocking dependencies

| Probe | Question | Branch if it fails |
|---|---|---|
| **P1** | Candle history depth | If 1h history < ~18 months, drop the 1d timeframe from *parameter fitting* (too few bars) and use it for context only. If < 6 months, the whole validation framework must shrink and Phase 9 needs rewriting. |
| **P11** | UTC bar alignment | If daily bars do **not** open at 00:00 UTC, every daily feature and every "prior day high/low" level must be redefined against the actual boundary, and the 4h aggregation grid must follow it. |
| **P12** | Aggregation exactness | If zero-volume bars are **omitted** rather than zero-filled, aggregation is unsafe. Fallback: drop 4h entirely and drop futures 15m; the stack reduces to natively-served resolutions only. |
| **P6** | Futures candle volume units | If quote-denominated, every futures volume feature needs division by typical price before it is comparable to spot. |
| **P3** | Spot market for BTC/ETH/SOL | If any is missing, that asset has no spot leg → no basis feature for it → Class 1 coverage becomes asymmetric. |
| **P8** | `data/stats` reachability | If unreachable, **delete Family 7 entirely**. Do not substitute a proxy. |
| **P10** | Basis signal-to-noise | If basis σ is dominated by measurement noise, Family 6 basis features are cut to funding only. |

**Do not implement a feature whose probe branch is unresolved.** Implement the Tier A
families first (§2) — none of them depend on the above except P1, P11 and P12.

---

## 2. The central structural decision: Tier A vs Tier B

This is the most important thing in this document.

**Tier A — backfillable.** Derived from OHLCV alone. Full history available from REST today.
**Can be backtested immediately.**

**Tier B — live-only.** Derived from funding, positioning, mark price, order book, or trade
tape. **No history exists anywhere.** These series begin accumulating the day the recorder
starts and not one second earlier.

| | Tier A | Tier B |
|---|---|---|
| Families | 1–5, 9 (partial) | 6, 7, 8, 9 (aggressor), 10 |
| History today | Full (pending P1) | Zero |
| Backtestable | Yes | **Not until N weeks of recording** |
| Walk-forward viable | Yes | Only after a full recording year for seasonal effects |

**Consequences that bind the whole project:**

1. Any strategy resting on Tier B features is **unvalidatable at launch**. It can be designed,
   implemented and shadow-run, but it cannot be backtested and must not be presented as
   though it has been.
2. The system therefore ships in two waves: **Tier A strategies validated first**, Tier B
   strategies added as their data matures.
3. Every signal carries a flag indicating whether its strategy was validated on historical
   data or is running unvalidated. This propagates to the UI. A user must never be unable to
   tell the difference.
4. Recording start date is a project milestone. Note it in `docs/findings.md`; every Tier B
   validation window is measured from it.

---

## 3. Timeframe decision record

**Selected stack.** Each entry states why it exists and why the alternatives were rejected.

| TF | Role | Source | Justification |
|---|---|---|---|
| **1d** | Regime context, structural levels, daily ATR for stop scale | Native both products | The swing horizon's outer bound. Defines the operative range a multi-day trade lives inside. |
| **4h** | Intermediate structure, swing legs | Aggregate 8× 30m? No — **aggregate 4× 1h** | 6 bars/day, clean UTC alignment, matches typical swing-leg duration. ⧗ PENDING P12 |
| **1h** | **Primary signal timeframe** | Native both products | Matches the fast end of the stated horizon. ~8,760 bars/year — the only timeframe with enough observations to fit anything. |
| **15m** | Entry-trigger refinement only. **Not a signal timeframe.** | Spot native; futures via 4× 5m | Lets the live-entry condition resolve within a fraction of an hourly bar instead of waiting for its close. |
| **1m** | Trigger evaluation and execution-quality checks. **Never alpha.** | Native both | Resolution at which a stop or trigger price is actually crossed. |
| **8h** | **Funding-cycle features only** (Family 6) | Aggregate 8× 1h | `funding_frequency = 8`. Funding features measured on an 8h grid align with settlement; measured on any other grid they straddle boundaries and mean less. |

**Rejected timeframes and why:**

- **2h, 6h, 12h** — 1h and 1d returns are driven by the same process; intermediate timeframes
  add near-duplicate information while multiplying the multiple-comparison surface. Each extra
  timeframe is a free opportunity to find a spurious result.
- **1w, 1M** — the horizon is days. Weekly structure is captured adequately by 1d lookbacks.
- **Tick / sub-minute** — no execution advantage at this horizon, large fragility cost.

**Multi-timeframe rule:** a signal timeframe is one that can *originate* a setup. Only **1h**
and **1d** originate setups. 4h and 8h *confirm or veto*. 15m and 1m *time the entry*. This
hierarchy is fixed and must not be blurred — "the 15m says go" is not a setup.

---

## 4. Shared lookback set — parameter economy

Rather than giving every feature its own tuned lookback, all features draw from **one shared
set per timeframe**. This cuts the parameter count by roughly an order of magnitude and
removes most of the curve-fitting surface.

| Timeframe | Short | Medium | Long |
|---|---|---|---|
| 1h | 24 (1 day) | 168 (1 week) | 720 (~1 month) |
| 4h | 6 (1 day) | 42 (1 week) | 180 (~1 month) |
| 1d | 7 | 30 | 90 |

These are calendar-anchored, not optimized. **Do not tune them.** If a feature appears to
need a different lookback, that is evidence about the feature, not a licence to add a
parameter — raise it as an open question.

---

## 5. Feature catalogue

Notation: `P` close, `H` high, `L` low, `O` open, `V` volume, `r_t = ln(P_t / P_{t-1})`,
`N` = lookback from §4. All rolling statistics are **trailing only** — never centred.

---

### Family 1 — Trend and directional state · **Tier A**

| ID | Inputs | Formula | TF | Update |
|---|---|---|---|---|
| `TRD_TSTAT` | close | OLS of `ln P` on bar index over `N`; report `β / SE(β)` | 1h, 4h, 1d | on bar close |
| `TRD_ER` | close | `\|P_t − P_{t−N}\| / Σ_{i=t−N+1}^{t} \|P_i − P_{i−1}\|` | 1h, 4h, 1d | on bar close |
| `TRD_RET` | close | `ln(P_t / P_{t−N})` for short/medium/long | 1h, 4h, 1d | on bar close |

**Why a t-statistic instead of moving-average crossovers:** it is scale-free, directly
answers "is this drift distinguishable from noise", and has one interpretation across assets
and timeframes. MA crossovers encode an arbitrary pair of lookbacks and say nothing about
significance.

**Limitations:** OLS on log price assumes roughly constant variance within the window; in a
volatility burst the t-stat inflates. `TRD_ER` is bounded [0,1] but saturates in strong trends.
`TRD_TSTAT` and `TRD_ER` are correlated (both rise with trend strength) but measure different
things — magnitude-vs-noise and path directness respectively. Both kept; flagged for §7.

---

### Family 2 — Volatility · **Tier A**

| ID | Inputs | Formula | TF | Update |
|---|---|---|---|---|
| `VOL_CC` | close | `sqrt( Σ(r_i − r̄)² / (N−1) )` | 1h, 4h, 1d | on bar close |
| `VOL_PARK` | H, L | `sqrt( Σ ln(H_i/L_i)² / (4N·ln2) )` | 1h, 4h, 1d | on bar close |
| `VOL_GK` | O,H,L,C | `sqrt( (1/N)Σ[ ½ln(H/L)² − (2ln2−1)ln(C/O)² ] )` | 1h, 1d | on bar close |
| `VOL_RATIO` | `VOL_CC` | `VOL_CC(short) / VOL_CC(long)` | 1h, 1d | on bar close |
| `VOL_OF_VOL` | `VOL_CC` | trailing std of `VOL_CC(short)` over medium | 1h, 1d | on bar close |
| `ATR` | O,H,L,C | `TR = max(H−L, \|H−C_prev\|, \|L−C_prev\|)`; **SMA** of TR over `N` | 1h, 4h, 1d | on bar close |

**Why SMA for ATR rather than Wilder's smoothing:** Wilder's recursive form has an effectively
infinite warm-up tail, so its value depends on where the series was seeded. That makes backtest
and live values differ for the same bar unless seeding is replicated exactly. SMA is
reproducible by construction. This costs a little responsiveness and buys exact reproducibility,
which the project values more.

**Limitations:** Parkinson and Garman-Klass assume continuous trading, zero drift and no
overnight jumps. Crypto trades continuously (good) but jumps hard (bad) — both estimators
understate volatility across a gap and `VOL_PARK` ignores drift entirely. Use `VOL_CC` as the
reference estimator and the other two as cross-checks; divergence between them is itself
informative about jump behaviour. `ATR` is near-collinear with `VOL_CC`; it exists **only** for
stop sizing in Phase 8, not as a regime input.

---

### Family 3 — Market structure · **Tier A**

| ID | Inputs | Formula | TF | Update |
|---|---|---|---|---|
| `STR_SWING` | H, L, `ATR` | Fractal pivot: bar `i` is a swing high if `H_i` exceeds the `k` bars either side **and** the excursion exceeds `θ·ATR` | 1h, 4h, 1d | on bar close |
| `STR_SEQ` | `STR_SWING` | Categorical: HH-HL / LH-LL / HH-LL / LH-HL / undefined | 1h, 4h, 1d | on new pivot |
| `STR_DIST_PDH` | prior-period H/L, close, `ATR` | `(P_t − PriorHigh) / ATR` and same for low | 1h | on bar close |
| `STR_RANGEPOS` | H, L, close | percentile rank of `P_t` within the `[min L, max H]` range over `N` | 1h, 4h, 1d | on bar close |

**Why a volatility-scaled pivot threshold rather than a fixed percentage:** a fixed 1% swing
threshold means something entirely different for BTC and SOL, and something different for the
same asset across volatility regimes. Scaling by ATR makes the structure definition comparable
across assets and time. `θ` is a registered parameter (§10).

**Limitations:** pivots are confirmed only `k` bars after the fact — **structure is inherently
lagging and must never be treated as real-time.** `STR_DIST_PDH` depends on the daily boundary
being correct (⧗ PENDING P11). `STR_SEQ` is undefined until at least two pivots of each type
exist.

---

### Family 4 — Deviation and mean reversion · **Tier A**

| ID | Inputs | Formula | TF | Update |
|---|---|---|---|---|
| `DEV_Z` | close | `(P_t − SMA_N) / σ_N`, trailing only | 1h, 4h, 1d | on bar close |
| `DEV_ATR` | close, `ATR` | `(P_t − SMA_N) / ATR_N` | 1h, 1d | on bar close |
| `DEV_VWAP` | typical price, V | `Σ(TP_i·V_i)/ΣV_i` over `N`; feature is `(P_t − VWAP)/ATR` | 1h, 1d | on bar close |
| `MR_AC1` | returns | lag-1 Pearson autocorrelation of `r` over `N` | 1h, 1d | on bar close |
| `MR_VR` | close | Lo–MacKinlay variance ratio, overlapping, heteroskedasticity-robust (below) | 1h, 1d | on bar close |

**Variance ratio, stated precisely.** With `n·k` observations and `μ` the mean 1-period
log return:

```
σ²(1) = Σ_{t=1}^{nk} (r_t − μ)² / (nk − 1)
σ²(k) = Σ_{t=k}^{nk} (ln P_t − ln P_{t−k} − kμ)² / m,
        m = k(nk − k + 1)(1 − k/(nk))
VR(k) = σ²(k) / σ²(1)
```

> **Amended 2026-10-03 (owner ruling on Q7, evidence in `docs/findings.md` F-031).** This line
> previously read `VR(k) = σ²(k) / (k · σ²(1))`. The factor `k` is already inside `m`, so dividing by
> it again biased every reading down by a factor of `k`: measured on a simulated random walk the old
> form gave **0.51, 0.26 and 0.14** at k = 2, 4, 8 instead of ~1.00. Under it every asset would have
> read mean-reverting permanently, the `MR_VR < 1` gates in C1-B (2), C1-C (7) and C2-A (3) would have
> passed forever, C2-A's `MR_VR > 1` would never have passed, and the regime model's P axis would have
> carried almost no information — all silently, since the numbers looked plausible. The corrected form
> is the standard Lo–MacKinlay ratio and matches the interpretation stated immediately below.

`VR > 1` → trending / positively autocorrelated. `VR < 1` → mean-reverting. `VR ≈ 1` → random
walk. Compute for `k ∈ {2, 4, 8}`.

> **Canonical `k` (owner ruling on Q-K, 2026-10-03).** Where any spec writes `MR_VR` without naming
> `k` — the regime model's P axis in §6, and the conditions in C1-B (2), C1-C (7) and C2-A (3) — it
> means **`k = 2`**. The other horizons are computed and published as `MR_VR_K4` and `MR_VR_K8`, and a
> condition that wants one of them must say so. Implemented as
> `features.f4_deviation.PRIMARY_VR`.

**Why VR matters more than it usually gets credit for:** it is the direct statistical test of
whether mean-reversion or momentum is currently the operative behaviour. Most systems assume
one and apply it permanently. This measures which is true right now, and is therefore the
primary input to the regime model in §6.

**Limitations:** VR is noisy in short windows and biased by a single large jump. `MR_AC1` is
the crude version of the same idea and is kept only as a sanity cross-check — it is highly
correlated with `VR(2)` and §7 will likely drop one of them. `DEV_VWAP` over a rolling window
is not the same object as an exchange session VWAP; there is no session here, and the
anchor choice is arbitrary. Treat it as a volume-weighted mean, nothing more.

---

### Family 5 — Cross-asset · **Tier A**

| ID | Inputs | Formula | TF | Update |
|---|---|---|---|---|
| `X_CORR` | returns, 3 assets | rolling Pearson correlation matrix over `N` | 1h, 1d | on bar close |
| `X_BETA` | returns | OLS slope of asset return on BTC return over `N` | 1h, 1d | on bar close |
| `X_RS` | close | `ln(P_asset,t/P_asset,t−N) − β·ln(P_BTC,t/P_BTC,t−N)` | 1h, 1d | on bar close |
| `X_DISP` | returns | cross-sectional std of the three assets' `N`-period returns | 1h, 1d | on bar close |

**Why `X_RS` is beta-adjusted rather than a raw return spread:** SOL is structurally
higher-beta than BTC. A raw spread would label SOL "strong" in every up-move and "weak" in
every down-move — measuring beta, not relative strength.

**The uncomfortable fact this family exists to expose:** BTC, ETH and SOL are typically
0.7–0.9 correlated. **Three simultaneous long signals are not three independent positions;
they are one leveraged position.** `X_CORR` is the input the Phase 8 risk engine uses to
enforce that. It is a risk feature first and an alpha feature second.

**Limitations:** rolling correlation is unstable and spikes toward 1 in stress — precisely
when the diversification assumption matters most. Lead-lag analysis is deliberately **excluded
from this catalogue**: with three assets and multiple lags it is a spurious-correlation
factory, and any lead-lag claim must clear a pre-registered test before it may be added.

---

### Family 6 — Funding and basis · **Tier B** ⧗ PENDING P8, P10

| ID | Inputs | Formula | TF | Update |
|---|---|---|---|---|
| `FND_RATE` | `fr` | as reported, per 8h interval | 8h grid | 60 s poll |
| `FND_PRED` | `efr` | as reported | 8h grid | 60 s poll |
| `FND_TENSION` | `fr`, `efr` | `efr − fr` | 8h grid | 60 s poll |
| `FND_CUM` | `fr` | Σ settled funding over trailing `k` intervals, `k ∈ {3, 9, 21}` (1d, 3d, 1w) | 8h grid | on interval close |
| `FND_Z` | `fr` history | `(fr − trailing mean) / trailing std` over accumulated history | 8h grid | on interval close |
| `FND_CAPDIST` | `efr` | `(0.02 − \|efr\|) / 0.02` — proximity to the ±2% cap | 8h grid | 60 s poll |
| `BAS_BPS` | `mp`, spot last | `10000 · (mp − spot_last)/spot_last`, **only if \|t_mp − t_spot\| ≤ 2 s** | continuous | 60 s poll |
| `BAS_Z` | `BAS_BPS` history | trailing z-score | continuous | 60 s poll |

**The settlement approximation, stated as a limitation rather than hidden.** We observe `fr`
as 60-second snapshots; we never observe the *settled* funding payment. `FND_CUM` therefore
approximates the settlement for an interval ending at `T` with the last `fr` observed before
`T`. This is an approximation of unknown error. It must be labelled as such everywhere it is
used, and it is a candidate for correction if a settlement-bearing endpoint is ever found.

**`BAS_BPS` has a hard gate for a reason.** An informal comparison during Phase 1 planning
showed a ~120-point spot/futures gap on BTC that was **entirely** explained by a 6.5-minute
fetch gap during which price moved. Basis computed from unsynchronized fetches measures
latency, not basis. The 2-second tolerance is not optional and the discard rate must be logged.

**Further limitations:** no funding history exists prior to recording start, so `FND_Z`'s
trailing window is short and unreliable for months. Per §7 of the capability map, basis here
is the **upstream venue's** basis, not a CoinDCX-local dislocation (⧗ PENDING P9), and must
never be framed otherwise.

---

### Family 7 — Crowd positioning · **Tier B** ⧗ PENDING P8

| ID | Inputs | Formula | Update |
|---|---|---|---|
| `POS_LONG_CNT` | `count_percent.long` | as reported | 15 min poll |
| `POS_LONG_VAL` | `value_percent.long` | as reported | 15 min poll |
| `POS_DIVERGENCE` | both | `count_percent.long − value_percent.long` | 15 min poll |
| `POS_Z` | history | trailing z-score of each | 15 min poll |
| `POS_ROC` | history | change over trailing 1d / 3d | 15 min poll |

**`POS_DIVERGENCE` is the most differentiated input this API offers.** An exchange publishing
*both* account-count-weighted and notional-value-weighted positioning is unusual. The spread
between them separates participation from size: strongly positive means many small accounts
are long while larger capital is not; strongly negative means concentrated large longs against
a dispersed crowd. Neither is a signal on its own — but the two are different market states
and no OHLCV feature can distinguish them.

**Limitations:** it is a *snapshot with no history*, so z-scores are unreliable until months
of recording exist. The population is CoinDCX's own users, not the market — this measures one
venue's retail base, which may or may not proxy anything broader. That limitation is
fundamental and no amount of processing removes it. If P8 fails, **delete this family; do not
substitute a proxy.**

---

### Family 8 — Liquidity and execution quality · **Tier B, risk-engine only**

| ID | Inputs | Formula | Update |
|---|---|---|---|
| `LIQ_SPREAD` | book L1 | `10000 · (ask − bid) / mid` | 5 min poll |
| `LIQ_DEPTH` | book | cumulative notional within 10 / 25 / 50 bps of mid | 5 min poll |
| `LIQ_IMBAL` | book | `(bid_notional − ask_notional)/(bid+ask)` within 25 bps | 5 min poll |

**`LIQ_IMBAL` is explicitly demoted and must not be used as alpha.** Book imbalance carries
real information on a seconds-to-minutes horizon and essentially none on a one-hour-to-daily
horizon. It exists here as a health and sanity check only. This is a rejection on **horizon
grounds**, not data grounds — the data is available and adequate; the timescale is wrong.

---

### Family 9 — Volume and participation · **Tier A (bar volume) / Tier B (aggressor)**

| ID | Inputs | Formula | Tier | Update |
|---|---|---|---|---|
| `VLM_ZROBUST` | V | `(V_t − median_N) / (1.4826 · MAD_N)` | A | on bar close |
| `VLM_TREND` | V | OLS slope of `ln V` over `N` | A | on bar close |
| `VLM_AGGR` | tape `m` flag | `(buy_vol − sell_vol) / total_vol` per bar | B | continuous |

**Why a robust z-score for volume:** volume is heavily right-skewed with fat tails. A
mean/std z-score is dominated by a handful of spikes and reports "normal" for almost
everything else. Median/MAD is the correct scale estimator for this distribution.

**Limitations:** volume units differ by endpoint (base on spot candles, quote on
`current_prices`, ⧗ PENDING P6 for futures candles) — **never compare across sources without
converting.** `VLM_AGGR` requires the full tape; with polling-only ingestion it is
reconstructible only approximately and should be treated as low-confidence.

---

### Family 10 — Data quality · **gates everything, never alpha**

| ID | Meaning | Threshold |
|---|---|---|
| `DQ_AGE` | seconds since the source's own timestamp | per-source, from P15 |
| `DQ_SKW` | `skw != 0` → row rejected | hard, binary |
| `DQ_COMPLETE` | expected vs received bars in the window | hard |
| `DQ_AGREE` | \|spot price − futures mark\| beyond a plausible bound | soft flag |
| `DQ_LATENCY` | local receive − `btST` | monitoring only |

Any feature whose inputs fail a hard gate is **NaN, not stale-forwarded**. A stale-forwarded
value is indistinguishable from a real one downstream, which is exactly how a data outage
becomes a trade.

---

## 6. Regime model

**Design stance:** no regime taxonomy is assumed. The number and boundaries of regimes are
derived from the data and must pass a validity test before use.

### Two axes

- **Axis V — volatility state**, from `VOL_RATIO` (short/long realized vol). Captures
  expansion vs contraction, which is the dimension that most changes how far price travels.
- **Axis P — directional persistence**, from `MR_VR` (variance ratio). Captures whether
  momentum or mean reversion is currently the operative behaviour.

These two axes are chosen because they are close to statistically orthogonal by construction:
one measures the scale of moves, the other their serial dependence. Adding a third axis is
prohibited without demonstrating it is not a linear combination of these two.

### Discretisation

Cut each axis at **quantiles of its own trailing distribution**, not at fixed numbers. Fixed
thresholds are the single most common form of accidental curve-fitting in regime models.
Start with terciles → a 3×3 grid, 9 cells.

### Validity tests — a regime model must pass all three

1. **Occupancy.** Every retained cell must contain ≥ 5% of observations in the training
   window. Collapse cells that do not, and re-test.
2. **Persistence.** Build the cell-to-cell transition matrix. It must be **diagonally
   dominant** — median dwell time of at least ~3 bars on the classifying timeframe. A label
   that flips every bar is not a regime, it is a noisy indicator with a fancy name. If
   persistence fails, coarsen to 2×2 and re-test.
3. **Separation.** Forward return distributions must differ measurably across cells —
   Kruskal–Wallis or equivalent. If they do not, the model has no economic content and should
   be reduced or abandoned rather than kept for decoration.

**If all three fail at every granularity, report that.** A finding of "no stable regime
structure" is a legitimate and useful result, and is far better than shipping a model that
labels noise.

### Constraints

- Classification at bar `t` uses **only data through bar `t`'s close**. No centring, no
  smoothing that peeks forward.
- Regimes are computed **per asset**, plus one market-wide regime from BTC. Strategies may
  require agreement between them.
- Regime is an **input to strategy gating** (Phase 3–6) — it never generates a signal itself.
- The transition matrix, dwell times and occupancy are published to the audit trail. A regime
  label with no supporting statistics is not acceptable output.

---

## 7. Redundancy protocol — run before any composite score exists

Summing correlated features is the fastest way to build something that looks like conviction
and is actually one signal counted five times.

**Procedure, run on the training window only:**

1. Compute the full pairwise Spearman correlation matrix across every feature, per timeframe.
   Spearman, not Pearson — several of these features are bounded or non-linear.
2. Hierarchically cluster on distance `1 − |ρ|`, average linkage.
3. Cut at `|ρ| ≥ 0.8`. Within each resulting cluster, retain **one** representative.
4. **Choose the representative by stability, never by performance.** Pick the feature with
   the lowest variance of its own rolling estimate. Choosing by backtest performance is
   textbook data snooping and would contaminate everything downstream.
5. Report the variance inflation factor of the retained set.
6. Publish the full matrix and the cluster assignments to `docs/findings.md`.

**Known clusters to expect** (a test of whether the procedure works): `VOL_CC` / `VOL_PARK` /
`VOL_GK` / `ATR`; `MR_AC1` / `MR_VR(2)`; `TRD_TSTAT` / `TRD_RET`. If the procedure does not
find these, it is broken.

---

## 8. Look-ahead prevention

Every one of these has caused a wrong backtest in someone's career.

1. **Closed bars only.** A feature at bar `t` may use bar `t` only after it has closed. The
   in-progress bar is available to the *trigger* logic (§3, 1m/15m role) and to nothing else.
2. **Spot candles are sorted DESCENDING** `[VERIFIED]`. Off-by-one here inverts time. Write an
   explicit test asserting monotonic ascending time *after* normalization, and assert the last
   row is the newest.
3. **Rolling statistics are trailing.** No centred windows, no `center=True`, no full-sample
   mean or std anywhere in feature computation.
4. **Funding has a subtle trap.** `fr` for the current interval is observable *during* the
   interval, but the settlement is not known until the interval ends. Using an intra-interval
   `fr` as if it were the settled rate is look-ahead. `FND_CUM` may only include intervals
   that have **closed**.
5. **Structure lags by construction.** A swing pivot needs `k` bars of confirmation. The
   feature timestamp must be the **confirmation** bar, not the pivot bar.
6. **Regime labels** use trailing quantiles only — never quantiles of the full sample.
7. **Backfill vs live must agree.** For any bar computable both ways, assert equality. A
   mismatch means the live path is using information the backfill path did not have, or the
   reverse. Build this test early; it catches more than any other single check.

---

## 9. Warm-up requirements

No signal may be emitted before every input feature is warm.

| Timeframe | Longest lookback | Required warm-up | Wall-clock |
|---|---|---|---|
| 1h | 720 | 1,440 bars (2×) | ~60 days |
| 4h | 180 | 360 bars | ~60 days |
| 1d | 90 | 180 bars | ~180 days |

Regime quantiles need a further multiple to be stable — require **≥ 2× the longest feature
warm-up** before emitting a regime label. The engine must expose per-feature warm-up state and
refuse, loudly, rather than emit a signal from a half-warm feature.

---

## 10. Parameter registry and budget

**Hard cap: 25 tunable parameters across the entire system.** Every one lives in a single
config file with a stated justification. Adding one requires removing one or explicitly
raising the cap with a written reason.

**Registered so far:**

| # | Parameter | Where | How it is set |
|---|---|---|---|
| 1–9 | Shared lookbacks (3 TFs × short/med/long) | §4 | **Fixed by calendar logic. Not tuned.** |
| 10 | `θ` — swing pivot ATR multiple | `STR_SWING` | Calibrate once on training data; single value shared across assets and timeframes |
| 11 | `k` — pivot confirmation bars | `STR_SWING` | Fixed at a small odd integer; not tuned |
| 12 | VR horizons `k ∈ {2,4,8}` | `MR_VR` | Fixed; not tuned |
| 13 | Redundancy cut `\|ρ\| ≥ 0.8` | §7 | Fixed; conventional |
| 14 | Regime quantile count | §6 | Determined by the §6 validity tests, not chosen |
| 15 | Minimum cell occupancy 5% | §6 | Fixed |
| 16 | Basis timestamp tolerance 2 s | `BAS_BPS` | Fixed; from measured latency |
| 17–25 | **Reserved for Phases 3–8** | — | Strategy thresholds, stop/target multiples, sizing |

**Everything else is a threshold, and no threshold is set in this document.** Thresholds are
calibrated in Phase 16 against a pre-registered protocol defined in Phase 9. Writing a
threshold here, before the validation framework exists, would be fitting the design to
intuition about data nobody has examined yet.

---

## 11. Explicit rejection list

Rejected for **data unavailability** — no proxy is permitted:

- Open interest, OI change, OI/price divergence
- Liquidation events, cascades, liquidation-cluster targeting
- Implied volatility, vol surface, skew, term structure
- Order-flow toxicity / VPIN and anything needing message-level adds and cancels
- Iceberg / hidden-liquidity detection
- Cross-venue dislocation and triangular arbitrage
- On-chain flows, exchange reserves, whale tracking
- News, social sentiment, funding-rate aggregators across venues

Rejected for **horizon mismatch** — data exists, timescale is wrong:

- Order-book imbalance as an alpha signal (retained as a health check only, Family 8)
- Microprice, queue position, short-term book pressure
- Sub-minute momentum or reversal

Rejected for **methodological risk**:

- Lead-lag relationships among BTC/ETH/SOL, until a pre-registered test is passed
- Any feature requiring a per-asset tuned lookback
- Composite scores built before §7 has run

---

## 12. Acceptance criteria for Phase 2

1. Every Tier A feature is implemented, unit-tested against **hand-computed** values on a
   small fixture, and produces identical output via the backfill and live paths (§8.7).
2. The redundancy protocol (§7) has run on the full backfill; the matrix, clusters and
   retained set are published to `docs/findings.md`.
3. The regime model has been fitted and has **passed or failed** all three validity tests,
   with the transition matrix and dwell-time distribution published. A failure is a valid
   outcome and must be reported as one.
4. Warm-up state is exposed per feature per asset per timeframe, and the engine demonstrably
   refuses to emit when any input is not warm.
5. The parameter registry file exists with every entry justified, and the count is ≤ 25.
6. Every ⧗ PENDING branch in §1 is resolved, with the taken branch recorded.
7. Tier B features are implemented but explicitly marked unvalidated, with their earliest
   possible validation date computed from the recording start date.
