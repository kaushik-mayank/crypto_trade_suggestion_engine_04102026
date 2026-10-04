# 09 — Validation Protocol

**Depends on:** all prior specs. **Executed by:** Phase 16.
**This document must be finalised and committed BEFORE any backtest is run.**

---

## 0. Statement of fact

**No strategy in this system has been tested. No backtest has been run. No performance figure of
any kind exists.**

Nothing in this document reports a result. It specifies how results will be produced and judged,
written in advance so that the judging cannot be adjusted once results are visible. Every number
appearing here is a threshold or a window boundary, never an outcome.

---

## 1. Pre-registration — the mechanism, not the aspiration

A protocol that can be edited after seeing results is not a protocol. Enforce it mechanically.

### 1.1 The lock file

Before the first backtest, commit `validation/protocol.lock.json` containing:

```
protocol_version, created_utc, git_commit,
window_boundaries,            # absolute UTC dates, not relative offsets
parameter_set,                # all 30, with their tested bands
metric_definitions,           # exact formulas
decision_rules,               # §12
hypotheses,                   # §11 — the falsifiable predictions
trial_count_declared          # §7.2
sha256                        # of the above, self-referencing
```

Window boundaries are **absolute dates**. Relative offsets ("last 20%") silently shift as data
accumulates, which is a slow-motion way of moving the goalposts.

### 1.2 The holdout is physically separated and burned on contact

The final holdout period is written to a separate directory that the backtest harness **cannot
read** in normal operation. Loading it requires an explicit flag that logs the event with a
timestamp and the protocol hash.

> **The holdout may be used exactly once, after all decisions are final. A second use burns it —
> it becomes in-sample forever and a new holdout must be carved from future data that does not
> yet exist.**

### 1.3 Protocol changes

Any change after the first run creates `protocol_version + 1`, recorded in `docs/findings.md`
with the reason. Changes made *after* seeing results **burn the holdout**. This is the intended
consequence, not a penalty to be worked around.

---

## 2. Data partitioning ⧗ PENDING P1

Window sizes depend on total available history, which probe P1 has not yet measured.

### 2.1 Rolling walk-forward, not anchored

Train on a **fixed-length rolling window**, not an expanding one. An expanding window lets the
earliest regimes dominate the fit, and the question this system actually needs answered is
whether a strategy survives regime change. A rolling window tests that; an anchored one hides it.

```
[--- train ---][test][--- train ---][test] ...
      ↑ rolls forward by exactly one test length each fold
```

### 2.2 Minimum viable window sizes

Constrained from below by feature warm-up (`specs/02` §9: 1,440 × 1h bars ≈ 60 days for the
longest lookback, and ≥ 2× that for stable regime quantiles):

| | Minimum | Rationale |
|---|---|---|
| Train | **6 months** | ≥ 2× longest warm-up, plus enough trades to fit anything |
| Test | **1 month** | Enough trades to be non-trivial at the designed frequencies |
| Step | = test length | Non-overlapping out-of-sample periods |
| Holdout | **most recent 15%**, minimum 2 months | Touched once, at the end |

### 2.3 Branches on P1's answer

| P1 result (1h history) | Consequence |
|---|---|
| **≥ 3 years** | Full protocol. ~24+ walk-forward folds. Proceed as written. |
| **18 months – 3 years** | ~6–18 folds. Workable but thin. Report fold count prominently with every result; a strategy passing on 6 folds is far weaker evidence than one passing on 24. |
| **< 18 months** | Walk-forward has ≤ 6 folds and is **statistically weak**. Do not treat any result as validation. Reduce to a single train/holdout split, report it as exploratory, and state explicitly that no strategy can be promoted to `validated` on this basis. |
| **< 9 months** | **Stop.** No meaningful validation is possible. Report this to the user as a project-level constraint rather than producing results that look like validation and are not. |

The 1d timeframe will always have fewer observations than 1h. If daily-timeframe features cannot
be fitted, use 1d for **context and gating only** and fit nothing on it — which is already how
`specs/02` §3 assigns its role.

---

## 3. Fill and cost model — where naive backtests lie

The single largest source of inflated backtest results is optimistic fills. Specify them
precisely and conservatively.

### 3.1 Entry fills

Signals trigger on a confirmed 1m close (`specs/03` §2).

> **Entry fills at the worse of {trigger price, next 1m bar open}, plus slippage.**

Never at the trigger price itself. The confirmation happens at the close; the earliest realistic
action is the next bar.

### 3.2 Stop fills — the gap rule

> **If a bar's range gaps beyond the stop, the fill is the bar's OPEN, not the stop price.**

This is the most important rule in this section. Naive backtests assume stops fill at the stop
level; in crypto they frequently do not, and the difference is concentrated in exactly the tail
events that determine whether a strategy survives.

Concretely: if a long's stop is at 100 and the next bar opens at 96 and trades to 94, the fill is
**96**, producing a loss of 1.33R on a trade the backtest would otherwise record as −1.00R.

Report the **distribution of realised R on stopped trades**. If the mean is materially worse than
−1.0R, the strategy's real risk exceeds its published risk, and `liq_safety` (`specs/08` §6)
should be re-derived from this measured distribution.

### 3.3 Target and trail fills

- T1 fills at the target price if the bar's range contains it — targets are limit orders and do
  not suffer the gap problem in the adverse direction.
- Trailing stops follow the §3.2 gap rule.
- **Intrabar ambiguity:** when a single bar contains both the stop and the target, assume the
  **stop** was hit first. Always. This is the conservative assumption and it must not be
  resolved by looking at finer data selectively — doing so on some bars and not others
  introduces bias whose direction is impossible to reason about.

### 3.4 Costs

Exactly as `specs/08` §7, via the **same module** the live system uses (`specs/08` acceptance
criterion 10). Fees live per instrument; slippage at the 75th percentile of P14 measurements;
funding accrued per 8h interval crossed.

**Sensitivity requirement:** re-run every headline result at **2× the slippage assumption**.
A strategy whose edge disappears at double slippage is not robust enough to deploy, because the
slippage estimate itself carries error.

---

## 4. Trade accounting

| Event | Counts as |
|---|---|
| `SETUP_DETECTED` → `EXPIRED` | Not a trade. Logged for frequency analysis (§11). |
| `SUPPRESSED` by arbitration | Not a trade. Logged — needed to measure what arbitration costs. |
| `MISSED` (`specs/07` §8.2) | Not a trade. Logged; a high `MISSED` rate indicates the trigger is too slow. |
| `TRIGGERED` → terminal | **One trade.** |
| Partial exit at T1, remainder trailed | **One trade**, P&L = weighted sum of both exits. |
| **Handoff** (`specs/07` §8.3) | **One trade** spanning both strategies. P&L attributed to the originator up to handoff, the receiver after. Never two trades. |

Counting handoffs as two trades would inflate trade counts and distort both strategies'
statistics in opposite directions.

---

## 5. Required metrics

### 5.1 Per-trade distribution — reported for every strategy

- Hit rate, **with 95% Wilson interval and n**
- Mean and **median** R multiple (they diverge sharply for skewed payoffs; reporting only the
  mean hides which)
- **Full histogram of R multiples** — not summary statistics alone
- Worst trade, best trade
- Realised R on stopped trades (§3.2)

### 5.2 Profit concentration — and its strategy-dependent reading

```
top5_dependency     = P&L of the 5 best trades / total P&L
top_decile_share    = P&L of the top 10% of trades / total P&L
```

> **If removing the 5 best trades makes a strategy unprofitable, it is a lottery, not an edge.**

But the interpretation depends on the strategy's intended payoff shape, and this must not be
applied uniformly:

- **Class 2, Class 3, C4-A, C4-B** — long-gamma by design. **High concentration is expected and
  correct.** Its absence suggests the trailing exit is cutting winners.
- **Class 1, C4-C** — short-gamma or mean-reverting. **High concentration is a red flag**: it
  means the edge rests on one or two lucky events rather than a repeatable process.

### 5.3 Portfolio level

Maximum drawdown and its duration; time in market; distribution of concurrent positions;
realised portfolio volatility against `portfolio_vol_target`; the distribution of
`effective_independent_positions`.

### 5.4 Risk-adjusted — and what is explicitly insufficient

Sharpe, Sortino, and MAR (return / max drawdown).

> **Sharpe alone is rejected as sufficient output for any strategy, and is specifically
> insufficient for C4-C.**

Sharpe under-penalises rare large losses, which flatters short-gamma profiles precisely where
the danger lives. Per `specs/06` §4.3, C4-C's evaluation **must** lead with maximum drawdown,
worst single trade, and the loss-distribution tail. A high hit rate and a good Sharpe on C4-C are
expected and prove nothing.

---

## 6. Regime-conditional analysis

Every strategy reports performance **broken down by regime cell** (`specs/02` §6), plus by
calendar quarter, by asset, and by volatility tercile.

Cells with fewer than 20 trades report the count and no statistics — a metric computed from
7 trades is noise wearing a decimal point.

**Pre-registered check:** C4-A is predicted to perform poorly in Axis-P mean-reverting cells
(`specs/06` §4.1). If it does not, then the regime model, the strategy, or the backtest is
wrong. Determining which is more valuable than the headline result, and it is a required
deliverable rather than an optional follow-up.

---

## 7. Statistical testing

### 7.1 Against the random-entry control

C4-X1 produces a **distribution** across many seeds (`specs/06` §5.1), not a single path.

> **A strategy must exceed the 95th percentile of the C4-X1 distribution to be considered to have
> any signal at all.**

This is non-parametric, makes no distributional assumption, and directly answers the right
question: does the *signal* add anything beyond the risk management it shares with the control?

### 7.2 Multiple comparisons — counted honestly

Twelve strategies tested at 95% yields roughly 0.6 false positives by chance alone, before any
parameter sweeps are counted.

- Apply **Benjamini–Hochberg FDR control at q = 0.10**. Bonferroni is rejected as excessively
  conservative given that these strategies are correlated by construction.
- **Declare the trial count in the lock file before running**, and update it honestly as work
  proceeds. The trial count includes **every parameter setting evaluated and every abandoned
  attempt** — not just the configurations that made it into the final report. This is the number
  people understate, and understating it invalidates the correction.

### 7.3 Effect size, not only significance

Report the effect size alongside every p-value. With enough trades, a trivially small edge
becomes statistically significant and remains economically worthless after costs.

---

## 8. Parameter sensitivity

For each of the 30 registered parameters, sweep across its registered band and record performance
at each point.

### Requirements

1. **A plateau, not a peak.** Performance should vary smoothly and modestly across the band.
2. **The chosen value must not sit at a band edge.** An edge optimum means the true optimum lies
   outside the band, i.e. the band was specified wrongly — which is itself a finding to record.
3. **The ±20% rule:**

> **A strategy whose performance halves when any single parameter moves ±20% is rejected as
> overfit, regardless of its peak performance.**

This directly implements the warning attached to `δ` in `specs/03` §2 — a sharp optimum is
evidence of overfitting, not evidence of a good parameter value — and generalises it to every
parameter.

4. **Joint sensitivity** on the parameters shared across strategies (`δ`, `s`, `t_min`): sweep
   them jointly for the two or three strategies most dependent on each. A parameter that suits
   one strategy and ruins another is a design problem, not a tuning problem.

---

## 9. Leakage and look-ahead audits

Run before any performance result is trusted. A leakage bug makes every other number meaningless.

| Audit | Method | Pass condition |
|---|---|---|
| **Permutation test** | Shuffle future returns, re-run the full pipeline | Performance **collapses to the random control's distribution**. If it does not, there is leakage. This is the single most diagnostic test available. |
| **Backfill/live equivalence** | `specs/02` §8.7 | Identical feature values on any bar computable by both paths |
| **Timestamp audit** | Automated scan | No feature at bar `t` consumes data timestamped after bar `t`'s close |
| **Sort-order trap** | Targeted test | Spot candles arrive **descending** (`specs/00` §3). Assert ascending order after normalisation, and assert the last row is newest |
| **Funding settlement** | Targeted test | `FND_CUM` includes only **closed** intervals (`specs/02` §8.4) |
| **Structure lag** | Targeted test | Swing pivots are timestamped at the **confirmation** bar, not the pivot bar |
| **Harness integrity** | C4-X2 result | **If the weak-evidence control backtests well, halt.** Audit for look-ahead, survivorship in level definitions, or cost under-modelling before accepting any other result from this harness (`specs/06` §5.2) |

---

## 10. The benchmark comparison protocol

Consolidated from `specs/06` §6 and `specs/04` §4, and hereby pre-registered.

| Bespoke | Benchmark | Question |
|---|---|---|
| C1-B | C4-C | Does variance-ratio conditioning beat unconditioned reversal? |
| C2-A | C4-A, C4-B | Does pullback entry beat breakout entry on the same premise? |
| C2-B | C4-B on BTC only | Does expression selection beat the factor alone? |
| C3-A | C4-A | Does contraction-conditioning beat generic breakout? |
| C3-B | C4-B | Does early transition entry beat established-trend entry? |
| **All** | **C4-X1** | Does the signal beat random entry with identical risk management? |
| **Harness** | **C4-X2** | Is the methodology sound? |

> **Governance: a bespoke strategy that fails its benchmark is CUT, not tuned.**

Tuning until a strategy beats its benchmark is fitting the benchmark, which is worse than having
no benchmark. The comparison only carries information if losing it has consequences.

---

## 11. Pre-registered predictions

Design reasoning from Phases 2–8 produced falsifiable claims. Registering them converts the
design into something testable: if these fail, that is information about the *design*, not only
about the strategies.

| # | Prediction | Source | If it fails |
|---|---|---|---|
| H1 | Class 1 fires ≤ 4–8 per asset per month | `specs/03` §11 | Thresholds loose, or a "correction" condition fires on noise |
| H2 | Class 2 fires 6–15 per asset per month | `specs/04` §12 | As above |
| H3 | Class 3 fires 4–10 per asset per month | `specs/05` §11 | As above |
| H4 | Class 3 hit rate ≈ 30%; **> 60% is a red flag** | `specs/05` §11 | Suspect look-ahead, survivorship, or a stop wider than the quoted R |
| H5 | **C1-C fails its cost gate most of the time** and produces few signals | `specs/03` §5 | If it produces many, the cost gate is miscomputed |
| H6 | **C3-C underperforms its backtest** due to whipsaw | `specs/05` §5 | Discount any strong C3-C backtest result accordingly |
| H7 | **C4-C shows a high hit rate that is misleading** | `specs/06` §4.3 | Go directly to tail analysis; do not report the hit rate alone |
| H8 | C4-A performs poorly in mean-reverting regime cells | `specs/06` §4.1 | Regime model, strategy, or backtest is wrong — determine which |
| H9 | The redundancy protocol finds the clusters `{VOL_CC, VOL_PARK, VOL_GK, ATR}`, `{MR_AC1, MR_VR(2)}`, `{TRD_TSTAT, TRD_RET}` | `specs/02` §7 | The protocol is broken |
| H10 | C1-B's edge rests almost entirely on the `MR_VR` gate | `specs/03` §4 | If removing the gate barely changes results, C1-B has no distinct edge and should be cut |
| H11 | C2-B does **not** beat BTC-only | `specs/04` §4 | If confirmed, reduce C2-B to BTC-only rather than keeping the selection layer |
| H12 | **Any strategy showing a smooth, low-drawdown equity curve** in this asset class over this history is a bug, not an edge | `specs/06` §12 | Audit before reporting |

Record each as **confirmed / refuted / inconclusive**. A refuted prediction is a finding to
publish, not a mistake to quietly correct.

---

## 12. Decision rules

Exactly three outcomes per strategy. No fourth.

### PROMOTE — `validated: true`, `validation_multiplier` → 1.0

All of:
1. Exceeds the 95th percentile of the C4-X1 control distribution
2. Survives BH-FDR correction at q = 0.10
3. Beats its §10 benchmark
4. Passes §8 parameter sensitivity, including the ±20% rule
5. Passes every §9 leakage audit
6. Edge survives 2× slippage (§3.4)
7. Positive in at least two distinct regime cells with ≥ 20 trades each

### REVISE — permitted once, and only for a defect

Permitted **only** when a specific implementation or specification **defect** is identified —
a mis-specified condition, a unit error, an interface bug.

> **Poor performance is not a defect.** "It didn't work so I changed a threshold" is optimisation
> and is prohibited.

A revision must:
- Name the defect in `docs/findings.md` before any change is made
- Be re-tested on **fresh folds**, never the folds that revealed the problem
- Increment the declared trial count (§7.2)
- Be limited to **one per strategy**. A second failure is a CUT.

### CUT

Failure of any PROMOTE criterion that is not attributable to a named defect.

**Cut strategies are removed from the system, not disabled and kept.** A disabled strategy is a
standing temptation to re-enable it after a good month. Record the cut and its reason; the code
goes.

### Tier B strategies

Cannot reach PROMOTE until their data matures (§13). Until then they run in the system with
`validated: false` and `unvalidated_multiplier` applied, and are excluded from §10 comparisons —
not because they are exempt, but because the comparison would be uninformative.

---

## 13. Tier B validation timeline

Funding and positioning history begins at `recorder_start` and nowhere earlier (`specs/01b`).

| Requirement | Amount | Reason |
|---|---|---|
| Minimum funding intervals for a stable `FND_Z` distribution | ~200 intervals ≈ **67 days** | Below this, the z-score's own scale is unreliable |
| Minimum for regime diversity | **~6 months** | Must span more than one market state or it measures that state |
| Minimum closed Tier B trades | **≥ 100** per §5.1 confidence bar | At Class 1 frequencies this may take considerably longer |

> **`earliest_tier_b_validation = recorder_start + 6 months`**, and later still if the trade count
> has not been reached.

Compute this date at startup, publish it in the system, and surface it in the UI next to any
`validated: false` signal. A user should be able to see not only that a strategy is unvalidated
but **when it could stop being so.**

---

## 14. Confidence — the gate, restated for execution

Per `specs/07` §9, `confidence` may be populated only when all hold: out-of-sample only; 95%
Wilson half-width ≤ 10 percentage points (≈ n ≥ 100 near p = 0.5); ≥ 2 regime strata with
meaningful occupancy; published with interval and n; rolling re-estimation with drift detection.

For confidence reporting only, collapse the regime model to **2–3 coarse strata**. This is a
reporting decision and does not alter the gating model.

**Expect `confidence` to remain `null` for low-frequency strategies for a long time, possibly
permanently.** That is the correct outcome and must be surfaced as an explained state, never a
blank.

---

## 15. Recalibrations triggered by results

Three quantities are specified to be replaced by measurement once results exist. Do these
explicitly, and record each as a decision:

| Quantity | Replace with | Source |
|---|---|---|
| `class3_multiplier` | `1/√(v₃/v₁)` using **measured** hit rates and R:R, per the formula given | `specs/08` §3 |
| `liq_safety` | The 99th percentile of measured adverse excursion beyond a level within one bar | `specs/08` §6 |
| Ranking basis | Switch from `strength` to estimated EV/R, **only if** the §14 confidence gate is cleared | `specs/07` §2 |

---

## 16. Parameters — zero

Running total: **30 → 30. Unchanged.** A protocol is not a model.

The quantities appearing here — BH q = 0.10, the 95th-percentile control threshold, the ±20%
sensitivity rule, the 2× slippage stress, the 20-trade cell minimum — are **statistical
conventions and decision thresholds**, consistent with the treatment of the `|ρ| ≥ 0.8`
redundancy cut (`specs/02` §7) and the 10-point confidence interval width (`specs/07` §9). They
are registered in the lock file but are not tunable model parameters and must never be adjusted
to change an outcome.

---

## 17. Acceptance criteria for Phase 9

1. `validation/protocol.lock.json` exists, is committed, is hashed, and predates the first
   backtest commit. **Verify by git history** — this is checkable, so check it.
2. The holdout is in a separate directory the harness cannot read without an explicit,
   logged flag.
3. The fill model implements §3 exactly, with a targeted test for the **gap-through-stop** rule
   and for the intrabar stop-before-target assumption.
4. Backtest and live share one cost/sizing module (`specs/08` criterion 10), asserted by import
   graph.
5. The permutation test (§9) runs and **collapses to the control distribution**. If it does not,
   halt — no other result from this harness means anything.
6. C4-X1 runs across many seeds and reports percentiles, not a single path.
7. All twelve §11 predictions are recorded as confirmed / refuted / inconclusive.
8. Parameter sweeps are produced for all 30 parameters, with plateau-versus-peak assessed and the
   ±20% rule applied.
9. Every strategy receives exactly one of PROMOTE / REVISE / CUT, with the reason recorded.
10. `earliest_tier_b_validation` is computed from the actual recorder start date and surfaced in
    the UI.
11. **No performance number appears in any spec, config default, UI string or code comment.**
    Results live in the results report and the audit trail, nowhere else.
