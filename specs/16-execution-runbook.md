# 16 — Validation Execution Runbook

**Executes:** `specs/09`. **Depends on:** Phases 1–15 complete.

---

## 0. What this document is, and what it is not

> **This is a runbook. It contains no results, and executing it is what completes Phase 16.**

Every number below is a placeholder, a threshold, or a red flag. **No strategy in this system has
been backtested.** Phase 16 is complete only when the coding agent has run this sequence and
filled the report in §10 with real output — not when this document exists.

The distinction matters because a filled-in-looking runbook is easy to mistake for evidence.

---

## 1. Prerequisites — all must hold before Stage 1

- [ ] Phase 1 probes complete; every `⧗ PENDING` branch in `specs/02` §1 resolved and recorded
- [ ] Phases 11–15 implemented; every phase's acceptance criteria met
- [ ] Full unit test suite green (§5)
- [ ] Backfill complete; total history depth recorded (this sets the §2.3 branch in `specs/09`)
- [ ] Recorder running; `recorder_start` recorded; `earliest_tier_b_validation` computed
- [ ] Parameter registry contains exactly 30 entries with justifications

If history depth falls in `specs/09` §2.3's "< 9 months" branch: **stop here and report it.**
Producing results that look like validation when validation is impossible is worse than producing
none.

---

## 2. Stage sequence

Each stage gates the next. **Stages 1 and 3 are halt gates.**

```
0  Prerequisites
1  HARNESS INTEGRITY          ◀ HALT GATE — no strategy result may be viewed before this passes
2  Protocol lock
3  Feature-level analysis     ◀ HALT GATE — changes strategy definitions
4  Control baseline
5  Walk-forward execution
6  Parameter sensitivity
7  Hypothesis evaluation
8  Benchmark comparisons
9  Decisions
10 Holdout                    ◀ ONCE, after decisions are final
11 Calibration
12 Report
```

---

### Stage 1 — Harness integrity ◀ HALT GATE

> **Complete this stage before looking at a single strategy's P&L.**

Once results are visible, there is unavoidable pressure to rationalise a failing integrity check —
"the permutation test is a bit off but the strategy looks great" is a sentence that ends projects.
Sequencing removes the temptation rather than relying on resisting it.

| Check | Source | Pass condition | On failure |
|---|---|---|---|
| **Permutation test** | `specs/09` §9 | Shuffled-target performance **collapses to the C4-X1 distribution** | **HALT.** There is leakage. No other number from this harness means anything |
| **C4-X2 weak-evidence control** | `specs/06` §5.2 | Performs poorly | **HALT.** Audit for look-ahead, survivorship in level definitions, cost under-modelling, fill-logic bugs |
| Backfill/live equivalence | `specs/02` §8.7 | Identical feature values | Fix before proceeding |
| `compute_series` == `compute_at` | `specs/12` §7 | Agree on every bar, every feature | Fix |
| `replay_exact` sample | `specs/13` §10 | Reproduces exactly | Fix |
| Fee unit assertion | `specs/10` §5 | Every fee entering the cost model `< 0.01`; the deliberate 100× error is caught | **HALT.** Costs understated 100× invalidates everything silently |
| Gap-through-stop fill | `specs/09` §3.2 | Bar opening beyond the stop fills at the **open** | Fix |
| Shared cost module | `specs/08` criterion 10 | Import graph shows one module for backtest and live | **HALT** |
| No look-ahead scan | `specs/12` §13 | Automated registry-wide check passes | Fix |

---

### Stage 2 — Protocol lock

1. Write `validation/protocol.lock.json` (`specs/09` §1.1) with **absolute UTC** window
   boundaries.
2. Commit. **Verify by `git log` that it predates any backtest output commit.**
3. Declare the initial trial count.
4. Confirm `data/holdout/` is physically separate and the harness cannot read it without the
   explicit logged flag.

**Fold reservation — decide now, not later.** `specs/09` §12 permits one revision per strategy,
re-tested on **fresh folds**. Fresh folds cannot be conjured after every fold has been seen.

> **Reserve the final 25% of walk-forward folds (before the holdout) as revision folds. Do not run
> them in Stage 5.**

If no revision is needed, they are added to the evidence base at Stage 9 and the result is
stronger. If a revision is needed, genuinely unseen data exists to test it on. Record the reserved
fold boundaries in the lock file.

---

### Stage 3 — Feature-level analysis ◀ HALT GATE

These change strategy *definitions*, so they must precede strategy backtests.

1. **Redundancy protocol** (`specs/02` §7) on the training window.
   - Check hypothesis **H9**: does it find `{VOL_CC, VOL_PARK, VOL_GK, ATR}`,
     `{MR_AC1, MR_VR(2)}`, `{TRD_TSTAT, TRD_RET}`?
   - **If not, HALT.** The protocol is broken and its output cannot be acted on.
2. **Resolve C1-B condition 4** (`specs/03` §4): if `ρ(DEV_Z, STR_DIST_PDH) ≥ 0.8`, demote from
   gate to strength-only. Record.
3. **Resolve `er_min`** (`specs/04` §8): if `TRD_TSTAT` and `TRD_ER` collapse, **delete C2-A
   condition 4** and never register parameter 24. Record. Parameter count becomes 29 or 30.
4. **Fit the regime model** (`specs/02` §6, `specs/12` §9). Run all three validity tests. Escalate
   3×3 → 2×2 if needed.
   - **If 2×2 also fails: engage degraded mode** (`specs/12` §10) — drop dwell-stability
     conditions, keep raw-axis thresholds. Record the failure as a substantive finding about this
     market.
5. Write every outcome to `docs/findings.md` before proceeding.

---

### Stage 4 — Control baseline

Run **C4-X1** across many seeds (`specs/06` §5.1). Report the full percentile distribution, not a
mean.

This is the yardstick every strategy is measured against, and it must exist **before** strategy
results are interpreted — otherwise the threshold gets chosen after seeing what would clear it.

---

### Stage 5 — Walk-forward execution

Run all 12 tradeable strategies plus C4-X2 across all folds **except the reserved revision folds**.
Holdout untouched.

Record per strategy per fold: every metric in `specs/09` §5, plus regime-cell breakdown and the
rejection histograms from `specs/13` §9.2 (needed for H1/H5/H10).

---

### Stage 6 — Parameter sensitivity

Sweep all 30 (or 29) parameters across their registered bands (`specs/09` §8).

- Plateau or peak? Record the shape, not just the optimum.
- Is the chosen value at a band edge? If so, the band was wrong — record it.
- **Apply the ±20% rule: performance halving under a ±20% move rejects the strategy as overfit**,
  regardless of peak performance.
- Joint sweeps on shared parameters (`δ`, `s`, `t_min`).
- **Update the declared trial count.** Every setting evaluated counts.

---

### Stage 7 — Hypothesis evaluation

Record each of H1–H12 (`specs/09` §11) as **confirmed / refuted / inconclusive**, with the
supporting statistic. A refuted prediction is a finding to publish, not a mistake to correct
quietly.

---

### Stage 8 — Benchmark comparisons

Run the `specs/09` §10 table. For each: the percentile against C4-X1, the BH-FDR adjusted
significance at q = 0.10, the effect size, and the head-to-head against the named benchmark.

---

### Stage 9 — Decisions

Apply `specs/09` §12 to each strategy: **PROMOTE / REVISE / CUT.** Record the reason.

**Revisions:** name the defect in `docs/findings.md` *before* changing anything, run the revision
on the **reserved folds only**, increment the trial count, one per strategy. Poor performance is
not a defect.

**Cuts:** remove the code. A disabled strategy is a standing temptation to re-enable it after a
good month.

If no revisions were needed, fold the reserved folds into the evidence base and re-report.

---

### Stage 10 — Holdout ◀ ONCE

After all decisions are final. Load with the explicit logged flag. Run only the PROMOTED set.

> **The holdout is not a decision input. If it changes a decision, it has become training data and
> is burned.**

Its job is a single question: **does the promoted set generalise?**

- Consistent with walk-forward → proceed to Phase 17.
- Materially worse → **the process overfit.** The response is **not to re-select** — it is to
  report that the promoted set failed out-of-sample and that nothing should be deployed on this
  evidence. Carve a new holdout from future data and repeat later.

That is the uncomfortable outcome the holdout exists to produce. Design the reporting so it can be
stated plainly.

---

### Stage 11 — Calibration

The three replacements `specs/09` §15 pre-declared:

| Quantity | Replace with | Source |
|---|---|---|
| `class3_multiplier` | `1/√(v₃/v₁)` with `v = p(1−p)(k+1)²` from **measured** hit rates and R:R | `specs/08` §3 |
| `liq_safety` | 99th percentile of measured adverse excursion beyond a level within one bar | `specs/08` §6 |
| Ranking basis | Switch from `strength` to estimated `EV/R` **only if** the confidence gate clears | `specs/07` §2 |

**Confidence gate** (`specs/09` §14): for each promoted strategy, in each coarse regime stratum —
out-of-sample only, Wilson half-width ≤ 10 points (≈ n ≥ 100), ≥ 2 strata occupied.

> **Expect most strategies to fail this gate and `confidence` to remain `null`.** That is the
> correct outcome (`specs/07` §9), and the UI already renders it as an explained state.

Record each recalibration as a dated decision.

---

## 3. Red flags — results that indicate error, not success

Consolidated from across the specs. **Check these before celebrating anything.**

| Observation | What it probably means |
|---|---|
| **C4-X2 performs well** | Harness broken. HALT (Stage 1) |
| **Class 3 hit rate > 60%** | Look-ahead, survivorship in boundary definitions, or a stop wider than the quoted `R` (`specs/05` §11) |
| **Smooth, low-drawdown equity curve** | A bug, not an edge, in this asset class over this history (H12) |
| **Mean realised R on stopped trades ≈ exactly −1.0** | The gap-through-stop rule is not firing. The fill model is optimistic and every loss is understated (`specs/09` §3.2) |
| **Zero `MISSED` signals** | Trigger or `remaining_rr` logic is not running (`specs/07` §8.2) |
| **Zero `NO VALID TRADE` outcomes** | A gate is not being evaluated |
| **A strategy above the 99.9th percentile of C4-X1** | Too good. Suspect shared state between control and strategy, or a leak |
| **C4-C shows a high hit rate and good Sharpe** | Expected and meaningless. Go straight to the tail (`specs/06` §4.3) |
| **All 12 strategies pass** | Multiple-comparison correction is not being applied, or the trial count is understated |
| **Signal frequency far above the design bands** | Thresholds loose, or a condition firing on noise (H1–H3) |
| **Identical results across parameter sweeps** | The parameter is not wired to anything |

---

## 4. Why the holdout cannot rescue a bad process

Worth stating once, plainly, because it is the most common misuse of a holdout.

A holdout tests whether decisions made without it generalise. If a poor holdout result triggers
re-selection, the holdout has silently become a validation set, and the *next* holdout does not
exist. There is no third dataset.

**This is why Stage 10 comes after Stage 9 and cannot be reordered.**

---

## 5. Consolidated test suite

Every test specified across Phases 2–15, in one checklist. **⛔ marks a halt gate.**

### Correctness-critical

- [ ] ⛔ Fee percent→fraction; deliberate 100× error caught — `specs/10`
- [ ] ⛔ Permutation test collapses to control — `specs/09`
- [ ] ⛔ C4-X2 performs poorly — `specs/06`
- [ ] ⛔ Shared cost/sizing module, verified by import graph — `specs/08`
- [ ] Gap-through-stop fill; intrabar stop-before-target — `specs/09`
- [ ] `compute_series` == `compute_at`, every feature — `specs/12`
- [ ] Backfill/live equivalence — `specs/02`
- [ ] `replay_exact` over a stored sample — `specs/13`
- [ ] Automated registry-wide look-ahead scan — `specs/12`
- [ ] Spot candles ascending after normalization, newest last — `specs/11`
- [ ] `get_point_in_time` respects `as_of_ms` — `specs/11`
- [ ] Negative Garman–Klass raises `INVALID_OHLC`, never clamps — `specs/12`
- [ ] Levels byte-identical through the risk engine — `specs/08`
- [ ] No hardcoded fee, tick, `min_notional` or margin value — `specs/08`

### Strategy logic

- [ ] ACT: ratchet, mirror, no-knife-catch, symmetry, unit scaling — `specs/13`
- [ ] One ACT implementation, nine consumers — `specs/04`/`05`/`06`
- [ ] Partitions: C1-B ⊕ C2-A · C1-A ⊕ C2-C · C1-C ⊕ C2-B · C3-B ⊕ C2-A — `specs/13`
- [ ] No short-circuit in conditions or vetoes — `specs/13`
- [ ] Determinism within and across processes — `specs/13`
- [ ] No clock read inside `src/strategies/` — `specs/13`
- [ ] `X*` reconstructible from bars — `specs/13`
- [ ] Trailing stops never loosen — `specs/04`
- [ ] Handoff = one trade, original entry preserved — `specs/13`
- [ ] Outage trigger → `MISSED_OUTAGE`, never `TRIGGERED` — `specs/13`
- [ ] No cross-strategy composite score exists — `specs/07`
- [ ] Arbitration order: dedup after conflict + merge — `specs/07`
- [ ] Corroboration leaves `strength` unchanged — `specs/07`

### Risk

- [ ] Sizing is `min(stop, vol)`; C3-A binds on vol-target — `specs/08`
- [ ] `sigma_forward` for C3-A uses expanded ATR — `specs/08`
- [ ] Signed correlation treats long/short as hedged — `specs/08`
- [ ] `recommended_leverage` never equals `max_safe_leverage` — `specs/08`
- [ ] `k_net` recomputed after tick rounding, floor re-applied — `specs/08`
- [ ] `MIN_NOTIONAL` publishes `min_equity_for_this_trade` — `specs/08`
- [ ] All vetoes evaluated and logged, no short-circuit — `specs/08`

### Data

- [ ] Every `specs/00` §5 hazard, with a **real captured payload** fixture — `specs/11`
- [ ] `B-NKN_USDT` stale/incomplete-row fixture — `specs/11`
- [ ] Bar final only after two identical fetches — `specs/11`
- [ ] Bar mutation → ERROR + `dq_events` + affected-signal flagging — `specs/11`
- [ ] `known_absent_bars`; aggregation refuses across unconfirmed gaps — `specs/11`
- [ ] Every numerical guard with its degenerate input — `specs/12`
- [ ] Warm-up status transitions at the exact threshold — `specs/12`
- [ ] Degraded regime mode: dwell conditions drop, raw-axis survive — `specs/12`

### API and UI

- [ ] Cache reports **data** age, not cache age — `specs/14`
- [ ] Five system states distinguishable; store failure → `DOWN`, never empty — `specs/14`
- [ ] Non-localhost bind refused without a token — `specs/14`
- [ ] Pagination stable under concurrent writes — `specs/14`
- [ ] Controls never reach API or UI — `specs/13`/`14`/`15`
- [ ] **No `<form>`, no POST target, no exchange trade link** — `specs/15`
- [ ] `confidence: null` renders reason + breakeven, never blank — `specs/15`
- [ ] Tier B separated structurally, not badged — `specs/15`
- [ ] Greyscale preserves every state distinction — `specs/15`
- [ ] Tabular figures on every numeric element — `specs/15`

---

## 6. Report template

`docs/validation_report.md`. **Every field below is a placeholder until executed.**

```
VALIDATION REPORT
protocol_version · lock_hash · executed_utc · code_version · trial_count_final

1. HARNESS INTEGRITY
   permutation: pass/fail + distribution comparison
   C4-X2: performance (must be poor)
   all Stage 1 checks: pass/fail

2. DATA
   history depth per product/asset/timeframe · specs/09 §2.3 branch taken
   fold count · train/test/reserved/holdout boundaries
   gap and mutation rates

3. FEATURE ANALYSIS
   redundancy clusters found · H9 verdict
   C1-B condition 4: gate or strength-only
   er_min: registered or deleted · final parameter count
   regime model: 3×3 / 2×2 / degraded · all three validity test results

4. CONTROL BASELINE
   C4-X1 distribution: p50 / p90 / p95 / p99, seed count

5. PER STRATEGY  (× 12)
   trades total and per fold
   hit rate + Wilson CI + n
   mean R · median R · full R histogram
   worst trade · realised R on stopped trades  ← see red flags
   profit concentration + strategy-appropriate reading
   max DD + duration · Sharpe · Sortino · MAR
   regime breakdown (cells with n < 20 report count only)
   C4-X1 percentile · BH-FDR result · effect size
   benchmark head-to-head
   sensitivity: plateau/peak, edge-of-band, ±20% verdict
   2× slippage verdict
   DECISION: PROMOTE / REVISE / CUT + reason

6. HYPOTHESES  H1–H12: confirmed / refuted / inconclusive + statistic

7. RED FLAGS  each item in §3: observed or not

8. HOLDOUT  (after decisions)
   promoted set performance · consistent or not · deploy verdict

9. CALIBRATION
   class3_multiplier: old → new
   liq_safety: old → new
   confidence gate: per strategy, pass/fail + n
   ranking basis: strength or EV/R

10. TIER B
   recorder_start · earliest_tier_b_validation · current status
```

---

## 7. Parameters

Running total: **30 → 30 or 29**, depending on Stage 3's `er_min` resolution. This is the only
phase that may *reduce* the count, and reducing it is a good outcome.

---

## 8. Acceptance criteria for Phase 16

1. **Stage 1 completed and passed before any strategy result was viewed.** Verify by commit
   ordering.
2. `protocol.lock.json` predates all backtest output in `git log`.
3. Revision folds were reserved in Stage 2 and remained unrun through Stage 5.
4. Every checklist item in §5 is ticked or has a recorded exemption with a reason.
5. All 12 hypotheses recorded with verdicts, including refutations.
6. Every §3 red flag explicitly checked and recorded as observed or not.
7. Holdout used **exactly once**, after Stage 9, with the access event logged.
8. A PROMOTE / REVISE / CUT decision exists for all 12 strategies, with reasons.
9. Cut strategies have their **code removed**, not merely disabled.
10. All three calibrations performed and dated.
11. **No performance number appears anywhere except the report and the audit trail** — not in
    specs, config defaults, UI strings, or code comments.
12. The report states plainly which strategies may enter Phase 17 and which may not.

---

> **Until this runbook has been executed and §6 filled with real output, the correct description
> of this system's validation status is: none.**
