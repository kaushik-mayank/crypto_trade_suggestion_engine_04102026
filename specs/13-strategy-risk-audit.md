# 13 — Strategy, Arbitration & Risk Engines, with the Audit Trail

**Implements:** `specs/03`–`specs/08`. **Depends on:** `specs/10` (snapshot), `specs/12` (features,
regime).
**Character:** implementation spec.

---

## 1. Strategy interface

All 14 strategies (12 tradeable + 2 controls) share one interface so arbitration and risk can
treat them uniformly, while each keeps its own logic.

```python
class Strategy(Protocol):
    id: str                    # "C1-B"
    version: str               # bumps on any logic change
    strategy_class: int        # 1-4
    tier: Literal["A", "B"]
    tradeable: bool            # False for C4-X1, C4-X2
    expansion_seeking: bool    # exempts from VOL_EXTREME veto (specs/08 §8 Tier 3)
    signal_timeframe: str      # only "1h" or "1d" may originate setups
    required_features: list[str]

    def evaluate(snapshot, features, regime, state) -> StrategyOutcome
```

`expansion_seeking` is a **declaration, not a special case in the risk engine** — C3-A sets it
true, everything else false, and the veto reads the flag. `specs/08` §8 requires this so the risk
engine contains no strategy names.

### Outcome types

```python
StrategyOutcome =
    | NoSetup(conditions: list[ConditionResult])
    | Setup(raw_signal, conditions)
    | Update(raw_signal, act_state)
    | Terminal(reason, conditions)      # INVALIDATED | EXPIRED | HANDED_OFF
```

> **Every outcome carries the full condition evaluation, including `NoSetup`.**

This is what makes the audit able to answer *"why did it reject the alternatives?"* — and it is
what makes hypotheses H1, H2, H3, H5 and H10 (`specs/09` §11) testable at all. Without the
rejection record, "Class 1 never fired" is an observation with no diagnosis.

---

## 2. Strategy state — explicit, persisted, reconstructible

Features hold no state (`specs/12` §1). Strategies legitimately do: ACT tracks `X*`, setups have
an arming time and an expiry.

```python
class SetupState:
    signal_id, strategy_id, asset, product, direction
    armed_at_ms: int
    armed_snapshot_id: str
    x_star: Decimal
    e_t: Decimal
    lifecycle: LifecycleState          # specs/07 §8
    expires_at_ms: int
    original_entry_price: Decimal | None    # set at TRIGGERED
    parameter_hash, strategy_version
```

### The reconstruction rule

> **`X*` is computed from closed 1m bars since `armed_at_ms`, not accumulated.**
> `X*_long = min(low) over closed 1m bars in [armed_at_ms, as_of]`

So state is **derived, not accumulated** — the same discipline as `specs/12` §1, applied one layer
up. Consequences:

- A restart rebuilds `X*` exactly from the bar store.
- Backtest and live produce identical `X*` for identical windows.
- The audit can reconstruct `X*` at any past moment without having stored it.

**Why closed bars only:** the trigger confirms on a 1m close (`specs/03` §2), so letting `X*`
track a forming bar's intra-minute low would make the trigger depend on data the confirmation
rule does not use. Closed-bar-only keeps the two consistent and keeps `X*` reconstructible.

State is persisted to SQLite on every change. In backtest it lives in memory and resets per run.

---

## 3. ACT — one implementation, nine consumers

```python
def act(direction: Direction,
        series: Sequence[Decimal],     # closed 1m closes, or the spread series for C1-C
        armed_at_idx: int,
        unit: Decimal,                 # ATR_15m, or rolling σ of the spread
        delta: Decimal) -> ActState:
    """Pure. No state. No I/O. No clock."""
```

Consumers: C1-A, C1-B, C1-C, C2-A, C2-B, C2-C, C3-B, C3-C (rejection branch), C4-B.
**A second implementation anywhere is a defect** (`specs/04` acceptance 1, `specs/05` acceptance 4,
`specs/06` acceptance 7).

### Properties to test with Hypothesis

| Property | Statement |
|---|---|
| Ratchet | For LONG, `E(t)` is monotone **non-increasing** in `t` |
| Mirror | For SHORT, `E(t)` is monotone **non-decreasing** |
| No knife-catching | On a strictly monotone decline, a LONG never triggers |
| Symmetry | `act(LONG, s) ≡ act(SHORT, −s)` under negation |
| Unit scaling | Doubling `unit` doubles the gap `E(t) − X*` |

---

## 4. Conditions as first-class objects

```python
class Condition:
    name: str
    description: str               # surfaced in the UI reasoning
    required_features: list[str]

    def evaluate(ctx) -> ConditionResult

class ConditionResult:
    name: str
    passed: bool
    value: Any                     # what was measured
    threshold: Any                 # what it was compared to
    reason: str | None             # set when a feature was not OK
```

### No short-circuiting

> **Evaluate every condition, always. Never stop at the first failure.**

`specs/08` §8 requires this of vetoes; it applies equally to setup conditions. A signal failing
one condition is diagnostically different from one failing five, and the difference is exactly
what tells you whether a threshold is slightly tight or the mechanism is absent (`specs/03` §11).

A condition whose feature is not `OK` returns `passed=False` with `reason` carrying the feature's
status — never a silent false.

---

## 5. Evaluation cadence

Following the timeframe hierarchy of `specs/02` §3 — **only 1h and 1d originate setups**:

| Trigger | Action |
|---|---|
| **1h bar final** | Full setup evaluation for all 1h-originating strategies. Arm, disarm, expire. Mechanism invalidations. |
| **1d bar final** | Same for 1d-originating strategies. |
| **1m bar final** | For armed setups only: recompute `X*` and `E(t)`, check crossing, check price invalidation and `MISSED` (`specs/07` §8.2). No setup conditions re-evaluated. |
| **Feature update** | Mechanism invalidations that depend on Tier B values (funding, positioning) may fire between bar closes. |

"Bar final" means `is_final = True` per `specs/11` §6 — never a freshly-fetched bar.

---

## 6. Determinism

Given the same snapshot and the same persisted state, evaluation must produce byte-identical
output.

- **No strategy calls `time.now()`.** `as_of_ms` comes from the snapshot. Enforce by review; a
  clock read inside `src/strategies/` is a defect.
- **No randomness** except the seeded control strategies, whose seeds are recorded.
- **No dependence on dict or set iteration order.** Sort explicitly wherever order affects output.
- **No I/O.** Strategies receive everything they need as arguments.

Test: evaluate the same snapshot twice in one process and across two processes; assert identical
output including the audit record.

---

## 7. Arbitration implementation

`specs/07` §3's pipeline, with each stage a **pure function on the signal set**:

```python
collect() -> resolve_conflicts() -> merge_same_direction()
          -> dedup_class4() -> rank_within_class() -> expose_portfolio()
```

Stage order is enforced structurally by the function composition, not by convention — `specs/07`
§3 warns that running Class 4 dedup before conflict resolution silently loses good signals.

Every stage writes to `arbitration_events`: conflicts with both sides' full condition sets,
merges with the chosen primary and the corroborators, suppressions with the superseding signal
id, handoffs with both strategy ids.

**Benchmark-mode Class 4 signals bypass the pipeline entirely** (`specs/06` §3) and are written
straight to storage, uncensored.

---

## 8. Risk engine implementation

### 8.1 Immutability enforced structurally

`specs/08` criterion 1 requires that the risk engine cannot modify strategy-produced levels.
Enforce with types, not discipline:

```python
@dataclass(frozen=True)
class RawSignal:
    entry, stop, targets, direction, ...     # immutable

@dataclass(frozen=True)
class Signal:
    raw: RawSignal                            # wrapped, never rewritten
    sizing: SizingResult
    vetoes: list[VetoResult]
    ...
```

The risk engine **wraps**; it never rebuilds. A test asserts `signal.raw` is byte-identical to
what the strategy produced.

### 8.2 The veto ladder

All four tiers evaluated, all results recorded, **no short-circuit** (`specs/08` §8). A signal
failing `DQ_STALE` still has its economic and portfolio vetoes evaluated and logged — that is how
you learn whether a data outage was masking a trade that would have been rejected anyway.

### 8.3 Sizing

`min(size_stop, size_vol)` per `specs/08` §2.2, with `size_basis` recorded. The C3-A case — where
`σ_forward` must be the **expanded** ATR, not the contracted one — gets its own named function so
the substitution cannot be made by accident:

```python
def sigma_forward(strategy_id, features) -> float:
    """C3-A expects expansion; its forward vol is ATR_long, not the contraction ATR."""
```

---

## 9. Audit trail

### 9.1 Built incrementally, written atomically

An `AuditBuilder` accumulates through the pipeline — features used, all condition results, ACT
state, regime state, arbitration decisions, all veto results, levels before and after rounding,
fees used — and writes one atomic record at the end of evaluation.

A partial audit record is worse than none: it looks complete and is not. Write it in one
transaction with the signal itself.

### 9.2 Rejections are recorded too

`specs/10` §7 requires a `rejections` table. Volume: 14 strategies × 3 assets × 2 products,
evaluated hourly, mostly rejecting ≈ **2,000 records/day**. Full condition detail on each is
several MB per day — fine for SQLite, but unbounded.

**Retention:**

| Age | Detail kept |
|---|---|
| ≤ 30 days | Full condition results, all features used |
| > 30 days | Rolled up to daily per-strategy condition-failure histograms |

The roll-up preserves exactly what `specs/09` §11's frequency hypotheses need — *which condition
is binding, how often* — while discarding the per-evaluation detail that is only useful for
recent debugging.

---

## 10. Replay

Two modes, both required:

| Mode | Behaviour |
|---|---|
| `replay_exact(signal_id)` | Requires matching `code_version` and `parameter_hash`. Rebuilds the snapshot from the bar store at `as_of_ms`, re-runs the full pipeline, **asserts identical output.** A mismatch is a bug. |
| `replay_current(signal_id)` | Re-runs under current code and reports the **diff**. This is a feature, not a fallback — it shows exactly what a change altered about a past decision. |

`replay_exact` runs in CI over a stored sample (`specs/10` criterion 6).

**Known cause of legitimate replay failure:** a bar mutation (`specs/11` §6.2) flags affected
signals. `replay_exact` on a flagged signal reports `BAR_MUTATED` rather than failing as a bug.

---

## 11. Restart and outage handling

### 11.1 Restart

On startup, for each persisted `SetupState`:

1. Re-evaluate the setup conditions against the current snapshot.
2. Conditions still hold and not expired → **re-arm with the original `armed_at_ms`**, so `X*`
   spans the correct window and expiry counts from the original arming.
3. Conditions no longer hold → `INVALIDATED`, with the failing condition recorded.
4. Past expiry → `EXPIRED`.

### 11.2 ⚠ A trigger that fired while the system was down

Reconstruct `E(t)` across the outage from 1m bars and check whether a crossing occurred.

> **If it did, mark the signal `MISSED_OUTAGE`. Do not mark it `TRIGGERED`.**

`TRIGGERED` implies the user had the opportunity to act. They did not — the dashboard was dark.
Recording it as `TRIGGERED` would insert a trade into the live record that no user could have
taken, quietly inflating live performance.

**Consequence for validation:** outage windows must be recorded in `dq_events` and **excluded
from any live-versus-backtest comparison**, or at minimum flagged in it. The backtest counts
those trades; live could not. `specs/09`'s Phase 17 shadow-run comparison depends on this
distinction being honest.

---

## 12. Handoffs

`specs/07` §8.3, implemented as one record spanning two signals:

```python
class Handoff:
    from_signal_id, to_signal_id
    from_strategy_id, to_strategy_id
    at_ms, at_price
    original_entry_price, original_entry_ms     # PRESERVED
```

- Originating signal → `HANDED_OFF`, a **success** terminal state. Never counted as a loss or an
  invalidation in any statistic.
- Receiving signal created directly in `TRIGGERED`, carrying the original entry price and time.
- Stop, targets and exit logic replaced with the receiving strategy's rules.
- **One trade** in all performance accounting (`specs/09` §4), P&L split at `at_price`.

Specified handoffs: `C2-C → C1-A` (`specs/04` §5), `C3-B → C2-A` (`specs/05` §4).

---

## 13. Control strategies — firewalled

C4-X1 and C4-X2 carry `tradeable = False`.

- They run **only** in backtest mode.
- The API layer filters on `tradeable`, so they can never reach a response.
- Seeds are recorded for reproducibility (`specs/06` §10) and are not parameters.

Test: assert no API endpoint ever returns a signal with `tradeable = False`, exercised against a
backtest run that produced control signals.

---

## 14. Module layout

```
src/strategies/
├── base.py            Strategy protocol, StrategyOutcome, SetupState
├── act.py             ← THE one implementation
├── conditions.py      Condition, ConditionResult, the no-short-circuit evaluator
├── lifecycle.py       the specs/07 §8 state machine
├── c1a.py … c4c.py    12 tradeable
└── controls/          c4x1.py, c4x2.py

src/arbitration/
├── pipeline.py        the six ordered stages
├── conflict.py        specs/07 §4 hierarchy
├── merge.py           specs/07 §5
└── portfolio.py       specs/07 §6 concentration exposure

src/risk/
├── engine.py          orchestration, immutable wrapping
├── vetoes/            one module per tier
├── sizing.py          min(stop, vol), sigma_forward
├── leverage.py        recommended vs max_safe, liquidation
└── costs.py           ← SHARED with backtest (specs/08 criterion 10)

src/audit/
├── builder.py         incremental accumulation
├── store.py           atomic write, retention roll-up
└── replay.py          exact + current
```

---

## 15. Tests

| Test | Asserts |
|---|---|
| **ACT properties** | All five §3 properties, via Hypothesis |
| **Single ACT** | Import graph shows one implementation, nine consumers |
| **Mutual exclusivity** | C1-B ⊕ C2-A on `MR_VR`; C1-A ⊕ C2-C on `m_extreme`; C1-C ⊕ C2-B on `MR_VR`; C3-B ⊕ C2-A on `t_min` **and** pullback band. Each asserted over generated market states |
| **No short-circuit** | With a first-condition failure, all remaining conditions still appear in the result |
| **Immutable levels** | `signal.raw` byte-identical before and after risk processing |
| **Determinism** | Same snapshot twice, same process and across processes → identical output and audit |
| **No clock in strategies** | Static check: no `time`/`datetime.now` import under `src/strategies/` |
| **`X*` reconstruction** | Rebuilt from bars equals the live-tracked value over a long window |
| **Restart** | Kill mid-setup; on restart the setup re-arms with the original `armed_at_ms` |
| **Outage trigger** | A crossing during an outage yields `MISSED_OUTAGE`, never `TRIGGERED` |
| **Replay exact** | Sample of stored signals reproduces exactly |
| **Replay current** | Produces a readable diff after a deliberate version bump |
| **Handoff** | One trade, original entry preserved, originator not counted as a loss |
| **Controls firewalled** | No API response ever contains `tradeable = False` |
| **C3-A sigma_forward** | Uses expanded ATR; a test with contracted ATR proves the size differs materially |
| **Rejection retention** | After roll-up, condition-failure histograms still answer H1/H5/H10 |

---

## 16. Parameters — zero

Running total: **30 → 30. Unchanged.** Retention windows, evaluation cadence and cache sizes are
operational configuration — they affect timeliness and storage, never a signal's content.

---

## 17. Acceptance criteria for Phase 13

1. One ACT implementation, nine consumers, all five properties tested.
2. Every strategy outcome — including `NoSetup` — carries the **complete** condition evaluation.
3. No short-circuit anywhere in condition or veto evaluation.
4. Strategy levels are structurally immutable through the risk engine.
5. `X*` is reconstructible from bars; live and rebuilt values match over a long window.
6. Determinism holds within and across processes.
7. No clock read, randomness (outside seeded controls), or I/O inside `src/strategies/`.
8. All four mutual-exclusivity partitions asserted under test.
9. `replay_exact` passes in CI over a stored sample; `replay_current` produces a usable diff.
10. Outage-window triggers are `MISSED_OUTAGE` and outage windows are excluded from
    live-versus-backtest comparison.
11. Handoffs preserve original entry and count as one trade.
12. Controls never reach the API, asserted against a backtest run that generated them.
13. Rejection records exist with full detail for 30 days and roll up thereafter without losing
    what `specs/09` §11 needs.
