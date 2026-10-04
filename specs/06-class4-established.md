# 06 — Strategy Class 4: Established Methodologies & the Benchmark Suite

**Depends on:** `specs/00`, `specs/02`, `specs/03` (ACT), `specs/04`, `specs/05`.
**Design intent:** moderate frequency, and — more importantly — **this class is the control
group for the entire system.**

---

## 1. Class identity: a different kind of warrant, not a different mechanism

Classes 1–3 are bespoke designs. Their justification is mechanism plus reasoning. **No one has
ever traded them, and they have never been tested by anyone, anywhere.**

Class 4 draws on methodologies with decades of published, cross-market, out-of-sample
scrutiny. That is a fundamentally different epistemic status, and it is the only honest
distinguishing feature available — because the mechanisms themselves (trend, reversion,
breakout) are already spoken for by Classes 1–3.

This gives the class two roles, and the second matters more than the first:

1. **Tradeable strategies**, implemented rigorously rather than as textbook versions.
2. **The benchmark against which Classes 1–3 must justify their own existence.**

If C1-B cannot beat a rigorously implemented short-horizon reversal rule, then C1-B's variance-
ratio conditioning is decoration. If C2-A cannot beat a Donchian breakout, its pullback-entry
machinery adds nothing but complexity and failure modes. **Sophistication has to earn its place
against the simple version, and Class 4 is how that gets measured.**

### On the class being trend-heavy

Two of the three tradeable members are trend-based. That is not a failure of imagination — the
external evidence base is genuinely lopsided toward trend and momentum, with far more
out-of-sample support than any other technical approach. Manufacturing variety by including
weakly-evidenced methods would defeat the purpose of the class. Where a weakly-evidenced method
appears here, it appears explicitly as a **negative control** (§5.2), not as a suggestion.

---

## 2. ⚠ Conceptual knowledge versus live data

Everything in this section about the external evidence base comes from **training knowledge, not
from any data this system can access.** It may be outdated or incomplete, and the live system
cannot verify a single claim in it.

Its role is strictly to justify *including a strategy for testing* — never to justify *believing
it works*. No performance expectation derived from external literature may appear in a signal,
in the UI, or in any parameter default. The only evidence that counts for deployment is this
system's own backtest on this exchange's data.

Named findings below are referenced for their general conclusion only. **No performance figure,
Sharpe ratio, win rate or return number is reproduced anywhere in this spec, because any such
number would be recalled rather than verified.**

---

## 3. Dual-mode operation

Class 4 overlaps Classes 1–3 by design — that is what makes it a benchmark. Which creates a
duplicate-signal problem, solved by running each strategy in two modes:

**Benchmark mode — always on, never suppressed.** Every Class 4 strategy is evaluated
continuously and logged in full, including when a Class 1–3 signal is active on the same asset.
Suppression would break the comparison by censoring exactly the periods where the bespoke
strategies were active.

**Suggestion mode — deduplicated.** A Class 4 signal surfaces to the user only if no Class 1–3
signal is active on the same asset in the same direction. If one is, the Class 4 signal is
logged as `SUPPRESSED_DUPLICATE` with a reference to the signal that superseded it.

The benchmark stream is complete. The user-facing stream is not redundant. Both are needed.

---

## 4. Tradeable members

### 4.1 C4-A — Donchian Channel Breakout with Volatility-Scaled Exit

**Evidence base (conceptual).** Channel-breakout trend following is among the most extensively
documented systematic approaches in finance — the Donchian rules, the Turtle system, and the
broader managed-futures literature, with the time-series momentum effect documented across
dozens of instruments and multiple decades in the academic work of Moskowitz, Ooi and Pedersen
and subsequent replications.

**Why it works.** Three mechanisms, not a pattern: gradual diffusion of information producing
under-reaction; positive-feedback and herding behaviour extending moves; and a risk-transfer
premium earned by taking the other side of hedging demand. The payoff is deliberately
fat-tailed — a minority of large winners funds a majority of small losers.

**When it works.** Sustained directional regimes, expanding volatility, markets with persistent
structural flows.

**When it fails.** Range-bound chop, where every breakout is false. Sharp V-reversals, where the
trailing exit gives back a large fraction of the move. Low-volatility grind, where breakouts
are too small to clear costs.

**Regimes where it performs poorly — and this is measurable.** The `specs/02` §6 regime model
lets us report C4-A's performance *conditioned on regime cell* rather than as a single blended
number. The prediction is poor performance in Axis P mean-reverting cells. **If the backtest
does not show that, the regime model, the strategy, or the backtest is wrong** — and finding out
which is more valuable than the headline result.

**Rigorous implementation versus textbook.**

| | Textbook | This implementation |
|---|---|---|
| Entry | N-period high/low breakout | Same — the entry is not where rigour is needed |
| Exit | M-period opposite channel | **Volatility-scaled trailing stop** at `s·ATR_1h` from the running extreme — removes a second arbitrary lookback |
| Sizing | Fixed contracts | ATR-normalized (Phase 8) |
| Pyramiding | Turtle system pyramids | **None** — it adds parameters and concentrates risk |
| Direction filter | None | **Only take breakouts aligned with `sign(TRD_TSTAT(1d))`** |
| Volatility filter | None | Skip breakouts occurring in the top decile of `VOL_RATIO` — these are usually exhaustion, not initiation |

**False-signal filtering.** The 1d direction filter is the single highest-value filter for any
breakout system and is well supported. **Deliberately, only two filters are applied.** Each
additional filter is a parameter fitted to a specific history, and breakout systems are
especially easy to over-filter into non-existence.

**Entry, stop, targets.** Entry on a confirmed 15m close beyond the channel boundary — the same
confirmation rule as C3-A, reused. Initial stop at `s·ATR_1h` inside the channel. No T1: the
position is **trailed from entry**, because capping a trend strategy's winner is the canonical
way to destroy its expectancy. `trail_level` published live, as in `specs/04`.

**Crypto appropriateness.** Favourable in principle — crypto trends strongly and is volatile
enough for breakouts to clear costs. But it whipsaws violently, and drawdowns in crypto trend
systems are severe. The volatility-scaled exit is what makes the profile survivable.

---

### 4.2 C4-B — Dual-Timeframe Momentum with Volatility Targeting

**Evidence base (conceptual).** Time-series momentum expressed as the sign of a trailing return,
combined with volatility scaling, is the canonical CTA formulation. The volatility-management
component draws on the finding — associated with Moreira and Muir — that scaling exposure
inversely to realized volatility improves the risk-adjusted profile of factor strategies. The
long-term trend filter as a tactical overlay is associated with Faber's work on tactical asset
allocation.

**How it differs from C4-A — and why both are here.** C4-A is *event-driven*: it waits for a
breakout. C4-B is *state-driven*: it holds a position whenever the signal is on. Same underlying
premise, materially different trade profile, different failure timing. Running both isolates
whether any observed edge comes from the trend premise itself or from the breakout entry
mechanic specifically.

**Why it works.** Same mechanisms as C4-A.

**When it fails.** At turning points — the strategy is maximally exposed at tops and bottoms by
construction. And at the signal boundary, where small oscillations around zero produce whipsaw.

**Rigorous implementation.**
- Signal: `sign(TRD_RET(1h, medium))`, with `sign(TRD_RET(1d, short))` required to agree.
  **Return sign rather than a moving-average crossover**, because a crossover needs two
  lookbacks to express the same information.
- Run on the 1h medium lookback with 1d as the filter, so holding periods land in days and
  match the stated horizon. A pure daily-signal version would hold for weeks and exceed it.
- Exposure scaled inversely to `VOL_CC` toward a volatility target (Phase 8 owns the target).
- Entry timing uses **ACT**, imported from `specs/03` §2, so that a signal flip does not cause
  entry into a spike. The signal decides direction; ACT decides the fill.
- Exit on signal flip, or on the `s·ATR_1h` trailing stop, whichever comes first.

**Crypto appropriateness.** The volatility targeting matters more here than in any other market
covered by the literature, because crypto's volatility range is far wider than equities or FX.
Without it, exposure would be wildly inconsistent across regimes.

---

### 4.3 C4-C — Short-Horizon Reversal

**Evidence base (conceptual).** Short-horizon return reversal is long documented in equities —
the Jegadeesh and Lehmann work on weekly and monthly reversal — with the standard explanation
being compensation earned by liquidity providers who absorb transient order-flow imbalance, plus
overreaction to non-informational flow. Crypto-specific study exists but is thinner and shorter.

**Why it works.** Not "price went down so it goes up." The mechanism is that a move driven by
liquidity demand rather than information temporarily displaces price, and the liquidity provider
who takes the other side is paid for the inventory risk. Where the move *is* informational, no
reversal should be expected — which is the basis of the single filter below.

**⚠ This strategy is structurally short-volatility, and that changes how it must be judged.**
Many small wins, occasional large loss. Consequences that must be enforced in Phase 9:

- **Hit rate will look excellent and will be misleading.** A strategy can win 70% of the time
  and lose money.
- **Sharpe ratio flatters short-gamma profiles** by under-penalising rare large losses.
- Evaluation therefore requires **maximum drawdown, worst single trade, and the loss
  distribution's tail** — not hit rate and not Sharpe alone.

This also gives the system genuine diversification in **payoff shape**: C4-C is short-gamma
where Class 3 is long-gamma. That is a more meaningful form of diversification than having four
strategies with different indicators and the same payoff profile.

**When it fails.** Trending regimes, where "reversal" is just fading a trend. After genuine
information, where repricing is permanent. During volatility expansions, where the tail loss
arrives.

**Deliberately left unconditioned — this is the point.**

C4-C's only filter is a volume-tail exclusion (top decile, as a crude news proxy; note the
literature is genuinely mixed on the direction of the volume interaction). **No regime gate is
applied.**

That is not an oversight. C1-B is the conditioned version of this idea — the same observation
gated on `MR_VR < 1`. Adding the same gate here would collapse C4-C into C1-B and destroy the
comparison. **Keeping C4-C unconditioned is what makes it possible to answer: does the
variance-ratio gate add anything, or is C1-B an expensive way to do what a simple rule already
does?**

**Rigorous implementation versus textbook.** The textbook version is "buy the biggest recent
loser" with no stop — which is exactly how short-gamma strategies produce a catastrophic loss.
This implementation adds a hard volatility-scaled stop at `s·ATR_1h` and a fixed target at the
short-lookback mean. That single addition is the difference between a tested strategy and an
untested one.

**Crypto appropriateness.** Questionable, and stated as such. Crypto's majors are liquid enough
for the liquidity-provision mechanism to be plausible, but crypto's tail risk is far larger than
the equity markets where the effect was documented. A short-gamma strategy in a fat-tailed asset
is the least comfortable combination in this entire system. **If the backtest tail analysis is
poor, cut it — do not widen the stop.**

---

## 5. The control suite — non-tradeable, never surfaced to the UI

Two controls that exist to test the *system*, not the market.

### 5.1 C4-X1 — Random-Entry Control

**Purpose.** Separate edge that comes from the **signal** from edge that comes from the **risk
management**. A great many published "strategies" are competent exit logic attached to a noise
entry, and the only way to find out is to run the exit logic with a noise entry.

**Construction.** Identical instrument, sizing, stop, target and trailing logic to C4-A. Entry
direction randomized from a seeded, reproducible generator. Entry timing drawn from the same
distribution of opportunity times as C4-A, so the comparison is like-for-like.

**Execution.** Run across **many seeds** to produce a performance *distribution*, never a single
path. A single random path proves nothing.

**The governing rule:**

> **If a strategy cannot beat C4-X1 with statistical significance, its signal contributes
> nothing and the strategy is cut.**

This applies to every strategy in the system, Classes 1–4 alike.

### 5.2 C4-X2 — Weak-Evidence Control

**Purpose.** Detect defects in the **backtest methodology itself**.

**Construction.** A textbook, unconditioned Bollinger-band-touch reversal: buy the lower band,
sell the upper, no regime filter, no trend filter. Selected precisely because external evidence
for the unconditioned version is weak to absent — Bollinger's own writing distinguishes
band-walking in trends from reversal, and the naive rule ignores that distinction.

**The governing rule:**

> **If C4-X2 backtests well, the backtest is suspect — not the strategy.**

A strong result here is a signal to audit for look-ahead bias, survivorship in level
definitions, cost under-modelling, or a bug in the fill logic. Investigate before accepting any
other result from the same harness.

**Neither control is ever tradeable, and neither ever reaches the UI.**

---

## 6. The benchmark comparison protocol — pre-register before any backtest runs

Register these in the Phase 9 protocol **before** Phase 16 executes anything. Results are
recorded whichever way they fall.

| Bespoke strategy | Benchmark | Question it answers |
|---|---|---|
| C1-B | C4-C | Does variance-ratio conditioning beat unconditioned reversal? |
| C2-A | C4-A, C4-B | Does pullback entry beat breakout entry on the same trend premise? |
| C2-B | C4-B on BTC only | Does expression selection beat the factor alone? *(also registered in `specs/04` §4)* |
| C3-A | C4-A | Does contraction-conditioned breakout beat generic breakout? |
| C3-B | C4-B | Does early transition entry beat established-trend entry? |
| **All strategies** | **C4-X1** | Does the signal beat random entry with identical risk management? |
| **The harness itself** | **C4-X2** | Is the backtest methodology sound? |

**Governance rule — this is the teeth:**

> A bespoke strategy that fails its benchmark comparison is **cut, not tuned.**

Tuning a strategy until it beats its benchmark is fitting the benchmark, which is worse than
having no benchmark at all. The comparison is only informative if losing it has consequences.

---

## 7. Crypto-specific appropriateness assessment

Most of the evidence base comes from equities, futures and FX. What transfers and what does not:

| Property | Effect on these methods |
|---|---|
| **24/7 trading** | No gaps or overnight risk — favourable for breakouts and stops. But no natural session reset, so daily bar boundaries are an arbitrary UTC convention rather than a real event (⧗ PENDING P11 confirms where they fall). |
| **No fundamental anchor** | Value and fundamental mean-reversion methods do not transfer at all. Correctly absent from this class. |
| **High retail participation** | Behavioural effects — under-reaction, herding, overreaction — are plausibly *stronger*, which favours both momentum and short-horizon reversal. |
| **Extreme tail risk** | Materially worse for short-gamma methods (C4-C). Better for long-gamma methods (C4-A, C3-A). |
| **Perpetual funding** | The carry factor has a direct crypto analogue. Evaluated and **deliberately not given a separate strategy** — C1-A already trades funding extremes and C2-C trades funding-aligned trends, and a third funding strategy would be the same signal a third time. |
| **Young history** | Less out-of-sample data than any market in the literature, and more regime instability. **This is the strongest single argument for the control suite in §5.** |
| **BTC-dominated correlation** | Cross-sectional methods are weak with n=3. Correctly absent. |

---

## 8. Parameters — zero

Running total against the 25-parameter cap: **25 → 25. Unchanged.**

This is the intended outcome. Established methods should not require bespoke knobs; if they did,
they would not be the simple baselines the class exists to provide.

**All reused:** channel and momentum lookbacks from the `specs/02` §4 shared set; `s` for every
stop and trail; `t_min` for the C4-A direction filter; `δ` for ACT entry timing in C4-B; the
15m-close confirmation rule from `specs/05` §3.

**Fixed as structural constants, consistent with prior phases:** the volume-tail exclusion in
C4-C at the top decile; no pyramiding; return-sign rather than MA crossover.

**Controls:** random seeds only. Seeds are recorded for reproducibility and are not parameters.

---

## 9. Traceability — C4-A

| Stage | Content |
|---|---|
| **Raw** | 1h / 15m / 1m OHLCV, 1d OHLCV; instrument metadata |
| **Features** | Channel high/low over the shared lookback, `TRD_TSTAT(1d)`, `VOL_RATIO(1h)`, `ATR(1h)`, regime cell |
| **Conditions** | Breakout confirmed; 1d direction aligned; `VOL_RATIO` not top decile; DQ gates |
| **Strength** | §11 — breakout extension and 1d trend significance |
| **Confidence** | `null`. `breakeven_hit_rate` published, as `specs/05` §2.1 |
| **Entry** | Confirmed 15m close beyond the channel boundary; boundary published live |
| **Stop** | `s·ATR_1h` inside the channel, rounded away from entry to `price_increment` |
| **Targets** | None. Trailed from entry at `s·ATR_1h`; `trail_level` published live |
| **Risk** | `R`, `c` from live `taker_fee`, `p*` |
| **Signal** | Emitted if the net-edge gate passes; deduplicated per §3; else logged with the reason |

Audit requirements as `specs/03` §6, plus: `mode` (benchmark or suggestion), and on suppression,
the ID of the superseding signal.

---

## 10. Signal object — additions

```
mode: "suggestion" | "benchmark",
evidence_basis: "external_literature",     # distinguishes Class 4's warrant from Classes 1-3
suppressed_by: null | signal_id,           # set when deduplicated per §3
benchmark_for: [strategy_ids],             # which bespoke strategies this benchmarks
control: null | "random_entry" | "weak_evidence",   # X-series only; never surfaced
seed: null | int                           # X-series reproducibility
```

`evidence_basis` exists so the UI can be honest about *why* a suggestion is being made: Class 4
rests on external evidence that this system has not verified; Classes 1–3 rest on mechanism
reasoning that no one has verified. Those are different claims and the user should be able to
tell them apart.

---

## 11. Signal strength

As `specs/03` §7 — at most two near-orthogonal quantities, gates never contribute:

- **C4-A:** breakout extension beyond the channel (percentile) and `|TRD_TSTAT(1d)|`.
- **C4-B:** `|TRD_RET|` percentile and the degree of 1h/1d agreement.
- **C4-C:** displacement percentile and inverse volume rank — a large move on *low* volume is the
  cleaner liquidity-demand case.

`confidence` remains `null` system-wide until Phase 16.

---

## 12. Expected frequency — diagnostic, not prediction

Design intent: **8–20 Class 4 signals per asset per month in benchmark mode**, materially fewer
in suggestion mode after deduplication. Higher than Classes 1–3 because these rules are
deliberately less conditioned.

- **Suggestion-mode frequency near zero** would mean Classes 1–3 are absorbing everything, which
  is informative rather than alarming — but check that deduplication is not over-matching.
- **C4-C showing a very high hit rate** is expected and is **not** evidence it works. Go
  straight to the tail analysis.
- **Any strategy showing a smooth, low-drawdown equity curve** in a fat-tailed asset class over a
  short history is more likely a bug than an edge.

---

## 13. Acceptance criteria for Phase 6

1. All three tradeable members implemented and backtestable; all are Tier A.
2. Dual-mode operation works: benchmark mode is never suppressed, suggestion mode deduplicates
   correctly, and suppressed signals are logged with `suppressed_by`.
3. C4-X1 runs across many seeds and produces a performance **distribution**; the harness reports
   percentiles, not a single path.
4. C4-X2 is implemented, non-tradeable, and firewalled from the UI.
5. The §6 comparison protocol is registered in the Phase 9 document **before** any backtest is
   executed, with the cut-not-tune rule written in.
6. C4-C's evaluation produces drawdown, worst-trade and loss-tail statistics — **hit rate and
   Sharpe alone are rejected as sufficient output** for this strategy.
7. ACT is imported, not reimplemented. Fourth consumer, still one implementation.
8. No external-literature performance figure appears anywhere in code, config, UI or defaults.
9. Regime-conditioned performance is reported for C4-A, and compared against the `specs/02` §6
   model's prediction of poor performance in mean-reverting cells.
10. No `confidence` value is published anywhere.

---

## 14. Status: all four strategy classes are now specified

| Class | Spec | Mechanism | Tier A members | Role |
|---|---|---|---|---|
| 1 | `specs/03` | Correction — restoring force | C1-B, C1-C | Fade dislocations |
| 2 | `specs/04` | Persistence — risk-adjusted drift | C2-A, C2-B | Participate in trends |
| 3 | `specs/05` | Transition — state change | C3-A, C3-B, C3-C | Anticipate, with quantified risk |
| 4 | `specs/06` | External evidence | C4-A, C4-B, C4-C | Trade **and** benchmark |

**12 strategies, 25 parameters, one entry primitive.**

Next: Phase 7 must resolve how these interact — scoring, cross-strategy arbitration, the
same-direction concentration case flagged in `specs/05` §6, and the `CLASS_CONFLICT` rule from
`specs/04` §7 which is currently only an interim measure.
