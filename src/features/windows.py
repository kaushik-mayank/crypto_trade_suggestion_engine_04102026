"""Window extraction, gap checks and warm-up accounting (specs/12 §5, §6).

A :class:`Window` is the only thing a feature function sees: float64 arrays and the metadata needed
to report what was used. Extraction is where the two gating rules live, and both fail *before* any
arithmetic happens:

**Warm-up** (§5) — below ``min_bars`` the answer is ``WARMING`` with "need N, have M", never a value
computed from a shorter window.

**Gaps** (§6) — on 1h, 4h and 1d **any** hole returns ``GAP_IN_WINDOW``, confirmed absent or not:
at these horizons on BTC/ETH/SOL an absent bar means something is wrong, not that the market was
quiet, and computing through it would hide that. On 1m a confirmed-absent bar is synthesised as a
no-trade bar (``O=H=L=C=prior close``, ``volume=0``) and counted, because specs/02 §3 confines 1m to
trigger evaluation and execution checks — never alpha — so the downward volatility bias synthesis
introduces cannot reach a signal's content.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np

from features.value import FeatureStatus
from normalize.canonical import Bar, Timeframe
from snapshot.models import SeriesKey, SeriesState

COMPONENT = "features.windows"

#: Timeframes where a confirmed-absent bar may be synthesised rather than refused (specs/12 §6.2).
SYNTHESIS_ALLOWED: frozenset[Timeframe] = frozenset({Timeframe.M1})


@dataclass(frozen=True, slots=True)
class Window:
    """One computation window as float64 arrays. Immutable; arrays are not shared with the store."""

    key: SeriesKey
    lookback: int
    open_time_ms: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    synthesised_bars: int = 0

    @property
    def size(self) -> int:
        return int(self.close.size)

    @property
    def window_start_ms(self) -> int:
        return int(self.open_time_ms[0])

    @property
    def window_end_ms(self) -> int:
        return int(self.open_time_ms[-1]) + self.key.timeframe.ms

    @property
    def typical_price(self) -> np.ndarray:
        return (self.high + self.low + self.close) / 3.0

    def upto(self, end_index: int) -> Window:
        """Bars ``0..end_index`` — the window as it stood at that bar.

        This is what the specs/12 §7 parity test walks: the scalar path applied at bar *i* must see
        exactly the history a live run would have had then, which is a prefix, not a tail.
        """
        if not 0 <= end_index < self.size:
            raise ValueError(f"end_index {end_index} outside a window of {self.size} bars")
        n = end_index + 1
        return Window(key=self.key, lookback=self.lookback, open_time_ms=self.open_time_ms[:n],
                      open=self.open[:n], high=self.high[:n], low=self.low[:n], close=self.close[:n],
                      volume=self.volume[:n], synthesised_bars=self.synthesised_bars)

    def tail(self, n: int) -> Window:
        """The last ``n`` bars — for a feature that needs a shorter window than was extracted."""
        if n > self.size:
            raise ValueError(f"cannot take {n} bars from a window of {self.size}")
        return Window(key=self.key, lookback=n, open_time_ms=self.open_time_ms[-n:],
                      open=self.open[-n:], high=self.high[-n:], low=self.low[-n:],
                      close=self.close[-n:], volume=self.volume[-n:],
                      synthesised_bars=self.synthesised_bars)


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    """Either a window, or the status and reason explaining why there is none."""

    window: Window | None
    status: FeatureStatus
    reason: str | None = None
    bars_available: int = 0
    detail: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.window is not None


def _arrays(bars: list[Bar], key: SeriesKey, lookback: int, synthesised: int) -> Window:
    n = len(bars)
    out = {name: np.empty(n, dtype=np.float64) for name in ("open", "high", "low", "close", "volume")}
    times = np.empty(n, dtype=np.int64)
    for i, b in enumerate(bars):
        times[i] = b.open_time_ms
        out["open"][i] = float(b.open)
        out["high"][i] = float(b.high)
        out["low"][i] = float(b.low)
        out["close"][i] = float(b.close)
        out["volume"][i] = float(b.volume_base)
    return Window(key=key, lookback=lookback, open_time_ms=times, open=out["open"], high=out["high"],
                  low=out["low"], close=out["close"], volume=out["volume"], synthesised_bars=synthesised)


def extract(state: SeriesState, *, lookback: int, extra_bars: int = 0,
            min_bars: int | None = None) -> WindowOutcome:
    """The last ``lookback + extra_bars`` bars of ``state``, gap-checked.

    ``extra_bars`` is for features that need one bar before the window proper — ``N`` returns need
    ``N+1`` closes, and ATR's true range needs the previous close. Asking for it explicitly keeps
    the off-by-one visible rather than buried in each formula.
    """
    need = lookback + extra_bars
    floor = min_bars if min_bars is not None else need
    key = state.key
    tf = key.timeframe

    if not state.ok and state.reason in ("EMPTY", "EDGE_BEHIND"):
        # The snapshot already judged this series unusable; repeating the arithmetic here would only
        # produce a second, less specific reason.
        return WindowOutcome(None, FeatureStatus.DQ_FAILED,
                             f"series {key} is not usable: {state.detail}",
                             bars_available=len(state.bars))
    if len(state.bars) < floor:
        return WindowOutcome(
            None, FeatureStatus.WARMING,
            f"need {floor} {tf.value} bars, have {len(state.bars)}",
            bars_available=len(state.bars),
            detail={"need": floor, "have": len(state.bars),
                    "hours_to_warm": round((floor - len(state.bars)) * tf.ms / 3_600_000, 2)})

    bars = list(state.bars[-need:])
    if len(bars) < 2:
        return WindowOutcome(None, FeatureStatus.INSUFFICIENT_DATA,
                             f"a window of {len(bars)} bar(s) cannot support a rolling statistic",
                             bars_available=len(bars))

    first, last = bars[0].open_time_ms, bars[-1].open_time_ms
    expected = (last - first) // tf.ms + 1
    holes = sorted(set(range(first, last + 1, tf.ms)) - {b.open_time_ms for b in bars})

    if holes:
        confirmed = [t for t in holes if t in state.absent_times]
        unconfirmed = [t for t in holes if t not in state.absent_times]
        if tf not in SYNTHESIS_ALLOWED:
            which = ("confirmed absent" if not unconfirmed else
                     "missing and not confirmed absent" if not confirmed else
                     f"{len(confirmed)} confirmed absent, {len(unconfirmed)} unconfirmed")
            return WindowOutcome(
                None, FeatureStatus.GAP_IN_WINDOW,
                f"{len(holes)} of {expected} {tf.value} bars in the window are missing ({which}); at "
                f"this horizon an absent bar means something is wrong, not a quiet market",
                bars_available=len(bars),
                detail={"holes": holes[:10], "confirmed_absent": len(confirmed),
                        "unconfirmed": len(unconfirmed), "expected": int(expected)})
        if unconfirmed:
            return WindowOutcome(
                None, FeatureStatus.GAP_IN_WINDOW,
                f"{len(unconfirmed)} {tf.value} bar(s) in the window are missing and not confirmed "
                "absent, so it is unknown whether they never existed or were not fetched",
                bars_available=len(bars),
                detail={"unconfirmed": unconfirmed[:10]})
        bars = _synthesise(bars, confirmed, tf)

    synthesised = sum(1 for b in bars if b.synthesised)
    window = _arrays(bars, key, lookback, synthesised)
    if np.any(window.close <= 0) or np.any(window.high < window.low):
        return WindowOutcome(None, FeatureStatus.DQ_FAILED,
                             "the window contains a non-positive price or an inverted high/low, which "
                             "normalization should have rejected",
                             bars_available=len(bars),
                             detail={"min_close": float(window.close.min())})
    return WindowOutcome(window, FeatureStatus.OK, bars_available=len(bars))


def _synthesise(bars: list[Bar], absent: list[int], tf: Timeframe) -> list[Bar]:
    """Insert flat no-trade bars for confirmed-absent slots (1m only, specs/12 §6.2)."""
    by_time = {b.open_time_ms: b for b in bars}
    for t in sorted(absent):
        prior = by_time.get(t - tf.ms)
        if prior is None:                      # nothing to carry forward from; leave the hole
            continue
        by_time[t] = Bar(asset=prior.asset, product=prior.product, timeframe=tf, open_time_ms=t,
                         open=prior.close, high=prior.close, low=prior.close, close=prior.close,
                         volume_base=Decimal(0), is_final=True, source="synthesised:no_trade",
                         fetched_at_ms=prior.fetched_at_ms, synthesised=True)
    return [by_time[t] for t in sorted(by_time)]
