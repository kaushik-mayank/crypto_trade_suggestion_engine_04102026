# crypto_trader

A suggestion system for BTC / ETH / SOL on CoinDCX (spot and USDT perpetuals). It **never places orders**.
Read `CLAUDE.md` and `specs/` first. `docs/findings.md` records what has been verified against the live API.

**Status:** Phase 1. The shared HTTP client, the cold-start recorder, all probes and the one-time backfill are built.

## Setup (once)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

**Always run the project with the venv's Python.** The package is installed into `.venv`, not
into your system Python. That is why plain `python -m recorder` fails with `No module named recorder`.
Either call `.\.venv\Scripts\python.exe` explicitly, as in every command below, or activate the venv
first with `.\.venv\Scripts\Activate.ps1`. If PowerShell blocks activation scripts, use the explicit path.

## Environment variables and keys

**Nothing in the project currently requires a key.** Every endpoint in use is public. That includes
positioning (`data/stats`), which the vendor docs describe as signed but which serves the full payload
to a plain GET (findings F-019).

The code reads exactly two variables, both optional, from the process environment or from `.env`:

| Variable | Used for | Needed when |
|---|---|---|
| `COINDCX_API_KEY` | Sent as the `X-AUTH-APIKEY` header on signed requests | Only for probe P8 part B (checking whether signed and unsigned `data/stats` agree), or if you set `auth = "signed"` for `[streams.positioning]` in `recorder.toml` |
| `COINDCX_API_SECRET` | Never sent. Signs the exact request body with HMAC-SHA256 to produce `X-AUTH-SIGNATURE` | Same as above |

`.env.example` is the committed template. `.env` is your local copy: it is gitignored, and was created
with both values empty. To enable the signed checks, put your values after the `=` in `.env`:

```
COINDCX_API_KEY=<the API key string from CoinDCX>
COINDCX_API_SECRET=<the API secret shown once when the key was created>
```

Use a key with **read-only access: no trading, no withdrawal or transfer permission** (CLAUDE.md
invariant 11). A process environment variable with the same name overrides the file. An empty value
means "no credentials": the signed probes are skipped, and nothing else changes.

## Recorder

```powershell
.\.venv\Scripts\python.exe -m recorder                  # foreground; Ctrl-C stops it cleanly
```

To run it in the background so it outlives the terminal (this is how the 72 h acceptance run was started):

```powershell
$root = (Get-Location).Path
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine = "`"$root\.venv\Scripts\python.exe`" -m recorder --config `"$root\recorder.toml`""
  CurrentDirectory = $root }
```

To stop the background copy **cleanly**, create a stop file. The recorder notices within about a
second, flushes, writes the `shutdown` marker, and exits:

```powershell
New-Item -ItemType File data\raw\_meta\STOP
```

(A stale `STOP` left from before a start is ignored and removed.) A forced stop with `Stop-Process`
is also safe. The next start detects the gap, records it, and the hourly candle task
fills candle gaps of up to 7 days. Only one recorder can write to a data directory at a time:
a lock file (`_meta/recorder.lock`) makes a second copy exit immediately.

All settings are in `recorder.toml`.

### Output (`data/raw/`, gitignored)

```
<stream>/<YYYY-MM-DD>.jsonl        today's file: plain, append-only, fsynced per record
<stream>/<YYYY-MM-DD>.jsonl.gz     completed days: gzip of the exact same bytes
_meta/gaps.jsonl                   startup/shutdown markers and every detected gap
_meta/errors.jsonl                 every non-2xx, transport error and malformed body (raw bytes in base64)
_meta/quality.jsonl                hourly per-stream rollup (probe P13)
_meta/compression.jsonl            one line per compressed day: raw bytes, gz bytes, sha256 of the raw bytes
```

**Compression is lossless and verified.** When a UTC day ends, and at every start-up, each completed
day file is gzipped to a temporary file. That file is decompressed again and its sha256 compared with
the original. Only then is it renamed into place and the `.jsonl` removed. The recorded bytes,
including torn crash fragments, are never altered.

Read either kind of file with `recorder.storage.iter_records(path)`, or `gzip.open(path, "rt")`.

Streams: `futures_rt` (S1), `positioning` (S2), `spot_ticker` (S3), `instrument_meta` (S4),
`futures_book` (S5), `spot_book` (S6), and `{spot,futures}_candles_<res>` (S7, one stream per resolution).

Every record has this shape:

```json
{"stream":"futures_rt","pair":"B-BTC_USDT","sent_at_ms":…,"received_at_ms":…,"http_status":200,
 "source_ts_ms":…,"request_url":"https://…","payload":{…verbatim…}}
```

- `payload` is the exact source text, sliced out of the response. It is never re-serialized (findings F-003).
  For S1 it is `{"ts":…,"vs":…,"row":{…}}`. For S3 it is the single ticker row. For everything else it is the whole body.
- `request_url` is an addition to the spec's envelope. It records exactly what was asked.
- `source_ts_ms` is the response's own timestamp and is not validated. S3's ticker `timestamp` is in seconds and is
  multiplied by 1000 here. Its payload keeps the original value.

## Probes (`src/probes/`, specs/01-verification.md)

```powershell
.\.venv\Scripts\python.exe -m probes.p08_positioning          # part A public; part B only if .env has values
.\.venv\Scripts\python.exe -m probes.p01_history_depth
.\.venv\Scripts\python.exe -m probes.p02_rate_limits          # ramps to 10 req/s; stops at first 429
.\.venv\Scripts\python.exe -m probes.p03_spot_markets
.\.venv\Scripts\python.exe -m probes.p04_p05_futures_candles
.\.venv\Scripts\python.exe -m probes.p04b_resolution_survey
.\.venv\Scripts\python.exe -m probes.p06_volume_units
.\.venv\Scripts\python.exe -m probes.p07_p17_trades
.\.venv\Scripts\python.exe -m probes.p09_p10_basis_sampler --hours 4.5
.\.venv\Scripts\python.exe -m probes.p11_alignment
.\.venv\Scripts\python.exe -m probes.p12_aggregation
.\.venv\Scripts\python.exe -m probes.recorder_analysis --since 2026-09-19T10:26:00Z   # P13-P16 over recorder data
.\.venv\Scripts\python.exe -m probes.recorder_analysis --depth-param                   # P14 live part
.\.venv\Scripts\python.exe -m probes.reliability_outage       # recorder acceptance test 3 (~19 min)
```

Results go to `data/probe_results/<probe>_<UTC stamp>.json`. Raw responses go to `data/raw/probe/`.
Conclusions are written up in `docs/findings.md`.

## One-time backfill

```powershell
.\.venv\Scripts\python.exe -m backfill                   # all pairs x products x served resolutions
.\.venv\Scripts\python.exe -m backfill --only futures:B-BTC_USDT:60 --force
```

Output:
- `data/backfill/{product}/{pair}/{res}.parquet`: `time_ms`, plus OHLCV as `decimal128(38,18)`. Only closed bars.
- `data/backfill/raw/...jsonl.gz`: every response page, verbatim.
- `data/backfill/summary.json`: bars, first/last bar, and internal gaps per series.

Re-run it with `--force` whenever P1's answer changes.

## Layout differences from CLAUDE.md

- `src/cdx_http/` instead of `src/http/`: a top-level package named `http` shadows the standard library.
- `src/backfill/` holds the one-time backfill. The recorder config is `recorder.toml` at the repo root.
