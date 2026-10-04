# 17 — Shadow Run & Observability

**Depends on:** Phase 16 complete, with a PROMOTE decision for at least one strategy.
**Capital deployed: zero.**

---

## 1. What a shadow run is for

Not "check that it works." That is what Phases 11–15's acceptance criteria were for.

> **A shadow run measures the gap between what the backtest predicted and what actually happens.**

That gap is the most informative number this project can produce, because the backtest is a
**model of execution** — of fills, latency, data availability, and the market's willingness to
trade where we assumed. If live diverges from backtest, the model was wrong. The market was not.

Every source of divergence is separately diagnosable, and most are fixable. The design below
exists to attribute divergence to its cause rather than reporting one aggregate number.

**Secondary purpose for Tier B:** C1-A and C2-C enter shadow with `validated: false` because they
cannot be backtested. The shadow period is simultaneously a test of the system **and the start of
their evidence base**, counting toward `earliest_tier_b_validation` (`specs/09` §13).

---

## 2. Parallel replay — the comparison design

Comparing live P&L to backtest P&L over the same calendar period conflates everything. Instead:

> **At the end of each shadow week, run the backtest engine over that same week's data and diff it
> against the live record, trade by trade.**

```
LiveSignal[]  ⟷  ReplaySignal[]   over an identical window
        → matched pairs · live-only · replay-only
```

Matching key: `(strategy_id, asset, product, direction, armed_at_ms ± tolerance)`.

This works because `specs/10` §2 made the engine a pure function of a snapshot and `specs/13` §6
made it deterministic. **If parallel replay does not reproduce live signals, one of those two
guarantees is broken** — which is itself the most important finding the shadow run can produce.

---

## 3. Divergence taxonomy

Five classes. Each has different causes and a different meaning.

| Class | Observation | Likely cause | Severity |
|---|---|---|---|
| **Live-only** | Live fired, replay did not | **Non-determinism, or live saw data that later changed (bar mutation)** | ⛔ **Bug.** `specs/13` §6 forbids this. Investigate every instance |
| **Replay-only** | Replay fired, live did not | Outage, DQ veto, rate-limit drop, late data | Expected. Quantifies operational cost (§3.1) |
| **Timing** | Same signal, different trigger time | Polling latency, bar-finalization delay | Measure the distribution |
| **Level** | Same signal, different entry/stop/target | Features computed on different data; rounding | Should be zero. Non-zero means data differed |
| **Outcome** | Same trade, same entry, different realised R | **Fill model wrong** | The thing this phase exists to measure |

### 3.1 Replay-only signals quantify what infrastructure costs

These are not failures to hide. They are the honest price of running in the real world, and
counting them gives a number nobody usually has:

```
missed_signal_rate = replay_only / (matched + replay_only)
```

Attributed by cause — outage, DQ veto, rate-limit drop, stale data — so it is actionable. A 12%
miss rate driven by rate limiting is a configuration fix; the same rate driven by laptop sleep is
a hosting decision.

**Excluded from the performance comparison, counted separately.** Including them would credit the
live system with trades it never saw.

---

## 4. The primary metric

For **matched** trades only:

```
ΔR = R_live − R_replay
```

Report the full distribution: mean, median, standard deviation, and the histogram.

| Result | Meaning |
|---|---|
| `mean ΔR ≈ 0` | The fill model is honest. Proceed. |
| `mean ΔR < 0` | **The backtest is optimistic.** Every Phase 16 promote decision was made on an inflated edge |
| `mean ΔR > 0` | Suspicious, not good. Check for a live-only data advantage or a matching error |

### The governance consequence

> **A materially negative `mean ΔR` requires re-examining Phase 16's promote decisions.**

A strategy that cleared its gates at the backtest's edge may not clear them at the live edge.
Re-run the Stage 8 benchmark comparisons with realised R substituted for modelled R. **Shadow can
demote a promoted strategy**, and that must be an expected outcome rather than a crisis.

### The specific check inherited from `specs/09` §3.2

Compare **realised R on stopped trades** between live and replay. If live is worse, the
gap-through-stop model understates slippage on exactly the trades where it matters most. Feed the
measured distribution back into `liq_safety` (`specs/08` §6).

---

## 5. Exclusions — the honesty rules

| Excluded | Reason |
|---|---|
| **Outage windows** | `specs/13` §11.2. Replay counts trades live could not have taken. Exclude the window from both sides, or the comparison is meaningless |
| **`MISSED_OUTAGE` signals** | Never a trade (`specs/09` §4). The user had no opportunity to act |
| **`MISSED` signals** | Not a trade, but **tracked**: a rising `MISSED` rate means the trigger is too slow or the poll cadence too coarse |
| **`SUPPRESSED` signals** | Not trades, but tracked — this measures what arbitration costs |
| Replay-only | §3.1 |

Outage windows are read from `dq_events`, not reconstructed by hand. Every exclusion is recorded
with its window and reason so the comparison's coverage is auditable.

---

## 6. Duration and sample requirements

| Requirement | Value | Reason |
|---|---|---|
| Minimum duration | **4 weeks** | Below this, `ΔR` is noise |
| Target duration | **8–12 weeks** | Enough matched closed trades for a usable distribution |
| Minimum matched closed trades | **≥ 30** | Below this, no distributional claim is defensible |
| Regime coverage | **≥ 2 distinct regimes** if the Phase 12 model is live | A month of pure trend says nothing about mean-reversion strategies |

Low-frequency strategies may need considerably longer. **Report each strategy's matched-trade
count individually** — a system-wide sample of 40 concentrated in one strategy tells you nothing
about the other five.

---

## 7. The freeze rule

> **No change that alters signal content may be made during a shadow window. Any such change
> resets the window.**

Comparing against a moving target produces nothing. The classification is mechanical, not a
judgment call:

| Change | Effect |
|---|---|
| Alters `parameter_hash` or `code_version` on the strategy/risk/feature path | **Window resets** |
| Display, logging, observability, API rendering | No reset |
| Ingestion fix that changes *what data arrives* | **Window resets** |
| Ingestion fix that changes only retry timing or logging | No reset |

Record every reset and its cause. Frequent resets are themselves a finding — a system that cannot
run four weeks without a content-changing fix is not ready for shadow.

---

## 8. Observability: the signal funnel

The centerpiece. Generic CPU and memory dashboards are irrelevant here; **where signals die** is
the question an operator actually has.

```
evaluations            14,832
  ├─ feature not ready     2,104   (warming / gap / numerical)
  ├─ conditions failed    12,610   ← broken out by binding condition, per strategy
  └─ setups armed            118
       ├─ expired             71
       ├─ invalidated         19
       ├─ suppressed           6   (conflict 2 · merged 3 · class-4 dedup 1)
       └─ triggered           22
            ├─ missed          3
            ├─ stopped         9
            ├─ target hit      7
            └─ open            3
```

The **binding-condition breakdown** is what makes this diagnostic rather than decorative. If a
strategy produces no setups, this shows whether one condition is blocking everything (a threshold
problem) or several rarely co-occur (a mechanism problem) — the distinction `specs/03` §11 says
demands opposite responses.

It also implements the Phase 9 frequency hypotheses (H1–H3, H5, H10) as **live monitoring** rather
than a one-off backtest result.

### Other operational views

| View | Contents |
|---|---|
| **Data health** | Per source: last update, age, error counts by code, `429`/`503` episodes, dropped polls by priority |
| **Pipeline** | Snapshot build time, feature compute time, NaN rate per feature, warm-up progress |
| **Divergence** | The §2–§4 weekly comparison — a separate analysis page, not on the trading dashboard |
| **Recorder** | Heartbeat age, records written per stream, gap log |

---

## 9. Alerting

Local, single-user. No paging infrastructure, no third-party service, no secret required by
default.

| Alert | Severity | Why |
|---|---|---|
| **Recorder heartbeat stale** | ⛔ **Loudest in the system** | The only failure that costs data **permanently**. Every minute down is an unrecoverable gap in funding and positioning history |
| Ingestion down > 10 min | High | Signals stop; recoverable |
| Repeated `429` | Medium | Rate limits misconfigured against P2's measurement |
| Disk space low | High | Writes will fail |
| Feature NaN rate spike | Medium | Data or numerical problem |
| **Signal drought** — nothing from any strategy for far longer than design frequency implies | Medium | May be correct; may be a gate stuck closed |
| **Signal flood** — far above design bands | Medium | Threshold loose, or a condition firing on noise |
| Live-only signal detected | ⛔ High | Non-determinism. A bug |

Drought and flood thresholds come from the design frequency bands in `specs/03` §11, `specs/04`
§12, `specs/05` §11 and `specs/06` §12 — the hypotheses become alerts.

**Recorder heartbeat:** the recorder touches a file each cycle; the app monitors its age and
surfaces staleness in the dashboard status bar with maximum prominence. This is deliberate
asymmetry — an app outage is an inconvenience, a recorder outage is permanent loss.

**Delivery:** dashboard status bar, desktop notification, and structured log. An optional outbound
webhook is supported but disabled by default, since enabling it introduces a secret and a failure
domain for a convenience.

---

## 10. Exit criteria

Shadow ends when **all** hold:

1. Minimum duration and ≥ 30 matched closed trades, reported per strategy
2. **Zero unexplained live-only signals.** Each instance either resolved or attributed to a
   recorded bar mutation
3. Level divergence zero on matched signals
4. `ΔR` distribution characterised, with its consequence for promote decisions stated
5. `missed_signal_rate` measured and attributed by cause
6. No window reset in the final 4 weeks
7. A written **go / no-go per strategy**

### The three honest outcomes

| Outcome | Response |
|---|---|
| **GO** | Divergence within tolerance; causes understood. Proceed to Phase 18 |
| **GO, reduced** | Live edge materially below backtest. Strategy proceeds at reduced size, or only its stronger members proceed. Record the revised expectation |
| **NO-GO** | `ΔR` shows the edge does not survive live execution. **Demote. Do not deploy.** Re-enter Phase 16 with the measured fill model |

NO-GO is a success of the process, not a failure of it. It is precisely the outcome shadow exists
to catch, arriving before capital rather than after.

---

## 11. Prohibitions

1. **No capital.** No order endpoint is callable; the key has no trading permission (`specs/00`
   §10). Nothing changes here.
2. **No parameter or strategy changes** during a window (§7).
3. **No P&L presentation to the user as if realised.** Shadow results are hypothetical outcomes of
   suggestions, not trades. Label them as such everywhere.
4. **No suppressing replay-only signals from the report** to make the miss rate look better.
5. **No extending a window because results look bad** — that is choosing a stopping rule after
   seeing data. Duration is fixed at the start and recorded.

---

## 12. Tests

| Test | Asserts |
|---|---|
| **Replay reproduces live** | Over a clean window with no outages, matched rate is 100% and level divergence is zero |
| **Outage exclusion** | An injected outage window is excluded from both sides and recorded |
| **`MISSED_OUTAGE` excluded** | Never enters the matched set or any performance statistic |
| **Live-only detection** | An injected non-determinism is caught and alerted |
| **Freeze rule** | A `parameter_hash` change resets the window; a template change does not |
| **Funnel arithmetic** | Every stage's inputs equal the sum of its outputs — no signals vanish silently |
| **Recorder heartbeat** | Killing the recorder raises the alert within one cycle |
| **Drought/flood** | Synthetic signal rates outside the design bands trigger alerts |
| **Stopping rule** | Duration is fixed at window start and cannot be extended by configuration mid-window |

---

## 13. Parameters — zero

Running total: **unchanged (29 or 30 after `specs/16` Stage 3).**

Alert thresholds derive from the design frequency bands already registered in the class specs;
durations and heartbeat intervals are operational configuration.

---

## 14. Acceptance criteria for Phase 17

1. Only strategies with a Phase 16 **PROMOTE** decision run in shadow. Tier B runs with
   `validated: false` and is excluded from the divergence-based promote review.
2. Parallel replay runs weekly and produces matched / live-only / replay-only sets.
3. All five divergence classes are measured and reported separately.
4. `ΔR` distribution is reported per strategy with matched-trade counts.
5. Outage windows and `MISSED_OUTAGE` signals are excluded, recorded, and auditable.
6. `missed_signal_rate` is attributed by cause.
7. The signal funnel is live, with the **binding-condition breakdown** per strategy.
8. Recorder heartbeat is the loudest alert in the system.
9. Window resets are recorded with cause; none in the final 4 weeks.
10. A go / no-go decision exists per strategy, including the possibility of demotion.
11. Shadow results are labelled hypothetical everywhere they appear.
12. Tier B matched-trade counts accumulate toward `earliest_tier_b_validation`.
