"""Family 6 — funding and basis (specs/02, **Tier B**).

Tier B means **zero history before recording started**, so nothing here is backtestable over the
Parquet archive and every trailing statistic says how much history it actually had.

Two honesty requirements the spec states explicitly and this module carries into the values:

**The settlement approximation.** ``fr`` is observed as 60-second snapshots; the *settled* funding
payment is never observed. ``FND_CUM`` therefore approximates the settlement for an interval ending
at ``T`` with the last ``fr`` seen before ``T``. That is an approximation of unknown error, so every
``FND_CUM`` value carries ``approximation: "last observed fr before each interval end"`` in its
detail — labelled everywhere it is used, as the spec requires.

**The basis gate is not optional.** ``BAS_BPS`` is computed only when the mark and the spot last
price are stamped within ``basis_timestamp_tolerance_s`` of each other. An informal Phase-1 comparison
showed a ~120-point spot/futures gap on BTC that was *entirely* explained by a 6.5-minute fetch gap:
basis from unsynchronised fetches measures latency, not basis. Discards are counted in the detail so
the discard rate is visible rather than inferred.

Basis here is the **upstream venue's** basis, not a CoinDCX-local dislocation (capability map §7), and
must never be framed otherwise.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from features.numerics import Degenerate, require_finite, safe_std
from features.spec import NO_HORIZON, ComputeContext, FeatureSpec, NotApplicable

#: The funding interval, from instrument metadata in principle and 8 h at this venue in practice.
FUNDING_INTERVAL_MS = 8 * 3_600_000
#: The documented funding cap, +/-2% (specs/08 §8). ``FND_CAPDIST`` measures proximity to it.
FUNDING_CAP = 0.02

PIT = (None,)                       # Tier B features have no timeframe


def _rt(ctx: ComputeContext) -> Any:
    """The latest futures real-time row, or a refusal carrying the freshness reason."""
    if ctx.snapshot is None:
        raise NotApplicable("no snapshot attached, so Tier B cannot be read")
    return ctx.snapshot.tier_b("futures_rt", ctx.asset)


def _history(ctx: ComputeContext, source: str) -> tuple[Any, ...]:
    if ctx.snapshot is None:
        raise NotApplicable("no snapshot attached, so Tier B history cannot be read")
    return ctx.snapshot.tier_b_series(source, ctx.asset)


# ---- as-reported values ------------------------------------------------------------------

def fnd_rate(ctx: ComputeContext) -> float:
    """``fr`` as reported, per 8 h interval. Not annualised: annualising invites comparison with
    returns measured over a different horizon."""
    row = _rt(ctx)
    ctx.detail = {"source_ts_ms": row.source_ts_ms, "interval_hours": FUNDING_INTERVAL_MS / 3_600_000,
                  "units": "fraction per 8h interval"}
    return require_finite(float(row.funding_rate), what="FND_RATE")


def fnd_pred(ctx: ComputeContext) -> float:
    """``efr`` — the venue's predicted rate for the interval in progress."""
    row = _rt(ctx)
    ctx.detail = {"source_ts_ms": row.source_ts_ms, "units": "fraction per 8h interval"}
    return require_finite(float(row.predicted_funding_rate), what="FND_PRED")


def fnd_tension(ctx: ComputeContext) -> float:
    """``efr - fr``: how far the prediction has moved from the current rate."""
    row = _rt(ctx)
    ctx.detail = {"fr": float(row.funding_rate), "efr": float(row.predicted_funding_rate)}
    return require_finite(float(row.predicted_funding_rate - row.funding_rate), what="FND_TENSION")


def fnd_capdist(ctx: ComputeContext) -> float:
    """``(0.02 - |efr|) / 0.02`` — 1.0 is far from the cap, 0.0 is at it.

    A negative value would mean the venue reported a rate beyond its own documented cap, which is a
    contract violation rather than an extreme reading, so it is refused rather than clipped.
    """
    row = _rt(ctx)
    efr = abs(float(row.predicted_funding_rate))
    if efr > FUNDING_CAP:
        raise Degenerate("CAP_EXCEEDED",
                         f"predicted funding {efr:.6f} exceeds the documented cap {FUNDING_CAP}; the "
                         "venue's contract or our units are wrong, and a proximity measure would be "
                         "meaningless either way")
    ctx.detail = {"efr": efr, "cap": FUNDING_CAP}
    return require_finite((FUNDING_CAP - efr) / FUNDING_CAP, what="FND_CAPDIST")


# ---- trailing statistics -----------------------------------------------------------------

def fnd_z(ctx: ComputeContext) -> float:
    """``(fr - trailing mean) / trailing std`` over whatever funding history exists.

    The window is "all recorded history", which is short: specs/02 Family 6 says so plainly, and the
    value records how many observations and how many hours it covered so nobody reads a z-score built
    from an afternoon as though it were built from a year.
    """
    row = _rt(ctx)
    history = _history(ctx, "futures_rt")
    rates = np.array([float(r.funding_rate) for r in history], dtype=np.float64)
    if rates.size < 30:
        raise NotApplicable(
            f"a funding z-score needs a trailing sample; {rates.size} observation(s) recorded so far "
            "(Tier B has no history before recording started)")
    span_h = (history[-1].ts_ms - history[0].ts_ms) / 3_600_000 if rates.size > 1 else 0.0
    sd = safe_std(rates, ddof=1, what="funding rate history")
    ctx.detail = {"observations": int(rates.size), "history_hours": round(span_h, 2),
                  "mean": float(rates.mean()), "std": sd,
                  "caveat": "trailing window is only as long as recording has run"}
    return require_finite((float(row.funding_rate) - float(rates.mean())) / sd, what="FND_Z")


def _cum(intervals: int):
    def compute(ctx: ComputeContext) -> float:
        history = _history(ctx, "futures_rt")
        if not history:
            raise NotApplicable("no funding history recorded yet")
        as_of = ctx.snapshot.as_of_ms if ctx.snapshot else history[-1].ts_ms
        boundaries = [((as_of // FUNDING_INTERVAL_MS) - i) * FUNDING_INTERVAL_MS
                      for i in range(intervals)]
        total = 0.0
        used: list[dict[str, Any]] = []
        for boundary in sorted(boundaries):
            before = [r for r in history if (r.source_ts_ms or r.ts_ms) <= boundary]
            if not before:
                raise NotApplicable(
                    f"no funding observation exists before the interval ending "
                    f"{(as_of - boundary) / 3_600_000:.0f}h ago; {intervals} interval(s) of history "
                    "are needed and recording does not reach back that far")
            last = before[-1]
            rate = float(last.funding_rate)
            total += rate
            used.append({"interval_end_ms": boundary, "fr": rate,
                         "observed_ms": last.source_ts_ms or last.ts_ms})
        ctx.detail = {
            "intervals": intervals, "days": round(intervals * FUNDING_INTERVAL_MS / 86_400_000, 2),
            "per_interval": used,
            "approximation": "last observed fr before each interval end; the settled payment is never "
                             "observed, so this is an approximation of unknown error",
        }
        return require_finite(total, what=f"FND_CUM({intervals})")

    compute.__name__ = f"fnd_cum_{intervals}"
    return compute


# ---- basis -------------------------------------------------------------------------------

def _basis_bps(mark: float, spot: float) -> float:
    if spot <= 0:
        raise Degenerate("NON_POSITIVE_PRICE", f"spot last price is {spot}")
    return (mark / spot - 1.0) * 10_000.0


def bas_bps(ctx: ComputeContext) -> float:
    """``10000 * (mark - spot_last) / spot_last``, **only** when the two are stamped within tolerance.

    Outside tolerance this is not "approximately the basis" — it is the price move over the gap
    between the two fetches, which is a different quantity entirely.
    """
    if ctx.snapshot is None:
        raise NotApplicable("no snapshot attached")
    rt = ctx.snapshot.tier_b("futures_rt", ctx.asset)
    ticker = ctx.snapshot.tier_b("spot_ticker", ctx.asset)
    tolerance_ms = int(float(ctx.param("basis_timestamp_tolerance_s")) * 1000)
    mark_ts = rt.source_ts_ms or rt.ts_ms
    skew = abs(mark_ts - ticker.ts_ms)
    if skew > tolerance_ms:
        raise NotApplicable(
            f"the mark and the spot last price are {skew / 1000:.1f}s apart, beyond the "
            f"{tolerance_ms / 1000:.0f}s tolerance; a basis computed from unsynchronised fetches "
            "measures elapsed price movement, not basis")
    bps = _basis_bps(float(rt.mark_price), float(ticker.last_price))
    ctx.detail = {"mark": float(rt.mark_price), "spot_last": float(ticker.last_price),
                  "timestamp_skew_ms": skew, "tolerance_ms": tolerance_ms,
                  "scope": "upstream venue basis, not a CoinDCX-local dislocation"}
    return require_finite(bps, what="BAS_BPS")


def bas_z(ctx: ComputeContext) -> float:
    """Trailing z-score of the basis, over the pairs of history that were timestamp-aligned.

    Unaligned pairs are **discarded, not approximated**, and the discard fraction travels with the
    value: a z-score computed from 5% of the observations is a different object from one computed
    from 95%.
    """
    if ctx.snapshot is None:
        raise NotApplicable("no snapshot attached")
    rt_hist = _history(ctx, "futures_rt")
    tk_hist = _history(ctx, "spot_ticker")
    if not rt_hist or not tk_hist:
        raise NotApplicable("basis history needs both futures and spot ticker history")
    tolerance_ms = int(float(ctx.param("basis_timestamp_tolerance_s")) * 1000)

    # Aligned by binary search rather than by scanning: the naive nearest-neighbour loop is O(n^2),
    # and with a week of 15-second polling on both sides that is ~25 million comparisons per asset --
    # measured as the slowest thing in the whole evaluation before this was changed.
    ticker_ts = np.array([t.ts_ms for t in tk_hist], dtype=np.int64)
    order = np.argsort(ticker_ts)
    sorted_ts = ticker_ts[order]
    series: list[float] = []
    discarded = 0
    for row in rt_hist:
        ts = int(row.source_ts_ms or row.ts_ms)
        pos = int(np.searchsorted(sorted_ts, ts))
        candidates = [p for p in (pos - 1, pos) if 0 <= p < sorted_ts.size]
        if not candidates:
            discarded += 1
            continue
        nearest = min(candidates, key=lambda p: abs(int(sorted_ts[p]) - ts))
        if abs(int(sorted_ts[nearest]) - ts) > tolerance_ms:
            discarded += 1
            continue
        ticker = tk_hist[int(order[nearest])]
        try:
            series.append(_basis_bps(float(row.mark_price), float(ticker.last_price)))
        except Degenerate:
            discarded += 1
    total = len(series) + discarded
    if len(series) < 30:
        raise NotApplicable(
            f"only {len(series)} of {total} recorded observations were timestamp-aligned within "
            f"{tolerance_ms / 1000:.0f}s; a basis z-score needs a trailing sample")
    current = bas_bps(ctx)                       # re-uses the same gate for the current value
    arr = np.array(series, dtype=np.float64)
    sd = safe_std(arr, ddof=1, what="basis history")
    ctx.detail = {"aligned": len(series), "discarded_unaligned": discarded,
                  "discard_fraction": round(discarded / total, 4) if total else None,
                  "mean_bps": float(arr.mean()), "std_bps": sd, "current_bps": current}
    return require_finite((current - float(arr.mean())) / sd, what="BAS_Z")


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="FND_RATE", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=False, compute=fnd_rate,
                description="funding rate as reported, per 8h interval"),
    FeatureSpec(name="FND_PRED", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=False, compute=fnd_pred,
                description="predicted funding rate as reported"),
    FeatureSpec(name="FND_TENSION", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=False, compute=fnd_tension,
                description="predicted minus current funding rate"),
    FeatureSpec(name="FND_CAPDIST", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=False, compute=fnd_capdist,
                description="proximity of the predicted rate to the +/-2% cap"),
    FeatureSpec(name="FND_Z", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=True, compute=fnd_z,
                description="z-score of the funding rate over recorded history"),
    FeatureSpec(name="FND_CUM_1D", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=True, compute=_cum(3),
                description="approximate cumulative funding over 3 intervals (1 day)"),
    FeatureSpec(name="FND_CUM_3D", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=True, compute=_cum(9),
                description="approximate cumulative funding over 9 intervals (3 days)"),
    FeatureSpec(name="FND_CUM_1W", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt",),
                needs_history=True, compute=_cum(21),
                description="approximate cumulative funding over 21 intervals (1 week)"),
    FeatureSpec(name="BAS_BPS", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt", "spot_ticker"),
                needs_history=False, compute=bas_bps,
                description="futures mark vs spot last in bps, gated on timestamp alignment"),
    FeatureSpec(name="BAS_Z", version="1.0.0", family=6, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_rt", "spot_ticker"),
                needs_history=True, compute=bas_z,
                description="z-score of the basis over aligned recorded history"),
)
