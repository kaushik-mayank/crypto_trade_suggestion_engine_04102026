"""Family 1 — trend and directional state (specs/02, Tier A).

A t-statistic rather than a moving-average crossover: it is scale-free, it answers "is this drift
distinguishable from noise", and it means the same thing on BTC 1h as on SOL 1d. A crossover encodes
an arbitrary pair of lookbacks and says nothing about significance.
"""
from __future__ import annotations

import numpy as np

from features.numerics import Degenerate, efficiency_ratio, ols_slope_tstat, require_finite
from features.spec import HORIZONS, ComputeContext, FeatureSpec
from features.windows import Window
from normalize.canonical import Timeframe

TFS = (Timeframe.H1, Timeframe.H4, Timeframe.D1)


def _sliding(a: np.ndarray, n: int) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(a, n)


# ---- TRD_TSTAT ----------------------------------------------------------------------------

def trd_tstat(ctx: ComputeContext) -> float:
    """OLS of ``ln P`` on bar index over ``N``; the feature is ``beta / SE(beta)``."""
    closes = ctx.bars.close[-ctx.lookback:]
    _, _, t = ols_slope_tstat(np.log(closes))
    return t


def trd_tstat_series(w: Window, lookback: int) -> np.ndarray:
    logp = np.log(w.close)
    out = np.full(w.close.size, np.nan)
    for i in range(lookback - 1, logp.size):
        try:
            out[i] = ols_slope_tstat(logp[i - lookback + 1:i + 1])[2]
        except Degenerate:
            out[i] = np.nan          # the scalar path reports the reason; the series marks the hole
    return out


# ---- TRD_ER ------------------------------------------------------------------------------

def trd_er(ctx: ComputeContext) -> float:
    """Path efficiency: net move over the summed absolute bar-to-bar moves, bounded [0, 1].

    Needs ``N+1`` closes: ``N`` steps require the close before the window opens.
    """
    return efficiency_ratio(ctx.bars.close[-(ctx.lookback + 1):])


def trd_er_series(w: Window, lookback: int) -> np.ndarray:
    closes = w.close
    out = np.full(closes.size, np.nan)
    if closes.size < lookback + 1:
        return out
    steps = np.abs(np.diff(closes))
    path = _sliding(steps, lookback).sum(axis=1)                 # ends at index lookback..size-1
    net = np.abs(closes[lookback:] - closes[:-lookback])
    with np.errstate(divide="ignore", invalid="ignore"):
        vals = np.where(path > 0, net / path, np.nan)
    out[lookback:] = vals
    return out


# ---- TRD_RET -----------------------------------------------------------------------------

def trd_ret(ctx: ComputeContext) -> float:
    """``ln(P_t / P_{t-N})`` — the plain N-period log return."""
    closes = ctx.bars.close
    if closes.size < ctx.lookback + 1:
        raise Degenerate("INSUFFICIENT_POINTS",
                         f"need {ctx.lookback + 1} closes for an {ctx.lookback}-period return, "
                         f"have {closes.size}")
    a, b = float(closes[-(ctx.lookback + 1)]), float(closes[-1])
    if a <= 0 or b <= 0:
        raise Degenerate("NON_POSITIVE_PRICE", f"cannot take a log return between {a} and {b}")
    return require_finite(float(np.log(b / a)), what="TRD_RET")


def trd_ret_series(w: Window, lookback: int) -> np.ndarray:
    logp = np.log(w.close)
    out = np.full(logp.size, np.nan)
    if logp.size > lookback:
        out[lookback:] = logp[lookback:] - logp[:-lookback]
    return out


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="TRD_TSTAT", version="1.0.0", family=1, tier="A", timeframes=TFS,
                horizons=HORIZONS, compute=trd_tstat, vectorised=trd_tstat_series,
                description="OLS slope of ln(price) on bar index divided by its standard error"),
    FeatureSpec(name="TRD_ER", version="1.0.0", family=1, tier="A", timeframes=TFS,
                horizons=HORIZONS, extra_bars=1, compute=trd_er, vectorised=trd_er_series,
                description="net move / summed absolute moves over N bars, bounded [0,1]"),
    FeatureSpec(name="TRD_RET", version="1.0.0", family=1, tier="A", timeframes=TFS,
                horizons=HORIZONS, extra_bars=1, compute=trd_ret, vectorised=trd_ret_series,
                description="ln(P_t / P_t-N)"),
)
