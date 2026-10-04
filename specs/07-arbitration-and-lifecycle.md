# 07 — Arbitration, Ranking, Lifecycle & Confidence

**Depends on:** `specs/03`–`specs/06`.
**Resolves:** the interim `CLASS_CONFLICT` rule (`specs/04` §7) and the same-direction
concentration case (`specs/05` §6).

---

## 1. The central decision: no cross-strategy composite score

**A composite score across strategies is rejected.** This is a deliberate decision, not an
omission.

Four reasons, each sufficient on its own:

1. **The quantities are incommensurable.** C3-A's strength is contraction depth and payoff
   ratio. C1-B's is deviation percentile and variance-ratio conviction. C4-C's is displacement
   and inverse volume rank. There is no defensible exchange rate between them, so any weighting
   would be invented.
2. **The inputs are shared, so agreement is not independent evidence.** Nearly every strategy
   consumes `ATR`, `MR_VR` and the regime cell. Two strategies agreeing frequently means one
   piece of information counted twice. Summing them manufactures conviction.
3. **A composite implies a calibration that does not exist.** A "signal score of 78" is
   meaningless without an empirical mapping from score to outcome, and no such mapping exists
   until Phase 16 — possibly not even then (§9).
4. **It rewards exactly the wrong instinct.** A scoring system creates pressure to add
   contributing dimensions, because more inputs feel like more rigour. The brief prohibits
   this, and the parameter budget exists to prevent it.

**What survives:** the per-strategy `strength` already defined in each class spec — at most two
near-orthogonal quantities, with setup conditions as gates that never contribute. That is a
composite within a single strategy's own logic, where the dimensions are commensurable and the
correlation is known by construction. That is the appropriate scope.

---

## 2. What replaces it: two numbers, deliberately not collapsed

Every signal carries two independent quantities that answer different questions:

| Quantity | Question it answers | Source |
|---|---|---|
| **`strength`** | How strongly are this strategy's conditions met? | Per-class spec §7/§9/§10/§11 |
| **`breakeven_hit_rate`** (`p*`) | What does this trade's geometry *require* to break even? | `specs/05` §2.1, computed with live fees |

**These must never be merged into one figure.** A trade with forgiving geometry and weak
conditions, and a trade with demanding geometry and strong conditions, are genuinely different
propositions. Collapsing them hides the difference and reintroduces the false precision §1
rejects.

### Ranking

- **Within a class:** rank by `strength` descending. Geometry is roughly comparable within a
  class — Class 3 members all clear `k ≥ 3`, for instance — so strength is the meaningful
  discriminator. `breakeven_hit_rate` is displayed alongside, never folded in.
- **Across classes: no ranking exists.** The classes occupy separate UI panels by design
  (`specs/00` roadmap, UI requirements), and comparing a Class 1 correction trade to a Class 3
  transition trade is comparing different risk profiles, not different qualities of the same
  thing. Any cross-class ordering would be a preference dressed as a measurement.

### After Phase 16

Once measured, out-of-sample, regime-conditioned hit rates exist and clear the §9 bar, ranking
within class **may** switch to estimated expected value per unit risk:

```
EV/R = p̂·k − (1 − p̂) − c/R
```

using the measured `p̂`. Until that bar is cleared, ranking stays on `strength`. Do not
substitute an assumed `p̂`.

---

## 3. The arbitration pipeline

Order of operations matters, and getting it wrong produces silent signal loss.

```
1. COLLECT      all raw signals from all 12 strategies, all states
2. CONFLICT     resolve opposing-direction conflicts        (§4)
3. MERGE        same-direction, same-asset consolidation    (§5)
4. DEDUP        Class 4 suggestion-mode deduplication       (§7)
5. RANK         within class, by strength                   (§2)
6. EXPOSE       compute portfolio concentration → Phase 8   (§6)
```

**Step 4 must come after steps 2 and 3, not before.** If Class 4 deduplication ran first, a
Class 4 signal could be suppressed as a duplicate of a Class 1–3 signal that is itself then
suppressed by conflict resolution — leaving the user with nothing when a perfectly good
benchmark signal existed. Assert this ordering in tests.

**Benchmark-mode Class 4 signals bypass the entire pipeline.** They are logged raw and
uncensored, because arbitration would contaminate the comparison the benchmark exists to
provide (`specs/06` §3).

---

## 4. Case A — opposing directions, same asset

### Why this still happens despite the partitions

The class partitions eliminate most opposing conflicts by construction — C1-B ⊕ C2-A on the
variance ratio, C1-A ⊕ C2-C on funding extremity, C1-C ⊕ C2-B after the `specs/04` §7 amendment,
C3-B ⊕ C2-A on `t_min`. But some pairs share no partition and genuinely can oppose:

- **C3-C rejection vs C4-A** — directly opposing on the *same event*. C4-A trades a channel
  break; C3-C rejection trades that same break failing.
- C1-B (short, price extended above the mean) vs C3-A (long, upside contraction resolution).
- C1-B vs C4-A across different timeframes.

### Resolution hierarchy — replaces the interim rule in `specs/04` §7

Applied in order, first decisive criterion wins:

**1. Evidential standing.**
- After Phase 16: a strategy with published, out-of-sample validation beats one without.
- Before Phase 16 (and for anything that never gets validated): **Tier A beats Tier B**, since
  Tier A *can* be validated and Tier B cannot.
- This is the only criterion grounded in evidence rather than design preference, so it ranks
  first.

**2. Timeframe seniority.** If both have equal standing, the signal originating on the higher
timeframe wins (1d > 1h). Higher-timeframe structure dominates lower-timeframe structure — a
market-structure principle, not an arbitrary tiebreak.

**3. Otherwise: suppress both.** Genuine ambiguity between equally-supported opposing readings
means the honest answer is no trade.

**In every case, the conflict is logged and surfaced.** The surviving signal's reasoning text
states that an opposing setup was suppressed, naming the strategy and its direction. That a
credible opposite case existed is decision-relevant information, and hiding it would make the
surviving signal look more certain than it is.

Where both are suppressed, the asset shows `NO VALID TRADE — conflicting evidence`, with both
suppressed signals available in the audit trail.

**Degradation note:** at launch, criterion 1 reduces to Tier A vs Tier B and will often be a
tie, pushing most conflicts to criterion 2 and then to suppression. That is acceptable and
expected. The hierarchy strengthens as validation data accumulates.

---

## 5. Case B — same direction, same asset

**Resolves `specs/05` §6.** Two strategies producing one economic position must not become two
entries.

### Merge, do not stack

The surviving output is **one suggestion**:

- **Primary strategy** is chosen by the §4 hierarchy (evidential standing, then timeframe
  seniority). Its entry, stop and targets govern.
- **Corroborating strategies** are listed in `corroborated_by` with their own IDs, directions
  and strengths, visible in the reasoning.
- **One risk allocation**, not two. This is the entire point of the merge and it is the fix for
  the concentration risk flagged in `specs/05` §6.

### ⚠ Strength is not additive

Agreement between two strategies does **not** increase conviction, because they likely share
inputs (§1.2). Pre-calibration:

> **Corroboration is displayed but does not change `strength`, does not change
> `breakeven_hit_rate`, and does not change sizing.**

Post-Phase-16, a strength adjustment for corroboration may be applied **only** if the measured
correlation between the two strategies' signal timings is known and low. The adjustment must be
derived from that measured correlation, never assumed. If the correlation is high, agreement is
one signal counted twice and warrants no adjustment at all.

### Why not the tightest or widest stop

Merging two theses means adopting one thesis's invalidation. Taking the tightest stop across
strategies would exit the wider-stop thesis prematurely on a level that means nothing to it;
taking the widest would degrade the tighter thesis's R:R below the gate that qualified it.
**The primary strategy's levels are used unmodified**, which keeps every published number
internally consistent with the reasoning that produced it.

---

## 6. Case C — same direction, different assets

At `X_CORR ≈ 0.85`, three concurrent longs across BTC/ETH/SOL are approximately one position at
2.6× size. This is the largest unmanaged risk in the system.

**Phase 7 does not size. It exposes.** The arbitration layer publishes:

```
portfolio_concentration: {
  net_direction_exposure: { long: n, short: m },
  pairwise_corr: { "BTC-ETH": ρ, "BTC-SOL": ρ, "ETH-SOL": ρ },
  effective_independent_positions,   # derived from the correlation matrix
  ranked_signal_set                  # the output of §3 step 5
}
```

`effective_independent_positions` is computed from the correlation matrix (e.g. via the sum of
eigenvalue-based diversification, or `n / (1 + (n−1)ρ̄)` as a transparent first form — the exact
estimator is a Phase 8 decision, but the inputs are produced here).

Phase 8 decides which signals survive sizing. Phase 7 guarantees it receives a **ranked,
conflict-resolved, deduplicated** set so that cutting exposure means cutting from the bottom of
a meaningful order rather than choosing arbitrarily.

---

## 7. Case E — Class 4 deduplication

Per `specs/06` §3, a Class 4 suggestion-mode signal surfaces only if no surviving Class 1–3
signal is active on the same asset in the same direction.

**Applied at step 4, against signals that survived steps 2 and 3 only.** Sets
`suppressed_by: <signal_id>`.

Benchmark mode is untouched.

---

## 8. Signal lifecycle state machine

Referenced throughout Classes 1–4; formalized here.

```
                  conditions pass
   CANDIDATE ──────────────────────► SETUP_DETECTED
   (internal)                            │  │  │
                                         │  │  └──► EXPIRED        (setup window elapsed)
                                         │  └─────► INVALIDATED    (setup invalidation hit)
                                         │  └─────► SUPPRESSED     (arbitration, §4/§5/§7)
                     price reaches E(t)  │
                                         ▼
                                    ENTER_NOW ──────► MISSED       (§8.2)
                                         │
                       price moves past  │
                                         ▼
                                    TRIGGERED
                                    │  │  │  │
                                    │  │  │  └─────► HANDED_OFF    (§8.3)
                                    │  │  └────────► INVALIDATED   (mechanism or structural)
                                    │  └───────────► STOPPED
                                    └──────────────► TARGET_HIT
```

### 8.1 What `TRIGGERED` does and does not claim

This is a suggestion system. It does not know whether the user entered.

`TRIGGERED` means **the entry condition occurred**, and the system is now tracking a
*hypothetical* trade so it can report invalidation, stop and target events. The UI must say
"entry triggered at X" — never "you are long." Every downstream number (`remaining_rr`, trail
level, mechanism invalidation) is explicitly about the hypothetical trade.

### 8.2 The missed-entry path — an honest answer to a common failure

`ENTER_NOW` is a **momentary** state: it holds while price remains within the measured spread
plus one tick of `E(t)`. Using the measured spread rather than an ATR multiple keeps this
structural and costs no parameter.

Once price moves beyond that band, the state becomes `TRIGGERED` with the trigger price
recorded. The UI then shows **"triggered at X, now at Y"** together with `remaining_rr`
computed from current price — so a user who looked away can see immediately that they missed it
and by how much.

**And a signal that has run too far past its trigger expires:**

> When `remaining_rr` falls below the originating class's minimum R:R gate, the signal
> transitions to `MISSED`.

The gate that qualified the trade now disqualifies it. This reuses an existing gate rather than
introducing a chase threshold, and it prevents the single most common way a suggestion system
harms its user: displaying a stale, once-good entry long after the geometry has decayed.

### 8.3 Handoffs

Two are specified — `C2-C → C1-A` (`specs/04` §5) and `C3-B → C2-A` (`specs/05` §4).

A handoff is **continuation, not exit-plus-new-entry**. The arbitration layer must:

1. Set the originating signal to `HANDED_OFF` — a success terminal state, never counted as a
   loss or an invalidation in any performance statistic.
2. Create the receiving signal in state `TRIGGERED` directly, **preserving the original entry
   price and timestamp**.
3. Replace stop, targets and exit logic with the receiving strategy's rules.
4. Record `handoff_from` and `handoff_to` on both records.
5. **Recompute and publish both R:R figures** (§8.4).

For performance attribution, a handed-off trade is **one trade** spanning both strategies, with
the P&L attributed to the originating strategy up to the handoff and the receiving strategy
thereafter. Counting it as two trades would inflate the trade count and distort both strategies'
statistics.

### 8.4 Two R:R figures, always

Any signal past `SETUP_DETECTED` publishes both:

- **`risk_reward`** — from the original entry. The trade's actual geometry. Fixed once triggered.
- **`remaining_rr`** — from current price to target, over the distance to the current stop. The
  live remaining opportunity.

Showing only the first misleads a late user; showing only the second erases the trade's actual
terms. Both are cheap to compute and both are decision-relevant.

---

## 9. Confidence — the exact bar for publishing one

`confidence` is `null` system-wide (`specs/03` §8). This section defines what would change that,
so the decision is a pre-registered standard rather than a judgment call made later under
pressure to show a number.

### Publishability criteria — ALL must hold

1. **Out-of-sample only.** In-sample hit rates are prohibited as a source, without exception.
2. **Sufficient sample.** The 95% Wilson score interval half-width on the hit rate must be
   ≤ 10 percentage points. Near `p = 0.5` that requires roughly **n ≥ 100 closed trades** in
   the relevant stratum.
3. **Regime diversity.** The sample must span at least two distinct regime cells with
   meaningful occupancy in each. One hundred trades drawn entirely from one directional regime
   measures that regime, not the strategy.
4. **Published with its interval and sample size**, never as a bare number. The UI shows
   `42% [31–53%], n=118`, not `42%`.
5. **Re-estimated on a rolling basis**, with a drift check against the prior estimate. A
   material shift invalidates the estimate rather than updating it quietly.

If the interval is too wide, show **the interval alone or nothing** — never a point estimate
with a hidden interval.

### ⚠ The uncomfortable arithmetic, stated now rather than discovered later

Class 1 is designed for 4–8 signals per asset per month. Across three assets that is roughly
15–25 per month, and not all close. Reaching 100 closed trades takes **months at best.**
Splitting that across nine regime cells takes **years**, and possibly never arrives.

Two consequences:

- **For confidence purposes only**, collapse the regime model to a coarse conditioning of **two
  or three cells** (for example: trending / mean-reverting / transitional). This is a reporting
  decision and does not alter the `specs/02` §6 model used for gating.
- Even then, expect confidence to remain `null` for low-frequency strategies **for a long time,
  possibly permanently.**

**That is a correct outcome, not a failure of the system.** A strategy that trades rarely cannot
support a reliable hit-rate estimate, and saying so is the honest response. The UI must present
`confidence: null` as a normal, explained state — "not enough closed trades to estimate
reliably (n=14)" — rather than as an error or a blank.

### What is published instead, and why it is better

`breakeven_hit_rate` is available from the first signal, requires no history, is computed from
live fees and the trade's own geometry, and answers a question the user actually needs answered:
*how often does this have to work?* It is a real number where `confidence` would be a guess.

---

## 10. Staleness

A signal is only as good as the data behind it.

- Every signal carries `data_freshness` per source (`specs/03` §9).
- If **any** hard `DQ_*` gate fails for an input the signal depends on, the signal is marked
  `STALE` and rendered non-actionable in the UI — greyed, with the failing source named.
- `STALE` is not a terminal state. If data recovers within the setup's expiry window, the signal
  returns to its prior state; otherwise it expires.
- A `TRIGGERED` signal that goes `STALE` **continues to display its stop and invalidation
  levels**, because a user who acted on it needs those more than ever when data is degraded.
  It stops updating `remaining_rr` and says so.

---

## 11. Signal object — additions

```
# arbitration
corroborated_by: [ {strategy_id, direction, strength} ],
conflict_suppressed: [ {strategy_id, direction, reason} ],
suppressed_by: null | signal_id,
arbitration_reason: null | string,

# lifecycle
state: "SETUP_DETECTED" | "ENTER_NOW" | "TRIGGERED" | "MISSED"
     | "TARGET_HIT" | "STOPPED" | "INVALIDATED" | "EXPIRED"
     | "HANDED_OFF" | "SUPPRESSED" | "STALE",
state_since, trigger_price, trigger_time,
risk_reward,        # from original entry, fixed once triggered
remaining_rr,       # from current price, live
handoff_from, handoff_to,

# confidence
confidence: null | { point, ci_low, ci_high, n, regime_stratum },
confidence_unavailable_reason: null | string,   # e.g. "n=14, below threshold"

# portfolio (attached to the signal set, not individual signals)
portfolio_concentration: { ... }   # §6
```

---

## 12. Parameters — zero

Running total: **25 → 25. Unchanged**, as forecast in `specs/05` §10.

**Made structural rather than fitted:**

- `ENTER_NOW` tolerance = measured spread + one tick. Derived from live data, not chosen.
- `MISSED` threshold = the originating class's existing minimum R:R gate. Reuses an existing
  gate rather than introducing a chase parameter.
- Confidence interval half-width ≤ 10pp. A statistical reporting convention, consistent with the
  fixed `|ρ| ≥ 0.8` redundancy cut in `specs/02` §7.
- The arbitration hierarchy is a rule with no free quantities.

Phase 8 remains the expected point at which the cap is raised to roughly 30, deliberately and
with each new parameter justified individually.

---

## 13. Acceptance criteria for Phase 7

1. **No cross-strategy composite score exists anywhere in the codebase.** Assert by review; a
   single number combining strengths from different strategies is a defect.
2. The §3 pipeline runs in the specified order, with a test proving Class 4 deduplication
   happens after conflict resolution and merging.
3. Benchmark-mode Class 4 signals bypass arbitration entirely and are logged uncensored.
4. The §4 hierarchy is implemented and degrades correctly pre-Phase-16 (Tier A over Tier B, then
   timeframe, then suppress both). Suppressed conflicts are surfaced in the surviving signal's
   reasoning.
5. Same-direction merges produce **one** signal with **one** risk allocation, and `strength` is
   provably unchanged by corroboration. Unit-test this — it is the easiest rule to violate
   accidentally.
6. The full state machine is implemented, including `MISSED` via the `remaining_rr` rule and
   `STALE` as a non-terminal state.
7. Handoffs preserve original entry price and timestamp, are recorded on both signals, and count
   as **one** trade in all performance statistics.
8. Both `risk_reward` and `remaining_rr` are published on every signal past `SETUP_DETECTED`.
9. The §9 confidence criteria are implemented as a gate, and `confidence_unavailable_reason` is
   populated whenever the gate fails, so the UI can explain the null rather than showing a blank.
10. The `specs/04` §7 interim conflict rule and the `specs/05` §6 concentration case are marked
    **resolved** in `docs/findings.md`, with a pointer to this document.
