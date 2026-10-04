"""Family 2 — volatility (specs/02, Tier A).

``VOL_CC`` is the reference estimator. Parkinson and Garman-Klass assume continuous trading, zero
drift and no jumps: crypto satisfies the first and violates the third, so both understate volatility
across a jump, and the *divergence between them* is itself informative about jump behaviour rather
than an error to reconcile.

``ATR`` uses an **SMA** of true range, not Wilder's smoothing. Wilder's recursive form has an
effectively infinite warm-up tail, so its value depends on where the series was seeded and live and
backtest disagree on the same bar unless the seeding is replicated exactly. SMA costs a little
responsiveness and buys exact reproducibility. ``ATR`` exists for stop sizing (specs/08), **not** as
a regime input — it is near-collinear with ``VOL_CC``.
"""
from __future__ import annotations

import math

import numpy as np

from features.numerics import (
    LN2,
    Degenerate,
    gk_variance,
    log_returns,
    require_finite,
    safe_std,
)
from features.spec import HORIZONS, NO_HORIZON, ComputeContext, FeatureSpec
from features.windows import Window
from normalize.canonical import Timeframe

TFS = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
TFS_1H_1D = (Timeframe.H1, Timeframe.D1)


def _sliding(a: np.ndarray, n: int) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(a, n)


# ---- VOL_CC ------------------------------------------------------------------------------

def vol_cc(ctx: ComputeContext) -> float:
    """Close-to-close sample standard deviation of log returns over ``N`` returns."""
    r = log_returns(ctx.bars.close[-(ctx.lookback + 1):])
    return safe_std(r, ddof=1, what=f"VOL_CC({ctx.lookback})")


def vol_cc_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.close.size, np.nan)
    if w.close.size < lookback + 1 or np.any(w.close <= 0):
        return out
    r = np.diff(np.log(w.close))
    out[lookback:] = _sliding(r, lookback).std(axis=1, ddof=1)
    return out


# ---- VOL_PARK ----------------------------------------------------------------------------

def vol_park(ctx: ComputeContext) -> float:
    """Parkinson: ``sqrt( sum ln(H/L)^2 / (4 N ln2) )``. Ignores drift entirely, by construction."""
    w = ctx.bars
    hi, lo = w.high[-ctx.lookback:], w.low[-ctx.lookback:]
    if np.any(lo <= 0):
        raise Degenerate("NON_POSITIVE_PRICE", "a low is <= 0, so ln(H/L) is undefined")
    hl = np.log(hi / lo)
    var = float(np.dot(hl, hl) / (4.0 * ctx.lookback * LN2))
    if var <= 0:
        raise Degenerate("ZERO_VARIANCE",
                         "every bar in the window has H == L (no intrabar range), so Parkinson "
                         "volatility is zero rather than small")
    return require_finite(math.sqrt(var), what="VOL_PARK")


def vol_park_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.close.size, np.nan)
    if w.close.size < lookback or np.any(w.low <= 0):
        return out
    hl2 = np.log(w.high / w.low) ** 2
    sums = _sliding(hl2, lookback).sum(axis=1)
    vals = sums / (4.0 * lookback * LN2)
    out[lookback - 1:] = np.where(vals > 0, np.sqrt(np.maximum(vals, 0.0)), np.nan)
    return out


# ---- VOL_GK ------------------------------------------------------------------------------

def vol_gk(ctx: ComputeContext) -> float:
    """Garman-Klass. A negative per-bar variance raises :class:`InvalidOHLC` rather than clamping —
    it is impossible on valid OHLC, so it is an alarm about the bar (specs/12 §4.1)."""
    w = ctx.bars
    n = ctx.lookback
    total = 0.0
    for i in range(-n, 0):
        total += gk_variance(float(w.open[i]), float(w.high[i]), float(w.low[i]), float(w.close[i]))
    var = total / n
    if var <= 0:
        raise Degenerate("ZERO_VARIANCE", "the window has no intrabar range at all")
    return require_finite(math.sqrt(var), what="VOL_GK")


# ---- VOL_RATIO ---------------------------------------------------------------------------

def vol_ratio(ctx: ComputeContext) -> float:
    """``VOL_CC(short) / VOL_CC(long)`` — the regime model's **V axis** (specs/02 §6)."""
    short = ctx.dep("VOL_CC", horizon="short")
    long_ = ctx.dep("VOL_CC", horizon="long")
    if long_ <= 0:
        raise Degenerate("ZERO_STDDEV", "the long-horizon volatility is zero, so the ratio is undefined")
    return require_finite(short / long_, what="VOL_RATIO",
                          inputs={"short": short, "long": long_})


# ---- VOL_OF_VOL --------------------------------------------------------------------------

def vol_of_vol(ctx: ComputeContext) -> float:
    """Trailing standard deviation of ``VOL_CC(short)`` over the medium lookback.

    Needs the *series* of the short-horizon volatility, not its latest value, so it is computed
    through the vectorised path of its own dependency — which is exactly what specs/12 §7 requires to
    agree with the scalar path.
    """
    medium = int(ctx.param(f"lookback_{ctx.tf.value}_medium"))
    series = ctx.dep_series("VOL_CC", horizon="short", length=medium)
    finite = series[np.isfinite(series)]
    if finite.size < medium:
        raise Degenerate("INSUFFICIENT_POINTS",
                         f"need {medium} VOL_CC(short) values to measure their dispersion, have "
                         f"{finite.size} finite ones")
    return safe_std(finite, ddof=1, what="VOL_OF_VOL")


# ---- ATR ---------------------------------------------------------------------------------

def true_range(w: Window) -> np.ndarray:
    """``TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|)`` — length ``size - 1``."""
    if w.size < 2:
        raise Degenerate("INSUFFICIENT_POINTS", "true range needs a prior close")
    prev_close = w.close[:-1]
    return np.maximum.reduce([
        w.high[1:] - w.low[1:],
        np.abs(w.high[1:] - prev_close),
        np.abs(w.low[1:] - prev_close),
    ])


def atr(ctx: ComputeContext) -> float:
    """SMA of true range over ``N``. Reproducible by construction, unlike Wilder's."""
    tr = true_range(ctx.bars)
    if tr.size < ctx.lookback:
        raise Degenerate("INSUFFICIENT_POINTS",
                         f"ATR({ctx.lookback}) needs {ctx.lookback + 1} bars, have {ctx.bars.size}")
    value = float(np.mean(tr[-ctx.lookback:]))
    if value <= 0:
        raise Degenerate("ZERO_VARIANCE",
                         "average true range is zero: every bar in the window is a single price, so "
                         "an ATR-scaled stop would be degenerate")
    return require_finite(value, what="ATR")


def atr_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.close.size, np.nan)
    if w.size < lookback + 1:
        return out
    tr = true_range(w)
    means = _sliding(tr, lookback).mean(axis=1)
    out[lookback:] = np.where(means > 0, means, np.nan)
    return out


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="VOL_CC", version="1.0.0", family=2, tier="A", timeframes=TFS,
                horizons=HORIZONS, extra_bars=1, compute=vol_cc, vectorised=vol_cc_series,
                description="sample std of log returns (the reference volatility estimator)"),
    FeatureSpec(name="VOL_PARK", version="1.0.0", family=2, tier="A", timeframes=TFS,
                horizons=HORIZONS, compute=vol_park, vectorised=vol_park_series,
                description="Parkinson high-low range volatility"),
    FeatureSpec(name="VOL_GK", version="1.0.0", family=2, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, compute=vol_gk,
                description="Garman-Klass OHLC volatility; negative variance is a data alarm"),
    FeatureSpec(name="ATR", version="1.0.0", family=2, tier="A", timeframes=TFS,
                horizons=HORIZONS, extra_bars=1, compute=atr, vectorised=atr_series,
                description="SMA of true range; for stop sizing only, never a regime input"),
    FeatureSpec(name="VOL_RATIO", version="1.0.0", family=2, tier="A", timeframes=TFS_1H_1D,
                horizons=NO_HORIZON, extra_bars=1, lookback_horizon="long",
                depends_on=(("VOL_CC", "short"), ("VOL_CC", "long")), compute=vol_ratio,
                description="VOL_CC(short)/VOL_CC(long) -- the regime model's V axis"),
    FeatureSpec(name="VOL_OF_VOL", version="1.0.0", family=2, tier="A", timeframes=TFS_1H_1D,
                horizons=NO_HORIZON, lookback_horizon="medium", extra_horizon="short", extra_bars=1,
                depends_on=(("VOL_CC", "short"),), compute=vol_of_vol,
                description="trailing std of VOL_CC(short) over the medium lookback"),
)
