# 00 — CoinDCX API Capability Map

**Status:** authoritative. Supersedes the vendor documentation wherever they disagree.
**Evidence tags:** `[DOC]` documented · `[VERIFIED]` confirmed against the live API
· `[UNVERIFIED]` documented but untested · `[UNKNOWN]` not addressed anywhere.

Verification performed 2026-09-18 against the live public API.

---

## 1. Surfaces

| Host | Product | Auth | Role |
|---|---|---|---|
| `https://api.coindcx.com` | Spot + Futures | mixed | Main REST |
| `https://public.coindcx.com` | Futures | none | Futures candles, book, RT prices |
| `wss://stream-spot.coindcx.com` | Spot | mixed | Socket.IO — **not used in v1** |
| `wss://stream.coindcx.com` | Futures | mixed | Socket.IO — **not used in v1** |

Pair format: `{ecode}-{target}_{base}`, e.g. `B-BTC_USDT`. Known ecodes: `B`, `I`, `G`, `KC`.
`B` dominates. The futures glossary references **"TPE" (Third-Party Exchange)** and supplies
separate send-time stamps (`btST`, `bmST`), i.e. much of this data is **relayed from an upstream
venue**, not matched natively on CoinDCX. Consequences in §7.

---

## 2. Instruments — VERIFIED

All three target instruments exist as active USDT-margined perpetuals `[VERIFIED]`:

- `B-BTC_USDT`
- `B-ETH_USDT`
- `B-SOL_USDT`

Spot markets exist for all three as `B-BTC_USDT`, `B-ETH_USDT`, `B-SOL_USDT` (ecode B, active, `min_notional` 5 USDT) `[VERIFIED]` P3, findings F-007. No non-B USDT markets exist; ecode I has INR-quoted markets only.

### Live `B-BTC_USDT` instrument metadata `[VERIFIED]`

| Field | Live value | Doc sample | Note |
|---|---|---|---|
| `maker_fee` | **0.0236** | 0.025 | percent |
| `taker_fee` | **0.059** | 0.075 | percent → round-trip taker **0.118%** |
| `price_increment` | **0.1** | 0.01 | tick size |
| `quantity_increment` | **0.001** | 0.1 | |
| `min_trade_size` | **0.001** | 0.1 | |
| `min_notional` | **60.0** | 6.0 | USDT |
| `multiplier_up` / `_down` | **4.0** | 8.0 | **limit orders capped at ±4% of LTP** |
| `funding_frequency` | 8 | 8 | hours |
| `max_leverage` tier top | 100x (≤20k) | 25x | full tier table returned |
| maintenance margin (≤20k) | **0.5%** | 1.5% | from `dynamic_safety_margin_details` |

**The documentation's sample values are wrong for BTC.** Fetch per instrument at runtime.
`multiplier_up = 4.0` is a real constraint on live-entry pricing: a resting limit entry more
than 4% from last price is rejected.

---

## 3. Market data endpoints

### Spot (public)

| Endpoint | Params | Returns | Limits |
|---|---|---|---|
| `GET /exchange/ticker` | — | `market, change_24_hour, high, low, volume, last_price, bid, ask, timestamp` | all markets; regenerated 1×/sec `[DOC]`. **No bid/ask sizes.** |
| `GET /exchange/v1/markets` | — | array of market name strings | |
| `GET /exchange/v1/markets_details` | — | precision, min/max qty, `min_notional`, `step`, `order_types`, `ecode`, `pair`, `status` | |
| `GET /market_data/trade_history` | `pair`, `limit` | `p, q, s, T, m` | default 30, max 500 `[DOC]` |
| `GET /market_data/orderbook` | `pair`, `depth` *(lowercase — capital `Depth` → 422, see findings F-001)* | `timestamp, bids{}, asks{}` | default 50 levels `[VERIFIED]`; 20, 50 `[VERIFIED]`; {1,5,10,100,200} `[UNVERIFIED]` |
| `GET /market_data/candles` | `pair`, `interval`, `startTime`, `endTime`, `limit` | `open, high, low, close, volume, time` | **default 500 `[VERIFIED]`**, max 1000 `[DOC]` |

**Spot candles verified behaviour:**
- Sorted **descending** by `time` `[VERIFIED]`
- `time` in **milliseconds**, bar **open** time `[VERIFIED]`
- Default return is **500 bars** `[VERIFIED]` (499-minute span on `interval=1m`)
- `volume` is **base-denominated** (BTC) `[VERIFIED]` — ~1–100 BTC per 1m bar
- No trade count, no quote volume
- Intervals: `1m`, `15m`, `1h`, `1d` only `[DOC]`

### Futures

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=USDT` | none | array of pair strings `[VERIFIED]` |
| `GET .../data/instrument?pair=&margin_currency_short_name=` | none | full instrument metadata `[VERIFIED]` |
| `GET .../data/trades?pair=` | none | `price, quantity, timestamp, is_maker` — **fixed 30 rows (~7 s), oldest-first; `limit` ignored** `[VERIFIED]` F-011 |
| `GET public.coindcx.com/market_data/v3/orderbook/{pair}-futures/{depth}` | none | `ts, vs, bids{}, asks{}` — depth ∈ {10,20,50} `[VERIFIED]`; **5 and 100 return 200 with an EMPTY book** ⚠ F-014 |
| `GET public.coindcx.com/market_data/candlesticks?pair=&from=&to=&resolution=&pcode=f` | none | `{s, data:[{open,high,low,close,volume,time}]}` — `from`/`to` in **seconds**, `time` in **ms**, ascending; **silently truncated to the OLDEST 30,000 bars** ⚠ F-009; empty range → `s:"no_data"`; `from>to` → 400 |
| `GET public.coindcx.com/market_data/v3/current_prices/futures/rt` | none | per-pair funding, mark, 24h stats `[VERIFIED]` |
| `GET /api/v1/derivatives/futures/data/stats?pair=` | ⚠ **none** (corrected, F-019) | price change, high/low, **long/short positioning** `[VERIFIED]`. POST → 404. Unknown/missing pair → 200 with empty objects |
| `POST /api/v1/derivatives/futures/data/conversions` | **signed** | USDT↔INR rate (INR margin only — not needed) |

Futures REST candle resolutions: ⚠ CORRECTED — `'1'`, `'5'`, `'15'`, `'30'`, `'60'`, `'240'`, `'480'`, `'1D'`, `'3D'`, `'1M'` `[VERIFIED]` (F-008). `3/120/360/720/1W` → 400. (Doc said `1, 5, 60, 1D` only.)

---

## 4. `current_prices/futures/rt` — field semantics VERIFIED

The vendor doc leaves most of these blank. Resolved empirically:

| Field | Meaning | Evidence |
|---|---|---|
| `fr` | **Funding rate, current 8h interval** | `[VERIFIED]` |
| `efr` | **Predicted next funding rate, bounded ±0.02** | `[VERIFIED]` |
| `mp` | Mark price | `[VERIFIED]` |
| `ls` | Last price | `[DOC]` glossary |
| `h` / `l` | 24h high / low | `[DOC]` |
| `v` | 24h volume, **quote-denominated (USDT notional)** | `[VERIFIED]` |
| `pc` | 24h price change percent | `[DOC]` |
| `mkt` | Upstream market symbol | `[VERIFIED]` |
| `btST` / `bmST` | Upstream tick / mark send time (ms) | `[DOC]` |
| `ctRT` / `cmRT` | CoinDCX receive time (ms) | `[INFERRED]` |
| `skw` | **NOT a health flag** — one value shared by every fresh row in a snapshot, drifting over time; meaning `[UNKNOWN]` (corrected 2026-09-18, findings F-004) | `[VERIFIED]` |

**Evidence for `fr`/`efr`:** values across the book cluster on `0.00005` and `0.0001`
(the standard 0.005% / 0.01%-per-8h baselines, consistent with `funding_frequency: 8`);
`fr` and `efr` differ but co-move, consistent with realized-vs-predicted; and a stressed
instrument (`B-ONE_USDT`, `pc: +148%`) showed `efr` pinned at exactly `-0.02`, the
canonical ±2% funding cap. A cap that precise only exists on a funding rate.

**⚠ CORRECTED 2026-09-18 (findings F-004) — `skw` is not a health flag.** The original
evidence (every live row `skw: 0`, non-zero only on stale rows) was a snapshot taken when
the shared live value happened to be 0. Repeated sampling shows every fresh row carrying
the **same** `skw` (e.g. all 496 = 40, then all 495 = 24 nine seconds later), drifting
over time; frozen rows retain whatever value was current when they froze. Freshness must
be judged by `btST`/`bmST` age only. Rejecting on `skw != 0` would reject live BTC/ETH/SOL rows.

*Original (superseded) text:* every actively-updating instrument returned `skw: 0`. Non-zero values
(`36`, `21`, `64`, `52`) appeared only on instruments whose `btST` was weeks-to-months stale.

### Observed live values (2026-09-18, for reference only)

| Pair | `fr` | `efr` | `mp` | `ls` | `skw` |
|---|---|---|---|---|---|
| B-BTC_USDT | 0.00008505 | 0.0001 | 76539.667 | 76533.5 | 0 |
| B-ETH_USDT | 0.00005107 | 0.00008497 | 2451.92 | 2451.91 | 0 |
| B-SOL_USDT | 0.00008823 | 0.0001 | 101.03 | 101.03 | 0 |

### ⚠ Payload contains stale and malformed rows `[VERIFIED]`

The envelope carries a fresh `ts`; individual rows do not. The same response contained:

- **Frozen rows** with `btST` months old, served as "current prices"
  (`B-NKN`, `B-CHESS`, `B-HIPPO`, `B-FIO`, `B-PUFFER`, `B-ACX`, `B-DENT`, `B-OM`, ~20 more)
- **Schema-incomplete rows** — `B-NKN_USDT` returned only
  `{fr, mp, efr, bmST, cmRT, skw}`, with no `h`, `l`, `v`, `ls`, `pc`, or `mkt`

Per-row freshness validation and per-field presence checks are **mandatory**.

---

## 5. Normalization hazards

Every one of these is verified or explicitly documented. All will silently corrupt results.

| # | Hazard |
|---|---|
| 1 | Spot candles `startTime`/`endTime` in **ms**; futures candlesticks `from`/`to` in **seconds** |
| 2 | Auth `timestamp`: sample code uses ms, several doc tables say seconds — contradiction |
| 3 | Spot candles **descending** `[VERIFIED]`; futures candlesticks **ascending** `[VERIFIED]` F-008 |
| 4 | Spot candle `volume` = **base** `[VERIFIED]`; `current_prices` `v` = **quote** `[VERIFIED]`; futures candle volume = **base** `[VERIFIED]` F-010 |
| 5 | Order book is a JSON **object** keyed by price string; order not guaranteed; precision inconsistent |
| 6 | Depth update quantity `"0"` = delete level |
| 7 | `m` / `is_maker` is boolean in spot REST, `0`/`1` in spot WS, boolean in futures |
| 8 | ⚠ CORRECTED: spot orderbook depth param is lowercase `depth`; capital `Depth` returns **422** (findings F-001) |
| 9 | `data/stats`, `cross_margin_details`, `futures/wallets` documented POST but sample code uses **GET with a signed body** |
| 10 | One documented URL contains a double slash (`//exchange/v1/...`) |
| 11 | Socket.IO doc contradicts itself (`2.4.0` vs `4.x.x`, and names the server package) — moot, no WS in v1 |

---

## 6. Capability verdict

| Capability | Status |
|---|---|
| OHLCV spot + futures | ✅ |
| Order book depth | ✅ spot {1,5,10,20,50,100,200}, futures {10,20,50} (other futures values silently empty) |
| Trade tape with aggressor flag | ✅ spot 500 trades ≈ 25 s; futures fixed 30 trades ≈ 7 s — snapshot only, no per-bar reconstruction |
| Mark price | ✅ futures, real-time |
| Funding rate + predicted funding | ✅ **verified** |
| Long/short positioning ratio | ✅ snapshot only, **public GET** (F-019) |
| Exact fees / tick / lot / min-notional | ✅ per instrument |
| Maintenance margin & leverage tiers | ✅ |
| **Funding rate history** | ❌ must self-record |
| **Positioning history** | ❌ must self-record |
| **Open interest** | ❌ **absent entirely** |
| **Liquidation feed / history** | ❌ **absent entirely** |
| Index price (distinct from mark) | ❌ |
| Order book history | ❌ must self-record |
| Message-level order flow (adds/cancels) | ❌ |
| Options / implied volatility | ❌ |
| On-chain / news / social sentiment | ❌ |
| Cross-venue data | ❌ single venue |

**Strategy families ruled out by the data, not by preference:** anything requiring open
interest (OI/price divergence, true positioning change), anything requiring a liquidation
tape (cascade fade, liquidation-cluster targeting), anything requiring implied volatility.
Do not substitute proxies that pretend to measure these.

---

## 7. The TPE caveat

Spot `B-*` and futures `B-*` are both relayed from an upstream third-party exchange.
Therefore perp-vs-spot basis computed from these feeds is **that venue's basis**, not a
CoinDCX-local dislocation. It remains a real, tradable signal — it is the basis everyone
sees — but it must not be presented as a venue-specific inefficiency. Whether CoinDCX-internal
(`ecode I`) markets diverge is probe P9.

`btST`/`bmST` versus local receive time gives a **directly measurable relay latency**, which
is useful as a data-quality metric.

---

## 8. Rate limits, errors, auth

- **Documented rate limits cover only nine spot *order* endpoints.** No documented limit for
  any market-data or futures endpoint `[UNKNOWN]` — but `429` is a documented response, so
  limits exist. Discover empirically (probe P2); assume nothing.
- **Error codes `[DOC]`:** `400` bad request · `401` bad key · `404` · `429` too many requests
  · `500` server error · `503` **temporarily offline for maintenance**. Treat `503` as
  "back off long and stay alive", distinct from `500`.
- No structured error-body schema for market data — handle defensively.
- **Pagination:** `page`/`size` (default 100, max 1000), details "in the response header" —
  **header names never given** `[UNKNOWN]`. Account endpoints only; irrelevant to market data.
- **Auth:** HMAC-SHA256 over the exact JSON-serialized body, hex digest, sent as
  `X-AUTH-SIGNATURE` with `X-AUTH-APIKEY`. Serialization must be byte-stable
  (`separators=(',',':')`, no key reordering after signing).
- ⚠ CORRECTED (F-019): `data/stats` does **not** need credentials (a plain GET returns the full payload; credentials are not checked). Only `conversions` is still assumed to need them `[UNVERIFIED]`, and it is not used. Everything the project uses is public.
  v1 can be built and validated with no key at all.

---

## 9. Timeframe availability

| TF | Spot REST | Futures REST | Route for this project |
|---|---|---|---|
| 1m | ✅ | ✅ (`"1"`) | direct |
| 5m | ❌ | ✅ (`"5"`) | direct (futures) |
| 15m | ✅ | ✅ (`"15"`) | direct (corrected F-008) |
| 1h | ✅ | ✅ (`"60"`) | direct |
| 4h | ❌ | ✅ (`"240"`) | futures direct; spot aggregate 4×1h (exact, F-013) |
| 1d | ✅ | ✅ (`"1D"`) | direct |

Also served on futures only: 30m (`"30"`), 8h (`"480"`), 3d (`"3D"`), 1M (`"1M"`).
Everything the hourly/daily swing horizon requires is backfillable. UTC alignment is verified
for all of these (P11, F-012), and aggregation reproduces native bars exactly (P12, F-013).

---

## 10. Cost floor

| Product | Round-trip taker | Round-trip maker |
|---|---|---|
| Futures (BTC, verified) | **0.118%** | 0.047% |
| Spot (doc sample `fee: 0.2`) | ~0.4% `[UNVERIFIED]` | ~0.4% |

Baseline funding over a 1–3 day hold: roughly **0.03–0.09%** at the 0.005–0.01%/8h baseline.
But funding is capped at **±2% per interval** and does reach the cap on stressed instruments.

**Therefore funding is a tail risk, not a baseline drag.** The risk engine should ignore it
in the normal case and veto or resize hard in the extreme case. Any suggestion whose target
sits inside roughly 0.3% of entry is economically noise on futures and worse on spot.
