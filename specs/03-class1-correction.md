# 03 — Strategy Class 1: Correction & Rebalancing

**Depends on:** `specs/00-capability-map.md`, `specs/02-features-and-regime.md`, Phase 1 probes.
**Design intent:** low frequency, high selectivity. This class is permitted — expected — to
return `NO VALID TRADE` for days at a time.

---

## 1. The governing principle

A correction strategy requires a **restoring mechanism**: an identifiable force that pushes
price back. Without one, "price deviated, therefore it will revert" is simply a bet against
momentum, and in crypto at a multi-day horizon that bet loses more often than it wins.

Every strategy in this class must name its restoring force and state the conditions under
which that force is absent. Three mechanisms survived scrutiny against the available data:

| ID | Mechanism | Type of force | Tier | Backtestable at launch |
|---|---|---|---|---|
| **C1-A** | Funding dislocation | **Economic** — a recurring, certain cash flow | B | ❌ |
| **C1-B** | Conditional statistical overextension | **Statistical** — measured serial dependence | A | ✅ |
| **C1-C** | Cross-asset beta dislocation | **Relative value** — flow between correlated assets | A | ✅ |

### ⚠ Funding and basis are the same signal

On a perpetual swap, funding is computed *from* the basis. `FND_RATE` and `BAS_BPS` are
therefore two measurements of one phenomenon, not two independent confirmations. **They must
never both contribute to a signal score.** C1-A uses funding as the primary observable and
basis only as a *consistency check* — if basis and funding disagree in sign, the data is
suspect and the setup is vetoed. This is a data-quality use, not a confirmation use.

---

## 2. Shared primitive: the Arrest-Confirmation Trigger (ACT)

All three strategies share one live-entry mechanism. One mechanism, one parameter, reused —
this is deliberate parameter economy, not laziness.

### Problem it solves

A correction setup identifies *that* something is dislocated. It says nothing about *when*
the dislocation stops widening. Entering on setup detection alone means catching a falling
knife. Entering on a fixed offset means an arbitrary number. ACT derives the entry level
from the market's own behaviour and updates it continuously.

### Definition

For a setup armed at `t₀` in direction LONG, over an observable series `X` (price for C1-A
and C1-B, the residual spread for C1-C):

```
X*(t) = min{ X(s) : t₀ ≤ s ≤ t }          running adverse extreme since arming
U(t)  = ATR on the trigger timeframe        (or rolling σ of the spread, for C1-C)
E(t)  = X*(t) + δ · U(t)                    the live entry trigger
ENTER_NOW  ⟺  X(t) ≥ E(t), confirmed on the close of a 1-minute bar
```

For SHORT, mirror: `X*(t) = max{...}`, `E(t) = X*(t) − δ·U(t)`, enter when `X(t) ≤ E(t)`.

### Properties that make this the right choice

1. **E(t) is monotone non-increasing for a LONG.** It ratchets down as new adverse extremes
   form. The system therefore never chases, and never triggers during a continuous decline —
   only after price has retraced `δ·ATR` off its extreme.
2. **Recomputed every minute**, satisfying the requirement that the live-entry condition be
   a live mathematical condition rather than a static range.
3. **Risk is set by the market, not by us.** The stop sits below `X*`, so `R` is determined
   by how far the dislocation actually extended before arresting. A violent overshoot produces
   a large `R`, which then fails the minimum-R:R gate and is rejected automatically. This is a
   quality filter that emerges from the mechanism rather than being bolted on.
4. **It cleanly separates the two states the system must distinguish:**
   - `SETUP_DETECTED` — armed, `E(t)` published and updating, `X(t)` has not crossed it.
   - `ENTER_NOW` — `X(t)` has crossed `E(t)` on a confirmed 1m close.

### Failure mode, stated

In a V-shaped reversal ACT enters late, giving up `δ·ATR` of the move. In a slow grinding
reversal it enters well. This is an accepted, deliberate trade: the cost is bounded and known,
whereas the cost of entering before arrest is unbounded. **Do not "improve" this by adding a
predictive early-entry path.** That reintroduces the exact risk the primitive exists to remove.

### Parameter

`δ` — one value, shared across all three strategies and available to later classes.
Calibrated once in Phase 16 against the pre-registered protocol. Sensible search band ~0.3–1.0
ATR. **Overfitting exposure: low** — it is a single shared parameter with a monotone,
interpretable effect (higher `δ` = later entry, fewer false triggers, worse fills). If the
backtest shows a sharp optimum rather than a broad plateau, that is evidence of overfitting
and the strategy should be distrusted, not the parameter re-tuned.

---

## 3. C1-A — Funding Dislocation Reversion

### Hypothesis

A perpetual swap has no expiry. Funding is the **only** mechanism tethering perp price to
spot. When funding reaches an extreme, the crowded side is paying a large, recurring,
*certain* cost to maintain its position, while the opposite side is *paid* to take the other
side. Both forces push price toward fair value.

### Why the force is real rather than assumed

The magnitudes are decisive. Baseline funding on these instruments is 0.005–0.01% per 8h
`[VERIFIED]`. The cap is ±2% per interval `[VERIFIED]` — **200 to 400× baseline**, and up to
6% per day. Even a moderate extreme of 0.1% per 8h is 0.3%/day, already material against a
0.118% round-trip cost. The carry cost is certain and compounds; the directional gain the
crowded side is hoping for is not. Expected value deteriorates mechanically for them.

### Why it fails, and when

Funding extremes frequently coincide with **genuine trends** — a real breakout attracts
leveraged longs, funding spikes, and price continues anyway. Fading strong funding inside a
strong trend is a classic way to be run over. The regime gate below is not decoration; it is
the difference between this strategy working and not.

### The no-history workaround

`FND_Z` requires accumulated history that does not exist. But **the ±2% cap is a structural
constant, not an estimated parameter**, and baseline funding is observable and stable.
Extremity is therefore anchored to *multiples of observed baseline*, not to a z-score:

```
FND_MULT = |fr| / baseline,   baseline = modal observed rate (≈0.0001)
```

This lets C1-A operate from day one in a deliberately conservative form, and tighten once
`FND_Z` becomes meaningful. Record which anchor was used on every signal.

### Setup conditions — ALL must hold

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | `FND_MULT ≥ m_extreme` (or `\|FND_Z\| ≥ z_f` once history allows) | `FND_RATE` | Dislocation exists |
| 2 | Extremity persisted across ≥2 consecutive observations | `FND_RATE` | Not a print spike |
| 3 | `sign(FND_RATE) == sign(BAS_BPS)` | `BAS_BPS` | **Data-quality check only** — disagreement means one feed is wrong |
| 4 | `FND_PRED` confirms persistence (`sign(efr) == sign(fr)`) | `FND_TENSION` | The next interval is not already normalising |
| 5 | Regime gate (below) passes | `VOL_RATIO`, `MR_VR` | Not fading a genuine trend |
| 6 | All `DQ_*` hard gates pass, `skw == 0` | Family 10 | Data is live |

**Direction:** `fr` strongly positive → longs are paying → perp is rich → **SHORT**.
`fr` strongly negative → **LONG**.

### Regime gate — hard veto

Veto when **both** of these hold on the 1h timeframe:
- `VOL_RATIO` in the top tercile (volatility expanding), **and**
- `MR_VR > 1` (trending / positively autocorrelated)

That is the squeeze condition: an expanding, trending market where the crowded side is
crowded *because they are right*. No trade, regardless of how extreme funding is.

### Entry, stop, target, invalidation

- **Entry:** ACT on price, trigger timeframe 15m, `U = ATR_15m`.
- **Price stop:** `X* − s·ATR_1h` (LONG) / `X* + s·ATR_1h` (SHORT).
- **Mechanism stop — dual invalidation.** If `|FND_MULT|` falls back inside the normal band
  while the position is not yet at T1, the thesis is dead independently of price. **Exit.**
  A correction trade whose restoring force has switched off is no longer the trade that was
  entered.
- **Target T1:** perp fair value = `spot_last × (1 + median_basis)`. This is where the
  mechanism's force dissipates — not an arbitrary R multiple.
- **Target T2:** the level at which `FND_MULT` returns to baseline, estimated from the
  basis/funding relationship once enough history exists. Until then, T2 is unpublished.
- **Setup expiry:** if ACT does not trigger within **3 funding intervals (24h)**, expire. The
  carry opportunity decays and the setup is stale.

### Limitations, stated plainly

- **Tier B. Cannot be backtested at launch.** No funding history exists. Every C1-A signal
  carries `validated: false` until the recorder has accumulated enough history for Phase 16.
- `FND_CUM` rests on the settlement approximation documented in `specs/02` Family 6. If C1-A's
  edge turns out to depend on precise cumulative carry rather than on the extremity itself,
  that approximation needs scrutiny before deployment.
- Per capability-map §7, basis here is the **upstream venue's** basis (⧗ PENDING P9). The
  funding mechanism is unaffected by this — funding is charged on CoinDCX regardless — but any
  language describing this as a CoinDCX-specific inefficiency would be false.
- Requires probe **P8** for nothing; requires nothing but the public RT feed. This is the one
  Tier B strategy that needs no credentials.

---

## 4. C1-B — Conditional Statistical Overextension

### Hypothesis

Price deviations from a trailing mean revert **only in regimes where the return process
exhibits negative serial dependence**. We do not assume such regimes exist or persist. We
measure whether one is currently operative, and trade only then.

### Why this is not just "buy the dip"

The edge is entirely in the conditioning. An unconditional mean-reversion strategy on crypto
at this horizon is close to a coin flip after costs, because the asset class trends. The
variance ratio (`MR_VR`, `specs/02` Family 4) is a direct statistical test of which behaviour
is operative *now*. Trading mean reversion only when `VR < 1`, and standing down otherwise,
is the whole strategy. Everything else is entry mechanics.

### Why it fails, and when

`MR_VR` is a trailing estimate over a finite window; the regime can flip immediately after
classification. It is also sensitive to a single large jump. Two defences: require regime
**persistence** (the dwell-time test from `specs/02` §6 must have passed for the regime model
to be usable at all), and require **structural confluence** so that the deviation has a second,
independent reason to arrest.

### Setup conditions — ALL must hold

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | `\|DEV_Z\| ≥ z_dev` on 1h | `DEV_Z` | Deviation is in the tail |
| 2 | `MR_VR(k) < 1` for a majority of `k ∈ {2,4,8}` on 1h | `MR_VR` | Mean reversion is measurably operative |
| 3 | Regime cell has been stable ≥ its median dwell time | regime model | Not a one-bar classification |
| 4 | Price within `c·ATR_1h` of a confirmed swing level or prior-period extreme | `STR_DIST_PDH`, `STR_SWING` | Independent structural reason to arrest |
| 5 | 1d `MR_VR` not strongly > 1 | `MR_VR` (1d) | Higher timeframe is not in a strong trend |
| 6 | `VLM_ZROBUST` not in the extreme upper tail | `VLM_ZROBUST` | Exclude the climactic-volume case, where the deviation may be a genuine repricing |
| 7 | All `DQ_*` hard gates pass | Family 10 | |

**Direction:** `DEV_Z` strongly negative → **LONG**. Strongly positive → **SHORT**.

**On condition 4 and double-counting:** `DEV_Z` measures distance from central tendency;
`STR_DIST_PDH` measures proximity to a historical extreme. These are conceptually different,
but they will correlate in practice. Their pairwise correlation must be reported by the
`specs/02` §7 redundancy protocol. **If `|ρ| ≥ 0.8`, condition 4 is not independent evidence
and must be demoted from a setup condition to a strength input only.**

### Entry, stop, target, invalidation

- **Entry:** ACT on price, trigger timeframe 15m.
- **Stop:** `X* − s·ATR_1h` (LONG) / `X* + s·ATR_1h` (SHORT).
- **Target T1:** 50% of the deviation closed — price returns halfway to `SMA_N`.
- **Target T2:** `SMA_N` itself. **The target is where the restoring force stops acting**, not
  an arbitrary multiple of R. If T1 does not clear the minimum-R:R gate, there is no trade.
- **Mechanism invalidation:** `MR_VR` crosses above 1 on the signal timeframe → the measured
  mean-reverting behaviour has ended → exit regardless of price.
- **Structural invalidation:** price closes beyond the structural level from condition 4 by
  more than `c·ATR_1h`.
- **Setup expiry:** if ACT does not trigger within `N_exp` 1h bars, expire — deviations decay
  naturally and a stale setup is no longer the setup that was detected.

### Limitations

- Tier A and fully backtestable, which makes it the **reference strategy for the whole
  system**: it is the first thing that can be validated, and the first evidence about whether
  the pipeline produces anything real.
- `DEV_Z` uses a trailing SMA, which lags in trends — in a strong trend `DEV_Z` reads extreme
  persistently. Condition 2 is what prevents that from generating continuous signals, which
  means **condition 2 carries almost all of the load**. If `MR_VR` proves unreliable in
  backtest, C1-B has no edge and should be cut rather than patched.

---

## 5. C1-C — Cross-Asset Beta Dislocation

### Hypothesis

BTC, ETH and SOL are typically 0.7–0.9 correlated. When one asset's return diverges materially
from its beta-implied path, the residual represents a temporary relative mispricing with a
tendency to reconverge.

### Honest assessment of the mechanism

This force is **weaker than C1-A's**. There is no cash flow compelling convergence — only
relative-value capital and rebalancing flow, which may or may not be present. It is included
because it is a genuinely *different* mechanism from the other two, it is Tier A and therefore
testable, and because the relative-value axis is the only one that exploits having three
correlated assets rather than treating them as three independent markets.

### The cost problem, stated up front

This is a **two-leg** trade: ~0.236% round-trip on futures taker fees, before spread and
slippage. Given the assets are 0.7–0.9 correlated, the residual spread is small. **I expect
this strategy to fail its cost gate most of the time and to produce few signals.** That is an
acceptable outcome for this class — but if a full backtest produces near-zero qualifying
signals, C1-C should be **cut, not loosened**. Loosening a cost gate to manufacture signals is
how a strategy becomes a fee-generation machine.

### Single-leg variant: rejected

A one-leg version (trade only the dislocated asset) is explicitly **not** offered. Without the
hedge it is a directional bet contaminated by market beta, which is not a correction strategy
at all. Presenting it as one would be mislabelling the risk.

### Setup conditions — ALL must hold

| # | Condition | Feature | Purpose |
|---|---|---|---|
| 1 | `\|z(X_RS)\| ≥ z_rs` for the asset vs BTC | `X_RS` | Residual is in the tail |
| 2 | `X_CORR(asset, BTC) ≥ ρ_floor` over the medium window | `X_CORR` | The relationship exists |
| 3 | `X_BETA` stable — rolling beta has not drifted beyond a band over the window | `X_BETA` | Dislocation, not regime change |
| 4 | Expected convergence (residual → 0) ≥ `κ ×` round-trip cost of **both** legs | `X_RS`, live fees | Economically viable |
| 5 | `X_DISP` not in the extreme upper tail | `X_DISP` | Exclude idiosyncratic-news regimes where reconvergence should not be expected |
| 6 | All `DQ_*` hard gates pass on **both** legs | Family 10 | |
| 7 | `MR_VR(1h) < 1` on the dislocated asset | `MR_VR` | ⚠ **AMENDMENT applied 2026-09-27** per `specs/04` §7 — restores mutual exclusivity with C2-B, which follows the factor trend on the same residual. Without it, C1-C and C2-B can fire on one residual in opposite directions. Recorded in `docs/findings.md` F-030 |

**Direction:** long the underperformer, short the outperformer, sized to be beta-neutral.

### Entry, stop, target, invalidation

- **Entry:** ACT applied to the **spread series**, not to either leg. `U` = rolling σ of the
  residual. Both legs execute together on trigger.
- **Stop:** residual extends beyond `X*_spread ± s_rs·σ`.
- **Target:** residual returns to its rolling mean (zero by construction).
- **Mechanism invalidation, two forms:**
  - `X_CORR` drops below `ρ_floor` → the pair relationship has broken; this is no longer a
    dislocation, it is a decoupling. Exit.
  - `X_BETA` shifts beyond its band → the relationship has re-specified, not dislocated. Exit.
- **Setup expiry:** `N_exp_rs` 1h bars.

### Limitations

- Beta-neutral sizing means the two legs have **different notional sizes**, which the
  suggestion output must state explicitly per leg or the user will execute it wrong.
- Rolling correlation spikes toward 1 in stress — exactly when condition 2 looks best and the
  trade is most dangerous. Condition 5 is the partial defence; it is not a complete one.
- Execution is materially harder than the other two strategies. The UI must show this as a
  two-leg suggestion with both legs' entries, stops and sizes, never as a single line.

---

## 6. Traceability — C1-B worked example

The required chain, concretely, for one strategy. The same structure applies to C1-A and C1-C.

| Stage | Content |
|---|---|
| **Raw** | Spot + futures 1h OHLCV; 15m + 1m OHLCV; instrument metadata (`ATR` scale, fees, tick) |
| **Features** | `DEV_Z(1h)`, `MR_VR(1h, k=2,4,8)`, `MR_VR(1d)`, `ATR(1h)`, `ATR(15m)`, `STR_SWING(1h)`, `STR_DIST_PDH`, `VLM_ZROBUST(1h)`, regime cell + dwell |
| **Conditions** | The seven of §4, each evaluated to true/false with its input values recorded |
| **Strength** | §7 below — monotone in deviation percentile and VR conviction only |
| **Confidence** | **Not published.** See §8. |
| **Entry** | `E(t) = X*(t) + δ·ATR_15m`, recomputed every 1m; `X*` ratchets |
| **Stop** | `X* − s·ATR_1h`, rounded **away** from entry to `price_increment` |
| **Target** | T1 = midpoint to `SMA_N`; T2 = `SMA_N`; both rounded to tick |
| **Risk** | `R = entry − stop`; `R:R = (T1 − entry)/R`; net-of-cost edge using live `taker_fee` |
| **Signal** | Emitted only if R:R gate and net-edge gate both pass; else `NO VALID TRADE` with the failing gate named |

**Audit record — every signal, including rejected ones.** Strategy ID and version, parameter
set hash, every raw input with its source timestamp and age, every derived feature value,
every condition with pass/fail and the value that decided it, the regime cell and its dwell
count, `X*` history, `E(t)` at emission, all price levels pre- and post-tick-rounding, the fee
values used, and every `DQ_*` gate state. A signal whose derivation cannot be replayed from
this record is a bug.

---

## 7. Signal strength

Strength is **not** a sum of indicators. Within this class it is monotone in at most two
near-orthogonal quantities:

- **Dislocation extremity** — percentile rank of the primary deviation measure
  (`FND_MULT`, `|DEV_Z|`, or `|z(X_RS)|`) against its own trailing distribution.
- **Mechanism conviction** — how strongly the restoring force is indicated
  (distance of `MR_VR` below 1 for C1-B; `FND_CAPDIST` for C1-A; `X_CORR` above floor for C1-C).

Setup conditions are **binary gates, not score contributors.** A condition that has already
been required cannot also add strength — that is double-counting by another name. Confluence
conditions demoted by the redundancy protocol (§4 condition 4) may contribute to strength but
never to the gate.

## 8. Confidence and probability — deliberately absent

No C1 signal publishes a probability or confidence percentage at launch.

There is no backtest. Any number would be invented, and an invented probability on a trading
suggestion is worse than no number at all, because it converts a guess into something that
looks measured. After Phase 16 calibration, signals may carry an **empirical hit rate with an
explicit confidence interval and sample size**, conditioned on regime. Until then the field is
`null` and the UI shows why.

---

## 9. Signal object schema

```
asset, product ("spot" | "futures" | "pair"),
strategy_class: 1, strategy_id: "C1-A" | "C1-B" | "C1-C", strategy_version,
direction: "LONG" | "SHORT",
state: "SETUP_DETECTED" | "ENTER_NOW" | "EXPIRED" | "INVALIDATED",
current_price, live_entry_trigger,        # E(t), updating
stop_loss, targets: [T1, T2?],
R, risk_reward, net_edge_after_costs,
strength,                                  # §7
confidence: null,                          # §8
signal_timeframe, trigger_timeframe,
regime_cell, regime_dwell,
setup_reason,                              # why this opportunity exists now
invalidation: { price, mechanism },        # both, explicitly
expires_at,
conditions: [ {name, passed, value} ],     # all, including passed
validated: bool,                           # Tier A true, Tier B false at launch
data_freshness: { source: age_seconds },
parameter_set_hash, created_at, updated_at
```

`live_entry_trigger` is the field that makes `SETUP_DETECTED` actionable — the user sees the
exact price at which the system will say enter, and watches it move.

---

## 10. Parameters introduced

Running total against the 25-parameter cap from `specs/02` §10: **16 → 22.**

| # | Parameter | Used by | Band | Overfitting exposure |
|---|---|---|---|---|
| 17 | `δ` — ACT trigger multiple | all three, and later classes | 0.3–1.0 ATR | **Low.** Shared, monotone, interpretable. A sharp optimum indicates overfitting. |
| 18 | `s` — stop buffer ATR multiple | all three | 0.5–1.5 ATR | **Low.** Shared; trades stop-out rate against R. |
| 19 | `m_extreme` — funding multiple of baseline | C1-A | 5–30× | **Medium.** Anchored to an observable baseline and a structural cap, not fitted to returns. |
| 20 | `z_dev` — deviation threshold | C1-B | 1.5–3.0 | **Medium.** Calibrate on the training window only. |
| 21 | `z_rs` — residual threshold | C1-C | 1.5–3.0 | **Medium.** |
| 22 | `ρ_floor` — correlation floor | C1-C | 0.6–0.8 | **Low.** Structural sanity bound, not an optimisation target. |

`c` (structural proximity), `N_exp` (expiry), `κ` (cost multiple) and `min_RR` are **not**
counted here — `c` and `N_exp` are fixed by the shared lookback logic of `specs/02` §4, and
`κ` and `min_RR` belong to the Phase 8 risk engine. Three slots remain for Phases 4–8, which
is tight by design.

---

## 11. Expected frequency — a diagnostic, not a claim

Design intent is **≤ 4–8 Class 1 signals per asset per month combined.** This is not a
prediction; it is a threshold for detecting that something is wrong.

- Materially more → thresholds are loose, or a "correction" condition is firing on ordinary
  noise. Tighten, or cut the offending strategy.
- Near zero across a full backtest → either thresholds are too tight, or the mechanism is not
  present in this market. **Distinguish these two before touching a parameter.** Check the
  distribution of the gating feature first: if `|DEV_Z|` never reaches `z_dev`, that is a
  threshold problem; if it reaches it often but condition 2 never co-occurs, the mechanism is
  absent and the strategy should be cut.

---

## 12. Acceptance criteria for Phase 3

1. ACT is implemented once, as a shared primitive, with unit tests covering: ratcheting
   behaviour, the no-trigger case in a continuous decline, and both direction mirrors.
2. C1-B is implemented and backtestable on the Phase 1 backfill. C1-A and C1-C are implemented
   and emit `validated: false` / run against accumulating data respectively.
3. Every strategy can return `NO VALID TRADE` with the specific failing gate named, and does so
   on historical data where conditions were absent.
4. The `SETUP_DETECTED` → `ENTER_NOW` transition is driven solely by `E(t)` crossing, with no
   alternative entry path anywhere in the code.
5. Dual invalidation (price **and** mechanism) is implemented for all three. A position whose
   restoring force has switched off exits regardless of price.
6. The audit record of §6 is complete enough to replay any signal, including rejected ones.
7. The redundancy check on `DEV_Z` vs `STR_DIST_PDH` has run, and condition 4's status
   (gate vs strength-only) is recorded in `docs/findings.md`.
8. No probability or confidence value is published anywhere in code or UI.
