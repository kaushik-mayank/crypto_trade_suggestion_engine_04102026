# 19 — Operations & Iteration Governance

**Depends on:** Phases 16–18 complete. **Duration:** indefinite.

---

## 1. The problem this phase actually solves

Running the system is the easy part — Phase 17 built the observability and Phase 18 the
supervision.

The hard part is **what happens over the following year**, when a strategy underperforms, a new
idea arrives, a parameter "obviously" needs adjusting, and the market changes. Each of these
arrives alone and looks reasonable in isolation. Together they are how a validated system becomes
an overfitted one, slowly, with nobody having made an obviously bad decision.

> **Governance exists to make the cumulative cost of individual changes visible at the moment each
> one is proposed.**

---

## 2. The cumulative trial counter

The single most important mechanism, and the one most often defeated by accident.

`specs/09` §7.2 corrects for multiple comparisons at q = 0.10 using a declared trial count. That
correction is only meaningful if the count is honest — and **the count never resets.**

```
trial_count.json   (committed, append-only)
  { n, entries: [ {date, type, description, phase, delta} ] }
```

Every parameter evaluation, every strategy modification, every abandoned attempt increments it.
After a year of maintenance the count may be in the hundreds, which means the significance
threshold a new change must clear is far stricter than it was at trial 1.

> **That is correct, not unfair.** You have been searching a large hypothesis space. The threshold
> should reflect it.

The counter is displayed in the operations view. Watching it grow is the intended deterrent.

---

## 3. Change classification

| Type | What it is | Trial count | Re-validation required |
|---|---|---|---|
| **A — Defect fix** | Implementation does not match spec | No increment | Re-test on reserved folds (`specs/16` Stage 2) |
| **B — Parameter change** | A registered parameter's value changes | **+1** | Full `specs/09` protocol |
| **C — Strategy modification** | Conditions, levels or logic change | **+1** | Full protocol; treat as a new strategy |
| **D — New strategy** | Addition | **+1** | Full protocol from scratch |
| **E — Non-signal change** | UI, logging, ops, docs | No increment | None |

### The rule that keeps Type A honest

> **The defect must be named, in writing, before its fix is observed to improve results.**

Otherwise "defect" becomes the label applied to any change made because performance was
disappointing, and Type A becomes an unlimited tuning channel that bypasses the trial counter
entirely. This is the exact loophole `specs/09` §12 closed for Phase 16 revisions; it stays closed
in operations.

---

## 4. Pre-registration applied to maintenance

Before changing any strategy:

1. **State the observation that would justify the change** — in writing, before looking.
2. **Check whether enough evidence exists to observe it.** You cannot distinguish a 30% strategy
   from a 45% one on 20 trades.
3. **Minimum 30 closed trades since the last change to that strategy**, and the stated reason may
   not be "recent performance."

A bad month is usually noise. The cooling-off requirement exists because the instinct to act
arrives long before the evidence does.

---

## 5. The deliberate asymmetry: cutting is cheap, tuning is expensive

| Action | Requirement |
|---|---|
| **Cut a strategy** | A recorded reason. That is all |
| **Tune a strategy** | Full `specs/09` protocol, trial count increment, cooling-off satisfied |
| **Add a strategy** | Same as tuning, plus a benchmark comparison against the existing set |

Systems accrete. Left alone, every quarter adds a strategy, a condition, a parameter — and
complexity is never removed because removing it feels like losing something.

Making removal nearly free and addition expensive counteracts that directly. **A year in which
the system got simpler is a good year**, and the parameter count and strategy count should be
reported alongside performance so that trajectory is visible.

---

## 6. Drift monitoring at checkpoints, not continuously

Live performance will drift from validated performance. Detecting it requires care: testing
repeatedly as data arrives inflates false positives, and a system that "detects" drift every few
weeks will be tuned to death.

> **Evaluate drift only at fixed checkpoints: every 50 closed trades per strategy.**

At each checkpoint, compare the live hit rate against the validated Wilson interval.

| Result | Response |
|---|---|
| Inside the interval | Nothing. Record and continue |
| Below for **one** checkpoint | Record. **No action** — one checkpoint is not evidence |
| Below for **two consecutive** checkpoints | **Demote**: reduce size via `validation_multiplier`, or halt the strategy |

**Demote or halt — do not tune.** A strategy whose live performance has fallen below its
validated range has failed, and adjusting it to fit recent data is the definition of the error
this system was built to avoid.

---

## 7. Annual full re-validation

Regardless of whether anything changed:

- Carve a **fresh holdout** from the accumulated year's data.
- Re-run the complete `specs/09` protocol against it.
- Re-fit the regime model and re-run its three validity tests.
- Re-run the redundancy protocol.
- Recompute `class3_multiplier` and `liq_safety` from the year's measurements.
- Report the parameter count and strategy count against last year's.

A strategy validated in 2026 may not hold in 2027, and the only way to know is to ask again with
data it has never seen.

---

## 8. Runbooks

| Situation | First action | Then |
|---|---|---|
| **Recorder heartbeat stale** | Restart immediately — data loss is accruing now | Check disk, check logs, record the gap window |
| Ingestion down > 1h | Check `429`/`503` counts and connectivity | If `503`, wait — documented maintenance. If `429`, rate limits are misconfigured against P2 |
| **Live-only signal detected** | Treat as a bug (`specs/17` §3) | Check for a bar mutation; if none, there is non-determinism — halt changes until found |
| Bar mutation | Check affected-signal flags | If frequent, record in `docs/findings.md` — it limits how much the backtest can be trusted |
| Signal drought | Open the funnel; read the binding condition | Usually correct. If a gate is stuck closed, that is a defect (Type A) |
| Signal flood | Open the funnel | A loose threshold is a Type B change, not a quick fix |
| Repeated `429` | Reduce polling rate | Re-measure limits; update the P2 record |
| Disk filling | Check `data/raw/` growth and Parquet regeneration | Extend storage; never delete `data/raw/` |
| Feature NaN spike | Identify which guard fired | A data problem, a numerical problem, or an upstream change (below) |

### The unexpected API change — where Phase 11's design pays off

If CoinDCX renames a field, changes units, or alters a payload's shape:

1. **Normalization rejects it** rather than coercing (`specs/11` §4.2). The system goes **quiet**
   instead of computing wrong numbers. This is the designed behaviour, and it is why
   reject-don't-coerce was worth the strictness.
2. Detection: a rejection-rate spike in `dq_events`, and `DOWN`/`DEGRADED` in the UI.
3. Inspect the raw payload — it was captured verbatim (`specs/01b`), so the actual change is
   visible rather than inferred.
4. Update the normalizer, **add the new payload as a fixture**, update
   `specs/00-capability-map.md`, record in `docs/findings.md`.
5. **If the change alters a feature's meaning** — a unit change, a semantic change — it is a
   **Type C** change and requires re-validation. A silent unit change is exactly the kind of thing
   that would otherwise invalidate months of results without any test failing.

---

## 9. What "done" means here

This system has no completion state. But it has a healthy steady state:

- The recorder has not missed a day.
- The trial counter grows slowly.
- Strategy and parameter counts are flat or falling.
- Drift checkpoints pass.
- Annual re-validation confirms the promoted set.
- `confidence` becomes publishable for the higher-frequency strategies as `n` accumulates.
- Tier B strategies pass their first validation at `recorder_start + 6 months`.

And an unhealthy one, worth naming so it is recognisable:

- Frequent Type A "defect" fixes that follow poor performance.
- The trial counter rising faster than closed trades.
- Strategy count only ever increasing.
- Drift checkpoints repeatedly ignored.
- Shadow windows resetting often.

---

## 10. Where the real uncertainty remains

Recorded at the end of nineteen phases, because the honest list is short and specific:

1. **Nothing has been backtested.** Every strategy rests on mechanism reasoning alone.
   `specs/16` has not been executed.
2. **History depth is unknown** (probe P1). If it is thin, `specs/09` §2.3 may conclude that
   meaningful validation is impossible — a project-level constraint, not a fixable problem.
3. **The regime model may fail its validity tests**, forcing the degraded mode in `specs/12` §10.
4. **Tier B strategies may never be validatable** if their frequency is too low to reach n ≥ 100.
5. **C1-C is predicted to fail its cost gate**, C3-C to underperform its backtest, and C2-B to not
   beat BTC-only. Three of twelve strategies are expected to be cut.
6. **`ΔR` may show the live edge is materially below backtest**, demoting strategies that passed
   Phase 16.
7. **Funding settlement is approximated** (`specs/02` Family 6) and the error is unquantified.

None of these is a reason not to proceed. All of them are reasons not to assume the system works
before it has been measured.

---

## 11. Parameters

Final count: **29 or 30**, depending on `specs/16` Stage 3's `er_min` resolution. The count is
reported in the operations view alongside the strategy count and the trial counter, so its
trajectory is always visible.

---

## 12. Acceptance criteria for Phase 19

1. `trial_count.json` is committed, append-only, and never reset.
2. Every change is classified A–E and recorded before it is made.
3. Type A defect descriptions are written **before** the fix's effect is observed.
4. The 30-closed-trade cooling-off rule is enforced before any strategy change.
5. Drift is evaluated only at 50-trade checkpoints, and two consecutive failures demote rather
   than tune.
6. Annual full re-validation is scheduled and executed against a fresh holdout.
7. Parameter count, strategy count and trial count appear in the operations view.
8. All §8 runbooks exist and have been walked through once.
9. An API-change drill has been run: inject a malformed payload, confirm the system goes quiet
   rather than computing, and confirm the raw payload is available for inspection.
10. `docs/findings.md` remains the project's memory and is still being written to.
