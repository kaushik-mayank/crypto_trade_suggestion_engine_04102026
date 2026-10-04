"""Recorder acceptance 7 + gzip verification: measure disk growth and prove compression lossless.

    python -m probes.storage_check [--data data/raw]

For every ``<stream>/<day>.jsonl.gz``: decompress, recompute sha256, and compare with the
sha256 of the raw bytes that ``_meta/compression.jsonl`` recorded before the plain file was
removed. Also checks that no completed day is left uncompressed, and that no ``.gz.tmp`` is
left behind. Reports bytes on disk per stream and per UTC day (raw and compressed), and
projects a month.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from recorder.storage import DAY_RE

from .common import DATA, ProbeRun, setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA / "raw")
    args = ap.parse_args()
    setup_logging()
    run = ProbeRun("storage_check", capture=False)
    root = args.data
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    logged = {}
    comp = root / "_meta" / "compression.jsonl"
    if comp.exists():
        for line in comp.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if "sha256_raw" in r:
                logged[Path(r["file"]).as_posix().split("/")[-2] + "/" + Path(r["file"]).name] = r

    per_stream = defaultdict(lambda: {"plain_bytes": 0, "gz_bytes": 0, "gz_raw_bytes": 0, "files": 0})
    per_day_raw = defaultdict(int)
    per_day_disk = defaultdict(int)
    verified, mismatched, unlogged, uncompressed_past, tmp_left = [], [], [], [], []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "_meta"):
        tmp_left += [str(p) for p in d.glob("*.gz.tmp")]
        for f in sorted(d.iterdir()):
            m = DAY_RE.match(f.name)
            if not m:
                continue
            day = m.group(1)
            st = per_stream[d.name]
            st["files"] += 1
            size = f.stat().st_size
            per_day_disk[day] += size
            if f.suffix == ".gz":
                raw = gzip.decompress(f.read_bytes())
                st["gz_bytes"] += size
                st["gz_raw_bytes"] += len(raw)
                per_day_raw[day] += len(raw)
                key = f"{d.name}/{f.name}"
                h = hashlib.sha256(raw).hexdigest()
                if key not in logged:
                    unlogged.append(key)
                elif logged[key]["sha256_raw"] == h and logged[key]["raw_bytes"] == len(raw):
                    verified.append(key)
                else:
                    mismatched.append(key)
            else:
                st["plain_bytes"] += size
                per_day_raw[day] += size
                if day < today:
                    uncompressed_past.append(f"{d.name}/{f.name}")
    meta_bytes = sum(p.stat().st_size for p in (root / "_meta").glob("*.jsonl")) if (root / "_meta").exists() else 0

    tot_gz = sum(s["gz_bytes"] for s in per_stream.values())
    tot_gz_raw = sum(s["gz_raw_bytes"] for s in per_stream.values())
    full_days = sorted(d for d in per_day_raw if d < today)
    raw_per_full_day = [per_day_raw[d] for d in full_days]
    ratio = tot_gz_raw / tot_gz if tot_gz else None
    avg_raw_day = sum(raw_per_full_day) / len(raw_per_full_day) if raw_per_full_day else None
    run.save({
        "data_dir": str(root), "today_utc": today,
        "gz_files_verified_sha256": len(verified), "gz_files_mismatched": mismatched,
        "gz_files_without_compression_log": unlogged, "completed_days_left_uncompressed": uncompressed_past,
        "tmp_files_left": tmp_left,
        "PASS": not mismatched and not uncompressed_past and not tmp_left and not unlogged,
        "per_stream": dict(per_stream), "meta_bytes": meta_bytes,
        "raw_bytes_per_utc_day": dict(sorted(per_day_raw.items())),
        "disk_bytes_per_utc_day": dict(sorted(per_day_disk.items())),
        "compression_ratio_raw_over_gz": round(ratio, 2) if ratio else None,
        "full_days_measured": full_days,
        "raw_mb_per_month_projected": round(avg_raw_day * 30 / 1e6, 1) if avg_raw_day else None,
        "disk_mb_per_month_projected_after_gzip": round(avg_raw_day * 30 / ratio / 1e6, 1) if avg_raw_day and ratio else None,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
