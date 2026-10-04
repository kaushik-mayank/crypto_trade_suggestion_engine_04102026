# 04 — Strategy Class 2: Trend Persistence & Risk-Normalized Participation

**Depends on:** `specs/00`, `specs/02`, `specs/03` (reuses the ACT primitive unchanged).
**Design intent:** moderate frequency, high robustness. Where Class 1 fades dislocations,
Class 2 participates in them.

---

## 1. Class identity, and why it is not Class 1 renamed

Class 1 asks: *has price deviated from fair value, and is there a force restoring it?*
Class 2 asks: *is there persistent risk-adjusted drift, and what is the best-priced way to
join it?*

These produce **opposite trades from the same raw observation**. Both classes notice "price
pulled back." C1-B buys that pullback expecting reversion to the mean. C2-A buys it expecting
resumption of the trend. They are separated by a single measurement — the variance ratio —
and are **mutually exclusive by construction**:

| | C1-B | C2-A |
|---|---|---|
| Gate | `MR_VR < 1` | `MR_VR > 1` |
| Thesis | Deviation closes | Drift resumes |
| Target | The mean | Prior extreme, then trail |
| Stop | Below the dislocation extreme | Below **structural** invalidation |

That is the test of whether these are genuinely different classes rather than four names for
one idea: **they can never both fire on the same asset and timeframe.** The same partition is
enforced between C1-A and C2-C on funding extremity, and between C1-C and C2-B on relative
strength (see §7 — this requires an amendment to `specs/03`).

### What makes this "institutional" rather than retail trend-following

Four specific things, none of them cosmetic:

1. **The trend measure is risk-adjusted.** `TRD_TSTAT` = β/SE(β) answers "is this drift
   distinguishable from noise." A price-versus-moving-average comparison does not, and will
   report a strong trend in a market that is merely volatile.
2. **Entry is on pullback, not on strength.** Buying strength pays the worst available price
   and puts the stop furthest away, mechanically destroying R:R. Desks accumulate into
   weakness within an established trend.
3. **Exposure is volatility-normalized**, so a SOL position and a BTC position represent the
   same risk. This is where much of the real institutional edge lives — less in the signal
   than in the risk normalization.
4. **Three correlated assets are treated as one factor exposure**, not three ideas (C2-B).

---

## 2. Strategies

| ID | Decision unit | Mechanism | Tier | Backtestable at launch |
|---|---|---|---|---|
| **C2-A** | The asset's own trend | Time-series momentum, pullback entry | A | ✅ |
| **C2-B** | The market factor | Factor direction + best expression | A | ✅ |
| **C2-C** | Composition of participation | Trend backed by size, not count | B | ❌ |

---

## 3. C2-A — Trend Continuation on Volatility-Normalized Pullback

### Hypothesis

In regimes with measurable positive serial dependence, drift persists. The best-priced entry
within such a trend is after a pullback that has **not** broken the trend's structure.

### Why it fails, and when

Trends end, and the last pullback in a trend looks identical to every prior one until it
isn't. There is no way to distinguish them ex ante — this is an irreducible property of the
strategy, not a gap in the design. The defence is not better filtering; it is **structural
stop placement and a trailing exit** so that the losing case is small and bounded while the
winning case is left open. Any attempt to "improve" the entry filter to avoid the last
pullback is curve-fitting to a specific history.

### Setup conditions — ALL must hold

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | `\|TRD_TSTAT(1h)\| ≥ t_min` | `TRD_TSTAT` | Drift is statistically distinguishable from noise |
| 2 | `sign(TRD_TSTAT(1d)) == sign(TRD_TSTAT(1h))` | `TRD_TSTAT` | Higher timeframe agrees on direction |
| 3 | `MR_VR(1h) > 1` for a majority of `k ∈ {2,4,8}` | `MR_VR` | **The mirror of C1-B's gate.** Persistence is measurably operative |
| 4 | `TRD_ER(1h) ≥ er_min` **(conditional — see §8)** | `TRD_ER` | The path is direct, not a drift through chop |
| 5 | Pullback depth: `\|DEV_ATR(1h)\|` ∈ `[1.0, 3.0]` ATR **against** the trend | `DEV_ATR` | Deep enough to be real, shallow enough not to be a break |
| 6 | Structure intact: `STR_SEQ` still HH-HL (up) / LH-LL (down); the pullback has **not** taken out the last confirmed swing | `STR_SEQ`, `STR_SWING` | The trend is still the trend |
| 7 | Regime cell stable ≥ its median dwell time | regime model | Not a one-bar classification |
| 8 | All `DQ_*` hard gates pass | Family 10 | |

**Direction:** `sign(TRD_TSTAT(1h))`.

**On condition 5's band:** `[1.0, 3.0]` ATR is **fixed by reasoning, not fitted.** Below 1 ATR
a pullback is indistinguishable from bar noise; beyond 3 ATR the structural premise is under
threat and condition 6 will usually fail anyway. These are structural constants in the sense
of `specs/02` §4 and consume no parameter budget. **Do not tune them.**

### Entry — ACT, reused unchanged

Import the primitive from `specs/03` §2. **Do not reimplement it.** For a LONG in an uptrend
the pullback moves down, so `X*(t)` is the running low since the pullback began and
`E(t) = X*(t) + δ·ATR_15m`, ratcheting down, recomputed every minute.

Same `δ`. Same confirmation rule (1m close). Same `SETUP_DETECTED` → `ENTER_NOW` transition.

### Stop — structural, not extremal

**This is a deliberate divergence from Class 1.** Class 1 stops below the dislocation extreme,
because the dislocation *is* the thesis. Class 2 stops below **structural invalidation**,
because the structure is the thesis:

```
LONG:   stop = last_confirmed_swing_low  − s·ATR_1h
SHORT:  stop = last_confirmed_swing_high + s·ATR_1h
```

If that stop produces an `R` larger than the position sizing allows at target risk, **there is
no trade.** Do not tighten the stop to make the trade fit — that severs the stop from its
meaning and converts a structural exit into an arbitrary one.

### Targets — partial, then trail

Trends are fat-tailed. Capping the winner at a fixed multiple is the single most common way to
destroy a trend strategy's expectancy, because the distribution's whole value sits in the tail.

- **T1** — the prior swing extreme in the trend direction. Structural, measurable, publishable,
  and the basis for the quoted R:R. Take partial here.
- **Remainder — trailed**, not targeted:
  ```
  LONG:  trail = max(high since entry) − s·ATR_1h
  ```
  Reusing `s`, the same buffer as the initial stop. Monotone: the trail never loosens.
- **T2 is deliberately not published.** A number there would be fiction. The UI shows "trailing"
  with the live trail level, which is a real, updating, actionable figure.

### Invalidation — three forms

- **Structural:** a 1h close beyond the last confirmed swing against the position.
- **Mechanism:** `MR_VR(1h)` falls below 1 — measured persistence has ended, so the thesis is
  void independent of price.
- **Trend decay:** `|TRD_TSTAT(1h)|` falls below `t_min × 0.5` — the drift is no longer
  distinguishable from noise.

**Setup expiry:** if ACT does not trigger within `N_exp` 1h bars, expire. A pullback that has
not arrested within its expected window is becoming a reversal, and the setup that was detected
no longer exists.

---

## 4. C2-B — Market-Factor Trend with Best-Expression Selection

### Hypothesis

BTC, ETH and SOL share a dominant common factor (`X_CORR` typically 0.7–0.9). Most of their
variance is one thing. Therefore the first decision is the **factor's direction**, and the
second is which instrument expresses it best — not three independent decisions.

### Why this is the institutional move

Taking long BTC, long ETH and long SOL simultaneously is not diversification; it is one
position at 3× size with a diversification story attached. A desk recognises this and asks a
different question: *given I want factor exposure, what is the best-priced expression of it?*

### The trap in expression selection, and the fix

A naive expression score always selects the highest-beta asset, because high beta looks like
"more trend." **That is not a better expression; it is the same exposure with more leverage**,
and leverage is a sizing decision, not a selection one.

The score is therefore beta-normalized:

```
expression_score(asset) = ( TRD_TSTAT(asset,1h) + z(X_RS(asset)) ) / X_BETA(asset)
```

Division by beta, with weight fixed at 1 — **structural, not fitted, zero parameter cost.**
An asset earns selection by trending *more than its beta explains*, not by moving more.

### Setup conditions — ALL must hold

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | `\|TRD_TSTAT(BTC,1d)\| ≥ t_min` and `sign` agrees with `TRD_TSTAT(BTC,1h)` | `TRD_TSTAT` | Factor direction is established |
| 2 | `MR_VR(BTC,1h) > 1` | `MR_VR` | Factor persistence is operative |
| 3 | `min X_CORR(asset, BTC) ≥ ρ_floor` over the medium window | `X_CORR` | The factor structure is intact — if correlations have broken, factor logic does not apply |
| 4 | `X_DISP` not in the extreme upper tail | `X_DISP` | Not an idiosyncratic-news regime where the factor is not driving |
| 5 | Selected asset satisfies C2-A conditions 5–8 (pullback, structure, dwell, DQ) | — | The expression is well-priced, not just well-chosen |

**Direction:** factor direction from BTC. **Asset:** `argmax(expression_score)`.

### Hard constraint

**At most one C2-B position at any time, across all three assets.** This is not a risk limit
bolted on afterwards — it is the strategy's entire premise. Enforce it in the strategy engine,
not only in the risk engine.

### Entry, stop, targets, invalidation

As C2-A, applied to the selected asset. Additional invalidations:

- **Factor flip:** `sign(TRD_TSTAT(BTC,1h))` reverses → exit regardless of the selected asset's
  own behaviour.
- **Correlation break:** `X_CORR(selected, BTC)` falls below `ρ_floor` → the expression logic
  is void; the position is no longer what it was opened as. Exit.

### The pre-registered test that decides whether this strategy survives

With only three assets, the selection layer has very little to choose between and may be
dominated by noise.

> **Test:** does C2-B's realized performance beat the same strategy run on BTC only?

If not, the selection layer adds nothing, and C2-B should be **reduced to BTC-only trend
participation** rather than kept for sophistication's sake. Register this test in Phase 9
before any backtest is run, and record the result whichever way it falls.

---

## 5. C2-C — Size-Backed Trend Participation · **Tier B** ⧗ PENDING P8

### Hypothesis

A trend financed by *moderate*, trend-aligned funding and backed by **notional size** is more
durable than one crowded by **account count**. `POS_DIVERGENCE` (`count_long% − value_long%`)
separates these two states, and no OHLCV feature can.

### Relationship to C1-A — hard partition

C1-A fades *extreme* funding against the crowd. C2-C follows *moderate* funding with the trend.
They must never fire together:

```
C1-A requires:  FND_MULT ≥ m_extreme
C2-C requires:  FND_MULT <  m_extreme
```

Same threshold, opposite sides. If both ever arm on the same asset, that is a **bug** and must
raise an alert, not a conflict-resolution decision.

### The lifecycle handoff

A trend that becomes progressively over-financed will cross `m_extreme` and transition from
C2-C (follow) to C1-A (fade). **This is coherent, not contradictory** — the market state genuinely
changed. Log it explicitly as `HANDOFF C2-C → C1-A` so it reads as designed behaviour in the
audit trail rather than as two strategies disagreeing.

### Setup conditions

| # | Condition | Purpose |
|---|---|---|
| 1 | C2-A conditions 1–3 and 5–8 hold | It is a genuine, well-priced trend pullback |
| 2 | `FND_MULT < m_extreme` | Hard partition from C1-A |
| 3 | `sign(FND_RATE)` aligned with trend direction | The trend is being financed normally |
| 4 | `POS_DIVERGENCE` **not** strongly skewed toward count in the trend direction | Participation is size-backed, not a crowd of small accounts |
| 5 | `\|POS_Z\|` not extreme | Positioning is not itself the dislocation — that is C1-A's domain |
| 6 | DQ gates pass; `data/stats` reachable | |

### Entry, stop, targets

As C2-A. No new mechanics.

### Additional mechanism invalidation

- `POS_DIVERGENCE` flips to count-crowded → the participation quality that justified the trade
  has deteriorated. Exit.
- `FND_MULT` crosses `m_extreme` → handoff, per above. Exit C2-C.

### Limitations

- **Tier B. `validated: false` at launch.** No positioning history exists.
- `POS_DIVERGENCE` measures **CoinDCX's own user base**, not the market. This is a fundamental
  proxy limitation that no amount of processing removes, and it must be stated in the signal's
  reasoning text, not buried in documentation.
- If P8 fails, **delete C2-C.** Do not substitute a proxy for positioning.

---

## 6. Risk normalization — the class's defining requirement

Every Class 2 suggestion sizes to a **volatility target**, not to a fixed notional or a fixed
percentage:

```
position_size ∝ target_risk / (ATR_1h × price)
```

so that a SOL position and a BTC position carry equal risk despite very different volatility.

The strategy layer computes and publishes the **inputs** — `ATR_1h`, `VOL_CC`, `X_CORR`, and
`R` in price terms. The Phase 8 risk engine owns the sizing decision and the correlation
haircut. Class 2 must not implement its own sizing; it must expose what sizing needs.

**The correlation point restated, because it is the one most often ignored:** at
`X_CORR ≈ 0.85`, three simultaneous Class 2 longs are approximately one position at 2.6× size.
Phase 8 must apply a correlation haircut across concurrent Class 2 exposure. C2-B partially
pre-empts this by construction; C2-A and C2-C do not.

---

## 7. ⚠ Amendment required to `specs/03`

C1-C (cross-asset beta dislocation, fade) and C2-B (factor trend, follow) can currently both
fire on the same residual in opposite directions. That is a genuine specification gap, not an
edge case.

**Amendment: add to `specs/03` §5, C1-C setup conditions:**

> **Condition 7:** `MR_VR(1h) < 1` on the dislocated asset.

This restores the same mutual-exclusivity partition that separates C1-B from C2-A. Apply the
amendment to `specs/03` and note it in `docs/findings.md`.

**Residual arbitration rule:** if any Class 1 and Class 2 strategy nonetheless fire on the same
asset in opposing directions, **suppress both** and log a `CLASS_CONFLICT` event with the full
condition set of each. Opposing evidence of comparable strength means the honest answer is no
trade. Phase 7 formalises arbitration; this is the interim rule and it is deliberately
conservative.

---

## 8. Parameters — and what the budget forced

Running total against the 25-parameter cap: **22 → 23** (possibly 24).

| # | Parameter | Used by | Band | Overfitting exposure |
|---|---|---|---|---|
| 23 | `t_min` — trend t-statistic threshold | C2-A, C2-B, C2-C | 1.5–3.0 | **Medium.** Has a natural interpretation as a significance level, which bounds the plausible range. Calibrate on training data only. |
| 24 | `er_min` — efficiency ratio floor | C2-A cond. 4 | 0.3–0.6 | **Conditional — register only if the `specs/02` §7 redundancy protocol retains both `TRD_TSTAT` and `TRD_ER`.** They are an expected cluster. If collapsed, **delete condition 4** rather than keeping a redundant gate. |

**Reused, costing nothing:** `δ` (ACT trigger), `s` (stop buffer and trailing multiple),
`m_extreme` (funding partition), `ρ_floor` (correlation floor).

**Made structural rather than fitted, under budget pressure:**

- Pullback band `[1.0, 3.0]` ATR — fixed by reasoning about noise and structure.
- Trailing stop multiple — reuses `s` instead of introducing `m_trail`.
- Expression-score beta weight — fixed at 1 (pure beta normalization).
- Trend-decay floor — fixed at `t_min × 0.5`, derived rather than independent.

**This is the budget working as intended.** Four quantities that would ordinarily have become
tunable knobs were instead forced into structural justification. Each is now something that can
be argued about from first principles rather than optimized against a return series. If the cap
had been 60 instead of 25, all four would have been fitted, and the system would have been
measurably more overfit for no gain in expressiveness.

One or two slots remain for Phases 5–8, which will require the same discipline.

---

## 9. Traceability — C2-A

| Stage | Content |
|---|---|
| **Raw** | 1h / 15m / 1m OHLCV, 1d OHLCV; instrument metadata (ATR scale, tick, fees) |
| **Features** | `TRD_TSTAT(1h,1d)`, `MR_VR(1h)`, `TRD_ER(1h)`, `DEV_ATR(1h)`, `STR_SWING(1h)`, `STR_SEQ(1h)`, `ATR(1h,15m)`, regime cell + dwell |
| **Conditions** | The eight of §3, each with pass/fail and the deciding value |
| **Strength** | §10 — trend significance and pullback quality only |
| **Confidence** | `null` — see `specs/03` §8. Unchanged. |
| **Entry** | `E(t) = X*(t) + δ·ATR_15m`, recomputed every 1m, ratcheting |
| **Stop** | `last_swing_low − s·ATR_1h`, rounded **away** from entry to `price_increment` |
| **Targets** | T1 = prior swing extreme; remainder trailed at `max(high since entry) − s·ATR_1h` |
| **Risk** | `R = entry − stop`; `R:R` quoted against T1 only; net-of-cost edge from live `taker_fee` |
| **Signal** | Emitted only if R:R and net-edge gates pass; else `NO VALID TRADE` naming the failing gate |

Audit record requirements are as `specs/03` §6, plus: the expression score for all three assets
when C2-B fires (so the rejected alternatives are recorded, not just the winner), and the live
trail level on every update once in position.

---

## 10. Signal strength

As `specs/03` §7 — monotone in at most two near-orthogonal quantities, and setup conditions are
**gates, not score contributors**:

- **Trend significance** — percentile rank of `|TRD_TSTAT(1h)|` against its trailing distribution.
- **Pullback quality** — where the pullback sits within the `[1.0, 3.0]` ATR band, favouring the
  middle; a 1.1 ATR dip is barely a pullback and a 2.9 ATR dip is nearly a break.

**Confidence remains `null` at launch**, for the reason given in `specs/03` §8. Class 2 being
backtestable does not change this — it changes when the field can be populated (after Phase 16
calibration), not whether it may be invented before then.

---

## 11. Signal object — additions to the `specs/03` §9 schema

```
targets: [T1],                      # T2 absent by design for Class 2
exit_mode: "partial_then_trail",
trail_level,                        # live, updating, published
trail_basis: "max_high_since_entry - s*ATR_1h",
factor_direction,                   # C2-B only
expression_scores: {asset: score},  # C2-B only — all three, including rejected
sizing_inputs: { atr_1h, vol_cc, x_corr },
handoff_from: null | "C2-C"         # set on a C2-C → C1-A transition
```

`trail_level` is the Class 2 analogue of `live_entry_trigger`: a real, updating number the user
can act on, in place of a fabricated second target.

---

## 12. Expected frequency — diagnostic, not prediction

Design intent: **6–15 Class 2 signals per asset per month**, higher than Class 1 because trends
persist and pullbacks recur within them.

- Materially more → `t_min` is too low, or condition 6 is not actually enforcing structure.
- Near zero across a full backtest → check the distribution of `MR_VR(1h)` **before** touching
  a threshold. If persistence regimes are genuinely rare in this market, that is a finding about
  crypto at this horizon, and Class 2 should be cut rather than loosened into existence.
- **If C2-A and C1-B ever fire simultaneously on the same asset and timeframe, that is a bug** —
  their VR gates are complementary. Assert this in tests.

---

## 13. Acceptance criteria for Phase 4

1. ACT is **imported** from the Phase 3 implementation. A second copy anywhere is a defect.
2. C2-A and C2-B are implemented and backtestable on the Phase 1 backfill. C2-C is implemented
   behind the P8 gate and emits `validated: false`.
3. Mutual exclusivity is asserted in tests: C1-B ⊕ C2-A on VR, C1-A ⊕ C2-C on `m_extreme`,
   C1-C ⊕ C2-B after the §7 amendment.
4. The `specs/03` amendment in §7 has been applied and recorded in `docs/findings.md`.
5. C2-B enforces at most one concurrent position across all three assets, in the strategy engine.
6. Trailing exits are monotone — the trail never loosens — with a unit test proving it.
7. `er_min` is registered **only if** the redundancy protocol retained both `TRD_TSTAT` and
   `TRD_ER`; otherwise condition 4 is deleted, and the decision is recorded.
8. The C2-B-versus-BTC-only test (§4) is registered in the Phase 9 protocol before any backtest.
9. No probability or confidence value is published anywhere.
