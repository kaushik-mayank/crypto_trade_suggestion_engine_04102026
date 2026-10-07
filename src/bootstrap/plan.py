"""What history is needed, what is already stored, and therefore what must be fetched.

The whole point of this module is that **it is a function of the live snapshot request**, not a list
someone maintains. :func:`plan` derives its depth from the same :class:`snapshot.builder.SnapshotRequest`
the app will use, so the bootstrap cannot drift from what the app actually asks for. Change
``app.__main__.build_request`` and the bootstrap follows automatically; a hand-written list of
timeframes would silently stop matching and the symptom would be a series that is permanently
``WARMING`` on a host nobody is watching.

Two profiles, because the two reasons to fetch history are different sizes:

* ``LIVE`` — enough for the app to run and the regime classifier to have its trailing quantiles.
  Derived as ``request.bars × LIVE_MARGIN``, so it is self-consistent by construction. Minutes.
* ``FULL`` — back to the earliest bar the venue serves, which is what a backtest needs. Tens of
  minutes to hours, almost all of it the 1-minute series.

**Idempotent by design.** The plan is recomputed from the database every time, so running the
bootstrap on a complete database produces an empty plan and costs one query per series. There is no
"have I already done this" marker to go stale, because the database *is* the marker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.logging import get_logger
from normalize.canonical import Product, Timeframe
from pipeline.gaps import detect_gaps
from snapshot.builder import SnapshotRequest

log = get_logger("bootstrap.plan")
COMPONENT = "bootstrap.plan"

#: How much more than the app asks for the LIVE profile fetches. Two reasons for a margin at all:
#: the regime classifier needs trailing axis samples *behind* the oldest bar a feature reads, and a
#: series sitting exactly on its warm-up threshold flips to ``WARMING`` the moment one bar is absent.
LIVE_MARGIN = 2

#: A floor on that margin, so a shallow series (the 1m trigger set asks for 240 bars) still gets a
#: usable cushion rather than 240 × 2.
LIVE_EXTRA_BARS = 500

#: The venue's futures candle resolutions, keyed by canonical timeframe. The inverse of
#: :func:`normalize.candles.timeframe_for`, which only maps one way. Futures only: the app's snapshot
#: request asks for no spot series, and fetching spot candles nobody reads would be an hour of a
#: host's time for nothing.
FUTURES_RESOLUTION: dict[Timeframe, str] = {
    Timeframe.M1: "1",
    Timeframe.M5: "5",
    Timeframe.M15: "15",
    Timeframe.M30: "30",
    Timeframe.H1: "60",
    Timeframe.H4: "240",
    Timeframe.D1: "1D",
}

#: The earliest bar observed across every series during the Phase-1 survey (F-018). Used as the
#: starting point for a FULL plan so the fetcher does not binary-search for a floor the project has
#: already measured. Pages before the first real bar come back empty and cost one request each.
KNOWN_FLOOR_MS = 1609459200000          # 2021-01-01T00:00:00Z


class Profile(str, Enum):
    LIVE = "live"
    FULL = "full"


@dataclass
class Job:
    """One series to fetch, and the window to fetch it over."""

    asset: str
    product: Product
    timeframe: Timeframe
    api_resolution: str
    start_ms: int
    end_ms: int
    #: Bars already stored in this window, for the report. Not used to narrow the window: the
    #: fetcher re-requests the whole span and relies on ``upsert_bars`` to deduplicate, because a
    #: span computed around existing bars would skip the holes *between* them.
    present: int = 0
    expected: int = 0
    missing: int = 0

    @property
    def key(self) -> str:
        return f"{self.product.value}:{self.asset}:{self.timeframe.value}"

    @property
    def complete(self) -> bool:
        return self.missing == 0

    @property
    def bars_to_fetch(self) -> int:
        """Upper bound on the span, not on the holes — what the fetcher will actually request."""
        return max(0, (self.end_ms - self.start_ms) // self.timeframe.ms)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "resolution": self.api_resolution,
                "start_ms": self.start_ms, "end_ms": self.end_ms,
                "expected": self.expected, "present": self.present, "missing": self.missing,
                "complete": self.complete, "span_bars": self.bars_to_fetch}


@dataclass
class Plan:
    profile: Profile
    jobs: list[Job] = field(default_factory=list)
    now_ms: int = 0

    @property
    def outstanding(self) -> list[Job]:
        return [j for j in self.jobs if not j.complete]

    @property
    def complete(self) -> bool:
        return not self.outstanding

    @property
    def missing_total(self) -> int:
        return sum(j.missing for j in self.jobs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.value, "now_ms": self.now_ms,
            "series": len(self.jobs), "outstanding": len(self.outstanding),
            "missing_bars_total": self.missing_total,
            "complete": self.complete,
            "jobs": [j.to_dict() for j in self.jobs],
            "idempotence_note": "recomputed from the database on every run, so a complete database "
                                "yields an empty plan. There is no completion marker to go stale",
        }


def live_depth(bars_requested: int) -> int:
    """How many bars the LIVE profile fetches for a series the app asks ``bars_requested`` of."""
    return max(bars_requested * LIVE_MARGIN, bars_requested + LIVE_EXTRA_BARS)


def plan(store: Any, request: SnapshotRequest, *, now_ms: int, profile: Profile = Profile.LIVE,
         floor_ms: int = KNOWN_FLOOR_MS) -> Plan:
    """Build the fetch plan by comparing what the app needs against what is stored.

    Gaps are counted with :func:`pipeline.gaps.detect_gaps`, which consults ``known_absent_bars`` —
    so a bar the venue has already confirmed it does not have is **not** counted as missing and does
    not cause an endless re-fetch. That distinction is the difference between a bootstrap that
    terminates and one that retries a hole in the venue's own history forever.
    """
    out = Plan(profile=profile, now_ms=now_ms)
    for series in sorted(request.series, key=lambda s: (s.key.asset, s.key.timeframe.ms)):
        key = series.key
        if key.product is not Product.FUTURES:
            # The app's request is futures-only today. Skipping rather than silently fetching keeps
            # the plan honest about what it covers, and a spot series would be reported here.
            log.info("plan_skipped_non_futures", series=str(key))
            continue
        resolution = FUTURES_RESOLUTION.get(key.timeframe)
        if resolution is None:
            log.warning("plan_skipped_unserved_timeframe", series=str(key),
                        served=sorted(r for r in FUTURES_RESOLUTION.values()))
            continue

        # The newest bar that has actually *closed*, which is the newest one the venue can serve.
        # The same convention `SnapshotBuilder._series` uses, and for the same reason: counting the
        # bar that opened at `now` as expected makes every series permanently one bar short, so the
        # plan would never report complete and the bootstrap would re-request forever. It only shows
        # up when `now` lands exactly on a boundary, which is why a test found it and a live run
        # against the wall clock did not.
        latest_closed = ((now_ms - key.timeframe.ms) // key.timeframe.ms) * key.timeframe.ms

        if profile is Profile.FULL:
            start = floor_ms
        else:
            depth = live_depth(series.bars)
            start = latest_closed - (depth - 1) * key.timeframe.ms
        start = (start // key.timeframe.ms) * key.timeframe.ms

        report = detect_gaps(store, key.asset, key.product, key.timeframe, start, latest_closed)
        out.jobs.append(Job(
            asset=key.asset, product=key.product, timeframe=key.timeframe,
            api_resolution=resolution, start_ms=start, end_ms=latest_closed,
            present=report.present, expected=report.expected, missing=len(report.missing)))
    log.info("plan_built", profile=profile.value, series=len(out.jobs),
             outstanding=len(out.outstanding), missing_bars=out.missing_total)
    return out
