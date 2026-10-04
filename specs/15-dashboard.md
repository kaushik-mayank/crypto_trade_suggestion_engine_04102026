# 15 — Dashboard

**Depends on:** `specs/14` (services, envelope, five system states), `specs/10` (Jinja2 + HTMX +
lightweight-charts, no build step).
**Governing constraint from the brief:** clarity and decision-making over visual decoration.

---

## 1. What this dashboard must not do

The design problem here is not "look professional." It is **not misleading a person who is about
to risk money.** Six specific failure modes, each of which a conventional dashboard would commit:

| # | Failure | Why it happens by default |
|---|---|---|
| 1 | A quiet market looks like a broken system | Both render as empty panels |
| 2 | `confidence: null` looks like a bug or an oversight | Nulls render as blank |
| 3 | `validated: false` gets scanned past | It arrives as a small badge among many |
| 4 | Warm-up looks like failure | 60–180 days of near-empty screens with no explanation |
| 5 | Three correlated longs look like three opportunities | Each renders in its own card |
| 6 | A stale number looks like a live one | Stale styling is usually subtle |

Every layout decision below traces to one of these.

---

## 2. Design grounding

The subject is **instrumentation**, not a product page: trading terminals, aircraft panels,
patient monitors. Surfaces read quickly, under time pressure, where misreading has consequences.
Viewed for hours, glanced at rather than studied.

**The governing principle, specific to this brief:**

> **Colour is a scarce resource.** Direction (long/short), state (setup/enter/triggered) and
> health (fresh/stale/down) all need to be distinguishable at a glance. So the base is near
> monochrome, and **any colour at all means something.** Nothing is coloured for emphasis, mood,
> or brand.

This rules out the two defaults a trading dashboard reaches for — near-black with a bright acid
accent, and the identical-rounded-cards kit where hierarchy is carried by shadow alone.

---

## 3. Token system

```
Base (cool graphite, not near-black — near-black + bright accent is the cliché)
  --bg          #14171A     page
  --surface     #1B1F24     panel
  --raised      #232830     card
  --line        #2E353E     structural rules
  --text        #E4E8ED     primary
  --muted       #8B95A1     labels, secondary

Meaning (used ONLY for meaning)
  --long        #4FA97A     direction: long
  --short       #C8664F     direction: short
  --act         #E8C468     ENTER NOW — the one loud colour, used nowhere else
  --stale       #6B5B3E     degraded/stale wash
  --down        #A33F3F     system down
```

**Type.** One family with a mono companion. The requirement is **tabular figures and
unambiguous digits** (0/O, 1/l/I), which matters more than the family name. IBM Plex Sans + IBM
Plex Mono meet it and were drawn for technical instrumentation; substitute only for something
that also does.

> **All numerals are tabular.** Proportional figures make the layout jitter as prices update,
> which is both ugly and genuinely harder to read at a glance.

Type scale: 12 / 14 / 16 / 20 / 28. Prices and levels in mono. Labels in sentence case —
**no all-caps labels, no eyebrow strings, no meta joined with middle dots.**

**Structure encodes information.** Borders and rules mark state boundaries, not decoration. A
card's treatment differs by state (§6), not by a uniform radius and shadow applied to everything.

**Motion.** One use only: a 200 ms flash on a value that changed. No entrance animations, no
hover transitions on cards. Respect `prefers-reduced-motion`.

---

## 4. Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ STATUS BAR   system: healthy · data 4s ago · warming 1d (30%)         │
├──────────────────────────────────────────────────────────────────────┤
│ ACTION       ENTER NOW — usually empty, and says so                   │
│              [ the one loud element on the page ]                     │
├──────────────────────────────────────────────────────────────────────┤
│ ASSETS       BTC 76,540 ▲0.8  │ ETH 2,451 ▲1.9  │ SOL 101.0 ▲2.9     │
│              regime · vol · funding · basis · freshness per asset     │
├──────────────────────────────────────────────────────────────────────┤
│ EXPOSURE     2 long signals ≈ 1.2 independent positions (ρ̄ 0.85)      │
├──────────────────────────────────────────────────────────────────────┤
│ CLASS 1 — Correction                                                  │
│   BTC · C1-B · LONG · setup      ETH —      SOL —                     │
│   [ empty panels show the binding condition, not blank space ]        │
├──────────────────────────────────────────────────────────────────────┤
│ CLASS 2 — Participation          CLASS 3 — Transition                 │
├──────────────────────────────────────────────────────────────────────┤
│ CLASS 4 — Established                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

### Why class-as-panel, asset-as-row

Not an aesthetic choice. `specs/07` §2 established that **no cross-class ranking exists** —
comparing a Class 1 correction to a Class 3 transition compares risk profiles, not qualities of
the same thing. Grouping by class and separating the panels makes that comparison structurally
awkward, which is correct. An asset-major layout would invite exactly the cross-class comparison
Phase 7 rejected.

---

## 5. The signal card

Collapsed state, scannable in under a second. Hierarchy answers one question first: **is this
actionable now, and at what price?**

```
┌─────────────────────────────────────────────┐
│ BTC  LONG   C1-B          setup · 1h        │
│                                             │
│   stop      trigger         T1              │
│   ├──────────●──────────────┤               │
│        now 76,540                           │
│                                             │
│   trigger   76,720      +0.24% away         │
│   stop      75,980      R 740               │
│   T1        78,200      R:R 2.0  net 1.9    │
│                                             │
│   needs 34% win rate to break even          │
│   strength 0.71 · backtested                │
│   mean reversion, 2.1σ below 168h mean      │
└─────────────────────────────────────────────┘
```

### The price-position bar

A single horizontal rule with `stop — trigger — T1` marked and a dot for current price. It
communicates distance, direction and geometry in one glance, which three lines of numbers do not.
When `TRIGGERED`, the entry marker replaces the trigger marker and the dot shows live position
between entry and target.

### Secondary numbers

`R:R` is shown as the **cost-adjusted** figure (`k_net`, `specs/08` §7.2) with the gross value
available on expand — the net number is the one that governs the gate, so it is the one displayed.

When past `SETUP_DETECTED`, both `risk_reward` and `remaining_rr` appear (`specs/07` §8.4);
showing only one misleads in one direction or the other.

---

## 6. `SETUP_DETECTED` versus `ENTER_NOW` — structural, never colour alone

This distinction is the one the brief calls out, and colour alone cannot carry it — colour is
already spent on direction and health, and roughly one in twelve men cannot separate the red/green
axis reliably.

**`ENTER_NOW` signals physically relocate to the action bar.** They leave their class panel and
move to the top of the page, rendered larger, on `--act`, with the word *enter* spelled out and
the trigger price in display size. That is three independent cues — **position, size, and a word**
— before colour is considered.

This is also the page's one loud moment. Everything else stays quiet, so when the action bar has
something in it, there is no ambiguity about what changed.

**When the action bar is empty it says so**: *"no entries live — 3 setups waiting"*. Never a blank
region, which reads as broken.

On transition the client fires a browser notification and an optional sound (`specs/14` §6).

---

## 7. Honest absence

Four states that a default UI renders as blank, and must not.

### `confidence: null`

```
probability    not estimated — 14 closed trades, ~100 needed
               needs 34% win rate to break even
```

Never blank, never a dash. The `confidence_unavailable_reason` from `specs/07` §11 is the copy,
and `breakeven_hit_rate` appears **in its place** as the honest, computable substitute.

### `validated: false`

Not a badge. Tier B signals render in a **separate labelled sub-section** within their class
panel — *"not yet backtested"* — with a dashed card border and muted surface, plus the date from
`earliest_tier_b_validation`:

```
not backtested · earliest validation 2027-03-15 · half size
```

A user must never be able to scan a Tier B signal as equivalent to a validated one.

### Warm-up

For the first 60–180 days this is most of what the dashboard shows (`specs/14` §5). It must read
as **progress**, not emptiness:

```
1h features   ████████░░░░░░░░  431 / 1440 bars   ready ~14 Nov
1d features   ███░░░░░░░░░░░░░   54 / 180  bars   ready ~20 Feb
waiting: C2-A, C2-B, C3-B  ·  available: C1-B, C4-C
```

Counting **available bars including backfill**, not elapsed time since install.

### Empty panels

Never blank. Each shows the binding condition from `specs/14` §7:

```
C1-B   no setup — variance ratio 1.18, needs < 1.00   (5 of 7 conditions pass)
```

This turns an empty screen into information about the market. It is also the visible payoff of the
evaluate-everything rule in `specs/13` §4.

---

## 8. The exposure strip — making correlation visible

Phase 8 computes a correlation haircut. **If the dashboard does not show it, the person taking the
risk never sees it**, and all of that work is invisible at the only moment it matters.

```
2 long · 0 short     ≈ 1.2 independent positions     ρ̄ 0.85
```

When three same-direction signals are live across the three assets, the strip states it plainly:

> *three long signals across BTC, ETH and SOL are approximately one position at 2.6× size*

This is the one place the dashboard editorialises, and it does so because the alternative — three
identical cards implying three independent opportunities — is actively misleading.

---

## 9. Degraded rendering

The five states of `specs/14` §4 must be **visibly different at a glance**, not by reading a label.

| State | Treatment |
|---|---|
| `HEALTHY` | Normal |
| `WARMING` | Panels present with progress bars; signal areas explicitly "warming", not empty |
| `DEGRADED` | Affected panels get a `--stale` left rule and name the failing source; healthy panels unchanged |
| `STALE` | Numbers desaturated, age shown in place of the value's normal label, source named. **A `TRIGGERED` signal still shows stop and invalidation** (`specs/07` §10) — a user holding a position needs those most when data is bad |
| `DOWN` | Full-width `--down` banner, all signal panels dimmed and non-interactive, last-known-good timestamp shown |

**Stale must look obviously wrong.** Subtle greying is not sufficient — the failure this prevents
is a person reading an old number as current.

Freshness appears as **age, not a timestamp**: "4s ago", turning amber past 60 s and red past
300 s.

---

## 10. Refresh cadence

| Region | Interval | Endpoint |
|---|---|---|
| Events (state transitions) | 2–3 s | `/api/events?since=` |
| Status bar | 5 s | `/ui/status` |
| Asset strip | 5 s | `/ui/asset/{asset}` |
| Action bar | on event, plus 5 s | `/ui/panel/action` |
| Class panels | 15 s | `/ui/panel/{class}` |
| Exposure strip | 15 s | `/ui/exposure` |

HTMX `hx-trigger="every Ns"` with `hx-swap="outerHTML"`. Changed values flash once for 200 ms.

---

## 11. Copy rules

- **Sentence case throughout.** No all-caps labels.
- **Active voice, plain terms.** "needs 34% win rate to break even", not "Breakeven Probability
  Threshold: 0.34".
- **Emptiness gives direction, not mood.** "no setup — variance ratio 1.18, needs < 1.00", never
  "Nothing here right now."
- **Errors are specific and do not apologise.** "futures price feed stopped responding 4m ago",
  not "Something went wrong."
- **One job per element.** No label that merely restates the value beneath it.

---

## 12. Hard prohibitions

1. **No order-placement affordance of any kind.** No buy/sell button, no form posting anywhere,
   no exchange deep link that could be mistaken for one. This is a suggestion system; there must
   be no path from this UI to an order. Enforce by test: no `<form>`, no `method="post"`, no
   outbound link to an exchange trade route.
2. **No P&L display.** The system does not know whether the user entered (`specs/07` §8.1). Any
   P&L figure would be fabricated.
3. **No performance statistics before Phase 16.** Nothing has been validated; there is nothing
   honest to show.
4. **No control strategies rendered** — `tradeable = False` is filtered at the service layer
   (`specs/14` §9), and a UI test asserts it.
5. **No decorative gradients, no shadow-as-hierarchy, no card chrome that does not encode state.**

---

## 13. Charts

lightweight-charts, **detail view only**. Twelve charts on one screen is noise, and a chart per
card would push the actionable numbers below the fold.

The detail chart shows the signal's timeframe with stop, entry/trigger, T1 and the trail level
drawn as horizontal lines, and the setup's arming point marked. Nothing else — no indicator
overlays, since the conditions are already stated in words.

---

## 14. Tests

| Test | Asserts |
|---|---|
| **No trading affordance** | No `<form>`, no POST target, no exchange trade link anywhere in rendered output |
| **Controls absent** | No `tradeable = False` signal renders, against a backtest run that produced them |
| **Confidence null** | Renders the reason plus `breakeven_hit_rate`; never blank, never "—" |
| **Unvalidated separation** | Tier B signals render in the labelled sub-section with distinct treatment, not inline |
| **ENTER_NOW relocation** | Snapshot test: the signal leaves its class panel and appears in the action bar |
| **Empty action bar** | Renders explanatory copy, never an empty region |
| **Empty panel** | Renders the binding condition |
| **Stale distinctness** | Snapshot diff between healthy and stale is large, not subtle |
| **Stale + triggered** | Stop and invalidation still render |
| **Warm-up** | Progress bars and ETAs render; counts include backfilled bars |
| **Exposure strip** | With three same-direction signals, the concentration statement appears |
| **Tabular figures** | CSS applies `font-variant-numeric: tabular-nums` to every numeric element |
| **Reduced motion** | With `prefers-reduced-motion`, the change-flash is suppressed |
| **No colour-only meaning** | Every state distinguishable with colour removed — verify with a greyscale snapshot |

---

## 15. Acceptance criteria for Phase 15

1. All six failure modes in §1 are specifically prevented, each with a test.
2. `ENTER_NOW` is distinguished by **position, size and wording** before colour.
3. A quiet market, a warming system and a broken system are visibly different at a glance.
4. `confidence: null` never renders blank; `breakeven_hit_rate` appears in its place.
5. Tier B signals are structurally separated, not badged.
6. Empty panels state the binding condition.
7. The exposure strip states the concentration in plain language when it binds.
8. No order-placement affordance exists anywhere, verified by test.
9. Every numeric element uses tabular figures.
10. Greyscale rendering preserves every state distinction.
11. Charts appear only in the detail view.
12. No build step: templates and static assets only, per `specs/10`.
