"""Family 3 — market structure (specs/02, Tier A).

Pivots are volatility-scaled, not percentage-scaled: a fixed 1% swing threshold means something
different for BTC than for SOL, and something different for the same asset across regimes. Scaling
the required excursion by ATR makes the definition comparable across assets and time. ``theta`` is a
registered parameter and ships **uncalibrated**, so every feature here reports ``PARAMETER_UNSET``
until it is set on training data — deliberately, because ``theta = 0`` would make every local
maximum a pivot and the resulting "structure" would look computed.

**Structure lags by construction.** A pivot is confirmed only ``k`` bars after the fact, so the most
recent bars can never contain one. specs/02 Family 3 is explicit that structure must never be treated
as real-time, and the confirmation delay is reported on every value so a consumer can see it.

These features carry their **levels** in ``detail``, not just a number: C2-A stops at "the last
confirmed swing low" and C3-B needs a boundary's touch count, so the levels themselves are the
payload and the scalar is the distance to the nearest one in ATR units.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from features.numerics import Degenerate, require_finite
from features.spec import HORIZONS, ComputeContext, FeatureSpec, NotApplicable
from features.windows import Window
from normalize.canonical import Timeframe

TFS = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
TFS_1H_1D = (Timeframe.H1, Timeframe.D1)

#: Labels for ``STR_SEQ`` (specs/02 Family 3).
SEQ_LABELS = ("HH-HL", "LH-LL", "HH-LL", "LH-HL", "undefined")

#: ``swing_confirmation_bars`` (registry parameter 11), fixed at 3 and never tuned. Mirrored here as a
#: constant **only** to derive the minimum lookback below, which :class:`FeatureSpec` needs statically;
#: every computation still reads the value from the registry via ``ctx.param``.
CONFIRMATION_BARS = 3

#: The shortest window in which a pivot can exist at all. ``find_pivots`` scans ``range(k, n - k)``, so
#: a window needs ``n > 2k`` bars to hold even one candidate, plus the one extra bar the spec requests.
#:
#: Declared so the engine *skips* these combinations instead of computing and failing them. Without it,
#: ``STR_SWING@4h/short`` (6 bars) and ``STR_SWING@1d/short`` (7 bars) were computed on every
#: evaluation and could never return a pivot for **any** value of theta — an impossibility reported as
#: a per-bar data-quality failure, which is indistinguishable from a real one (F-038).
MIN_PIVOT_BARS = 2 * CONFIRMATION_BARS + 2


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    open_time_ms: int
    price: float
    is_high: bool
    excursion: float          # how far it stands out, in price units
    touches: int              # bars whose range contains this level, after confirmation


def find_pivots(w: Window, *, k: int, theta: float, atr: float) -> list[Pivot]:
    """Fractal pivots: an extreme of the ``k`` bars either side, standing out by ``theta * ATR``.

    "Standing out" is measured as the *smaller* of the two excursions — the drop from a candidate high
    to the lowest low on its left, and to the lowest low on its right. Taking the smaller of the two
    means a level only counts when it is a genuine turning point on **both** sides; taking the larger
    would promote every step in a staircase into a pivot.

    Touches are counted without a tolerance parameter: a bar touches a level when its own
    ``[low, high]`` range contains it. That keeps the 30-parameter budget intact and makes the count
    unambiguous.
    """
    if atr <= 0:
        raise Degenerate("ZERO_VARIANCE", "ATR is zero, so an ATR-scaled pivot threshold is degenerate")
    required = theta * atr
    pivots: list[Pivot] = []
    n = w.size
    for i in range(k, n - k):
        left_h, right_h = w.high[i - k:i], w.high[i + 1:i + k + 1]
        left_l, right_l = w.low[i - k:i], w.low[i + 1:i + k + 1]
        if w.high[i] > left_h.max() and w.high[i] > right_h.max():
            excursion = min(w.high[i] - left_l.min(), w.high[i] - right_l.min())
            if excursion >= required:
                pivots.append(Pivot(i, int(w.open_time_ms[i]), float(w.high[i]), True, float(excursion),
                                    _touches(w, float(w.high[i]), after=i + k)))
        elif w.low[i] < left_l.min() and w.low[i] < right_l.min():
            excursion = min(left_h.max() - w.low[i], right_h.max() - w.low[i])
            if excursion >= required:
                pivots.append(Pivot(i, int(w.open_time_ms[i]), float(w.low[i]), False, float(excursion),
                                    _touches(w, float(w.low[i]), after=i + k)))
    return pivots


def _touches(w: Window, level: float, *, after: int) -> int:
    if after >= w.size:
        return 0
    return int(np.sum((w.low[after:] <= level) & (w.high[after:] >= level)))


def _pivot_context(ctx: ComputeContext) -> tuple[list[Pivot], float, int]:
    """Pivots for this window, plus the ATR they were scaled by and the confirmation delay."""
    theta = float(ctx.param("swing_atr_multiple"))          # raises PARAMETER_UNSET while unset
    k = int(ctx.param("swing_confirmation_bars"))
    atr = ctx.dep("ATR", horizon=ctx.horizon if ctx.horizon != "-" else "short")
    pivots = find_pivots(ctx.bars, k=k, theta=theta, atr=atr)
    if not pivots:
        raise NotApplicable(
            f"no pivot in the last {ctx.bars.size} {ctx.tf.value} bars stands out by "
            f"{theta} x ATR ({theta * atr:.4f}); there is no confirmed structure to report")
    return pivots, atr, k


# ---- STR_SWING ---------------------------------------------------------------------------

def str_swing(ctx: ComputeContext) -> float:
    """Distance from the current close to the **nearest confirmed swing level**, in ATR units.

    The levels themselves travel in ``detail`` because that is what C2-A's stop and C3-B's boundary
    conditions need; the scalar answers "how close is price to structure", which is C1-C condition 4.
    """
    pivots, atr, k = _pivot_context(ctx)
    price = float(ctx.bars.close[-1])
    nearest = min(pivots, key=lambda p: abs(price - p.price))
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]
    ctx.detail = {
        "nearest_level": nearest.price,
        "nearest_is_high": nearest.is_high,
        "nearest_touches": nearest.touches,
        "last_swing_high": highs[-1].price if highs else None,
        "last_swing_high_ms": highs[-1].open_time_ms if highs else None,
        "last_swing_low": lows[-1].price if lows else None,
        "last_swing_low_ms": lows[-1].open_time_ms if lows else None,
        "pivots_confirmed": len(pivots),
        "confirmation_lag_bars": k,
        "atr_used": atr,
        "close": price,
    }
    return require_finite(abs(price - nearest.price) / atr, what="STR_SWING")


# ---- STR_SEQ -----------------------------------------------------------------------------

def str_seq(ctx: ComputeContext) -> str:
    """Categorical structure: HH-HL / LH-LL / HH-LL / LH-HL, from the last two pivots of each type.

    Undefined until two of each exist (specs/02 Family 3), which is reported as not-applicable rather
    than as a failure — nothing is broken, the structure simply has not formed.
    """
    pivots, _, _ = _pivot_context(ctx)
    highs = [p.price for p in pivots if p.is_high]
    lows = [p.price for p in pivots if not p.is_high]
    if len(highs) < 2 or len(lows) < 2:
        raise NotApplicable(
            f"STR_SEQ needs two confirmed pivots of each kind; have {len(highs)} high(s) and "
            f"{len(lows)} low(s)")
    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]
    ctx.detail = {"highs": highs[-2:], "lows": lows[-2:]}
    if hh and hl:
        return "HH-HL"          # uptrend structure
    if not hh and not hl:
        return "LH-LL"          # downtrend structure
    if hh and not hl:
        return "HH-LL"          # broadening
    return "LH-HL"              # contracting


# ---- STR_DIST_PDH ------------------------------------------------------------------------

def str_dist_pdh(ctx: ComputeContext) -> float:
    """``(P_t - PriorDayHigh) / ATR``, with the low's distance alongside it in ``detail``.

    The prior *period* comes from the daily series in the same snapshot rather than from resampling
    the 1h window: the daily boundary has to be the venue's own (F-012 verified 1d bars are UTC
    aligned), and re-deriving it here would risk a different boundary than the stored 1d bar uses.
    """
    atr = ctx.dep("ATR", horizon=ctx.horizon)
    daily = ctx.window_for(ctx.asset, Timeframe.D1, lookback=3)
    price = float(ctx.bars.close[-1])
    now_ms = int(ctx.bars.open_time_ms[-1])
    day_ms = Timeframe.D1.ms
    current_day = (now_ms // day_ms) * day_ms
    prior = np.where(daily.open_time_ms < current_day)[0]
    if prior.size == 0:
        raise NotApplicable("the daily series in this snapshot has no bar before the current day, so "
                            "there is no prior-period extreme to measure against")
    i = int(prior[-1])
    prior_high, prior_low = float(daily.high[i]), float(daily.low[i])
    ctx.detail = {
        "prior_day_ms": int(daily.open_time_ms[i]),
        "prior_high": prior_high, "prior_low": prior_low,
        "dist_high_atr": (price - prior_high) / atr,
        "dist_low_atr": (price - prior_low) / atr,
        "atr_used": atr,
        "close": price,
    }
    return require_finite((price - prior_high) / atr, what="STR_DIST_PDH")


# ---- STR_RANGEPOS -----------------------------------------------------------------------

def str_rangepos(ctx: ComputeContext) -> float:
    """Where the close sits in the window's ``[min low, max high]`` range, in [0, 1]."""
    w = ctx.bars
    n = ctx.lookback
    lo, hi = float(w.low[-n:].min()), float(w.high[-n:].max())
    if hi - lo <= 0:
        raise Degenerate("ZERO_VARIANCE",
                         f"the {n}-bar range is zero (high == low == {hi}), so there is no position "
                         "within it")
    ctx.detail = {"range_low": lo, "range_high": hi}
    return require_finite((float(w.close[-1]) - lo) / (hi - lo), what="STR_RANGEPOS")


def str_rangepos_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.size, np.nan)
    if w.size < lookback:
        return out
    lows = np.lib.stride_tricks.sliding_window_view(w.low, lookback).min(axis=1)
    highs = np.lib.stride_tricks.sliding_window_view(w.high, lookback).max(axis=1)
    span = highs - lows
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.where(span > 0, (w.close[lookback - 1:] - lows) / span, np.nan)
    out[lookback - 1:] = vals
    return out


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="STR_SWING", version="1.0.0", family=3, tier="A", timeframes=TFS,
                horizons=HORIZONS, extra_bars=1, min_lookback=MIN_PIVOT_BARS,
                depends_on=(("ATR", "short"), ("ATR", "medium"), ("ATR", "long")),
                compute=str_swing,
                description="distance to the nearest confirmed ATR-scaled swing level, with the "
                            "levels in detail"),
    FeatureSpec(name="STR_SEQ", version="1.0.0", family=3, tier="A", timeframes=TFS,
                horizons=HORIZONS, extra_bars=1, categorical=True, min_lookback=MIN_PIVOT_BARS,
                depends_on=(("ATR", "short"), ("ATR", "medium"), ("ATR", "long")),
                compute=str_seq,
                description="HH-HL / LH-LL / HH-LL / LH-HL from the last two pivots of each kind"),
    FeatureSpec(name="STR_DIST_PDH", version="1.0.0", family=3, tier="A",
                timeframes=(Timeframe.H1,), horizons=HORIZONS, extra_bars=1,
                depends_on=(("ATR", "short"), ("ATR", "medium"), ("ATR", "long")),
                compute=str_dist_pdh,
                description="(close - prior-day high) / ATR, prior-day low alongside in detail"),
    FeatureSpec(name="STR_RANGEPOS", version="1.0.0", family=3, tier="A", timeframes=TFS,
                horizons=HORIZONS, compute=str_rangepos, vectorised=str_rangepos_series,
                description="position of the close within the N-bar high-low range"),
)


def detail_of(value: Any) -> dict[str, Any]:
    """Convenience for consumers: a value's structural payload, or an empty mapping."""
    return dict(getattr(value, "detail", {}) or {})
