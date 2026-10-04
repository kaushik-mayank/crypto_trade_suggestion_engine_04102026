"""Numerical stability guards (specs/12 §4).

Every degenerate case in the spec's table gets a *named* failure rather than a number. The
distinction that matters: a guard says "this cannot be computed here", while a clamp would say "here
is a plausible number" — and a clamped t-statistic on a perfectly straight series is indistinguishable
from a real one.

:func:`gk_variance` is the exception, and deliberately so: Garman-Klass negativity is impossible on
valid OHLC, so it is not guarded but *reported* as a data-integrity alarm (specs/12 §4.1).
"""
from __future__ import annotations

import math

import numpy as np

from core.errors import InvalidOHLC

COMPONENT = "features.numerics"

#: Thresholds below which a denominator counts as zero. Mirrors settings.numerics so the feature
#: layer can be used without a Settings object; the engine passes the configured values through.
ZERO_VARIANCE_EPS = 1e-12
ZERO_STDDEV_EPS = 1e-12
ZERO_MAD_EPS = 1e-12
TSTAT_SE_REL_EPS = 1e-9
NO_MOVEMENT_EPS = 1e-12

LN2 = math.log(2.0)
GK_C = 2.0 * LN2 - 1.0          # 0.3862943611198906


class Degenerate(Exception):
    """Raised by a computation when its inputs are degenerate.

    An exception rather than a sentinel so a compute function cannot forget to check: the engine
    catches it and turns it into ``NUMERICAL_FAILURE`` with this reason attached.

    Deliberately **not** a frozen dataclass: Python assigns ``__traceback__`` on an exception as it
    propagates, and a frozen instance raises ``FrozenInstanceError`` the moment it crosses a frame —
    replacing the real numerical failure with a confusing one about dataclasses.
    """

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}"


def log_returns(closes: np.ndarray) -> np.ndarray:
    """``r_t = ln(P_t / P_{t-1})``. Non-positive prices are impossible here and would produce -inf."""
    if closes.size < 2:
        raise Degenerate("INSUFFICIENT_POINTS", f"need at least 2 closes for a return, have {closes.size}")
    if np.any(closes <= 0):
        raise Degenerate("NON_POSITIVE_PRICE",
                         f"{int(np.sum(closes <= 0))} close(s) are <= 0, so a log return is undefined")
    return np.diff(np.log(closes))


def require_finite(value: float, *, what: str, inputs: dict[str, float] | None = None) -> float:
    """The catch-all row of the spec's table: ``inf``/``nan`` out is ``NON_FINITE``, with inputs."""
    if not math.isfinite(value):
        raise Degenerate("NON_FINITE", f"{what} produced {value}"
                         + (f" from {inputs}" if inputs else ""))
    return value


def safe_std(x: np.ndarray, *, ddof: int = 1, what: str = "series", eps: float = ZERO_STDDEV_EPS) -> float:
    """Sample standard deviation, or ``ZERO_STDDEV``.

    A near-zero trailing std is what turns a z-score into an arbitrarily large number; specs/12 §4
    lists it for ``DEV_Z``, ``FND_Z`` and ``BAS_Z``.
    """
    if x.size <= ddof:
        raise Degenerate("INSUFFICIENT_POINTS", f"{what}: need more than {ddof} points, have {x.size}")
    sd = float(np.std(x, ddof=ddof))
    if sd < eps:
        raise Degenerate("ZERO_STDDEV",
                         f"{what}: standard deviation {sd:.3e} is below {eps:.0e}; every value in the "
                         "window is effectively identical, so a z-score has no meaning")
    return require_finite(sd, what=f"std({what})")


def safe_variance(x: np.ndarray, *, ddof: int = 1, what: str = "series",
                  eps: float = ZERO_VARIANCE_EPS) -> float:
    if x.size <= ddof:
        raise Degenerate("INSUFFICIENT_POINTS", f"{what}: need more than {ddof} points, have {x.size}")
    var = float(np.var(x, ddof=ddof))
    if var < eps:
        raise Degenerate("ZERO_VARIANCE",
                         f"{what}: variance {var:.3e} is below {eps:.0e}; the series does not move in "
                         "this window")
    return require_finite(var, what=f"var({what})")


def safe_mad(x: np.ndarray, *, what: str = "series", eps: float = ZERO_MAD_EPS) -> tuple[float, float]:
    """``(median, MAD)`` or ``ZERO_MAD`` — MAD is 0 once half the window is identical, which happens
    on a thin series of repeated volumes (specs/12 §4, ``VLM_ZROBUST``)."""
    if x.size == 0:
        raise Degenerate("INSUFFICIENT_POINTS", f"{what}: empty window")
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    if mad < eps:
        raise Degenerate("ZERO_MAD",
                         f"{what}: median absolute deviation {mad:.3e} is below {eps:.0e}; at least "
                         "half the window is identical, so a robust z-score is undefined")
    return med, mad


def ols_slope_tstat(y: np.ndarray, *, se_rel_eps: float = TSTAT_SE_REL_EPS) -> tuple[float, float, float]:
    """OLS of ``y`` on bar index. Returns ``(beta, se_beta, t)``.

    The guard is the first row of the spec's table: on a perfectly straight series the residual
    variance is zero, ``SE(beta) -> 0`` and ``t -> inf``. That is not "an extremely strong trend", it
    is a series with no noise to compare the drift against, so it fails as
    ``ZERO_RESIDUAL_VARIANCE``.
    """
    n = y.size
    if n < 3:
        raise Degenerate("INSUFFICIENT_POINTS", f"OLS needs at least 3 points, have {n}")
    x = np.arange(n, dtype=np.float64)
    xm, ym = x.mean(), y.mean()
    dx = x - xm
    sxx = float(np.dot(dx, dx))
    if sxx < ZERO_VARIANCE_EPS:
        raise Degenerate("ZERO_VARIANCE", "the index has no variance (a single distinct x)")
    beta = float(np.dot(dx, y - ym) / sxx)
    resid = y - (ym + beta * dx)
    sse = float(np.dot(resid, resid))
    se = math.sqrt(sse / (n - 2) / sxx) if sse > 0 else 0.0
    scale = max(abs(beta), 1e-30)
    if se < se_rel_eps * scale or se == 0.0:
        raise Degenerate("ZERO_RESIDUAL_VARIANCE",
                         f"SE(beta)={se:.3e} is negligible against |beta|={abs(beta):.3e}; the series "
                         "is a straight line in this window, so the t-statistic is unbounded rather "
                         "than large")
    t = beta / se
    return beta, se, require_finite(t, what="t-statistic", inputs={"beta": beta, "se": se})


def ols_beta(y: np.ndarray, x: np.ndarray, *, what: str = "beta") -> float:
    """Slope of ``y`` on ``x`` (``X_BETA``). Zero variance in either series fails."""
    if y.size != x.size:
        raise Degenerate("LENGTH_MISMATCH", f"{what}: {y.size} vs {x.size} observations")
    safe_variance(x, what=f"{what}: explanatory series")
    dx = x - x.mean()
    beta = float(np.dot(dx, y - y.mean()) / np.dot(dx, dx))
    return require_finite(beta, what=what)


def pearson(a: np.ndarray, b: np.ndarray, *, what: str = "correlation") -> float:
    """Pearson correlation with the spec's zero-variance guard on **either** series."""
    if a.size != b.size:
        raise Degenerate("LENGTH_MISMATCH", f"{what}: {a.size} vs {b.size} observations")
    sa = safe_std(a, what=f"{what}: first series", eps=ZERO_VARIANCE_EPS)
    sb = safe_std(b, what=f"{what}: second series", eps=ZERO_VARIANCE_EPS)
    n = a.size
    cov = float(np.dot(a - a.mean(), b - b.mean()) / (n - 1))
    r = cov / (sa * sb)
    return require_finite(max(-1.0, min(1.0, r)), what=what)


def autocorr1(r: np.ndarray) -> float:
    """``MR_AC1``: lag-1 Pearson autocorrelation of returns."""
    if r.size < 3:
        raise Degenerate("INSUFFICIENT_POINTS", f"lag-1 autocorrelation needs 3+ returns, have {r.size}")
    safe_variance(r, what="returns")
    return pearson(r[:-1], r[1:], what="MR_AC1")


def gk_variance(open_: float, high: float, low: float, close: float) -> float:
    """Single-bar Garman-Klass variance: ``0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2``.

    Not guarded. Valid OHLC guarantees ``|ln(C/O)| <= ln(H/L)``, hence the result is positive; a
    negative one means the bar is invalid and normalization failed to catch it, so it raises
    :class:`InvalidOHLC` (specs/12 §4.1). **Never clamped to zero** — that would turn a live
    data-integrity alarm into a quietly understated volatility.
    """
    if min(open_, high, low, close) <= 0:
        raise Degenerate("NON_POSITIVE_PRICE", f"OHLC contains a non-positive price: "
                                               f"O={open_} H={high} L={low} C={close}")
    r = math.log(high / low)
    c = math.log(close / open_)
    var = 0.5 * r * r - GK_C * c * c
    if var < 0:
        raise InvalidOHLC(
            f"Garman-Klass variance is negative ({var:.6e}), which valid OHLC makes impossible",
            component=COMPONENT, operation="gk_variance",
            context={"open": open_, "high": high, "low": low, "close": close,
                     "ln_hl": r, "ln_co": c, "variance": var},
            likely_cause="the bar violates L <= min(O,C) <= max(O,C) <= H, so normalization let an "
                         "invalid bar through",
            remediation="write a dq_events row of type INVALID_OHLC and find the source bar; do not "
                        "clamp this to zero",
        )
    return var


def variance_ratio(log_prices: np.ndarray, k: int) -> float:
    """Lo-MacKinlay overlapping, heteroskedasticity-consistent variance ratio (specs/02 Family 4).

    ``sigma2_1 = sum (r_t - mu)^2 / (nk - 1)``
    ``sigma2_k = sum (lnP_t - lnP_{t-k} - k*mu)^2 / m``, ``m = k(nk - k + 1)(1 - k/(nk))``
    ``VR(k) = sigma2_k / sigma2_1``

    ``VR > 1`` trending, ``< 1`` mean-reverting, ``~1`` a random walk.

    **Deviation from the literal text of specs/02, deliberate (finding F-031).** The spec writes the
    last line as ``sigma2_k / (k * sigma2_1)``, but ``m`` already carries the factor ``k``, so that
    divides by ``k`` twice: measured on a simulated random walk it yields 0.51, 0.26 and 0.14 for
    k=2, 4 and 8 instead of ~1. That contradicts the spec's own stated interpretation two lines
    above ("VR ~ 1 -> random walk"), and would make every asset read as mean-reverting at all times
    -- silently disabling the ``MR_VR < 1`` gates in C1-B, C1-C and C2-A and skewing the regime
    model's P axis. The interpretation is implemented; the extra division is not.
    """
    nk = log_prices.size - 1                       # number of 1-period returns
    if k < 2:
        raise Degenerate("INVALID_HORIZON", f"variance ratio needs k >= 2, got {k}")
    if nk <= k:
        raise Degenerate("INSUFFICIENT_POINTS",
                         f"VR({k}) needs more than {k} one-period returns, have {nk}")
    r = np.diff(log_prices)
    mu = float(r.mean())
    sigma2_1 = float(np.dot(r - mu, r - mu) / (nk - 1))
    if sigma2_1 < ZERO_VARIANCE_EPS:
        raise Degenerate("ZERO_VARIANCE",
                         f"VR({k}): one-period variance {sigma2_1:.3e} is below {ZERO_VARIANCE_EPS:.0e}; "
                         "the price does not move in this window")
    k_diff = log_prices[k:] - log_prices[:-k] - k * mu
    m = k * (nk - k + 1) * (1.0 - k / nk)
    if m <= 0:
        raise Degenerate("INSUFFICIENT_POINTS", f"VR({k}): normalisation m={m:.3f} is non-positive")
    sigma2_k = float(np.dot(k_diff, k_diff) / m)
    return require_finite(sigma2_k / sigma2_1, what=f"VR({k})",
                          inputs={"sigma2_k": sigma2_k, "sigma2_1": sigma2_1})


def efficiency_ratio(closes: np.ndarray) -> float:
    """``TRD_ER``: net move over summed absolute moves. ``NO_MOVEMENT`` when the denominator is 0."""
    if closes.size < 2:
        raise Degenerate("INSUFFICIENT_POINTS", f"need 2+ closes, have {closes.size}")
    path = float(np.sum(np.abs(np.diff(closes))))
    if path < NO_MOVEMENT_EPS:
        raise Degenerate("NO_MOVEMENT",
                         "the sum of absolute bar-to-bar moves is zero in this window, so path "
                         "efficiency is undefined (0/0), not 1")
    return require_finite(abs(float(closes[-1] - closes[0])) / path, what="TRD_ER")
