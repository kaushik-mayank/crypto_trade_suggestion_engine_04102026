# HANDOVER — Driving the build with Claude Code

Everything needed to take this from twenty spec documents to a running system.

---

## 1. What you have

| File | Purpose |
|---|---|
| `CLAUDE.md` | Root. Auto-loaded by Claude Code every session. **The control panel** |
| `HANDOVER.md` | This file |
| `specs/00-capability-map.md` | What the API actually provides. Authoritative over vendor docs |
| `specs/01-verification.md` | Probe suite — 17 unknowns to resolve empirically |
| `specs/01b-recorder.md` | The cold-start recorder |
| `specs/02-features-and-regime.md` | Derived-metric catalogue, timeframes, regime model |
| `specs/03-class1-correction.md` | Class 1 + the ACT entry primitive |
| `specs/04-class2-participation.md` | Class 2 |
| `specs/05-class3-transition.md` | Class 3 |
| `specs/06-class4-established.md` | Class 4 + the benchmark and control suite |
| `specs/07-arbitration-and-lifecycle.md` | Scoring decision, arbitration, state machine, confidence |
| `specs/08-risk-engine.md` | Vetoes, sizing, leverage, costs |
| `specs/09-validation-protocol.md` | The pre-registered validation protocol |
| `specs/10-architecture.md` | Stack, snapshot boundary, data model, audit schema |
| `specs/11-ingestion-normalization-storage.md` | Implementation |
| `specs/12-feature-and-regime-engine.md` | Implementation |
| `specs/13-strategy-risk-audit.md` | Implementation |
| `specs/14-backend-api.md` | Implementation |
| `specs/15-dashboard.md` | Implementation |
| `specs/16-execution-runbook.md` | How to run validation, with halt gates |
| `specs/17-shadow-run-observability.md` | Shadow run design |
| `specs/18-deployment-security.md` | Repo safety, hosting, deployment |
| `specs/19-operations-governance.md` | Running it, and change control |

**Put `CLAUDE.md` at the repo root and everything else in `specs/`. Commit all of it before
starting.** The agent reads files; don't paste spec contents into chat — that burns context you
need for the work.

---

## 2. ⚠ Spec numbers are not build order

This trips people up. Specs 02–09 are **design** documents that implementation phases consume.
The actual build order is:

| Step | Work | Specs to read | Gate before moving on |
|---|---|---|---|
| **1** | Scaffold, HTTP client, **recorder**, probes, backfill | `00`, `01`, `01b` | All tier-1 and tier-2 probes answered; recorder running ≥72h |
| **2** | Resolve Phase 2's seven `⧗ PENDING` branches | `02` §1 | Each branch resolved and recorded |
| **3** | Ingestion, normalization, storage | `00` §5, `11` | Every hazard test passing on real captured payloads |
| **4** | Features + regime engine | `02`, `12` | `compute_series == compute_at`; regime validity tests run |
| **5** | Strategies, arbitration, risk, audit | `03`–`08`, `13` | Mutual exclusivity asserted; `replay_exact` passes |
| **6** | Backend API | `14` | Five system states distinguishable |
| **7** | Dashboard | `15` | No trading affordance; greyscale test passes |
| **8** | **Run validation** | `09`, `16` | Halt gates passed; decisions recorded |
| **9** | Shadow run | `17` | 8–12 weeks; go/no-go per strategy |
| **10** | Deploy | `18` | All eight recovery drills |
| **11** | Operate | `19` | — |

**Start the recorder in step 1 and never stop it.** The funding and positioning history it
captures cannot be backfilled from anywhere, and the clock on Tier B validation starts when it
does.

---

## 3. The control panel

Claude Code loads `CLAUDE.md` automatically each session, so its status table is how the agent
orients itself. **Change exactly one line to unlock a step.**

```markdown
| Phase | Spec | Status |
|---|---|---|
| 0 — Capability map | specs/00 | Reference — always applicable |
| 1 — Verification + recorder | specs/01, 01b | **ACTIVE** |
| 2–19 | specs/02 … specs/19 | LOCKED |

LOCKED means: read for context so you don't build an incompatible interface.
Do not implement. Only the ACTIVE phase gets built.
```

---

## 4. Kickoff prompt

Paste this as your first message:

> This repo is a crypto trading **suggestion** system for BTC/ETH/SOL on CoinDCX. It never places
> orders.
>
> Read `CLAUDE.md` first, then `specs/00-capability-map.md`, `specs/01-verification.md`,
> `specs/01b-recorder.md`. Skim `HANDOVER.md` §2 for the build order.
>
> **Execute step 1 only.** Everything else is LOCKED — read for context so you don't build
> incompatible interfaces, but do not implement.
>
> Order of work:
> 1. Repo scaffold per `CLAUDE.md`, with `.gitignore` containing `.env` and `data/` **before the
>    first commit**.
> 2. The shared HTTP client (`specs/01` §0). Everything depends on it.
> 3. The cold-start recorder (`specs/01b`), with `data/stats` wired but **disabled** until probe
>    P8 passes. Start it running as soon as it passes its acceptance tests.
> 4. Probes in the stated priority order. **P1 first** — candle history depth bounds the entire
>    validation framework.
> 5. One-time historical backfill, after P1 establishes real depth.
>
> You may build in parallel without waiting on probes: the parameter-registry config file, feature
> module interfaces (signatures and warm-up accounting only, no logic), and the
> backfill-vs-live equivalence test harness.
>
> Write every probe result to `docs/findings.md` with the UTC date, the exact request, and the raw
> evidence. **If a probe contradicts `specs/00-capability-map.md`, the probe wins** — update the
> spec and flag the correction.
>
> Stop after step 1 and report findings.

---

## 5. Unlock messages

Flip the status line in `CLAUDE.md`, then send the matching message.

**Step 2 — resolve pending branches** (before any Phase 2 code):

> Phase 1 complete. Before unlocking anything: resolve every `⧗ PENDING` branch in `specs/02` §1
> against your probe results. For each, state which branch you're taking and why, and record it in
> `docs/findings.md`. Report back before writing feature code.

**Step 3 — ingestion:**

> Ingestion is now ACTIVE. Implement `specs/11-ingestion-normalization-storage.md`. Read
> `specs/00` §5 first — the eleven normalization hazards are the substance of this phase, and each
> needs a test backed by a **real captured payload**, not a synthetic one. Note the two revisions
> in §1: no WebSocket, and the recorder does not retire. Stop at §13.

**Step 4 — features:**

> Features are now ACTIVE. Implement `specs/12` against `specs/02`, Tier A families only (1–5, 9
> bar-volume). Build Tier B interfaces returning null until their data accumulates.
>
> Order: features → §7 redundancy protocol → §6 regime model with all three validity tests → §8
> look-ahead tests. **The regime model may legitimately fail** — if it does, engage the degraded
> mode in `specs/12` §10 and report the failure rather than relaxing the tests. Stop at §15.

**Step 5 — strategies:**

> Strategies are now ACTIVE. Implement `specs/13` against `specs/03`–`08`.
>
> Build the **ACT primitive first and once** (`specs/03` §2) — nine consumers import it unchanged,
> and a second copy is a defect. Then C1-B (the only immediately backtestable strategy), then the
> rest.
>
> Assert all four mutual-exclusivity partitions. Stop at §17.

**Steps 6–7 — API and UI:** unlock `specs/14`, then `specs/15`, one at a time.

**Step 8 — validation:**

> Validation is now ACTIVE. Follow `specs/16` **exactly in stage order**.
>
> **Stage 1 is a halt gate — complete it before viewing any strategy's P&L.** If the permutation
> test doesn't collapse to the control distribution, or C4-X2 performs well, HALT and report.
>
> Reserve the revision folds in Stage 2 and do not run them in Stage 5. Use the holdout once, at
> Stage 10, after decisions are final.
>
> Fill `docs/validation_report.md` using the §6 template. Check every red flag in §3.

---

## 6. Verify before each unlock

| Before unlocking | Check |
|---|---|
| Step 3 | `docs/findings.md` has an entry per probe with raw evidence. Recorder uptime confirmed |
| Step 4 | Every hazard test uses a real payload. The `B-NKN_USDT` fixture exists |
| Step 5 | `compute_series == compute_at` passes for every feature |
| Step 6 | One ACT implementation in the import graph. `replay_exact` green |
| Step 8 | No trading affordance in the UI. Controls absent from every API response |
| Step 9 | Stage 1 halt gates passed **before** results were viewed — verify by commit ordering |

---

## 7. Session hygiene

1. **One step per session.** Long sessions degrade — the agent starts pattern-matching instead of
   reading.
2. **`docs/findings.md` is the memory.** Anything learned that isn't written there is lost at
   session end.
3. **Commit and tag after each step** (`git tag step-1-complete`). You will need rollback points
   when a probe invalidates an assumption three steps later.
4. **If the agent builds ahead, that's a context problem, not a judgment problem.** Fresh session,
   point at the status table.
5. **When a probe contradicts a spec, the probe wins.** Expect it at least once and treat it as
   the system working.
6. **Reference paths, never paste specs.**

---

## 8. Timeline — what actually gates what

| Milestone | Realistic timing | Gated by |
|---|---|---|
| Steps 1–7 built | Weeks | Development |
| **Feature warm-up** | **Immediate if backfill is deep** | Warm-up counts *available bars*, not elapsed time (`specs/14` §5) |
| Validation (step 8) | Days after step 7 | History depth (probe P1) |
| Shadow run | **+8–12 weeks** | Needs ≥30 matched closed trades |
| Tier B validation | **recorder_start + 6 months** | Funding and positioning history accumulating now |

**Backfill removes the warm-up wait entirely.** The only genuinely unavoidable delays are the
shadow run and Tier B accumulation — which is why the recorder starts in step 1, not step 10.

---

## 9. Four things only you can do

1. **Provide a 24/7 host** for the recorder (`specs/18` §2 — a ~$5/month VPS in Mumbai or
   Singapore). The recorder can run on your laptop meanwhile, accepting gaps.
2. **Create the read-only API key** (`specs/18`; creation steps were given in Phase 0). No trading
   permission, no withdrawal permission. **Never paste it into chat** — it goes in `.env`.
   Not needed until probe P8.
3. **Decide the outcomes the agent cannot.** When a strategy fails its benchmark, `specs/09` §12
   says CUT, not tune. When the regime model fails, `specs/12` §10 says degrade, not relax. The
   agent will follow the specs; you have to be willing to accept what they produce.
4. **Keep the trial counter honest** (`specs/19` §2). It never resets, and nothing can enforce
   that but you.

---

## 10. One thing to hold onto

The specs say this in several places, and it's the thing most likely to be lost between here and
a running system:

> **Nothing in this system has been validated. Twelve strategies rest on mechanism reasoning
> alone. Three of them are predicted to be cut.**

`specs/16` exists to find out which. The value of the work so far is that the finding-out will be
honest — pre-registered, halt-gated, benchmarked against random entry, and with a report format
designed so "this doesn't work" can be stated plainly.

Build it so that answer, whatever it is, is trustworthy.
