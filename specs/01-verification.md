# 01 — Empirical Verification Suite

**Goal:** convert every remaining `[UNKNOWN]` in `specs/00-capability-map.md` into a recorded
fact, before any strategy is designed against assumptions.

**Output:** `docs/findings.md` — one entry per probe, with UTC date, exact request, raw
evidence, and the resolved answer. Plus fixtures in `tests/fixtures/`.

**Rule:** a probe that "looks fine" is not a result. Record the actual values.

---

## 0. Shared HTTP client (build this first)

Every probe uses one client. Getting it right once prevents subtle inconsistency across
seventeen scripts.

Requirements:

- **Adaptive token-bucket rate limiter**, per host. Start at a deliberately low rate
  (suggest 5 req/s to `api.coindcx.com`, 5 req/s to `public.coindcx.com`). Configurable.
- **Backoff:** exponential on `429`; longer, capped backoff on `503` (documented maintenance)
  with a distinct log line. Never hot-loop.
- **Raw capture:** every response body written verbatim to `data/raw/probe/<probe_id>/` with
  the request URL, request headers (**secrets redacted**), response status, response headers,
  local send time and local receive time. This is evidence; it is not optional.
- **No parsing in the client.** It returns bytes plus metadata. Probes parse.
- **Decimal-safe JSON parsing helper** available to probes
  (`json.loads(..., parse_float=Decimal)`).

---

## P1 — Candle history depth ⭐ HIGHEST PRIORITY

**Why it matters more than anything else here:** history depth bounds backtest length, which
bounds how many strategies can ever be validated. If spot 1h only goes back six months,
walk-forward testing is statistically weak and we need to know now, not in Phase 9.

**Question:** how far back does each endpoint actually serve, per resolution?

**Method — spot** (`GET /market_data/candles`):
Binary-search `startTime`. For each `interval` in `{1m, 15m, 1h, 1d}`:
1. Request with `startTime` = a clearly pre-listing epoch (e.g. 2017-01-01) and `limit=10`.
2. Record the **oldest `time`** actually returned.
3. Narrow until the true earliest available bar is bracketed.
Note: spot returns **descending**, so with a wide window you may receive the *newest* bars.
Use a narrow `startTime`/`endTime` window around the candidate boundary rather than relying
on `limit` alone.

**Method — futures** (`GET public.coindcx.com/market_data/candlesticks`):
Same approach with `from`/`to` in **seconds**, for each `resolution` in `{1, 5, 60, 1D}`.

**Run for all three pairs** — depth may differ per instrument (SOL listed later than BTC).

**Record:** earliest available bar timestamp per (product, pair, resolution); total bar count;
whether the endpoint errors, truncates, or silently returns recent data when asked for
out-of-range history.

**Decision it feeds:** backtest window length (Phase 9); whether daily-timeframe strategies
have enough observations to validate at all; warm-up periods for long-lookback features.

---

## P2 — Actual rate limits ⭐ HIGH PRIORITY

**Question:** what request rate triggers `429` on each market-data endpoint?

**Method:** controlled ramp. For each of `/exchange/ticker`, `/market_data/candles`,
`/market_data/orderbook`, `/market_data/trade_history`,
`public.../current_prices/futures/rt`, `public.../candlesticks`:
start at 1 req/s, increase stepwise, stop **immediately** on the first `429`. Record the rate
and burst size at which it triggered, and the recovery time before requests succeed again.

**Be a good citizen.** Ramp slowly, stop at the first `429`, do not repeat the test
unnecessarily, and never run it concurrently across endpoints.

**Record:** trigger rate, burst tolerance, recovery time, whether any `Retry-After` or
rate-limit headers are returned (undocumented — check).

**Decision it feeds:** the recorder's polling budget; backfill chunking; whether three pairs
× two products is comfortably within budget (it almost certainly is at this horizon).

---

## P3 — Spot market existence for BTC / ETH / SOL

**Question:** do spot markets exist for all three, and what are the exact `pair` strings?

**Method:** `GET /exchange/v1/markets_details`. Filter for
`target_currency_short_name ∈ {BTC, ETH, SOL}` and `base_currency_short_name == USDT`.

**Record:** for each match — `pair`, `ecode`, `status`, `min_notional`, `step`,
`base_currency_precision`, `target_currency_precision`, `min_quantity`.
Also record **every** ecode variant found for these three assets (there may be `B-`, `I-`,
and others).

**Decision it feeds:** whether the spot leg covers all three assets or only some; exact pair
strings for all downstream config; whether §P9 (ecode divergence) is even applicable.

---

## P4 — Futures candlestick sort order

**Question:** ascending or descending?

**Method:** request a known 24h window at `resolution=60` for `B-BTC_USDT`. Compare
`data[0].time` against `data[-1].time`.

**Record:** direction; also whether the `s` status field is always `"ok"` and what it returns
for an empty or invalid range.

**Decision it feeds:** normalization layer. Spot is descending `[VERIFIED]`; if futures is
ascending, that asymmetry must be handled explicitly rather than assumed away.

---

## P5 — Futures candlestick max bars per request

**Question:** the endpoint takes `from`/`to` but has no `limit`. Is there a server-side cap?

**Method:** request progressively wider windows at `resolution=1` (1m) — 1 hour, 1 day,
1 week, 1 month — and count returned bars versus expected.

**Record:** the bar count at which truncation begins, and whether truncation is silent
(returns fewer bars with `s: "ok"`) or signalled.

**Decision it feeds:** backfill chunking strategy. Silent truncation is the dangerous case —
it produces gap-riddled history that looks complete.

---

## P6 — Futures candlestick volume units

**Question:** base or quote denominated?

**Method:** pull one 1h `B-BTC_USDT` bar. Compute `volume × typical_price` and compare against
`volume` alone. At BTC ~76,500 the two differ by ~4–5 orders of magnitude, so this is
unambiguous. Cross-check against the same hour's spot 1h volume (known base-denominated) and
against `current_prices` `v` (known quote-denominated).

**Record:** the raw numbers and the conclusion.

**Decision it feeds:** every volume-derived feature. Getting this wrong makes volume features
meaningless without raising any error.

---

## P7 — Futures `data/trades` response cap

**Question:** how many trades are returned? Is there an undocumented limit param?

**Method:** call `GET /exchange/v1/derivatives/futures/data/trades?pair=B-BTC_USDT`, count
rows, and check the time span covered. Probe whether `limit` is silently accepted.

**Record:** row count, time span, whether ordering is newest-first, whether `limit` works.

**Decision it feeds:** whether the tape is usable for reconstructing per-bar aggressor volume,
or whether that must come from continuous polling.

---

## P8 — `data/stats` reachability and shape

**Question:** does the positioning endpoint work as documented, and how?

**Method:** *requires credentials — run only once `.env` is populated.* Test:
1. As documented in the sample: `requests.get(url, data=json_body, headers=...)`
   (GET with a signed body — unusual; many clients drop GET bodies).
2. As documented in the HTTP Request line: `POST`.
3. With the `timestamp` in **milliseconds**, then in **seconds** (the doc contradicts itself).

**Record:** which combination returns `200`; the full response for all three pairs; whether
`position.count_percent` and `position.value_percent` are actually populated; whether the
endpoint requires futures to be enabled on the account.

**Decision it feeds:** whether crowd-positioning features are available at all. If this
endpoint is unreachable, Class 1 loses its most distinctive input and must be redesigned
around funding and basis alone.

---

## P9 — ecode divergence: is `B-` really upstream-relayed?

**Question:** do CoinDCX-internal markets for these assets exist, and do they price differently
from the `B-` markets?

**Method:** from P3's results, identify any non-`B` ecode market for BTC/ETH/SOL. If one
exists, sample its last price and the corresponding `B-` last price **within the same second**
and record the spread. Repeat ~50 times over an hour.

**Record:** mean and distribution of the cross-ecode spread; whether the non-`B` market has
meaningful volume.

**Decision it feeds:** whether "basis" is an upstream-venue signal only (§7 of the capability
map) or whether a genuine CoinDCX-local dislocation exists. This determines how honestly
Class 1 can be framed.

---

## P10 — Synchronous basis measurement

**Question:** what is the actual distribution of perp-mark minus spot-last, measured properly?

**Why this needs its own probe:** an earlier informal comparison showed a ~120-point gap
between spot and futures BTC that turned out to be **entirely explained by a ~6.5-minute
fetch gap during which price moved**. Basis computed from unsynchronized fetches measures
latency, not basis.

**Method:** poll `current_prices/futures/rt` (for `mp` and `ls`) and `/exchange/ticker`
(for spot `last_price`) as close together as possible. Record the local send/receive time of
each and the upstream `btST`. **Discard any sample pair whose timestamps differ by more than
2 seconds.** Collect ≥500 valid samples across ≥4 hours.

**Record:** basis in basis points — mean, standard deviation, min, max, and its relationship
to concurrent `fr`. Also record what fraction of attempts had to be discarded for timestamp
misalignment (this is itself a useful latency metric).

**Decision it feeds:** whether basis has enough signal-to-noise to support a Class 1 strategy,
and the maximum acceptable timestamp tolerance for any cross-source feature.

---

## P11 — Bar boundary alignment

**Question:** do 1h and 1d bars align to UTC boundaries, on both products?

**Method:** pull 1h and 1d bars for all three pairs on both products. Check every `time`
value modulo 3600000 ms and 86400000 ms.

**Record:** any non-zero remainder; whether spot and futures agree; whether daily bars open
at 00:00 UTC or some other offset (IST-offset boundaries are plausible given the exchange's
home market and would be a significant finding).

**Decision it feeds:** aggregation validity (P12), and whether daily-timeframe features mean
what we assume.

---

## P12 — Aggregation exactness

**Question:** does aggregating fine bars reproduce native coarse bars exactly?

**Method:** two tests.
1. **Spot:** aggregate 15× `1m` bars → compare against the native `15m` bar for the same
   window. Aggregation rule: `open` = first open, `high` = max high, `low` = min low,
   `close` = last close, `volume` = sum.
2. **Futures:** aggregate 12× `5m` → compare against a native `15m`… which futures REST does
   not serve. So instead aggregate 12× `5m` → compare against the **spot** native `15m` for
   shape (not equality, different books), and aggregate 60× `1m` → native `60` on futures for
   exact equality.

Run over ≥200 windows, including at least one known low-volume overnight period.

**Record:** exact-match rate; the nature and size of any discrepancy; whether missing 1m bars
(zero-volume minutes) are omitted from the response rather than returned with zero volume
— **this is the likeliest failure mode and it silently breaks aggregation.**

**Decision it feeds:** whether 4h (aggregated from 1h) and futures 15m (aggregated from 5m)
are trustworthy, or whether the timeframe stack must be reduced to natively-served
resolutions only.

---

## P13 — Gap, duplicate and continuity baseline

**Question:** how clean is the data over a sustained window?

**Method:** run for ≥72 hours as part of the recorder (see `01b-recorder.md`). For each
stream, count: missing bars, duplicate `time` values, out-of-order arrivals, rows failing
freshness validation, rows failing schema validation, HTTP error rates by code.

**Record:** rates per stream per day, plus the worst contiguous gap.

**Decision it feeds:** the data-quality veto thresholds in the risk engine (Phase 8), and
whether any stream is too unreliable to build on.

---

## P14 — Order book depth and spread

**Question:** what depth is actually returned, and what is the realistic spread for our three?

**Method:** request spot `depth=200` (lowercase — see F-001) and futures depth `50` for all three pairs, ~20 times
across different hours. Count levels actually returned. Compute best bid/ask spread in bps
and cumulative depth within 10/25/50 bps of mid.

**Record:** levels returned vs requested; spread distribution in bps; depth within bands;
whether the `Depth` parameter (capital D) actually works or is silently ignored.
*Partly answered 2026-09-18 (findings F-001): capital `Depth` → 422; lowercase `depth` works for 20 and 50. Still to test: 100, 200.*

**Decision it feeds:** the slippage model for Phase 9's cost assumptions, and the
liquidity-veto thresholds in Phase 8. **Not** an alpha input at this horizon.

---

## P15 — Server clock skew

**Question:** how far does the exchange's clock differ from local?

**Method:** for ~200 requests across several hours, compare the server-side timestamp in the
response (`timestamp` on spot ticker, `ts` on `current_prices`) against local receive time,
net of measured round-trip time.

**Record:** mean and max skew; drift over the session.

**Decision it feeds:** the freshness-validation window (invariant #2), and — if a key is ever
used — whether signed-request `timestamp` rejection is a risk. The futures order docs mention
a 10-second rejection window, which suggests the tolerance is tight.

---

## P16 — Spot liquidity reality check

**Question:** is CoinDCX spot deep enough that spot suggestions are actionable?

**Method:** from P14's depth data plus 24h spot volume from `/exchange/ticker`, compare spot
against futures notional volume for the same assets. Futures reference points already
captured: BTC ~11.2B, ETH ~9.15B, SOL ~1.89B USDT/24h.

**Record:** spot/futures volume ratio and depth ratio per asset.

**Decision it feeds:** honestly — whether the spot leg should be a *tradable* product at all,
or demoted to a pure cross-reference for basis and confirmation. If spot is an order of
magnitude thinner, suggesting spot entries at meaningful size would be misleading, and the
UI must say so.

---

## P17 — Spot trade tape aggressor flag

**Question:** is `m` populated and meaningful on spot `trade_history`?

**Method:** pull 500 trades for `B-BTC_USDT`. Count the `m` distribution; check it is not
constant. Cross-check that trades at the ask are `m: false` where the book is available.

**Record:** true/false split, any nulls, whether the split is plausible (~50/50 over a large
sample).

**Decision it feeds:** whether per-bar aggressor volume can be reconstructed from the tape.
Relevant only as a secondary feature at this horizon — deprioritize if time-constrained.

---

## Priority order

| Tier | Probes | Rationale |
|---|---|---|
| **Must complete before Phase 2** | P1, P2, P3, P4, P6, P11, P12 | These determine the timeframe stack and the backtest window. Phase 2 cannot be specified without them. |
| **Must complete before Phase 3** | P8, P9, P10 | These determine which Class 1 mechanisms are real. |
| **Runs continuously** | P13 | Part of the recorder. |
| **Before Phase 8** | P5, P14, P15, P16 | Risk-engine and cost-model inputs. |
| **Low priority** | P7, P17 | Secondary features at this horizon. |

---

## Completion criteria for Phase 1

1. Every tier-1 and tier-2 probe has an entry in `docs/findings.md` with raw evidence.
2. `specs/00-capability-map.md` is updated with every correction, and each correction is
   flagged in `docs/findings.md`.
3. `tests/fixtures/` contains at least: one `current_prices/futures/rt` payload with a stale
   row and a schema-incomplete row; one spot candle page; one futures candlestick page; one
   order book payload for each product.
4. The recorder has run ≥72 hours with a measured gap rate and has demonstrably recovered
   from a forced kill mid-write.
5. Full REST-available candle history is backfilled to `data/backfill/` for all three pairs,
   both products, all served resolutions.
6. Any probe that could not be completed is listed in `docs/findings.md` under
   **"Open questions"**, with its blocking reason and the downstream decision it leaves
   unresolved. Do not quietly skip one.
