"""Family 5 — cross-asset (specs/02, Tier A).

> **Three simultaneous long signals are not three independent positions; they are one leveraged
> position.**

BTC, ETH and SOL run 0.7-0.9 correlated, so this family is a **risk input first and an alpha input
second** — ``X_CORR`` is what the specs/08 risk engine uses to refuse exactly that concentration.

``X_RS`` is beta-adjusted rather than a raw return spread, because SOL is structurally higher beta:
a raw spread would call SOL "strong" in every up-move and "weak" in every down-move, measuring beta
rather than relative strength.

Lead-lag features are **deliberately absent**. specs/02 Family 5 excludes them: with three assets and
several lags it is a spurious-correlation factory, and any lead-lag claim must clear a pre-registered
test before it may be added.
"""
from __future__ import annotations

import numpy as np

from features.numerics import Degenerate, log_returns, ols_beta, pearson, require_finite, safe_std
from features.spec import HORIZONS, ComputeContext, FeatureSpec
from normalize.canonical import Timeframe

TFS_1H_1D = (Timeframe.H1, Timeframe.D1)

#: The reference asset for beta and relative strength. Not a parameter: specs/02 Family 5 names BTC.
BENCHMARK = "B-BTC_USDT"
#: The universe whose cross-sectional dispersion ``X_DISP`` measures.
UNIVERSE = ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")


def _aligned_returns(ctx: ComputeContext, other: str) -> tuple[np.ndarray, np.ndarray]:
    """Returns for this asset and ``other``, over the **same bars**.

    Alignment is by timestamp, not by position: two series of equal length can cover different bars
    if one has a hole, and a correlation between misaligned series measures the offset rather than the
    relationship (the same error F-023 found in the basis comparison).
    """
    mine, theirs = ctx.bars, ctx.other_window(other, lookback=ctx.lookback)
    shared = np.intersect1d(mine.open_time_ms, theirs.open_time_ms, assume_unique=True)
    if shared.size < ctx.lookback + 1:
        raise Degenerate(
            "INSUFFICIENT_POINTS",
            f"{ctx.asset} and {other} share only {shared.size} common {ctx.tf.value} bars in "
            f"this window; {ctx.lookback + 1} are needed for a {ctx.lookback}-period statistic")
    take = shared[-(ctx.lookback + 1):]
    a = mine.close[np.isin(mine.open_time_ms, take)]
    b = theirs.close[np.isin(theirs.open_time_ms, take)]
    return log_returns(a), log_returns(b)


# ---- X_CORR ------------------------------------------------------------------------------

def x_corr(ctx: ComputeContext) -> float:
    """Rolling Pearson correlation of this asset's returns with the benchmark's.

    Unstable by nature, and it spikes toward 1 in stress — precisely when the diversification
    assumption matters most. That instability is the finding, not a defect to smooth away.
    """
    if ctx.asset == BENCHMARK:
        mine = ctx.bars
        ctx.detail = {"against": BENCHMARK, "self": True}
        # A series' correlation with itself is 1 by definition; computing it would divide by its own
        # variance and report 1.0 as though it were measured.
        safe_std(log_returns(mine.close[-(ctx.lookback + 1):]), what="own returns")
        return 1.0
    a, b = _aligned_returns(ctx, BENCHMARK)
    ctx.detail = {"against": BENCHMARK, "bars": int(a.size)}
    return pearson(a, b, what=f"X_CORR({ctx.asset} vs {BENCHMARK})")


# ---- X_BETA ------------------------------------------------------------------------------

def x_beta(ctx: ComputeContext) -> float:
    """OLS slope of this asset's returns on the benchmark's."""
    if ctx.asset == BENCHMARK:
        ctx.detail = {"against": BENCHMARK, "self": True}
        safe_std(log_returns(ctx.bars.close[-(ctx.lookback + 1):]), what="own returns")
        return 1.0
    a, b = _aligned_returns(ctx, BENCHMARK)
    ctx.detail = {"against": BENCHMARK, "bars": int(a.size)}
    return ols_beta(a, b, what=f"X_BETA({ctx.asset} on {BENCHMARK})")


# ---- X_RS --------------------------------------------------------------------------------

def x_rs(ctx: ComputeContext) -> float:
    """Beta-adjusted relative strength: ``ln(P/P_-N) - beta * ln(BTC/BTC_-N)``."""
    beta = ctx.dep("X_BETA", horizon=ctx.horizon)
    mine = ctx.bars
    own = float(np.log(mine.close[-1] / mine.close[-(ctx.lookback + 1)]))
    if ctx.asset == BENCHMARK:
        ctx.detail = {"self": True, "own_return": own, "beta": beta}
        return 0.0                      # the benchmark has no strength relative to itself
    theirs = ctx.other_window(BENCHMARK, lookback=ctx.lookback)
    if theirs.size < ctx.lookback + 1:
        raise Degenerate("INSUFFICIENT_POINTS",
                         f"the benchmark window has {theirs.size} bars, {ctx.lookback + 1} needed")
    bench = float(np.log(theirs.close[-1] / theirs.close[-(ctx.lookback + 1)]))
    ctx.detail = {"own_return": own, "benchmark_return": bench, "beta": beta}
    return require_finite(own - beta * bench, what="X_RS")


# ---- X_DISP ------------------------------------------------------------------------------

def x_disp(ctx: ComputeContext) -> float:
    """Cross-sectional standard deviation of the universe's ``N``-period returns.

    Identical for every asset in the universe by construction — it describes the universe, not the
    asset — and it is computed per asset so it travels in each asset's FeatureSet.
    """
    returns: dict[str, float] = {}
    for asset in UNIVERSE:
        w = ctx.bars if asset == ctx.asset else ctx.other_window(asset, lookback=ctx.lookback)
        if w.size < ctx.lookback + 1:
            raise Degenerate("INSUFFICIENT_POINTS",
                             f"{asset} has {w.size} bars, {ctx.lookback + 1} needed for dispersion")
        returns[asset] = float(np.log(w.close[-1] / w.close[-(ctx.lookback + 1)]))
    values = np.array(list(returns.values()))
    ctx.detail = {"returns": {k: round(v, 8) for k, v in returns.items()}, "assets": len(returns)}
    return safe_std(values, ddof=1, what="X_DISP across the universe")


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="X_BETA", version="1.0.0", family=5, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, extra_bars=1, compute=x_beta,
                description="OLS slope of the asset's returns on BTC's"),
    FeatureSpec(name="X_CORR", version="1.0.0", family=5, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, extra_bars=1, compute=x_corr,
                description="rolling Pearson correlation with BTC -- a risk input first"),
    FeatureSpec(name="X_RS", version="1.0.0", family=5, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, extra_bars=1, depends_on=(("X_BETA", "short"), ("X_BETA", "medium"),
                                                             ("X_BETA", "long")),
                compute=x_rs,
                description="beta-adjusted relative strength against BTC"),
    FeatureSpec(name="X_DISP", version="1.0.0", family=5, tier="A", timeframes=TFS_1H_1D,
                horizons=HORIZONS, extra_bars=1, compute=x_disp,
                description="cross-sectional std of the three assets' N-period returns"),
)
