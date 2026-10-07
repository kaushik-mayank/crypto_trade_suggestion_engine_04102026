"""Rebuild price history from the venue, straight into SQLite.

    python -m bootstrap --check              # what is missing, then exit. Makes no requests
    python -m bootstrap --run                # fetch what is missing (live profile)
    python -m bootstrap --run --profile full # everything the venue serves, for backtesting
    python -m bootstrap --verify             # re-fetch a sample and compare, then exit

This exists because **local data is not deployable**. The 5.8 GB database on the development machine
cannot be pushed to a repository (it holds the holdout, and `.gitignore` keeps it out for that
reason) and copying it to a host by hand is a step that can be done wrong silently. So a fresh host
rebuilds history from the API, and the only thing that has to be correct is this command.

**Safe to run repeatedly, and safe to interrupt.** The plan is recomputed from the database every
time, so:

* a complete database produces an empty plan and costs one query per series;
* an interrupted run is resumed by running it again — there is no marker to reset;
* chunks are fetched newest-first and written as they arrive, so an interruption leaves the *recent*
  history complete, which is the half the live system needs to run at all.

**It fetches futures candles only**, because that is all the app's snapshot request asks for. Tier B
(funding, positioning, order book) cannot be rebuilt at all: it is point-in-time with no archive, so
it only accumulates from the moment the ingest process starts. That is a real limitation with real
consequences for two strategies, and ``--check`` prints it rather than leaving it to be discovered.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from app.__main__ import build_request
from bootstrap.fetch import VERIFY_FRACTION, run_plan
from bootstrap.plan import Profile, plan
from config.settings import Settings
from core.errors import ALL_ERRORS, BootstrapError
from core.logging import get_logger, setup_logging
from ingest.client import IngestClient
from store import open_store

log = get_logger("bootstrap.main")

OUT = Path("data/probe_results/bootstrap.json")

#: Requests per second per host. The venue advertised 5,000 per 60 s on api.coindcx.com and did not
#: throttle up to 10/s (F-022), so 5/s is about 6% of the advertised budget — deliberately polite,
#: because a bootstrap competes with the live ingest process for the same budget.
DEFAULT_RATE = 5.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="rebuild price history from the venue into SQLite")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="report what is missing and exit; makes no requests")
    mode.add_argument("--run", action="store_true", help="fetch what is missing")
    mode.add_argument("--verify", action="store_true",
                      help="re-fetch a sample of what is stored and compare, then exit")
    ap.add_argument("--profile", choices=[p.value for p in Profile], default=Profile.LIVE.value,
                    help="live: enough for the app to run (minutes). full: everything the venue "
                         "serves, for backtesting (much longer, mostly the 1-minute series)")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE,
                    help="requests per second per host")
    ap.add_argument("--verify-fraction", type=float, default=VERIFY_FRACTION,
                    help="fraction of fetched chunks re-requested and compared")
    args = ap.parse_args(argv)

    settings = Settings()
    setup_logging(settings.paths.log_dir, log_name="bootstrap")
    # migrate=True: on a fresh host the database does not exist yet, and this is the command that
    # runs first. Creating the schema here is the difference between a one-command setup and a
    # documented ordering someone can get wrong.
    store = open_store(settings.paths.state_db, migrate=True)
    now_ms = int(time.time() * 1000)
    profile = Profile(args.profile)

    try:
        request = build_request()
        current = plan(store, request, now_ms=now_ms, profile=profile)

        if args.check:
            _print_plan(current)
            return 0 if current.complete else 1

        if args.verify:
            # Verify re-checks what is already stored by re-fetching a sample of every series'
            # window. An empty plan is the normal case here: there is nothing to fetch, and the
            # point is to compare what the venue says now against what was written earlier.
            full = plan(store, request, now_ms=now_ms, profile=profile)
            for job in full.jobs:
                job.missing = max(job.missing, 1)       # force every series to be visited
            client = IngestClient()
            summary = run_plan(store, client, full, now_ms=now_ms, verify_fraction=1.0)
            _write(summary | {"mode": "verify", "profile": profile.value})
            print(f"verified {summary['verified_chunks']} chunk(s); no mismatches")
            return 0

        if current.complete:
            print(f"nothing to do: every series is complete for the {profile.value} profile")
            _print_plan(current)
            return 0

        _print_plan(current)
        print(f"\nfetching {len(current.outstanding)} series at {args.rate}/s ...")
        client = IngestClient(host_rates={"api.coindcx.com": args.rate,
                                          "public.coindcx.com": args.rate})
        summary = run_plan(store, client, current, now_ms=now_ms,
                           verify_fraction=args.verify_fraction)

        after = plan(store, request, now_ms=int(time.time() * 1000), profile=profile)
        _write(summary | {"mode": "run", "profile": profile.value,
                          "plan_before": current.to_dict(), "plan_after": after.to_dict()})
        _print_summary(summary, after)
        return 0 if after.complete else 1

    except BootstrapError as e:
        print(f"BOOTSTRAP FAILED: {e.message}", file=sys.stderr)
        print(f"  likely cause: {e.likely_cause}", file=sys.stderr)
        print(f"  fix: {e.remediation}", file=sys.stderr)
        return 2
    except ALL_ERRORS as e:
        print(f"BOOTSTRAP FAILED: {e}", file=sys.stderr)
        return 2
    finally:
        store.close()


def _print_plan(current: Any) -> None:
    print(f"\nprofile: {current.profile.value}")
    print(f"{'series':28s} {'expected':>9s} {'present':>9s} {'missing':>9s}  status")
    for job in current.jobs:
        status = "complete" if job.complete else "incomplete"
        print(f"{job.key:28s} {job.expected:9d} {job.present:9d} {job.missing:9d}  {status}")
    print(f"\n{len(current.outstanding)} of {len(current.jobs)} series incomplete, "
          f"{current.missing_total} bars missing")
    print("\nTier B (funding, positioning, order book) cannot be rebuilt: it is point-in-time with "
          "no archive.\nIt accumulates only while `python -m ingest` is running, which is why C1-A "
          "and C2-C\nhave no history to be tested against.")


def _print_summary(summary: dict[str, Any], after: Any) -> None:
    print(f"\nrequests            {summary['requests']}")
    print(f"bars written        {summary['rows_written']}")
    print(f"  final immediately {summary['written_final_on_first_fetch']}")
    print(f"  left for the sweep{summary['written_pending_for_the_live_sweep']:>5d}")
    print(f"chunks verified     {summary['verified_chunks']} (no mismatches)")
    if summary["bar_mutations"]:
        print(f"BAR MUTATIONS       {summary['bar_mutations']} — a stored final bar came back "
              f"different; see the log")
    print(f"\nremaining incomplete: {len(after.outstanding)} series, {after.missing_total} bars")
    if after.outstanding:
        print("Bars still missing after a successful run are usually bars the venue does not have.")
        print("Re-run with --check; if the count is stable across runs, that is the venue's own "
              "history, not a failure here.")


def _write(payload: dict[str, Any]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {OUT}")


if __name__ == "__main__":
    sys.exit(main())
