# 05 — Strategy Class 3: Anticipatory Transition Strategies

**Depends on:** `specs/00`, `specs/02` (regime model is load-bearing here), `specs/03` (ACT),
`specs/04` (partition boundaries).
**Design intent:** moderate frequency, **low hit rate, high payoff ratio**. Every element of
this class exists to make a low hit rate survivable and visible.

---

## 1. Class identity

Classes 1 and 2 trade **states**. Class 1 trades the dislocated state; Class 2 trades the
persistent state. Both wait for their state to be confirmed before acting.

**Class 3 trades transitions between states** — and by definition, a transition is the moment
when state classification is least reliable. That is precisely where the added risk comes from,
and it is a structural property, not a tuning choice.

This maps directly onto the two axes of the `specs/02` §6 regime model, plus the structural
dimension:

| ID | Transition | Axis | Direction known at setup? |
|---|---|---|---|
| **C3-A** | Volatility contraction → expansion | Axis V | **No** — directionless bracket |
| **C3-B** | Mean-reverting → trending (trend birth) | Axis P | Yes, from the impulse |
| **C3-C** | Range → acceptance or rejection at a boundary | Structure | No — decided by the resolution |

The partition is derived from the regime model rather than invented, which is the test of
whether this class is principled or merely a third bucket.

### Every Class 3 strategy is Tier A. This is deliberate.

There is no Tier B strategy in this class, and there must not be one.

A class whose defining property is a **low hit rate** cannot responsibly be deployed without
empirical validation, because the gap between "this loses 7 of 10 as designed" and "this
loses 9 of 10 because the premise is wrong" is invisible without a backtest. Classes 1 and 2
can tolerate an unvalidated member because their premises are individually defensible on
mechanism alone. This class cannot. Any future Tier B idea for Class 3 must wait until its
data has matured enough to backtest.

---

## 2. Quantifying the additional risk

The brief requires the extra risk to be exposed, not merely acknowledged. Four mechanisms.

### 2.1 The breakeven hit rate — published on every signal

With `R` = risk in price terms, `W` = reward to T1, `c` = round-trip cost in price terms:

```
EV = p·(W − c) − (1−p)·(R + c)
EV = 0  ⟹  p* = (R + c) / (W + R)
```

Equivalently, with `k = W/R`:  **`p* = (1 + c/R) / (k + 1)`**

| `k` (R:R) | `p*` at `c/R = 0.10` |
|---|---|
| 1.5 | 44.0% |
| 2.0 | 36.7% |
| 3.0 | **27.5%** |
| 4.0 | 22.0% |

**`breakeven_hit_rate` is a required field on every Class 3 signal.** We cannot publish a
probability of success — there is no backtest, and `specs/03` §8 forbids inventing one. But we
*can* publish exactly what the trade's own geometry requires in order to break even. That is a
computed, honest, decision-relevant number, and it is the right substitute for the probability
we do not have.

### 2.2 A higher R:R floor, derived rather than chosen

Class 3 requires **`k ≥ 3.0`**, so that the strategy remains profitable down to a ~27.5% hit
rate at realistic costs. This is not a fitted parameter — it is read off the breakeven formula
by choosing the worst hit rate the class should survive. If Phase 16 measures an actual hit
rate materially below 27.5%, the strategy is cut, not re-floored.

### 2.3 Reduced risk budget per trade

Class 3 positions receive a fraction of the per-trade risk budget allocated to Classes 1 and 2
(suggested 0.5×, owned by Phase 8). Justification: higher outcome variance and an
unvalidated hit rate. Volatility-targeted sizing normalizes across assets; this multiplier
normalizes across *classes*.

### 2.4 Explicit disclosure fields

Every Class 3 signal carries `risk_classification: "elevated"` and a reasoning line stating in
plain language that the strategy expects to be wrong more often than right and depends on
payoff ratio for its expectancy. This must appear in the UI next to the signal, not in a
footnote or a documentation page.

---

## 3. C3-A — Volatility Contraction Resolution

### Hypothesis

Realized volatility clusters and mean-reverts — among the most robust empirical regularities
in any market. A period of unusually compressed volatility is reliably followed by expansion.
**The timing is partially predictable; the direction is not.** So do not predict direction:
arm a bracket and react to the first credible commitment.

### Why the asymmetry is mechanical rather than hoped for

This is the strongest structural property in the entire system:

- The **stop** is scaled by *contracted* volatility — narrow, by definition of the setup.
- The **target** is scaled by *expanded* volatility — the un-contracted norm.
- Their ratio is therefore approximately `ATR_long / ATR_contraction ≈ 1 / VOL_RATIO`.

A `VOL_RATIO` of 0.4 implies roughly a 2.5:1 structure **before any target is chosen**. The
setup can compute and publish its own expected payoff ratio from measured quantities, and the
R:R gate of §2.2 becomes a filter on the contraction's tightness rather than an arbitrary
requirement. Loose contractions fail the gate automatically.

### Setup conditions — ALL must hold

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | `VOL_RATIO(1h)` ≤ `q_contract` percentile of its trailing distribution | `VOL_RATIO` | Compression is genuinely unusual |
| 2 | Compression persisted ≥ the short lookback (`specs/02` §4) | `VOL_RATIO` | Not one quiet bar |
| 3 | `VOL_OF_VOL` low | `VOL_OF_VOL` | A stable contraction, not a pause inside chaos |
| 4 | Range compression confirmed: 1h true range over the window in the bottom tail | `ATR` | Structural agreement with the statistical measure |
| 5 | Contraction boundaries are well defined: high and low of the compression window | `STR_SWING` | There is something to break |
| 6 | Expected payoff `ATR_long_1h / contraction_range ≥ 3.0` | derived | The §2.2 gate, applied at setup |
| 7 | DQ gates pass | Family 10 | |

**No Axis P requirement.** This setup is direction-agnostic by construction.

**Optional strength input — session timing.** Crypto exhibits strong intraday seasonality in
volume and volatility around session boundaries and the 00:00 / 08:00 / 16:00 UTC funding
boundaries. A contraction approaching a historically high-volatility window is more likely to
resolve promptly. This is computable Tier A from backfilled 1h data (hour-of-day volatility
profile) and contributes to **strength only** — never to the gate, and never to direction.

### Entry — a directionless bracket, not ACT

ACT is an arrest-confirmation mechanism for entering *against* a move. Here we enter *with*
the resolution, so ACT does not apply.

```
E_up = high of the contraction window
E_dn = low  of the contraction window

ENTER LONG   ⟺  a confirmed 15m close > E_up
ENTER SHORT  ⟺  a confirmed 15m close < E_dn
```

Using a **15m close** rather than an ATR-multiple buffer is a deliberate choice: it filters
wicks without introducing a new parameter, and the two trigger levels are exact, structural,
and publishable. The user sees both prices and watches price sit between them.

The signal schema therefore needs `entry_triggers: {long, short}` rather than a single
`live_entry_trigger` (§8).

### Stop, target, invalidation

- **Stop:** the **opposite boundary** of the contraction. Full structural invalidation — if
  price breaks up, re-enters, and crosses the entire compressed range, the break failed.
  This is wide *relative to the contraction* and narrow in absolute terms, which is the point.
- **T1:** `entry ± ATR_long_1h` — the expansion target, derived from the un-contracted norm.
  Not a parameter.
- **Remainder:** trailed at `s·ATR_1h` from the running extreme, reusing `s`.
- **Structural invalidation:** stop as above.
- **Mechanism invalidation:** `VOL_RATIO` returns to normal **without** a directional
  resolution — the expansion occurred as chop rather than as a move, so the premise is spent.
  Void the setup even though no trigger fired.
- **Expiry:** contractions do not persist indefinitely. If no resolution within `N_exp` 1h bars,
  expire.

### Dominant failure mode, stated

**False breakouts.** Price will frequently clear a boundary, fail, and reverse through the
range. This is common, expected, and is the single reason the R:R floor of §2.2 exists. Do not
add filters to suppress false breaks — the standard attempts (volume confirmation, multi-bar
holds) cost more in missed resolutions than they save in avoided whipsaws, and each one is a
parameter fitted to a specific history.

---

## 4. C3-B — Persistence Transition (Trend Birth)

### Hypothesis

The regime model's Axis P classifies mean-reverting versus trending behaviour. C2-A requires
an **established** trend (`|TRD_TSTAT| ≥ t_min`), by which point a meaningful portion of the
move has already occurred. The transition itself — `MR_VR` crossing above 1 with a directional
impulse — marks trend birth. Entering there captures what C2-A concedes.

### A measured base rate, used honestly

The `specs/02` §6 regime transition matrix gives the **empirical probability that a newly
entered trending cell persists**. This is a real, measured quantity, not an assumption — and
it directly bounds how often this setup can be expected to work.

**It is published as a labelled regime statistic (`regime_persistence_prob`), never as a trade
win rate.** Regime persistence and trade success are different events; conflating them would
be exactly the kind of manufactured probability `specs/03` §8 prohibits. If the transition
matrix shows newly-entered trending cells persist only marginally better than chance, that is
strong evidence against this strategy and should be treated as such.

### Setup conditions — ALL must hold

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | `MR_VR(1h)` crosses from < 1 to > 1 | `MR_VR` | The transition event |
| 2 | Accompanied by a directional impulse: `\|TRD_RET(short)\|` in the upper tail | `TRD_RET` | Establishes direction |
| 3 | **`\|TRD_TSTAT(1h)\| < t_min`** | `TRD_TSTAT` | **Hard partition from C2-A** — the trend is not yet established |
| 4 | The impulse created a new swing extreme | `STR_SWING` | Structural confirmation |
| 5 | `VOL_RATIO` expanding | `VOL_RATIO` | Transitions normally coincide with vol expansion; disagreement means one measure is wrong |
| 6 | `sign(TRD_TSTAT(1d))` not significantly opposed | `TRD_TSTAT` | Not fighting the higher timeframe |
| 7 | Transition is fresh — within `N_fresh` bars of the crossing | regime model | This edge decays quickly |
| 8 | DQ gates pass | Family 10 | |

**Direction:** sign of the impulse.

### Entry — ACT, with the pullback band that C2-A leaves vacant

At trend birth, pullbacks are shallow because urgency is high. C2-A requires a pullback in
`[1.0, 3.0]` ATR. C3-B takes the **contiguous band below it: `[0, 1.0)` ATR.**

This is derived from C2-A's existing structural constant, consumes **zero new parameters**,
and makes the two strategies non-overlapping on pullback depth as well as on `t_min`.

ACT is reused unchanged from `specs/03` §2: `X*` is the running low of the shallow pullback,
`E(t) = X* + δ·ATR_15m`.

### Stop, target, invalidation

- **Stop:** `X* − s·ATR_1h`, with a **hard structural requirement**: this must sit above the
  impulse origin. If the pullback has already retraced to where the impulse began, the
  transition is failing and the setup **voids** rather than trading with a wider stop.
- **T1:** measured move — the impulse length projected from the pullback extreme. Structural,
  no parameter.
- **Remainder:** trailed at `s·ATR_1h`.
- **Mechanism invalidation:** `MR_VR(1h)` falls back below 1 — the transition failed. Exit.
- **Structural invalidation:** close beyond the impulse origin.

### Handoff to C2-A

When `|TRD_TSTAT(1h)|` reaches `t_min`, C3-B's partition condition (3) no longer holds. **This
is success, not invalidation:** the trend has matured into one C2-A recognises. The position is
retained and transfers to C2-A's management rules (structural stop, T1 at prior swing, trail).

Log as `HANDOFF C3-B → C2-A`. This mirrors the `C2-C → C1-A` handoff in `specs/04` §5 — a
consistent system-level pattern in which strategies pass positions along a lifecycle rather
than each holding its own independent view.

---

## 5. C3-C — Structural Boundary Resolution

### Hypothesis

Well-defined structural boundaries — prior-day and prior-week extremes, confirmed swing levels
with repeated touches — concentrate resting orders and stop clusters. When price reaches one,
it resolves as **acceptance** (sustained trade beyond, with participation) or **rejection**
(failure to sustain, reclaim of the level). The strategy does not predict which; it reacts to
whichever occurs.

### The mechanism, and the honest limit of our evidence for it

The premise is that stops cluster beyond obvious levels, so a break that triggers them and then
fails leaves those participants trapped and converts them into forced flow in the opposite
direction.

**We cannot observe any of this.** There is no liquidation feed and no message-level order flow
(`specs/00` §6). The mechanism is *inferred from structure*, not measured. That is a weaker
epistemic footing than C1-A's funding cash flow or C3-A's volatility mean reversion, and it
must be stated in the strategy's reasoning output rather than left implicit.

### Setup conditions

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | Price within `c·ATR_1h` of a defined boundary | `STR_DIST_PDH`, `STR_SWING` | Proximity |
| 2 | Boundary is significant: ≥ 2 prior touches respected | `STR_SWING` | Not an arbitrary line. Touch count fixed at 2 — structural |
| 3 | `VOL_RATIO` **not** in the top decile | `VOL_RATIO` | In a volatility blowout, levels do not hold and the premise fails |
| 4 | Not already breached and reclaimed ≥ 2 times in the current approach | `STR_SWING` | The level is not working; chop voids the setup |
| 5 | DQ gates pass | Family 10 | |

### Resolution — the direction decision

The setup arms *pending*. Direction is assigned by which resolution occurs:

- **ACCEPTANCE** — a 15m close beyond the boundary, then a second 15m close that does not
  reclaim it, with `VLM_ZROBUST` above median. → **Trade with the break.**
- **REJECTION** — a 15m close beyond the boundary, then a close back inside within
  `N_reclaim = 2` bars (fixed, structural). → **Trade against the break.**

The reclaim window is deliberately short. A long window would let almost any pullback qualify
as a rejection, which is how this pattern becomes unfalsifiable.

### Entry, stop, target

| | Acceptance | Rejection |
|---|---|---|
| **Entry** | The second confirmed 15m close beyond | ACT on the reclaim: `X*` = extreme of the failed break, `E(t) = X* ∓ δ·ATR_15m` |
| **Stop** | Back inside the boundary by `s·ATR_1h` | Beyond the failed extreme by `s·ATR_1h` — tight, and the source of the asymmetry |
| **T1** | Next structural level or measured move | The opposite side of the range, or `SMA_N` — wide |
| **Remainder** | Trailed at `s·ATR_1h` | Trailed at `s·ATR_1h` |

**The rejection case carries the better geometry** — a tight stop just beyond a failed extreme
against a target spanning the range — and will usually be the branch that clears the `k ≥ 3`
gate. Acceptance frequently will not, and should be allowed to fail the gate rather than
having its target stretched to qualify.

### Dominant failure mode, stated

**Whipsaw.** Acceptance and rejection are genuinely ambiguous in real time, and in choppy
conditions a level will be breached and reclaimed repeatedly. Condition 4 is the defence and
**it will not be sufficient.** Of all strategies in this system, C3-C is the most likely to
underperform its backtest, because whipsaw is exactly the behaviour that historical bars
under-represent relative to live execution. Weight this accordingly in Phase 9 and treat any
strong backtest result here with more suspicion than elsewhere.

---

## 6. Partitions and conflicts

Enforced by construction:

| Boundary | Separating condition |
|---|---|
| C3-B ⊕ C2-A | `\|TRD_TSTAT\| < t_min` vs `≥ t_min`; pullback `[0,1.0)` vs `[1.0,3.0]` |
| C3-A ⊕ C1-B | C3-A requires low `VOL_RATIO` and is directionless; C1-B requires a tail deviation with `MR_VR < 1` |
| C3-C rejection ⊕ C1-B | Different anchors: a structural boundary versus distance from a trailing mean. **Overlap is possible** — see below |

**C3-C rejection and C1-B can both fire in the same direction.** This is not a conflict (same
direction, different reasoning) but it **is a concentration risk**: two strategies producing
one economic position. The Phase 7 arbitration layer must treat same-direction multi-strategy
signals on the same asset as a **single position with combined strength**, never as two
independent entries. Flag this explicitly to Phase 7.

Opposing-direction firings across any classes remain governed by the `CLASS_CONFLICT`
suppression rule in `specs/04` §7.

---

## 7. Traceability — C3-A

| Stage | Content |
|---|---|
| **Raw** | 1h / 15m / 1m OHLCV; instrument metadata; hour-of-day volatility profile (Tier A, precomputed) |
| **Features** | `VOL_RATIO(1h)`, `VOL_OF_VOL(1h)`, `ATR(1h)` short and long, contraction high/low, `STR_SWING` |
| **Conditions** | The seven of §3, each with pass/fail and the deciding value |
| **Derived payoff** | `expected_payoff_ratio = ATR_long_1h / contraction_range`, computed at setup |
| **Strength** | §9 — contraction depth and payoff ratio; session timing as a minor contributor |
| **Confidence** | `null`. **`breakeven_hit_rate` published instead** — see §2.1 |
| **Entry** | `entry_triggers = {long: contraction_high, short: contraction_low}`, both live |
| **Stop** | The opposite boundary, rounded away from entry to `price_increment` |
| **Targets** | T1 = `entry ± ATR_long_1h`; remainder trailed at `s·ATR_1h` |
| **Risk** | `R`, `k = W/R`, `c` from live `taker_fee`, and `p* = (1 + c/R)/(k + 1)` |
| **Signal** | Emitted only if `k ≥ 3.0` and the net-edge gate passes; else `NO VALID TRADE` naming the gate |

Audit requirements as `specs/03` §6, plus: the full contraction window with its boundary
prices, the percentile rank of `VOL_RATIO` at arming, and — for C3-C — the complete
acceptance/rejection decision trace including every boundary touch.

---

## 8. Signal object — additions to the `specs/03` §9 schema

```
risk_classification: "elevated",       # required for all Class 3
breakeven_hit_rate,                    # p* — REQUIRED, see §2.1
entry_triggers: {long, short} | null,  # C3-A: directionless bracket
expected_payoff_ratio,                 # C3-A: derived at setup
resolution_mode: "pending" | "acceptance" | "rejection",   # C3-C
regime_persistence_prob,               # C3-B — labelled regime statistic, NOT a win rate
impulse_origin,                        # C3-B — hard stop floor
handoff_to: null | "C2-A",             # C3-B on maturation
boundary: { level, touch_count, type } # C3-C
```

`breakeven_hit_rate` is the field that makes this class honest. `confidence` stays `null`
system-wide until Phase 16.

---

## 9. Signal strength

As `specs/03` §7 — at most two near-orthogonal quantities, gates never contribute:

- **C3-A:** contraction depth (percentile of `VOL_RATIO`) and `expected_payoff_ratio`. Session
  timing may contribute as a minor third term but never to the gate.
- **C3-B:** impulse magnitude percentile and transition freshness (decaying in `N_fresh`).
- **C3-C:** boundary significance (touch count, capped) and the clarity of the resolution —
  how decisively the reclaim or the hold occurred.

---

## 10. Parameters

Running total against the 25-parameter cap: **23 → 24.**

| # | Parameter | Used by | Band | Overfitting exposure |
|---|---|---|---|---|
| 25 | `q_contract` — contraction percentile threshold | C3-A | 10th–25th | **Medium.** Percentile-based, so it adapts to each asset's own distribution rather than encoding an absolute volatility level. |

**Reused:** `δ` (ACT), `s` (stop and trail), `c` (structural proximity), `t_min` (C3-B
partition).

**Made structural instead of fitted:**

- `k ≥ 3.0` R:R floor — **derived from the breakeven formula** by choosing the worst survivable
  hit rate. Not an optimisation target.
- 15m-close trigger confirmation — a confirmation rule, not an ATR-multiple parameter.
- C3-B pullback band `[0, 1.0)` — derived as the complement of C2-A's existing band.
- `N_reclaim = 2` bars — minimal confirmation; a longer window makes the rejection pattern
  unfalsifiable.
- Boundary touch count `= 2` — minimum for a level to have been "respected" at all.
- T1 targets — all derived (`ATR_long`, measured move, opposite range boundary).

### ⚠ The cap will need to be raised at Phase 8, deliberately

**The budget is now full at 25/25.** Class 4 and the risk engine remain.

Class 4 is "established methods," which should overwhelmingly reuse `δ`, `s`, `t_min`, `z_dev`
and the shared lookbacks rather than introduce new machinery — that is a reasonable expectation,
not wishful thinking.

The risk engine genuinely will need its own parameters: per-trade risk budget, the Class 3
multiplier of §2.3, concurrent-exposure caps, correlation haircut strength, and veto thresholds.
**Expect to raise the cap to roughly 30 at Phase 8.**

That raise must be an explicit, documented decision with each new parameter justified
individually — not a quiet drift past the limit. The distinction matters: a cap raised on
purpose is a governance decision; a cap ignored is the start of a sixty-parameter system.

---

## 11. Expected frequency — diagnostic, not prediction

Design intent: **4–10 Class 3 signals per asset per month.** Contractions and transitions recur
more often than Class 1 dislocations but less often than Class 2 pullbacks.

- Materially more → `q_contract` is loose, or C3-C's condition 4 is not suppressing chop.
- Near zero → check the distribution of `VOL_RATIO` and the regime transition counts **before**
  touching a threshold.
- **A high hit rate in backtest is a warning sign, not a success.** This class is designed for
  a ~30% hit rate with a large payoff ratio. If backtesting shows 60%+, the most likely
  explanations are look-ahead bias, survivorship in the boundary definitions, or a stop too
  wide relative to the quoted `R`. Investigate before celebrating.

---

## 12. Acceptance criteria for Phase 5

1. `breakeven_hit_rate` is computed and published on every Class 3 signal, using live fees for
   `c`. Unit-tested against hand-computed values from §2.1.
2. The `k ≥ 3.0` gate is enforced at setup, and setups that fail it are logged as
   `NO VALID TRADE` with the gate named — not silently dropped.
3. C3-A's directionless bracket is implemented with both triggers live and published; direction
   is assigned only at trigger.
4. ACT is imported, not reimplemented — third consumer, still one implementation.
5. Partitions asserted in tests: C3-B ⊕ C2-A on both `t_min` and pullback band.
6. `HANDOFF C3-B → C2-A` transfers the position to C2-A management and is logged as success,
   not as an exit.
7. C3-C's acceptance/rejection decision trace is fully recorded, including rejected touches.
8. The same-direction concentration case (§6) is flagged to Phase 7 in `docs/findings.md`.
9. `regime_persistence_prob` is labelled as a regime statistic everywhere it appears and is
   never presented as a trade probability.
10. No `confidence` value is published anywhere.
