"""Family 9 — volume and participation (specs/02).

Volume is heavily right-skewed with fat tails, so ``VLM_ZROBUST`` uses median/MAD rather than
mean/std: a mean-based z-score is dominated by a handful of spikes and reports "normal" for almost
everything else. ``1.4826`` is the constant that makes MAD a consistent estimator of the standard
deviation for a normal distribution.

**Units never cross sources.** Spot candles are base-denominated, ``current_prices`` is quote-
denominated (F-027), and futures candles are base (F-010). These features read the bar window only,
which is one source with one unit, so the comparison hazard cannot arise here — but nothing may
compare these values with a ticker's 24 h volume without converting first.

``VLM_AGGR`` (Tier B, from the tape's maker flag) is **not registered**: with polling-only ingestion
the tape is sampled, not complete — F-011 measured a fixed 30 rows per request regardless of `limit`
— so a per-bar buy/sell split would be a reconstruction presented as a measurement. It is listed in
specs/02 as low-confidence; here it waits for a tape ingestion path that can justify it (PHASE-13).
"""
from __future__ import annotations

import numpy as np

from features.numerics import Degenerate, ols_slope_tstat, require_finite, safe_mad
from features.spec import HORIZONS, ComputeContext, FeatureSpec
from features.windows import Window
from normalize.canonical import Timeframe

TFS = (Timeframe.H1, Timeframe.H4, Timeframe.D1)

#: Makes MAD a consistent estimator of sigma under normality.
MAD_TO_SIGMA = 1.4826


# ---- VLM_ZROBUST -------------------------------------------------------------------------

def vlm_zrobust(ctx: ComputeContext) -> float:
    """``(V_t - median_N) / (1.4826 * MAD_N)``."""
    vol = ctx.bars.volume[-ctx.lookback:]
    median, mad = safe_mad(vol, what=f"VLM_ZROBUST({ctx.lookback}) volume window")
    ctx.detail = {"median": median, "mad": mad, "volume": float(vol[-1]),
                  "synthesised_bars": ctx.bars.synthesised_bars}
    return require_finite((float(vol[-1]) - median) / (MAD_TO_SIGMA * mad), what="VLM_ZROBUST")


def vlm_zrobust_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.size, np.nan)
    if w.size < lookback:
        return out
    win = np.lib.stride_tricks.sliding_window_view(w.volume, lookback)
    medians = np.median(win, axis=1)
    mads = np.median(np.abs(win - medians[:, None]), axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.where(mads > 0, (w.volume[lookback - 1:] - medians) / (MAD_TO_SIGMA * mads), np.nan)
    out[lookback - 1:] = vals
    return out


# ---- VLM_TREND ---------------------------------------------------------------------------

def vlm_trend(ctx: ComputeContext) -> float:
    """OLS slope of ``ln V`` over ``N``.

    A zero-volume bar has no logarithm. Rather than adding an epsilon — which would make the slope
    depend on the epsilon — the window is refused, and on 1m a synthesised no-trade bar is exactly
    such a case (specs/12 §6.2), which is why the synthesised count travels with the value.
    """
    vol = ctx.bars.volume[-ctx.lookback:]
    zeros = int(np.sum(vol <= 0))
    if zeros:
        raise Degenerate("NON_POSITIVE_VOLUME",
                         f"{zeros} of {vol.size} bars have zero volume, so ln(V) is undefined; adding "
                         "an epsilon would make the slope an artefact of the epsilon")
    beta, _, _ = ols_slope_tstat(np.log(vol))
    ctx.detail = {"zero_volume_bars": zeros, "synthesised_bars": ctx.bars.synthesised_bars}
    return require_finite(beta, what="VLM_TREND")


def vlm_trend_series(w: Window, lookback: int) -> np.ndarray:
    out = np.full(w.size, np.nan)
    if w.size < lookback:
        return out
    for i in range(lookback - 1, w.size):
        seg = w.volume[i - lookback + 1:i + 1]
        if np.any(seg <= 0):
            continue
        try:
            out[i] = ols_slope_tstat(np.log(seg))[0]
        except Degenerate:
            out[i] = np.nan
    return out


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="VLM_ZROBUST", version="1.0.0", family=9, tier="A", timeframes=TFS,
                horizons=HORIZONS, compute=vlm_zrobust, vectorised=vlm_zrobust_series,
                description="robust (median/MAD) volume z-score"),
    FeatureSpec(name="VLM_TREND", version="1.0.0", family=9, tier="A", timeframes=TFS,
                horizons=HORIZONS, compute=vlm_trend, vectorised=vlm_trend_series,
                description="OLS slope of ln(volume) over N bars"),
)
