"""Required metrics (specs/09 §5, §6) — and the ones the spec rejects as sufficient.

Three things here are deliberate and easy to get wrong by omission.

**Hit rate never travels alone.** It is published with its 95% Wilson interval and ``n``. A 70% hit
rate on 10 trades and on 400 trades are different claims, and a bare percentage makes them look
identical. Wilson rather than the normal approximation because the normal interval is badly wrong for
small ``n`` and for proportions near 0 or 1 — exactly the cases a new strategy produces.

**Mean and median R are both reported, always.** They diverge sharply for skewed payoffs, and
reporting only the mean hides which. A long-gamma strategy with a positive mean and a negative median
is working as designed; a short-gamma one with the same pair is in trouble. One number cannot say
which.

**Sharpe is marked insufficient in the output, not only in the document.** specs/09 §5.4 rejects
Sharpe alone for any strategy and *specifically* for C4-C, because it under-penalises rare large
losses — flattering short-gamma profiles precisely where the danger lives. :func:`risk_adjusted`
therefore emits ``sufficient: false`` alongside the number, and :func:`short_gamma_required` carries
the drawdown / worst-trade / loss-tail set that C4-C must lead with. A reader who sees only a good
Sharpe for C4-C has been told, in the payload, that it proves nothing.

**Profit concentration is read against the strategy's intended payoff shape**, never uniformly
(specs/09 §5.2). High concentration in Class 2, Class 3, C4-A and C4-B is *expected and correct* —
its absence suggests the trailing exit is cutting winners. In Class 1 and C4-C it is a **red flag**,
meaning the edge rests on one or two lucky events. The same number, opposite conclusions, so
:func:`concentration` reports the expectation beside the measurement rather than leaving the reader
to remember which class they are looking at.

**Cells with fewer than 20 trades report the count and no statistics** (specs/09 §6): "a metric
computed from 7 trades is noise wearing a decimal point."
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from backtest.accounting import Trade

COMPONENT = "backtest.metrics"

#: specs/09 §6: below this, a cell reports its count and nothing else.
MIN_TRADES_FOR_STATISTICS = 20

#: Strategies whose payoff is long-gamma by design, where high profit concentration is *expected*
#: (specs/09 §5.2). Everything else reads high concentration as a red flag.
LONG_GAMMA_STRATEGIES = frozenset({"C2-A", "C2-B", "C2-C", "C3-A", "C3-B", "C3-C", "C4-A", "C4-B"})

#: specs/06 §4.3 / specs/09 §5.4: the statistics C4-C's evaluation must lead with. Hit rate and
#: Sharpe are rejected as sufficient for it.
SHORT_GAMMA_REQUIRED = ("max_drawdown_r", "worst_trade_r", "loss_tail_p95_r", "loss_tail_p99_r")

#: Annualisation factor for a strategy evaluated on 1h bars held for hours to days. Stated rather
#: than assumed: an hourly-sampled Sharpe scaled by sqrt(24*365) describes a different strategy than
#: one sampled per trade.
TRADES_PER_YEAR_ASSUMED = 252.0


def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval. ``None`` when there are no observations.

    Chosen over the normal approximation because the normal interval can extend below 0 or above 1
    and is badly calibrated for small ``n`` — which is every strategy's first fold.
    """
    if n <= 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def _r_values(trades: Sequence[Trade]) -> list[float]:
    return [float(t.r_multiple) for t in trades if t.r_multiple is not None]


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile. Written out rather than imported so the definition is visible
    in the lock file's ``metric_definitions`` and cannot drift with a library version."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@dataclass(frozen=True)
class Distribution:
    """The per-trade distribution of specs/09 §5.1. Summary statistics **and** the histogram."""

    n: int
    hit_rate: float | None
    hit_rate_ci95: tuple[float, float] | None
    mean_r: float | None
    median_r: float | None
    stdev_r: float | None
    best_r: float | None
    worst_r: float | None
    histogram: dict[str, int]
    #: Realised R on stopped trades specifically (specs/09 §3.2). If the mean is materially worse
    #: than -1.0R the strategy's real risk exceeds its published risk.
    stopped_n: int = 0
    stopped_mean_r: float | None = None
    stopped_gapped_n: int = 0

    @property
    def stop_slippage_r(self) -> float | None:
        """How much worse than -1.0R stopped trades actually realised.

        The number that re-derives ``liq_safety`` from measurement instead of assumption. Positive
        means stops cost more than they promised.
        """
        return None if self.stopped_mean_r is None else -1.0 - self.stopped_mean_r

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "hit_rate": self.hit_rate,
            "hit_rate_ci95": list(self.hit_rate_ci95) if self.hit_rate_ci95 else None,
            "hit_rate_note": "published with its interval and n, always: 70% on 10 trades and on 400 "
                             "are different claims that a bare percentage makes look identical",
            "mean_r": self.mean_r, "median_r": self.median_r,
            "mean_median_note": "both always reported — they diverge sharply for skewed payoffs and "
                                "the mean alone hides which",
            "stdev_r": self.stdev_r, "best_r": self.best_r, "worst_r": self.worst_r,
            "histogram_r": dict(self.histogram),
            "stopped": {"n": self.stopped_n, "mean_r": self.stopped_mean_r,
                        "gapped_n": self.stopped_gapped_n,
                        "slippage_vs_1R": self.stop_slippage_r,
                        "note": "if the mean is materially worse than -1.0R, real risk exceeds "
                                "published risk and liq_safety must be re-derived from this "
                                "distribution rather than left at its assumed value"},
        }


#: Histogram edges in R. Fixed rather than data-derived, so two runs' histograms are comparable and
#: so the bucketing cannot be chosen after seeing the results.
R_BUCKETS = (-5.0, -3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)


def histogram(values: Sequence[float]) -> dict[str, int]:
    out: dict[str, int] = {}
    labels = [f"<{R_BUCKETS[0]}"]
    labels += [f"[{a}, {b})" for a, b in zip(R_BUCKETS, R_BUCKETS[1:])]
    labels.append(f">={R_BUCKETS[-1]}")
    for label in labels:
        out[label] = 0
    for v in values:
        if v < R_BUCKETS[0]:
            out[labels[0]] += 1
        elif v >= R_BUCKETS[-1]:
            out[labels[-1]] += 1
        else:
            for i, (a, b) in enumerate(zip(R_BUCKETS, R_BUCKETS[1:])):
                if a <= v < b:
                    out[labels[i + 1]] += 1
                    break
    return out


def distribution(trades: Sequence[Trade]) -> Distribution:
    values = _r_values(trades)
    wins = sum(1 for v in values if v > 0)
    stopped = [t for t in trades if t.stopped and t.r_multiple is not None]
    stopped_r = [float(t.r_multiple) for t in stopped]  # type: ignore[arg-type]
    return Distribution(
        n=len(values),
        hit_rate=(wins / len(values)) if values else None,
        hit_rate_ci95=wilson_interval(wins, len(values)),
        mean_r=statistics.fmean(values) if values else None,
        median_r=statistics.median(values) if values else None,
        stdev_r=statistics.stdev(values) if len(values) > 1 else None,
        best_r=max(values) if values else None,
        worst_r=min(values) if values else None,
        histogram=histogram(values),
        stopped_n=len(stopped_r),
        stopped_mean_r=statistics.fmean(stopped_r) if stopped_r else None,
        stopped_gapped_n=sum(1 for t in stopped if t.gapped_through_stop),
    )


def concentration(trades: Sequence[Trade], *, strategy_id: str) -> dict[str, Any]:
    """Profit concentration, with the reading its payoff shape implies (specs/09 §5.2).

    > If removing the 5 best trades makes a strategy unprofitable, it is a lottery, not an edge.

    But that sentence is only a red flag for the strategies whose payoff is *not* long-gamma. Class 2,
    Class 3, C4-A and C4-B are fat-tailed by design, and low concentration there suggests the trailing
    exit is cutting winners. Same number, opposite conclusion — so the expectation is published beside
    the measurement.
    """
    values = _r_values(trades)
    total = sum(values)
    long_gamma = strategy_id in LONG_GAMMA_STRATEGIES
    ordered = sorted(values, reverse=True)
    top5 = sum(ordered[:5])
    decile = max(1, len(ordered) // 10)
    top_decile = sum(ordered[:decile])
    without_top5 = total - top5
    return {
        "total_r": total,
        "top5_dependency": (top5 / total) if total else None,
        "top_decile_share": (top_decile / total) if total else None,
        "total_r_excluding_top5": without_top5,
        "profitable_without_top5": without_top5 > 0 if values else None,
        "payoff_shape": "long_gamma" if long_gamma else "short_gamma_or_mean_reverting",
        "expectation": ("high concentration is expected and correct for this strategy; its absence "
                        "suggests the trailing exit is cutting winners"
                        if long_gamma else
                        "high concentration is a RED FLAG for this strategy: it means the edge rests "
                        "on one or two lucky events rather than a repeatable process"),
        "lottery_test": ("not applicable as a failure test — this strategy is fat-tailed by design"
                         if long_gamma else
                         "if removing the 5 best trades makes it unprofitable, it is a lottery, not "
                         "an edge"),
    }


def equity_curve(trades: Sequence[Trade]) -> list[tuple[int, float]]:
    """Cumulative R against exit time. The basis for drawdown; order is by **exit**, not entry.

    By exit because that is when the result became known. Ordering by entry would let a trade opened
    early and closed late appear in the curve before trades that had already resolved.
    """
    closed = sorted((t for t in trades if t.r_multiple is not None and t.exit is not None),
                    key=lambda t: t.exit.at_ms)  # type: ignore[union-attr]
    cumulative = 0.0
    out = []
    for t in closed:
        cumulative += float(t.r_multiple)  # type: ignore[arg-type]
        out.append((t.exit.at_ms, cumulative))  # type: ignore[union-attr]
    return out


def drawdown(curve: Sequence[tuple[int, float]]) -> dict[str, Any]:
    """Maximum drawdown in R and its duration (specs/09 §5.3).

    In R rather than percent, because position sizing is the risk engine's job and a percentage
    drawdown would mix this measurement with a sizing assumption. Duration is in milliseconds between
    the peak and the trough, with the recovery time reported separately — a drawdown that never
    recovers inside the window is a different fact from one that recovers slowly.
    """
    if not curve:
        return {"max_drawdown_r": None, "duration_ms": None, "recovered": None}
    peak = curve[0][1]
    peak_at = curve[0][0]
    worst = 0.0
    worst_peak_at = peak_at
    worst_at = peak_at
    for at_ms, value in curve:
        if value > peak:
            peak, peak_at = value, at_ms
        depth = peak - value
        if depth > worst:
            worst, worst_peak_at, worst_at = depth, peak_at, at_ms
    recovered_at = next((at for at, v in curve if at > worst_at and v >= peak), None)
    return {
        "max_drawdown_r": worst,
        "peak_at_ms": worst_peak_at,
        "trough_at_ms": worst_at,
        "duration_ms": worst_at - worst_peak_at,
        "recovered": recovered_at is not None,
        "recovery_ms": (recovered_at - worst_at) if recovered_at is not None else None,
        "unit_note": "R, not percent: a percentage drawdown would fold a sizing assumption into a "
                     "measurement the risk engine owns separately",
    }


def risk_adjusted(trades: Sequence[Trade], curve: Sequence[tuple[int, float]]) -> dict[str, Any]:
    """Sharpe, Sortino and MAR — each marked ``sufficient: false`` (specs/09 §5.4).

    The flag is in the payload, not only in the document, because "Sharpe alone is rejected as
    sufficient output for any strategy" is a rule a reader can forget and a dashboard can omit.
    """
    values = _r_values(trades)
    dd = drawdown(curve)
    mean = statistics.fmean(values) if values else None
    sd = statistics.stdev(values) if len(values) > 1 else None
    downside = [v for v in values if v < 0]
    downside_sd = statistics.stdev(downside) if len(downside) > 1 else None
    scale = math.sqrt(TRADES_PER_YEAR_ASSUMED)
    total = sum(values)
    max_dd = dd["max_drawdown_r"]
    return {
        "sharpe_per_trade": (mean / sd) if mean is not None and sd else None,
        "sharpe_annualised": (mean / sd * scale) if mean is not None and sd else None,
        "sortino_per_trade": (mean / downside_sd) if mean is not None and downside_sd else None,
        "mar": (total / max_dd) if max_dd else None,
        "annualisation_assumed_trades_per_year": TRADES_PER_YEAR_ASSUMED,
        "sufficient": False,
        "insufficiency_note": "Sharpe alone is rejected as sufficient output for any strategy "
                              "(specs/09 §5.4). It under-penalises rare large losses, which flatters "
                              "short-gamma profiles precisely where the danger lives. Read it beside "
                              "max drawdown, worst trade and the loss tail, never instead of them",
    }


def short_gamma_required(trades: Sequence[Trade],
                         curve: Sequence[tuple[int, float]]) -> dict[str, Any]:
    """The set C4-C's evaluation must **lead** with (specs/06 §4.3, specs/09 §5.4).

    A high hit rate and a good Sharpe on a short-gamma strategy are expected and prove nothing; a
    strategy can win 70% of the time and lose money. These four numbers are where its danger is
    visible, so they are computed as a named group rather than left for a reader to assemble.
    """
    values = _r_values(trades)
    losses = [v for v in values if v < 0]
    return {
        "max_drawdown_r": drawdown(curve)["max_drawdown_r"],
        "worst_trade_r": min(values) if values else None,
        "loss_tail_p95_r": _percentile([abs(v) for v in losses], 0.95) if losses else None,
        "loss_tail_p99_r": _percentile([abs(v) for v in losses], 0.99) if losses else None,
        "loss_n": len(losses),
        "lead_with_these": list(SHORT_GAMMA_REQUIRED),
        "note": "a high hit rate and a good Sharpe here are expected and prove nothing — a strategy "
                "can win 70% of the time and lose money. If the tail analysis is poor, cut the "
                "strategy; do not widen the stop (specs/06 §4.3)",
    }


def by_group(trades: Sequence[Trade], key, *, label: str) -> dict[str, Any]:
    """Statistics per group, suppressed below :data:`MIN_TRADES_FOR_STATISTICS`.

    Groups with too few trades report their count and **no statistics at all** — specs/09 §6: "a
    metric computed from 7 trades is noise wearing a decimal point." Suppressed rather than flagged,
    because a number that is present will be read however it is labelled.
    """
    buckets: dict[str, list[Trade]] = {}
    for t in trades:
        buckets.setdefault(str(key(t)), []).append(t)
    out: dict[str, Any] = {"grouped_by": label, "min_trades_for_statistics":
                           MIN_TRADES_FOR_STATISTICS, "groups": {}}
    for name, group in sorted(buckets.items()):
        if len(group) < MIN_TRADES_FOR_STATISTICS:
            out["groups"][name] = {
                "n": len(group), "statistics": None,
                "reason": f"fewer than {MIN_TRADES_FOR_STATISTICS} trades; a metric computed from "
                          f"{len(group)} is noise wearing a decimal point (specs/09 §6)"}
            continue
        d = distribution(group)
        out["groups"][name] = {"n": d.n, "statistics": d.to_dict()}
    return out


def strategy_report(trades: Sequence[Trade], *, strategy_id: str,
                    short_gamma: bool = False) -> dict[str, Any]:
    """Everything specs/09 §5 and §6 require for one strategy."""
    curve = equity_curve(trades)
    report: dict[str, Any] = {
        "strategy_id": strategy_id,
        "distribution": distribution(trades).to_dict(),
        "concentration": concentration(trades, strategy_id=strategy_id),
        "equity_curve_r": [[at, round(v, 6)] for at, v in curve],
        "drawdown": drawdown(curve),
        "risk_adjusted": risk_adjusted(trades, curve),
        "by_regime_cell": by_group(trades, lambda t: t.regime_cell or "unclassified",
                                   label="regime cell (specs/02 §6)"),
        "by_asset": by_group(trades, lambda t: t.asset, label="asset"),
        "by_quarter": by_group(trades, _quarter, label="calendar quarter"),
        "confidence": None,
        "confidence_note": "null system-wide until Phase 16 (specs/09 §14)",
    }
    if short_gamma:
        report["short_gamma_required"] = short_gamma_required(trades, curve)
    return report


def _quarter(trade: Trade) -> str:
    """Calendar quarter of the **exit**, from the millisecond timestamp.

    Computed from the exit because that is when the result landed; a trade opened in Q1 and closed in
    Q2 belongs to the quarter whose statistics it affected.
    """
    import datetime as dt

    at = trade.exit.at_ms if trade.exit else trade.entry.at_ms
    d = dt.datetime.fromtimestamp(at / 1000, dt.UTC)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def portfolio_report(trades: Sequence[Trade]) -> dict[str, Any]:
    """Portfolio level (specs/09 §5.3): drawdown, time in market, concurrency."""
    curve = equity_curve(trades)
    closed = [t for t in trades if t.exit is not None]
    spans = sorted((t.entry.at_ms, t.exit.at_ms) for t in closed)  # type: ignore[union-attr]
    concurrency = _max_concurrent(spans)
    in_market_ms = _union_duration(spans)
    window_ms = (max(e for _, e in spans) - min(s for s, _ in spans)) if spans else 0
    return {
        "trades": len(closed),
        "drawdown": drawdown(curve),
        "risk_adjusted": risk_adjusted(trades, curve),
        "time_in_market_ms": in_market_ms,
        "time_in_market_fraction": (in_market_ms / window_ms) if window_ms else None,
        "max_concurrent_positions": concurrency["max"],
        "concurrent_position_histogram": concurrency["histogram"],
        "total_r": sum(_r_values(trades)),
    }


def _max_concurrent(spans: Sequence[tuple[int, int]]) -> dict[str, Any]:
    """Peak overlap and the distribution of it, by sweeping the endpoints.

    Concurrency matters beyond bookkeeping: specs/02 Family 5 warns that three simultaneous longs on
    0.7-0.9 correlated assets are **one leveraged position**, so the distribution is part of the
    output rather than a footnote.
    """
    events: list[tuple[int, int]] = []
    for start, end in spans:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()
    current = 0
    peak = 0
    hist: dict[str, int] = {}
    for _, delta in events:
        current += delta
        peak = max(peak, current)
        hist[str(current)] = hist.get(str(current), 0) + 1
    return {"max": peak, "histogram": dict(sorted(hist.items(), key=lambda kv: int(kv[0])))}


def _union_duration(spans: Sequence[tuple[int, int]]) -> int:
    """Total time at least one position was open, counting overlap once."""
    total = 0
    cursor = None
    for start, end in sorted(spans):
        if cursor is None or start > cursor:
            total += end - start
            cursor = end
        elif end > cursor:
            total += end - cursor
            cursor = end
    return total
