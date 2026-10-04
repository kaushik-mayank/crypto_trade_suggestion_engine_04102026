# 08 — Risk Engine & Position Sizing

**Depends on:** `specs/00` (verified instrument metadata), `specs/02` (volatility, correlation),
`specs/03`–`specs/06` (strategy levels), `specs/07` (arbitration output).
**Position in the pipeline:** last stage before the UI. Nothing reaches a user without passing
every gate here.

---

## 1. Prime directive

> **The risk engine vetoes. It never improves a signal to make it pass.**

It may not widen a target, tighten a stop, shift an entry, or relax a gate. A signal either
clears every check as the strategy constructed it, or it is rejected with the failing check
named.

This is the discipline that keeps strategy logic honest. The moment the risk engine is allowed
to adjust levels, every published number stops corresponding to the reasoning that produced it,
and the audit trail becomes fiction.

**Corollary:** the backtest and the live system must call the **identical** cost and sizing code
path. A divergence there invalidates every validation result, silently.

---

## 2. Position sizing — two constraints, take the smaller

### 2.1 The catch that forces this

The obvious approach is stop-based sizing:

```
size_stop = (equity × risk_per_trade) / R          R = |entry − stop| in price
```

This is correct for most strategies, because their stops are ATR-scaled and therefore already
volatility-normalized. **It fails badly for C3-A.**

C3-A's stop is the opposite boundary of a *volatility contraction* — narrow by definition of the
setup. Stop-based sizing would therefore produce an enormous position **immediately before a
volatility expansion**. The most dangerous moment in the strategy's life would receive the
largest size. That is precisely backwards, and it is not an edge case — it is the strategy
working as designed.

### 2.2 The fix

Size is the **minimum** of stop-based and volatility-target sizing:

```
size_stop = (equity × risk_per_trade) / R
size_vol  = (equity × per_position_vol_budget) / (price × σ_forward)

size = min(size_stop, size_vol) × class_multiplier × validation_multiplier
```

**`σ_forward` must be the volatility expected during the hold, not the volatility observed
now.** For C3-A that means `ATR_long_1h` (the un-contracted norm the setup expects to return to),
not the contracted ATR. Using the contracted value would reproduce the exact bug this rule
exists to prevent.

For every other strategy `σ_forward` is `VOL_CC` over the medium lookback, and `size_vol` will
usually be non-binding.

### 2.3 Equity is never read from the exchange

Sizing is expressed as a **fraction of account equity**. The system does not call
`users/balances` or `futures/wallets`, and the API key retains no permission to reach them
(`specs/00` §10). The user may enter their equity in local config for display convenience only;
it never leaves the machine and is never required.

This preserves the least-privilege conclusion from Phase 0 — the key exists for `data/stats` and
nothing else — and it keeps a suggestion system from needing to know the user's balance in order
to suggest anything.

---

## 3. Class and validation multipliers

```
class_multiplier:        Classes 1, 2, 4 → 1.0
                         Class 3         → class3_multiplier
validation_multiplier:   validated (Phase 16 passed) → 1.0
                         unvalidated / Tier B        → unvalidated_multiplier
```

They compound. C1-A (Tier B, Class 1) carries `1.0 × unvalidated_multiplier`.

**`class3_multiplier` is an honest judgment, not a derivation — and here is the derivation it
will be replaced by.** Class 3 targets a ~30% hit rate at `k ≥ 3`; Classes 1/2/4 target roughly
50% at `k ≥ 1.8`. Per-trade outcome variance scales as `p(1−p)(k+1)²`, giving ≈3.36 versus
≈1.56, so equalising risk contribution implies a multiplier near `1/√(3.36/1.56) ≈ 0.68`.

That derivation depends on assumed hit rates we do not have. So the initial value is set
**conservatively below** the derived figure, and **recalibrated using this exact formula with
measured hit rates after Phase 16.** Record the recalibration as a decision when it happens.

**`validation_multiplier` needs no derivation.** A strategy that cannot be backtested carries
model risk that a backtested one does not, and the correct response to unquantified model risk
is less capital.

---

## 4. Correlation-adjusted portfolio sizing

### 4.1 The problem, quantified

At `X_CORR ≈ 0.85`, three concurrent same-direction positions across BTC/ETH/SOL are one
position at roughly 2.6× size. Treating them as three independent trades is the largest
unmanaged risk in the system.

### 4.2 The derived constraint

For positions with **signed** weights `wᵢ` (positive long, negative short), volatilities `σᵢ`
and the live correlation matrix `ρᵢⱼ` from `X_CORR`:

```
σ_portfolio² = Σᵢ Σⱼ wᵢ wⱼ ρᵢⱼ σᵢ σⱼ
```

**Constraint:** `σ_portfolio ≤ portfolio_vol_target`.

Signed weights matter: a long BTC against a short ETH at ρ = 0.85 is **hedged**, not
concentrated, and the general quadratic form handles that correctly where a simple position
count would not.

For the equal-weight same-direction case this reduces to a transparent per-position cap:

```
per-position size ∝ 1 / √( n + n(n−1)ρ̄ )
```

At n = 3, ρ̄ = 0.85 that is `1/√8.1 = 1/2.85`, versus `1/√3 = 1/1.73` for uncorrelated
positions — **a factor of 1.64 haircut, derived rather than chosen.** No separate "correlation
haircut" parameter exists, because the volatility target and the measured correlation matrix
together produce it.

### 4.3 Allocation order

When the constraint binds, cut from the bottom of Phase 7's within-class ranking. `specs/07` §6
guarantees the ranking exists and is conflict-resolved, so cutting is principled rather than
arbitrary.

### 4.4 Known failure mode

Rolling correlation spikes toward 1 in stress — exactly when the haircut matters most and when
the estimate is least stable. Use the **longer** of the available correlation windows for
sizing, and flag any period where short-window and long-window correlation diverge materially.
This does not solve the problem; nothing does. It makes it visible.

---

## 5. Leverage — the rule most systems get backwards

> **Leverage does not increase position size. Size is set by risk. Leverage only determines how
> much margin is posted and how close liquidation sits.**

Once `size` is fixed by §2, raising leverage adds no exposure and no expected return. It
subtracts margin and moves the liquidation price closer. The only thing higher leverage buys is
a worse tail.

**Therefore: recommend the *minimum* leverage that satisfies `min_notional` and the margin
requirement — never the maximum that is "safe."**

Both numbers are published so the user can see the distinction:

- `recommended_leverage` — the minimum that works. This is the actionable one.
- `max_safe_leverage` — the ceiling from §6, shown so the user can tell how much headroom their
  own choice consumes.

---

## 6. Liquidation distance

Using the verified `dynamic_safety_margin_details` tier table (`specs/00` §2 — BTC returns
`{20000: 0.5, ...}`, i.e. 0.5% maintenance margin at small notional):

```
LONG:   liq_price ≈ E × (1 − 1/L + mm)
SHORT:  liq_price ≈ E × (1 + 1/L − mm)
```

with `mm` read from the tier matching the position's notional — **never hardcoded**, and
re-read when notional crosses a tier boundary.

**Requirement:** liquidation must sit at least `liq_safety × R` beyond the stop. Solving for the
leverage ceiling:

```
max_safe_leverage  =  1 / ( mm + liq_safety × R / E )
```

**Why a buffer beyond the stop at all:** a stop is not a guarantee. Price gaps through stops, and
a gap that reaches liquidation converts a planned `1R` loss into a total loss of posted margin.
`liq_safety` exists to survive that gap.

**`liq_safety` becomes data-derived after Phase 1.** Set it initially by judgment, then replace
it with a measured value: the distribution of maximum adverse excursion beyond a level within a
single bar is computable from backfilled 1m data, and `liq_safety` should place liquidation
beyond its 99th percentile. Record the replacement as a decision.

Isolated margin uses the per-position figure. Cross margin aggregates maintenance margin across
positions, so the check runs at portfolio level and a new position may be vetoed for bringing an
*existing* position too close to liquidation.

---

## 7. Cost model

### 7.1 Components — all three, always

```
c_price = entry_cost + exit_cost + slippage + expected_funding     (expressed in price units)
```

| Component | Source |
|---|---|
| Fees | Live `maker_fee` / `taker_fee` per instrument. **Never hardcoded** — the doc's samples are wrong for BTC (`specs/00` §2). Assume **taker on both legs** unless the strategy explicitly rests a limit order. |
| Slippage | Derived from probe P14 (spread and depth). Use the **75th percentile** of measured effective cost, not the mean — a conservative estimator by construction, so no separate safety multiplier parameter is needed. |
| Funding | `fr × ⌈expected_hold_hours / 8⌉ × notional × sign`. Signed: paid against the position, received in favour of it. |

Spot uses spot fees (the documented `fee: 0.2` sample implies roughly 0.4% round trip,
`[UNVERIFIED]` — confirm per-market before trusting it).

### 7.2 One gate, not two: cost-adjusted R:R

All risk/reward gates apply to the **cost-adjusted** figure:

```
k_net = ( |T1 − entry| − c_price ) / ( R + c_price )
```

Class gates run against `k_net`, never `k_gross`:

| Class | Floor on `k_net` | Origin |
|---|---|---|
| 3 | **3.0** | Derived in `specs/05` §2.2 — survives a ~27.5% hit rate |
| 1, 2, 4 | **1.8** | Derived here by the same method — survives a ~40% hit rate at realistic costs |

Both floors come from the breakeven identity `p* = (1 + c/R)/(k + 1)` by choosing the worst hit
rate the class should survive. **Neither is a fitted parameter**, and they are consistent with
the `breakeven_hit_rate` already published on every signal (`specs/07` §2).

### 7.3 Funding as a veto, not only a cost

Baseline funding over a two-day hold is roughly 0.06% — meaningful against a 0.118% round trip
but not decisive. At the verified ±2% cap it is **12% over two days**, which no swing target
survives.

> **Veto:** if `FND_MULT` is extreme **against** the position and the strategy is not C1-A
> (whose entire thesis is that extremity), reject.

A Class 2 long into capped positive funding bleeds up to 6% per day. No trend signal justifies
that.

---

## 8. The veto ladder

Evaluated in order. **Every veto is logged individually with its inputs**, whether or not an
earlier one already fired — a signal that fails four checks is diagnostically different from one
that fails one, and short-circuiting hides that.

### Tier 1 — Data integrity (hard, binary)

| Veto | Condition |
|---|---|
| `DQ_STALE` | Any input source older than its freshness window |
| `DQ_SKW` | `skw != 0` on any futures row used |
| `DQ_SCHEMA` | A required field absent from a payload row (`specs/00` §4) |
| `DQ_DISAGREE` | Spot and futures prices diverge beyond the plausible bound |
| `DQ_META` | Instrument metadata unavailable → costs uncomputable → **no signal is possible** |
| `DQ_CLOCK` | Server/local skew beyond the P15-measured tolerance |

### Tier 2 — Economic viability

| Veto | Condition |
|---|---|
| `RR_FLOOR` | `k_net` below the class floor (§7.2) |
| `MIN_NOTIONAL` | Risk-correct size falls below `min_notional` (§9) |
| `TICK_DEGENERATE` | After tick rounding, stop rounds into or past entry, or `R` collapses below one tick |
| `PRICE_BAND` | Entry more than `multiplier_down/up` (**4.0% for BTC, verified**) from LTP — a resting limit entry there is rejected by the exchange |

### Tier 3 — Market conditions

| Veto | Condition |
|---|---|
| `SPREAD` | Half-spread exceeds 10% of `R` — structural, expressed as a fraction of the trade's own risk rather than an absolute bps figure, so it scales across assets and regimes |
| `DEPTH` | Cumulative depth within 25 bps insufficient for the suggested size |
| `VOL_EXTREME` | `VOL_RATIO` in the extreme top percentile — **applies only to strategies that do not declare themselves expansion-seeking.** Each strategy carries an `expansion_seeking` flag; C3-A sets it true. No special-casing in the risk engine. |
| `FUNDING_ADVERSE` | §7.3 |

### Tier 4 — Portfolio

| Veto | Condition |
|---|---|
| `PORTFOLIO_VOL` | §4 constraint would be breached |
| `CLASS3_CONCURRENT` | More than 2 concurrent Class 3 positions — structural sanity ceiling |
| `LIQ_DISTANCE` | §6 requirement unmet at any permissible leverage |
| `CROSS_MARGIN` | In cross margin, the new position brings an existing one inside its liquidation buffer |

---

## 9. `min_notional` and the small-account reality

BTC `min_notional` is **60 USDT (verified)**. If the risk-correct size implies a notional below
that:

> **Skip the trade. Never take a position larger than risk sizing permits in order to satisfy an
> exchange minimum.**

Taking it at `min_notional` means over-risking — silently, and by an amount that grows as the
account shrinks. That is the exact mechanism by which small accounts are destroyed.

Instead, publish what the constraint implies:

```
min_equity_for_this_trade = min_notional × R / (price × risk_per_trade × multipliers)
```

The UI shows: *"requires ≥ $X equity to size correctly — skipped."* This is honest, actionable,
and tells the user something true about their situation rather than quietly degrading their risk
management.

---

## 10. Tick and lot rounding

Using live `price_increment` (BTC: **0.1**) and `quantity_increment` (BTC: **0.001**):

- **Stops round *away* from entry.** Rounding a stop closer would tighten risk below what the
  strategy specified and make the published `R` a lie.
- **Targets round *toward* entry.** Conservative in the same direction.
- **Size rounds *down*** to `quantity_increment`.
- **Recompute `k_net` after rounding**, then re-apply the §7.2 gate. On low-priced assets or tight
  stops, rounding alone can drop a trade below its floor — and that trade must be rejected, not
  published with a pre-rounding R:R.

---

## 11. Parameters — the cap raise to 30, itemised

Running total: **25 → 30.** This is the deliberate, documented raise forecast in `specs/05` §10,
`specs/06` §8 and `specs/07` §12. Each is justified individually below; none is a convenience.

| # | Parameter | Why it cannot be derived | Band |
|---|---|---|---|
| 26 | `risk_per_trade` | A risk-appetite choice belonging to the user, not a property of the market. | 0.25%–1% of equity |
| 27 | `portfolio_vol_target` | Same — the aggregate risk appetite. Everything in §4 is derived *from* it. | annualised, user-set |
| 28 | `class3_multiplier` | Requires hit rates that do not yet exist. §3 specifies the exact derivation that replaces it post-Phase-16. | 0.4–0.7 |
| 29 | `unvalidated_multiplier` | A model-risk judgment. No data can produce it, since it exists precisely because data is absent. | 0.3–0.6 |
| 30 | `liq_safety` | Becomes data-derived once the adverse-gap distribution is measured (§6). Judgment until then. | 2–4 × R |

**Derived or made structural instead of registered — five quantities that would ordinarily be
knobs:**

- **Minimum R:R floors** (1.8 and 3.0) — derived from the breakeven identity, not fitted.
- **Correlation haircut** — falls out of `portfolio_vol_target` and the measured matrix. No
  separate parameter.
- **Slippage safety margin** — folded into the estimator by using the 75th percentile rather than
  the mean.
- **Spread veto** — expressed as 10% of `R`, so it scales with the trade instead of encoding an
  absolute bps figure.
- **Class 3 concurrent cap** = 2 — a structural sanity ceiling.

Note that **three of the five registered parameters carry a specified path to being replaced by
measurement** (28, 30) or are explicitly user preferences rather than model quantities (26, 27).
Only `unvalidated_multiplier` is a permanent judgment, and it exists because unquantified model
risk cannot, by definition, be quantified.

---

## 12. Signal object — additions

```
# sizing
size_fraction,                 # of equity, before user's own scaling
size_basis: "stop" | "vol_target",   # which constraint bound — diagnostic
class_multiplier, validation_multiplier,
sigma_forward,                 # the volatility used, not the one observed

# leverage & liquidation
recommended_leverage,          # the MINIMUM that works — the actionable number
max_safe_leverage,             # the ceiling, for context
liquidation_price, liq_distance_in_R,
maintenance_margin_rate, margin_tier,

# costs
cost_breakdown: { fees, slippage, funding, total_price_units },
k_gross, k_net,                # both, so cost drag is visible
expected_hold_hours, funding_intervals_crossed,

# portfolio
portfolio_vol_before, portfolio_vol_after,
effective_independent_positions,
correlation_window_used,       # long window per §4.4
correlation_divergence_flag,

# vetoes
vetoes: [ {tier, name, passed, inputs, value} ],   # ALL of them, not just the first failure
min_equity_for_this_trade,     # populated when MIN_NOTIONAL vetoes
```

`size_basis` is worth surfacing: when it reads `vol_target` on a C3-A signal, the §2.1 protection
is doing its job and that is visible rather than invisible.

---

## 13. Acceptance criteria for Phase 8

1. **The risk engine cannot modify any strategy-produced level.** Enforce structurally — levels
   arrive immutable. Assert in tests that entry, stop and targets are byte-identical before and
   after risk processing.
2. Sizing takes the **minimum** of stop-based and volatility-target sizing, with a specific test
   on a synthetic C3-A signal proving the volatility-target constraint binds and caps the size.
3. `σ_forward` for C3-A uses the expanded ATR, not the contracted one. Unit-tested directly —
   this is the subtlest bug in the phase.
4. Correlation sizing uses **signed** weights, with a test proving a long/short pair at high
   correlation is treated as hedged rather than concentrated.
5. Every veto is evaluated and logged, including those after the first failure. No
   short-circuiting.
6. `recommended_leverage` is the minimum that satisfies constraints, and a test asserts it is
   never set to `max_safe_leverage`.
7. All fees, tick sizes, increments, `min_notional`, `multiplier_up/down` and margin tiers are
   read live per instrument. **Any hardcoded value is a defect** — grep for numeric literals in
   the cost path as part of review.
8. `k_net` is recomputed after tick rounding and the class floor re-applied.
9. `MIN_NOTIONAL` vetoes publish `min_equity_for_this_trade` rather than silently dropping.
10. Backtest and live share one cost/sizing module. Assert by import graph — two code paths here
    would invalidate every Phase 16 result without any test failing.
11. The parameter registry contains exactly 30 entries, each with the justification from §11, and
    the raise from 25 is recorded as an explicit decision in `docs/findings.md`.
