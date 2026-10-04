# CLAUDE.md — Operating Instructions

You are the implementing engineer on a **crypto trading *suggestion* system** for BTC / ETH / SOL
on CoinDCX (spot + USDT-margined perpetual futures). Horizon: **hourly and daily swings**.

This system **never places orders**. It produces suggestions a human acts on.

Read `specs/00-capability-map.md` before writing any code that touches the API.
It is the authoritative record of what the API actually provides — not the vendor docs,
which contain errors this project has already verified.

---

## Project status

**Read `latest-updates.md` for the master plan, the active phase and the exact next action.**
It is the durable implementation state; this table is only the coarse lock.

| Phase | Spec | Status |
|---|---|---|
| 0 — Capability map | `specs/00` | Reference — always applicable |
| 1 — Verification + recorder | `specs/01`, `01b` | Complete (72 h unattended run pending a 24/7 host) |
| 2 — Branch resolution, parameter registry, config | `specs/02` §1, §10 | **ACTIVE** (`PHASE-01`) |
| 3–19 | `specs/03` … `specs/19` | LOCKED |

LOCKED means: read for context so you do not build an incompatible interface. Do not implement.
Only the ACTIVE phase gets built, and `latest-updates.md` names which of its phases is current.

**Do not build ahead of the specs.** Every spec 02–19 now exists, so "no spec" is no longer the
reason to stop — the phase lock is. Speculative scaffolding for a locked phase is worse than none.

**Deployment target: Microsoft Azure** (single B-series Linux VM, Central India). See
`latest-updates.md` §Azure architecture. Do not introduce managed databases, containers or
orchestration; the two-process topology of `specs/10` §3 is unchanged by the move to Azure.

---

## Hard invariants

These are not style preferences. Violating any of them produces silently wrong numbers,
which in a trading system is the worst possible failure mode.

### Data correctness

1. **Never hardcode instrument parameters.** `maker_fee`, `taker_fee`, `price_increment`,
   `quantity_increment`, `min_notional`, `multiplier_up`, `multiplier_down`, and the margin
   tier tables are fetched from `/derivatives/futures/data/instrument` at startup and
   refreshed periodically. The vendor documentation's sample values are **wrong for BTC**
   (verified: doc says taker 0.075, live BTC is 0.059).

2. **Never trust an envelope timestamp.** `current_prices/futures/rt` returns a fresh
   top-level `ts` while individual rows may be **months stale**. Validate every row against
   its own `btST` / `bmST`. Reject rows older than the configured freshness window.

3. **`skw` is not a health flag; judge freshness by `btST`/`bmST` age only.** Every fresh
   row in a snapshot carries the same `skw`, and the value drifts over time; frozen rows
   keep the value from when they froze. Never reject a row on `skw`. Exact semantics
   unknown (`docs/findings.md` F-004).

4. **Rows are not schema-uniform.** Some rows in `current_prices/futures/rt` omit
   `h`, `l`, `v`, `ls`, `pc`, `mkt` entirely (verified: `B-NKN_USDT`). Use explicit
   per-field presence checks. Never `row["ls"]` without a guard.

5. **Parse all prices and quantities as `Decimal`, never `float`.** Order book keys arrive
   as strings with inconsistent precision (`"11570.67000000"` vs `"2001"`).

6. **Order book payloads are JSON *objects*, not arrays.** Key order is not guaranteed.
   Sort numerically after parsing. A quantity of `"0"` in a depth update means *delete
   this level*.

7. **Units and ordering differ per endpoint.** See `specs/00-capability-map.md` §5.
   The three that bite hardest:
   - Spot candles: time in **milliseconds**, sorted **descending**, volume in **base**.
   - Futures candlesticks: `from`/`to` in **seconds**, volume units **unverified**.
   - `current_prices/futures/rt`: `v` is **quote-denominated (USDT notional)**.

8. **Raw-first.** The recorder stores unmodified payloads plus a local receive timestamp.
   It does not normalize, clean, or reshape. Normalization happens downstream where it can
   be re-run. A transform bug at ingest corrupts data permanently.

9. **Cross-source comparisons must be timestamp-aligned.** Computing basis from a spot
   price and a futures mark price fetched minutes apart measures latency, not basis.
   Reject any comparison whose inputs differ by more than the configured tolerance.

### Security

10. **Credentials live in `.env` only.** Never in code, commits, logs, error messages,
    exception traces, or test fixtures. `.env` is gitignored before the first commit.
11. **The API key has no trading or withdrawal permission.** Nothing in this codebase may
    call an order, cancel, transfer, or leverage endpoint. If you find yourself writing one,
    stop.
12. **Nothing this project uses needs authentication.** `data/stats` is **public** — a plain
    unauthenticated GET returns the full payload, and the documented POST is a 404 (verified:
    `docs/findings.md` F-019). Only `conversions` is still assumed to be signed, and it is not
    used. Keep the signed-request code isolated in `src/cdx_http/signed.py` anyway, and send
    credentials to nothing that does not require them.

### Architecture

13. **No WebSocket in v1.** REST polling only. This is a deliberate decision — at an
    hourly/daily horizon streaming buys nothing and costs a great deal of fragility.
    Do not add Socket.IO.
14. **Assume the process can die at any instant and restart hours later.** This runs on a
    laptop that sleeps. Append-only writes, gap detection on startup, idempotent resume.
15. **Adaptive rate limiting.** Public market-data rate limits are undocumented but `429`
    is real. Start conservative, back off on `429`, back off harder and longer on `503`
    (documented as a maintenance response).

---

## Repository layout

```
.
├── CLAUDE.md
├── README.md
├── .env.example            # committed, placeholders only
├── .gitignore              # must contain .env and data/
├── pyproject.toml
├── specs/                  # authoritative design docs, one per phase
├── src/
│   ├── cdx_http/           # shared client: rate limiting, retries, raw capture
│   │                       # (not `http/`: that name shadows the stdlib and breaks requests)
│   ├── probes/             # Phase 1 verification scripts
│   └── recorder/           # Phase 1 cold-start recorder
├── data/                   # GITIGNORED
│   ├── raw/                # recorder output, append-only
│   └── backfill/           # one-time historical candle pulls
├── tests/
│   └── fixtures/           # real captured payloads, committed
└── docs/
    └── findings.md         # accumulating verified facts, append-only
```

---

## Working rules

- **Every probe result is written to `docs/findings.md`** with the UTC date, the exact
  request made, and the raw evidence. This file is the project's memory. A finding not
  written down did not happen.
- **Capture real payloads as test fixtures.** Any payload that surprised you becomes a
  fixture in `tests/fixtures/`. Start with a `current_prices/futures/rt` response
  containing at least one stale row and one schema-incomplete row.
- **Tests assert against hand-computed values**, not against the code's own output.
- When a probe contradicts `specs/00-capability-map.md`, **the probe wins.** Update the
  spec, note the correction in `docs/findings.md`, and flag it prominently — a contradiction
  usually means something else nearby is also wrong.
- When something is ambiguous and the answer matters, **stop and ask** rather than choosing
  silently. Record the question in `docs/findings.md` under "Open questions".

---

## Environment

- Python 3.11+
- Runs locally first. Git remote added later. Hosting decided later.
- Keep dependencies minimal: an HTTP client, a dataframe/columnar library, a test runner.
  No framework commitments in Phase 1.
