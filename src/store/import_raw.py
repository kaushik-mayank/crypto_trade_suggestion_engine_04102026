"""Import the recorder's raw JSONL archive into SQLite (specs/11 §8.1).

The recorder writes verbatim response bytes and never interprets them (specs/01b). This is the step
that interprets them: each record is handed to the same normalizer the live pipeline uses, and
accepted records are upserted. So the archive stays the irreplaceable source, SQLite stays the
queryable source of truth, and **the import path and the live path share one interpretation** — if
they diverged, a backtest would be reading something the live system never saw.

It is also the largest real test the normalizers get: every recorded payload, including the ones from
outages and maintenance windows, run through them at once. A rejection here is a finding, so the
summary reports rejections by reason rather than counting them as noise.

Idempotent: re-importing a day upserts the same rows, so a partial run can simply be repeated.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.errors import ConfigError, CryptoTraderError
from core.logging import get_logger, setup_logging
from normalize import book as book_norm
from normalize import futures_rt as rt_norm
from normalize import instrument as instrument_norm
from normalize import positioning as pos_norm
from normalize import ticker as ticker_norm
from normalize.base import PayloadContext
from normalize.canonical import Product
from normalize.errors import Partial, Rejected
from store import open_store

log = get_logger("store.import_raw")
COMPONENT = "store.import_raw"

ASSETS = ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")
#: The ticker's ``market`` name -> our asset id.
MARKETS = {"BTCUSDT": "B-BTC_USDT", "ETHUSDT": "B-ETH_USDT", "SOLUSDT": "B-SOL_USDT"}

#: Freshness windows used **only** to decide what the recorder captured at the time, not to gate now:
#: a row that was stale when recorded is still stale history, and importing it as fresh would be a
#: lie. Generous, because the point is to preserve what was seen, and the snapshot builder re-judges
#: freshness against ``as_of`` on every read.
IMPORT_MAX_AGE_MS = 15 * 60_000

#: Streams this importer understands. Candles are deliberately absent: they arrive through the
#: backfill importer and the live pipeline, both of which apply the two-fetch finalization rule that
#: a one-shot import cannot.
STREAMS = ("futures_rt", "spot_ticker", "positioning", "spot_book", "futures_book", "instrument_meta")


@dataclass
class StreamResult:
    stream: str
    files: int = 0
    records: int = 0
    accepted_rows: int = 0
    stored_rows: int = 0
    rejected_payloads: int = 0
    rejected_rows: int = 0
    torn_lines: int = 0
    non_200: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str, n: int = 1) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + n

    def to_dict(self) -> dict[str, Any]:
        return {"stream": self.stream, "files": self.files, "records": self.records,
                "accepted_rows": self.accepted_rows, "stored_rows": self.stored_rows,
                "rejected_payloads": self.rejected_payloads, "rejected_rows": self.rejected_rows,
                "torn_lines": self.torn_lines, "non_200": self.non_200,
                "reasons": dict(sorted(self.reasons.items(), key=lambda kv: -kv[1]))}


def read_records(path: Path, result: StreamResult) -> Iterator[dict[str, Any]]:
    """Yield records from a plain or gzipped day file.

    A torn final line is expected, not exceptional: the recorder appends, and a file open when the
    process died ends mid-line. It is counted and skipped; anything else raises.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                result.torn_lines += 1
                log.debug("torn_line", file=str(path), lineno=lineno, bytes=len(line))


def _context(record: dict[str, Any], endpoint: str, path: Path, product: Product | None = None
             ) -> PayloadContext:
    return PayloadContext(
        endpoint=endpoint,
        received_at_ms=int(record.get("received_at_ms") or 0),
        sent_at_ms=int(record.get("sent_at_ms") or 0),
        request_url=str(record.get("request_url") or ""),
        asset=record.get("pair"),
        product=product,
        raw_ref=f"{path.name}",
    )


def _payload_bytes(record: dict[str, Any], stream: str) -> bytes:
    """The bytes to hand the normalizer, rebuilt from the recorder's stored slice.

    The recorder does not store whole responses for the fan-out endpoints: ``current_prices/futures/rt``
    carries ~540 pairs and is polled every 15 s, so the recorder stores the **verbatim byte slice** for
    each tracked pair and nothing else (`specs/01b`; `recorder.tasks.FuturesRtTask`). Its archive shape
    is therefore per-pair:

    | stream | stored shape | what the normalizer parses |
    |---|---|---|
    | ``futures_rt`` | ``{"ts":…,"vs":…,"row":{…}}`` | ``{"ts":…,"prices":{pair:{…}}}`` |
    | ``spot_ticker`` | one ticker object | an array of ticker objects |
    | others | as the endpoint returned it | the same |

    So the two fan-out streams are **rewrapped into their original container**, with the row's own
    values untouched. That keeps one interpretation of the data — the live normalizer — rather than a
    second parser that could drift from it. Rewrapping the container is not reinterpretation; parsing
    the row differently here would be.
    """
    payload = record.get("payload")
    if stream == "futures_rt" and isinstance(payload, dict) and "row" in payload:
        pair = record.get("pair")
        payload = {"ts": payload.get("ts"), "vs": payload.get("vs"),
                   "prices": {str(pair): payload["row"]}}
    elif stream == "spot_ticker" and isinstance(payload, dict):
        payload = [payload]
    return json.dumps(payload, separators=(",", ":"), default=str).encode()


def import_stream(store: Any, stream: str, raw_dir: Path, *, since: str | None = None,
                  until: str | None = None) -> StreamResult:
    result = StreamResult(stream=stream)
    folder = raw_dir / stream
    if not folder.is_dir():
        log.info("stream_absent", stream=stream, folder=str(folder))
        return result

    for path in sorted(folder.iterdir()):
        day = path.name.split(".")[0]
        if not path.is_file() or (since and day < since) or (until and day > until):
            continue
        result.files += 1
        for record in read_records(path, result):
            result.records += 1
            if int(record.get("http_status") or 0) != 200:
                result.non_200 += 1
                result.note(f"http_{record.get('http_status')}")
                continue
            try:
                stored = _dispatch(store, stream, record, path, result)
            except CryptoTraderError as e:
                # A store failure is not a data-quality event: it means the import itself cannot
                # continue, and swallowing it would produce a silently partial history.
                log.error("import_failed", stream=stream, file=str(path), error=e.to_dict())
                raise
            result.stored_rows += stored
    log.info("stream_imported", **result.to_dict())
    return result


def _dispatch(store: Any, stream: str, record: dict[str, Any], path: Path,
              result: StreamResult) -> int:
    raw = _payload_bytes(record, stream)

    # A stored slice holds exactly one pair, so the normalizer is asked for that pair only. Asking
    # for all three would report the other two as PAIR_ABSENT on every record -- thousands of
    # "rejections" that are an artefact of the loop, and which would bury a real one.
    pair = record.get("pair")

    if stream == "futures_rt":
        if pair not in ASSETS:
            result.note("UNTRACKED_PAIR")
            return 0
        outcome = rt_norm.normalize(raw, _context(record, "futures_rt", path, Product.FUTURES),
                                    assets=(pair,), max_age_ms=IMPORT_MAX_AGE_MS)
        return _store(store.upsert_futures_rt, outcome, result)

    if stream == "spot_ticker":
        market = next((m for m, a in MARKETS.items() if a == pair), None)
        if market is None or pair is None:
            result.note("UNTRACKED_PAIR")
            return 0
        outcome = ticker_norm.normalize(raw, _context(record, "spot_ticker", path, Product.SPOT),
                                        markets={market: str(pair)})
        return _store(store.upsert_spot_ticker, outcome, result)

    if stream == "positioning":
        outcome = pos_norm.normalize(raw, _context(record, "positioning", path))
        return _store(store.upsert_positioning, outcome, result)

    if stream in ("spot_book", "futures_book"):
        product = Product.SPOT if stream == "spot_book" else Product.FUTURES
        ctx = _context(record, stream, path, product)
        fn = book_norm.normalize_spot if product is Product.SPOT else book_norm.normalize_futures
        outcome = fn(raw, ctx, max_age_ms=IMPORT_MAX_AGE_MS)
        return _store(store.upsert_books, outcome, result)

    if stream == "instrument_meta":
        outcome = instrument_norm.normalize(raw, _context(record, "instrument_meta", path))
        if isinstance(outcome, Rejected):
            result.rejected_payloads += 1
            result.note(outcome.error.code)
            return 0
        # markets_details returns every instrument; only the tracked ones are stored.
        records = tuple(r for r in outcome.records if r.asset in ASSETS)
        result.accepted_rows += len(records)
        return int(store.upsert_instrument(list(records))) if records else 0

    raise ConfigError(
        f"no importer for stream {stream!r}",
        component=COMPONENT, operation="dispatch",
        context={"stream": stream, "known": list(STREAMS)},
        likely_cause="a new recorder stream was added without an import path",
        remediation="add it to STREAMS and _dispatch, or exclude it explicitly",
    )


def _store(upsert: Any, outcome: Any, result: StreamResult) -> int:
    if isinstance(outcome, Rejected):
        result.rejected_payloads += 1
        result.note(outcome.error.code)
        return 0
    if isinstance(outcome, Partial):
        result.rejected_rows += len(outcome.rejected)
        for row in outcome.rejected:
            result.note(row.reason)
    result.accepted_rows += len(outcome.records)
    if not outcome.records:
        return 0
    return int(upsert(list(outcome.records)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import recorder raw JSONL into SQLite")
    ap.add_argument("--raw-dir", default="data/raw")
    ap.add_argument("--db", default="data/state.db")
    ap.add_argument("--streams", nargs="*", default=list(STREAMS))
    ap.add_argument("--since", help="earliest day file, YYYY-MM-DD")
    ap.add_argument("--until", help="latest day file, YYYY-MM-DD")
    args = ap.parse_args(argv)

    setup_logging(Path("logs"), log_name="import_raw")
    raw_dir = Path(args.raw_dir)
    if not raw_dir.is_dir():
        raise ConfigError(
            f"raw directory {raw_dir} does not exist",
            component=COMPONENT, operation="main",
            context={"raw_dir": str(raw_dir)},
            likely_cause="the recorder has never run here, or --raw-dir points elsewhere",
            remediation="run the recorder first, or pass the correct --raw-dir",
        )
    unknown = [s for s in args.streams if s not in STREAMS]
    if unknown:
        raise ConfigError(
            f"unknown stream(s): {', '.join(unknown)}",
            component=COMPONENT, operation="main",
            context={"unknown": unknown, "known": list(STREAMS)},
            likely_cause="a typo, or a stream that has no importer",
            remediation=f"choose from {list(STREAMS)}",
        )

    store = open_store(args.db)
    try:
        results = [import_stream(store, s, raw_dir, since=args.since, until=args.until)
                   for s in args.streams]
    finally:
        store.close()

    print(f"\n{'stream':16s} {'files':>5s} {'recs':>7s} {'rows':>7s} {'stored':>7s} "
          f"{'rej-pay':>7s} {'rej-row':>7s} {'torn':>5s}")
    for r in results:
        print(f"{r.stream:16s} {r.files:5d} {r.records:7d} {r.accepted_rows:7d} {r.stored_rows:7d} "
              f"{r.rejected_payloads:7d} {r.rejected_rows:7d} {r.torn_lines:5d}")
        for reason, n in list(r.to_dict()["reasons"].items())[:6]:
            print(f"    {reason}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
