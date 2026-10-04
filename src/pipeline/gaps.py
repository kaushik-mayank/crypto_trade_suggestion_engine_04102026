"""Gap detection, absent-bar confirmation and safe aggregation (specs/11 §6, §7).

The distinction this module exists to make:

> **A bar we failed to fetch and a bar that never existed look identical.**

If the exchange omits zero-volume bars, a hole is ambiguous. So a hole is re-requested in a *narrow
bracketing window*: if the surrounding bars come back and the target does not, the bar **does not
exist**, and after **two** such confirmations it is recorded in ``known_absent_bars`` and never
requested again.

That distinction is load-bearing for aggregation: building a 4h bar from three of four 1h bars
produces a silently wrong bar. :func:`aggregate_bars` therefore **refuses** across an unconfirmed
gap and returns the reason, rather than quietly aggregating what it has.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.errors import GapError
from core.logging import get_logger
from normalize.canonical import Bar, Product, Timeframe

log = get_logger("pipeline.gaps")

#: Bars fetched either side of a suspected hole when confirming absence.
BRACKET_BARS = 3
#: Confirmations required before absence is treated as settled (specs/11 §7.2).
REQUIRED_CONFIRMATIONS = 2


def expected_bar_times(timeframe: Timeframe, start_ms: int, end_ms: int) -> list[int]:
    """Every boundary in ``[start, end]`` aligned to the timeframe."""
    step = timeframe.ms
    first = start_ms - (start_ms % step)
    if first < start_ms:
        first += step
    return list(range(first, end_ms + 1, step))


@dataclass
class GapReport:
    asset: str
    product: Product
    timeframe: Timeframe
    start_ms: int
    end_ms: int
    expected: int = 0
    present: int = 0
    known_absent: int = 0
    missing: list[int] = field(default_factory=list)

    @property
    def completeness(self) -> float:
        denominator = self.expected - self.known_absent
        return 1.0 if denominator <= 0 else 1 - len(self.missing) / denominator

    @property
    def has_unconfirmed_gap(self) -> bool:
        return bool(self.missing)

    def to_dict(self) -> dict[str, Any]:
        return {"asset": self.asset, "product": self.product.value, "timeframe": self.timeframe.value,
                "window": [self.start_ms, self.end_ms], "expected": self.expected, "present": self.present,
                "known_absent": self.known_absent, "missing": len(self.missing),
                "first_missing": self.missing[:5], "completeness": round(self.completeness, 6)}


def detect_gaps(store: Any, asset: str, product: Product, timeframe: Timeframe,
                start_ms: int, end_ms: int) -> GapReport:
    """Compare what should exist against what is stored, consulting ``known_absent_bars``."""
    expected = expected_bar_times(timeframe, start_ms, end_ms)
    present = {b.open_time_ms for b in store.get_bars(asset, product, timeframe, start_ms, end_ms,
                                                      final_only=False)}
    absent = store.known_absent_bars(asset, product, timeframe, start_ms, end_ms,
                                     required=REQUIRED_CONFIRMATIONS)
    missing = [t for t in expected if t not in present and t not in absent]
    report = GapReport(asset=asset, product=product, timeframe=timeframe, start_ms=start_ms, end_ms=end_ms,
                       expected=len(expected), present=len(present & set(expected)),
                       known_absent=len(absent & set(expected)), missing=missing)
    if missing:
        log.info("gap_detected", **report.to_dict())
    return report


def confirm_absence(store: Any, fetch_window, asset: str, product: Product, timeframe: Timeframe,
                    open_time_ms: int, *, now_ms: int) -> bool:
    """Re-request a narrow window bracketing a suspected hole.

    ``fetch_window(start_ms, end_ms) -> list[Bar]`` performs the fetch (injected so this is testable
    and so the caller owns rate limiting). Returns True once absence is **settled**.

    Three outcomes:
      * the bar arrives → it was a fetch gap; it is stored and nothing is confirmed;
      * the bar is missing **and** neighbours arrived → one confirmation recorded;
      * nothing arrived → inconclusive: the window itself failed, so no confirmation is recorded.
    """
    step = timeframe.ms
    start = open_time_ms - BRACKET_BARS * step
    end = open_time_ms + BRACKET_BARS * step
    bars = list(fetch_window(start, end))
    times = {b.open_time_ms for b in bars}

    if open_time_ms in times:
        store.upsert_bars([b for b in bars if b.open_time_ms == open_time_ms])
        log.info("gap_filled", asset=asset, timeframe=timeframe.value, open_time_ms=open_time_ms)
        return False
    if not times:
        log.warning("absence_inconclusive", asset=asset, timeframe=timeframe.value,
                    open_time_ms=open_time_ms,
                    detail="the bracketing window returned nothing, so absence cannot be concluded")
        return False

    count = store.confirm_absent_bar(asset, product, timeframe, open_time_ms, at_ms=now_ms)
    settled = count >= REQUIRED_CONFIRMATIONS
    log.info("absence_confirmed" if settled else "absence_observed", asset=asset,
             timeframe=timeframe.value, open_time_ms=open_time_ms, confirmations=count,
             neighbours_returned=len(times))
    return settled


def aggregate_bars(bars: list[Bar], *, factor: int, target: Timeframe,
                   known_absent: set[int] | None = None, window_start_ms: int | None = None) -> Bar:
    """Aggregate ``factor`` fine bars into one ``target`` bar.

    Rule (specs/02 §5, verified exact in F-013): ``open`` = first open, ``high`` = max,
    ``low`` = min, ``close`` = last close, ``volume`` = sum.

    **Refuses** unless every constituent slot is either present or confirmed absent — aggregating
    across an unconfirmed gap is how a silently wrong bar is produced (specs/11 §7.2).
    """
    if not bars:
        raise GapError(
            "cannot aggregate an empty set of bars",
            component="pipeline.gaps", operation="aggregate_bars",
            context={"factor": factor, "target": target.value},
            likely_cause="the window contained no stored bars at all",
            remediation="check the gap report for this window before aggregating",
        )
    fine = sorted(bars, key=lambda b: b.open_time_ms)
    fine_tf = fine[0].timeframe
    if any(b.timeframe is not fine_tf for b in fine):
        raise GapError(
            "cannot aggregate bars of mixed timeframes",
            component="pipeline.gaps", operation="aggregate_bars",
            context={"timeframes": sorted({b.timeframe.value for b in fine})},
            likely_cause="a caller merged two series",
            remediation="aggregate one timeframe at a time",
        )
    if fine_tf.ms * factor != target.ms:
        raise GapError(
            f"{factor} x {fine_tf.value} does not make one {target.value}",
            component="pipeline.gaps", operation="aggregate_bars",
            context={"fine_ms": fine_tf.ms, "factor": factor, "target_ms": target.ms},
            likely_cause="a wrong factor — 12 x 5m is an hour, not 15 minutes (a real error in specs/01 §P12)",
            remediation=f"use factor={target.ms // fine_tf.ms}",
        )

    start = window_start_ms if window_start_ms is not None else fine[0].open_time_ms
    if start % target.ms:
        raise GapError(
            f"aggregation window start {start} is not aligned to a {target.value} boundary",
            component="pipeline.gaps", operation="aggregate_bars",
            context={"start_ms": start, "target": target.value},
            likely_cause="the window was derived from the first available bar rather than the grid",
            remediation="align the window to the target timeframe's UTC boundary (verified aligned, F-012)",
        )

    slots = [start + i * fine_tf.ms for i in range(factor)]
    absent = known_absent or set()
    present = {b.open_time_ms: b for b in fine}
    unconfirmed = [t for t in slots if t not in present and t not in absent]
    if unconfirmed:
        raise GapError(
            f"refusing to aggregate {target.value}: {len(unconfirmed)} of {factor} constituent bars are "
            "missing and not confirmed absent",
            component="pipeline.gaps", operation="aggregate_bars",
            context={"target": target.value, "window_start_ms": start, "missing": unconfirmed[:5],
                     "have": len(present), "need": factor},
            likely_cause="a fetch gap, or bars the exchange omits for zero-volume periods",
            remediation="confirm absence with confirm_absence() (two sightings) or backfill the window; "
                        "aggregating what is present would produce a silently wrong bar",
        )

    used = [present[t] for t in slots if t in present]
    if not used:
        raise GapError(
            f"every constituent bar of this {target.value} window is confirmed absent",
            component="pipeline.gaps", operation="aggregate_bars",
            context={"target": target.value, "window_start_ms": start},
            likely_cause="a genuinely empty period (all bars confirmed absent)",
            remediation="skip the window; there is no trading to aggregate",
        )
    return Bar(
        asset=used[0].asset, product=used[0].product, timeframe=target, open_time_ms=start,
        open=used[0].open,
        high=max(b.high for b in used),
        low=min(b.low for b in used),
        close=used[-1].close,
        volume_base=sum((b.volume_base for b in used), Decimal(0)),
        is_final=all(b.is_final for b in used),
        source=f"aggregated:{factor}x{fine_tf.value}",
        fetched_at_ms=max(b.fetched_at_ms for b in used),
        synthesised=any(b.synthesised for b in used),
    )


def aggregate_series(bars: list[Bar], *, factor: int, target: Timeframe,
                     known_absent: set[int] | None = None) -> tuple[list[Bar], list[dict[str, Any]]]:
    """Aggregate a whole series. Returns ``(bars, refusals)`` — refusals are reported, not raised,
    so one bad window does not discard a decade of history."""
    if not bars:
        return [], []
    fine = sorted(bars, key=lambda b: b.open_time_ms)
    step = target.ms
    first = fine[0].open_time_ms - (fine[0].open_time_ms % step)
    out: list[Bar] = []
    refusals: list[dict[str, Any]] = []
    by_window: dict[int, list[Bar]] = {}
    for b in fine:
        by_window.setdefault(b.open_time_ms - (b.open_time_ms % step), []).append(b)
    for window_start in sorted(by_window):
        if window_start < first:
            continue
        try:
            out.append(aggregate_bars(by_window[window_start], factor=factor, target=target,
                                      known_absent=known_absent, window_start_ms=window_start))
        except GapError as e:
            refusals.append({"window_start_ms": window_start, "reason": e.code, "detail": e.message,
                             **{k: v for k, v in e.context.items() if k in ("missing", "have", "need")}})
    if refusals:
        log.info("aggregation_refused", target=target.value, windows_refused=len(refusals),
                 windows_built=len(out))
    return out, refusals
