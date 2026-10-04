"""Family 4 — deviation and mean reversion (specs/02, Tier A).

``MR_VR`` is the substantive member. Most systems assume either mean reversion or momentum and apply
it permanently; the variance ratio is a direct statistical test of **which is operative right now**,
which is why specs/02 §6 makes it the regime model's second axis.

Three horizons ``k ∈ {2, 4, 8}`` are registered separately (``MR_VR_K2`` …) rather than folded into
one value, because a condition like C2-A's "``MR_VR > 1``" has to name a specific k to be replayable.
:data:`PRIMARY_VR` records which one the class specs' bare ``MR_VR(1h)`` means: ``k = 2``, on the
spec's own evidence that ``MR_AC1`` is "the crude version of the same idea ... highly correlated with
VR(2)".

The variance ratio implemented here is the standard Lo-MacKinlay ratio, not the literal formula in
specs/02 Family 4, which divides by ``k`` twice — see finding F-031 and open question Q7.
"""
from __future__ import annotations

import numpy as np

from features.numerics import (
    Degenerate,
    autocorr1,
    log_returns,
    require_finite,
    safe_std,
    variance_ratio,
)
from features.spec import HORIZONS, ComputeContext, FeatureSpec
from features.windows import Window
from normalize.canonical import Timeframe

TFS = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
TFS_1H_1D = (Timeframe.H1, Timeframe.D1)

#: Which variance-ratio horizon the class specs mean when they write ``MR_VR(1h)``.
PRIMARY_VR = "MR_VR_K2"


# ---- DEV_Z -------------------------------------------------------------------------------

def dev_z(ctx: ComputeContext) -> float:
    """``(P_t - SMA_N) / sigma_N``, trailing only — never centred (specs/02 §5).

    The SMA travels in ``detail`` because it is a **published level**, not a diagnostic: C1-B's targets
    are "halfway back to the SMA" and "the SMA itself" (specs/03 §4), and recomputing it in the
    strategy would be a second implementation of the same mean.
    """
    closes = ctx.bars.close[-ctx.lookback:]
    sd = safe_std(closes, ddof=1, what=f"DEV_Z({ctx.lookback}) price window")
    sma = float(closes.mean())
    ctx.detail = {"sma": sma, "std": sd, "close": float(closes[-1])}
    return require_finite((float(closes[-1]) - sma) / sd, what="DEV_Z")


def dev_z_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.size, np.nan)
    if w.size < lookback:
        return out
    win = np.lib.stride_tricks.sliding_window_view(w.close, lookback)
    means = win.mean(axis=1)
    sds = win.std(axis=1, ddof=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.where(sds > 0, (w.close[lookback - 1:] - means) / sds, np.nan)
    out[lookback - 1:] = vals
    return out


# ---- DEV_ATR -----------------------------------------------------------------------------

def dev_atr(ctx: ComputeContext) -> float:
    """``(P_t - SMA_N) / ATR_N`` — the same deviation in units a stop can be sized in."""
    closes = ctx.bars.close[-ctx.lookback:]
    atr = ctx.dep("ATR", horizon=ctx.horizon)
    return require_finite((float(closes[-1]) - float(closes.mean())) / atr, what="DEV_ATR")


# ---- DEV_VWAP ----------------------------------------------------------------------------

def dev_vwap(ctx: ComputeContext) -> float:
    """``(P_t - VWAP_N) / ATR``, VWAP over typical price weighted by volume.

    Not a session VWAP: there is no session here and the anchor is arbitrary, so specs/02 Family 4
    says to treat it as a volume-weighted mean and nothing more. Zero volume across the window means
    there is no weighting to apply, which fails rather than silently becoming an unweighted mean.
    """
    w = ctx.bars
    n = ctx.lookback
    tp = w.typical_price[-n:]
    vol = w.volume[-n:]
    total = float(vol.sum())
    if total <= 0:
        raise Degenerate("NO_MOVEMENT",
                         f"total volume over the last {n} bars is {total}; a volume-weighted mean has "
                         "no weights, and substituting an unweighted mean would be a different feature")
    vwap = float(np.dot(tp, vol) / total)
    atr = ctx.dep("ATR", horizon=ctx.horizon)
    ctx.detail = {"vwap": vwap, "volume_total": total}
    return require_finite((float(w.close[-1]) - vwap) / atr, what="DEV_VWAP")


# ---- MR_AC1 ------------------------------------------------------------------------------

def mr_ac1(ctx: ComputeContext) -> float:
    """Lag-1 autocorrelation of returns. Kept only as a sanity cross-check on ``MR_VR_K2``."""
    return autocorr1(log_returns(ctx.bars.close[-(ctx.lookback + 1):]))


def mr_ac1_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.size, np.nan)
    if w.size < lookback + 1 or np.any(w.close <= 0):
        return out
    r = np.diff(np.log(w.close))
    for i in range(lookback, w.size):
        seg = r[i - lookback:i]
        try:
            out[i] = autocorr1(seg)
        except Degenerate:
            out[i] = np.nan
    return out


# ---- MR_VR -------------------------------------------------------------------------------

def _vr(k: int):
    def compute(ctx: ComputeContext) -> float:
        logp = np.log(ctx.bars.close[-(ctx.lookback + 1):])
        if np.any(~np.isfinite(logp)):
            raise Degenerate("NON_POSITIVE_PRICE", "the window contains a non-positive close")
        vr = variance_ratio(logp, k)
        ctx.detail = {"k": k, "interpretation": "trending" if vr > 1 else
                      "mean-reverting" if vr < 1 else "random walk"}
        return vr
    compute.__name__ = f"mr_vr_k{k}"
    compute.__doc__ = (f"Lo-MacKinlay variance ratio at k={k}. >1 trending, <1 mean-reverting, "
                       "~1 a random walk.")
    return compute


def _vr_series(k: int):
    def series(w: Window, lookback: int) -> np.ndarray:
        out = np.full(w.size, np.nan)
        if w.size < lookback + 1 or np.any(w.close <= 0):
            return out
        logp = np.log(w.close)
        for i in range(lookback, w.size):
            try:
                out[i] = variance_ratio(logp[i - lookback:i + 1], k)
            except Degenerate:
                out[i] = np.nan
        return out
    return series


VR_HORIZONS = (2, 4, 8)          # parameter 12, `vr_horizons`, fixed

SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="DEV_Z", version="1.0.0", family=4, tier="A", timeframes=TFS,
                horizons=HORIZONS, compute=dev_z, vectorised=dev_z_series,
                description="(close - SMA_N) / trailing std"),
    FeatureSpec(name="DEV_ATR", version="1.0.0", family=4, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, extra_bars=1,
                depends_on=(("ATR", "short"), ("ATR", "medium"), ("ATR", "long")),
                compute=dev_atr,
                description="(close - SMA_N) / ATR"),
    FeatureSpec(name="DEV_VWAP", version="1.0.0", family=4, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, extra_bars=1,
                depends_on=(("ATR", "short"), ("ATR", "medium"), ("ATR", "long")),
                compute=dev_vwap,
                description="(close - volume-weighted mean of typical price) / ATR"),
    FeatureSpec(name="MR_AC1", version="1.0.0", family=4, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, extra_bars=1, compute=mr_ac1, vectorised=mr_ac1_series,
                description="lag-1 autocorrelation of log returns"),
    *(FeatureSpec(name=f"MR_VR_K{k}", version="1.0.0", family=4, tier="A", timeframes=TFS_1H_1D,
                  horizons=HORIZONS, extra_bars=1, min_lookback=k + 1,
                  compute=_vr(k), vectorised=_vr_series(k),
                  description=f"Lo-MacKinlay variance ratio at k={k} (F-031: not the literal "
                              "specs/02 formula, which divides by k twice)")
      for k in VR_HORIZONS),
)
