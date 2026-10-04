"""P14 — order book spread and depth, sampled across hours at maximum depth.

Every ``--every-min`` minutes for ``--hours``: spot ``depth=200`` and futures depth 50 (the
futures maximum; F-014) for all three pairs. Raw bodies are appended to
``data/probe_results/p14_books_<stamp>.jsonl`` as they arrive; the summary (spread, reach,
depth within 10/25/50 bps where the book actually reaches, levels returned vs requested)
is saved at the end, or with ``--summarize <file>``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .common import DATA, FUTURES_BOOK, PAIRS, SPOT_BOOK, ProbeRun, now_ms, setup_logging
from .recorder_analysis import BANDS, _book_metrics, _stats

REQ = {"spot": 200, "futures": 50}


def summarize(path: Path) -> dict:
    per = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("status") != 200:
            per[(r["product"], r["pair"])].append({"error": r.get("status")})
            continue
        m = _book_metrics(json.loads(r["body"]))
        if m is None:
            per[(r["product"], r["pair"])].append({"error": "empty_book"})
            continue
        m["hour"] = datetime.fromtimestamp(r["received_at_ms"] / 1000, tz=UTC).hour
        per[(r["product"], r["pair"])].append(m)
    out = {}
    for (product, pair), ms in sorted(per.items()):
        ok = [m for m in ms if "error" not in m]
        out[f"{product}:{pair}"] = {
            "snapshots": len(ms), "errors": [m["error"] for m in ms if "error" in m],
            "distinct_utc_hours": len({m["hour"] for m in ok}), "levels_requested": REQ[product],
            "levels_bid": _stats([m["levels_bid"] for m in ok]), "levels_ask": _stats([m["levels_ask"] for m in ok]),
            "spread_bps": _stats([m["spread_bps"] for m in ok]), "reach_bps": _stats([m["reach_bps"] for m in ok]),
            "crossed_books": sum(m["crossed"] for m in ok),
            "depth_usdt_whole_book": _stats([m["depth_usdt_whole_book"] for m in ok]),
            **{f"depth_usdt_{b}bps": _stats([m[f"depth_usdt_{b}bps"] for m in ok if m[f"depth_usdt_{b}bps"] is not None])
               for b in BANDS},
            **{f"band_{b}bps_covered_fraction": round(sum(m[f"depth_usdt_{b}bps"] is not None for m in ok) / len(ok), 3)
               if ok else None for b in BANDS},
        }
    return {"samples_file": str(path), "books": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8)
    ap.add_argument("--every-min", type=float, default=20)
    ap.add_argument("--summarize", type=Path)
    ap.add_argument("--out", type=Path, help="append to this samples file (resume after an interruption)")
    args = ap.parse_args()
    setup_logging()
    run = ProbeRun("p14_books", capture=False)
    if args.summarize:
        run.save(summarize(args.summarize))
        return 0
    path = args.out or DATA / "probe_results" / f"p14_books_{run.stamp}.jsonl"
    deadline = time.time() + args.hours * 3600
    with open(path, "a", encoding="utf-8") as fh:
        while time.time() < deadline:
            t0 = time.time()
            for pair in PAIRS:
                for product, url, params in (("spot", SPOT_BOOK, {"pair": pair, "depth": REQ["spot"]}),
                                             ("futures", FUTURES_BOOK.format(pair=pair, depth=REQ["futures"]), None)):
                    try:
                        r = run.get(url, params, retry=False)
                        rec = {"product": product, "pair": pair, "status": r.status, "sent_at_ms": r.sent_at_ms,
                               "received_at_ms": r.received_at_ms, "url": r.url, "body": r.body.decode("utf-8")}
                    except Exception as e:  # noqa: BLE001 - recorded, not fatal
                        rec = {"product": product, "pair": pair, "status": None, "received_at_ms": now_ms(),
                               "error": type(e).__name__}
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    fh.flush()
            time.sleep(max(0.0, args.every_min * 60 - (time.time() - t0)))
    run.save(summarize(path))
    return 0


if __name__ == "__main__":
    from .common import run_logged
    sys.exit(run_logged("p14_book_sampler", main))
