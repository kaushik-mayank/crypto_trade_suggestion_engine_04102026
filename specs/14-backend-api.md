# 14 — Backend API & Signal Lifecycle Service

**Depends on:** `specs/07` (lifecycle), `specs/10` (FastAPI, localhost binding), `specs/13`
(engines, audit, rejections).
**Serves:** `specs/15` (dashboard).

---

## 1. Prime directive: the API reads, it never computes

> **Engines write. The API reads. No signal logic, no feature computation, no risk evaluation
> lives behind an HTTP handler.**

A slow or repeated API query must never delay signal evaluation, and a dashboard refresh must
never change a signal's content. The two paths touch only through the store.

---

## 2. One service layer, two renderings

`specs/10` chose Jinja2 + HTMX, so the dashboard consumes HTML fragments. A JSON API is still
required — for debugging, replay tooling, tests, and any future client.

Both render from **one service layer**. The services hold the logic; the two renderings are thin.

```python
class SignalService:
    def live(filters) -> SignalSet
    def detail(signal_id) -> SignalDetail
    def audit(signal_id) -> SignalAudit
    def history(filters, cursor) -> Page[SignalSummary]
    def events_since(ms) -> list[LifecycleEvent]

class MarketService:
    def asset_state(asset) -> AssetState
    def regime(asset) -> RegimeState
    def rejections(asset) -> list[StrategyRejection]

class HealthService:
    def health() -> HealthReport
    def warmup() -> WarmupReport
```

Duplicating logic between the JSON and HTML paths is a defect — assert by having the fragment
handlers call the same service methods.

---

## 3. Freshness has three layers, and they must be distinguishable

A user looking at a number needs to know whether it is current, old but valid, or meaningless.

| Layer | Question | Where answered |
|---|---|---|
| **Data** | How old is the market data behind this? | `FreshnessEnvelope.sources` |
| **System** | Is ingestion working at all? | `FreshnessEnvelope.system_status` |
| **Display** | When did this browser last refresh? | Client-side, rendered as age |

### The envelope — attached to every response

```python
class FreshnessEnvelope:
    served_at_ms: int
    data_as_of_ms: int                  # newest data underlying this response
    age_seconds: float
    sources: {name: {last_update_ms, age_s, status, consecutive_failures}}
    system_status: HEALTHY | WARMING | DEGRADED | STALE | DOWN
    degraded_reasons: list[str]
```

**Render age, never a bare timestamp.** "Updated 4s ago" is actionable; "14:32:07" makes the user
do arithmetic to find out whether the system is alive.

### ⚠ The cache trap

A short cache on the live endpoint (§11) is safe and useful. But `age_seconds` must be computed
from `data_as_of_ms`, **never from when the cache entry was created.** A cached response that
reports itself as fresh is worse than a slow one — it turns a visible outage into an invisible
one. Test this explicitly.

---

## 4. Degraded-mode behaviour

`specs/10` §10 says the system emits nothing when in doubt. But "nothing" is ambiguous with "no
setups found," and those are entirely different states.

| State | Meaning | Presentation |
|---|---|---|
| **`NO_VALID_TRADE`** | Healthy. Conditions simply not met. | Normal panel, plus the **binding condition** per strategy (§7) |
| **`WARMING`** | Features not yet warm | Progress, not emptiness (§5) |
| **`DEGRADED`** | Some sources failing, others fine | Panels that depend on healthy sources render normally; panels that depend on failing sources are marked with the named source |
| **`STALE`** | Data present but too old to trust | Greyed, age shown, source named. **Triggered signals still display stop and invalidation** (`specs/07` §10) |
| **`DOWN`** | Ingestion broken | Loud banner, all signal panels disabled, last-known-good timestamp shown |

> **The single most important distinction: a quiet market and a broken system must never look the
> same.** An empty dashboard with no explanation is indistinguishable from a healthy market with
> no setups, and a user cannot act correctly on that ambiguity.

Partial degradation is genuinely partial: if `data/stats` is failing, Tier B strategies (C1-A,
C2-C) are marked unavailable with the reason, while Tier A panels continue normally.

---

## 5. Warm-up is a first-class state, not an error

This matters more at launch than anything else in this phase.

Per `specs/02` §9 and `specs/12` §5, the system needs roughly **60 days** before 1h features are
warm and **180 days** before 1d features are. During that period it will legitimately produce
almost nothing.

> **For its first two months, this system's correct behaviour is to show very little. The UI must
> make that legible as progress rather than as failure.**

```
GET /api/warmup →
{
  "by_timeframe": {
    "1h": { "required": 1440, "available": 431, "pct": 30, "eta_utc": "..." },
    "1d": { "required": 180,  "available": 54,  "pct": 30, "eta_utc": "..." }
  },
  "by_feature": { "TRD_TSTAT@1h": {...}, ... },
  "regime_model": { "status": "WARMING", "required": 2880, "available": 431 },
  "strategies_available": ["..."],
  "strategies_warming": [{ "id": "C2-A", "blocked_by": ["TRD_TSTAT@1h"], "eta_utc": "..." }]
}
```

Backfilled history (`specs/11` §7.3) shortens this substantially — warm-up counts **available
bars**, not elapsed wall-clock since install. State that in the UI, because the difference
between "60 days from now" and "as soon as backfill completes" is large.

Also surfaced here: `earliest_tier_b_validation` from `specs/09` §13, so a user seeing
`validated: false` can see **when that could change.**

---

## 6. State transitions and the `ENTER_NOW` problem

### The problem

`ENTER_NOW` is momentary — it holds only while price stays within the measured spread plus one
tick of `E(t)` (`specs/07` §8.2). A dashboard polling every 5 seconds can miss it entirely.

### The resolution

Missing a momentary `ENTER_NOW` by seconds is tolerable at an hourly horizon — the
`TRIGGERED` + `remaining_rr` path exists precisely for that, and the `MISSED` rule retires the
signal once geometry decays. But the user should be **alerted**, not left to notice.

```
GET /api/events?since={ms} → [ {signal_id, from_state, to_state, at_ms, asset, strategy_id} ]
```

Cheap and small, so the client polls it every 2–3 seconds while polling full panels far less
often. On a new `ENTER_NOW` event the client fires a browser notification (optional sound) and
refreshes the affected panel.

No email, SMS or push infrastructure — this is a local, single-user system, and adding a delivery
channel would add a failure domain and a secret for no benefit.

---

## 7. "Why is there no signal?" — an endpoint most systems cannot offer

`specs/13` §4's no-short-circuit rule means every rejection carries **all** condition results.
That makes a genuinely useful question answerable:

```
GET /api/assets/{asset}/rejections →
[ { strategy_id: "C1-B",
    evaluated_at_ms: ...,
    binding_conditions: [ {name: "MR_VR(1h) < 1", value: 1.18, threshold: 1.0} ],
    passed_count: 5, total_count: 7 } ]
```

The dashboard renders this under each empty panel, so `NO_VALID_TRADE` is accompanied by
*why* — "waiting on: variance ratio 1.18, needs < 1.00".

This is the operational payoff of the evaluate-everything discipline, and it is also what makes
the Phase 9 frequency hypotheses (H1, H5, H10) inspectable live rather than only in backtest.

---

## 8. Three levels of signal detail

Audit records are large (`specs/10` §8). Do not send them to a panel.

| Level | Contents | Served by |
|---|---|---|
| **`SignalSummary`** | Asset, strategy, direction, state, entry/trigger, stop, T1, R:R, `remaining_rr`, strength, `breakeven_hit_rate`, `validated`, freshness | Panels |
| **`SignalDetail`** | Summary + all conditions, regime, reasoning text, invalidations, corroborations, suppressed conflicts, sizing, leverage, liquidation, cost breakdown | On open |
| **`SignalAudit`** | The complete Phase 13 trace: every feature value, every veto with inputs, `X*` history, levels pre/post rounding, parameter hash, code version | Behind an explicit "why?" action |

The audit level is the debuggability requirement made **reachable by a human**, not just
queryable by SQL.

---

## 9. Endpoint catalogue

### JSON

| Endpoint | Returns |
|---|---|
| `GET /api/signals/live` | Current set, grouped by class and asset, with envelope |
| `GET /api/signals/{id}` | `SignalDetail` |
| `GET /api/signals/{id}/audit` | `SignalAudit` |
| `GET /api/signals/{id}/replay` | `replay_current` diff (`specs/13` §10) |
| `GET /api/signals/history` | Cursor-paginated `SignalSummary` |
| `GET /api/events?since=` | Lifecycle transitions |
| `GET /api/assets/{asset}` | Price, regime, volatility, funding, basis, DQ |
| `GET /api/assets/{asset}/rejections` | §7 |
| `GET /api/regime` | Regime state per asset, model version, degraded flag |
| `GET /api/health` | `HealthReport` |
| `GET /api/warmup` | §5 |

### HTMX fragments

`GET /ui/status` · `GET /ui/panel/{class}` · `GET /ui/asset/{asset}` ·
`GET /ui/signal/{id}` · `GET /ui/rejections/{asset}`

### Filtering

`tradeable = False` is filtered **at the service layer**, not per endpoint, so control strategies
cannot leak through a new route added later (`specs/13` §13).

---

## 10. Errors — absence of data is not absence of signals

> **A store failure must never render as an empty dashboard.**

An empty panel means "healthy market, no setups." A failed query means "we don't know." Conflating
them tells the user the market is quiet when in fact the system is blind — the single most
dangerous thing this API can do.

- Structured errors only. Never a stack trace, never a 500 with a traceback.
- A service failure returns `system_status: DOWN` with the reason, **not** an empty list.
- Partial failures return what succeeded plus an explicit list of what did not.
- Every error carries a correlation id that appears in the logs.

---

## 11. Caching, performance, security

**Caching.** Live signals change at most once per minute (the 1m trigger cadence), so a 1–2 s
cache on `/api/signals/live` is safe. The cache stores the **envelope alongside the payload**, so
`age_seconds` reflects data age and not cache age (§3).

**Pagination.** Cursor-based on `(created_at_ms, signal_id)`, stable under concurrent writes.
Offset pagination is rejected — it skips or duplicates rows when new signals arrive mid-page.

**Security.** Per `specs/10` §9:
- Binds `127.0.0.1` by default.
- **The app refuses to bind a non-localhost address unless an auth token is configured.** Exposure
  without authentication is not a supported configuration, not merely discouraged.
- No secret appears in any response, including errors and the audit view.
- No endpoint accepts a parameter that reaches the strategy or risk engines. The API is read-only
  in the strongest sense: it cannot influence a signal.

---

## 12. Module layout

```
src/api/
├── app.py            FastAPI app, lifespan, bind guard
├── envelope.py       FreshnessEnvelope construction — used by BOTH renderings
├── services/         signal.py · market.py · health.py   ← the logic
├── routes/           json.py · fragments.py               ← thin
├── models/           SignalSummary · SignalDetail · SignalAudit
├── cache.py          short TTL, envelope-aware
└── errors.py         structured errors, correlation ids
```

---

## 13. Tests

| Test | Asserts |
|---|---|
| **Cache vs freshness** | A cached response reports **data** age, not cache age. Freeze data, advance clock, assert age grows |
| **Degraded states distinguishable** | `NO_VALID_TRADE`, `WARMING`, `DEGRADED`, `STALE`, `DOWN` produce visibly different responses |
| **Store failure** | Returns `DOWN` with a reason, **never** an empty signal list |
| **Partial degradation** | With `data/stats` failing, Tier A panels are normal and Tier B are marked unavailable with the source named |
| **Controls firewalled** | No endpoint returns `tradeable = False`, exercised against a backtest run that produced controls |
| **Bind guard** | Non-localhost bind without a token refuses to start |
| **Pagination stability** | No skips or duplicates when signals are inserted mid-pagination |
| **Events completeness** | Every lifecycle transition in a window appears in `/api/events`, exactly once |
| **STALE triggered signal** | A `TRIGGERED` signal that goes `STALE` still returns stop and invalidation levels |
| **No logic duplication** | Fragment handlers call the same service methods as the JSON routes |
| **Warm-up accuracy** | Warm-up counts available bars including backfill, not elapsed wall-clock |

---

## 14. Parameters — zero

Running total: **30 → 30. Unchanged.** Cache TTLs, poll intervals and page sizes are operational
configuration; they affect timeliness, never a signal's content.

---

## 15. Acceptance criteria for Phase 14

1. No signal, feature or risk computation occurs behind any HTTP handler.
2. Every response carries a `FreshnessEnvelope`, and `age_seconds` is data age under caching.
3. The five system states are distinguishable in responses and cannot collapse into one another —
   in particular, a quiet market and a broken system produce different output.
4. `/api/warmup` reports per-timeframe and per-strategy progress, counts backfilled bars, and
   surfaces `earliest_tier_b_validation`.
5. `/api/events` captures every lifecycle transition exactly once and is cheap enough to poll at
   2–3 s.
6. `/api/assets/{asset}/rejections` returns the binding condition per strategy.
7. Three detail levels exist, and audit is reachable through a user action rather than only by SQL.
8. A store failure returns `DOWN`, never an empty list.
9. `tradeable = False` filtering happens at the service layer.
10. The app refuses to bind a non-localhost address without an auth token.
11. JSON and HTML renderings share one service layer, verified by inspection.
