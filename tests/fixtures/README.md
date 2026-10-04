# Fixtures

Real payloads captured from the live API on 2026-09-18. Bodies are byte-for-byte as received.

| File | Request | Why it matters |
|---|---|---|
| `futures_rt_2026-09-18T1237Z.json` | `GET https://public.coindcx.com/market_data/v3/current_prices/futures/rt` | 539 rows. Includes the stale, schema-incomplete `B-NKN_USDT` row (`skw:1`, `bmST` from Feb 2026) and ~20 frozen rows. BTC `fr` is sent as `0.00007903` (F-003). |
| `spot_ticker_2026-09-18T1237Z.json` | `GET https://api.coindcx.com/exchange/ticker` | 997 rows. All values are strings. `timestamp` is in seconds. |
| `futures_book_B-BTC_USDT_50_2026-09-18T1237Z.json` | `GET https://public.coindcx.com/market_data/v3/orderbook/B-BTC_USDT-futures/50` | Object-keyed levels with mixed key precision; keys are not sorted. |
| `spot_book_B-BTC_USDT_depth50_2026-09-18T1238Z.json` | `GET https://api.coindcx.com/market_data/orderbook?pair=B-BTC_USDT&depth=50` | Spot book, 50 levels per side. |
| `spot_book_Depth_422_2026-09-18T1237Z.json` | `GET https://api.coindcx.com/market_data/orderbook?pair=B-BTC_USDT&Depth=50` | The 422 returned for capital `Depth` (F-001). |
| `instrument_B-BTC_USDT_2026-09-18T1237Z.json` | `GET https://api.coindcx.com/exchange/v1/derivatives/futures/data/instrument?pair=B-BTC_USDT&margin_currency_short_name=USDT` | Live fees, tick, lot, min-notional. |
| `spot_candles_B-BTC_USDT_1h_2026-09-18T1237Z.json` | `GET https://api.coindcx.com/market_data/candles?pair=B-BTC_USDT&interval=1h&limit=3` | Descending, ms bar-open times. |
| `futures_candles_B-BTC_USDT_60_2026-09-18T1237Z.json` | `GET https://public.coindcx.com/market_data/candlesticks?pair=B-BTC_USDT&from=1789724278&to=1789735078&resolution=60&pcode=f` | Ascending, ms times, wrapped in `{"s":"ok","data":[...]}`. |
