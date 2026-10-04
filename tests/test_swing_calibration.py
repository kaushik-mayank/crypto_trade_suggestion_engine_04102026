"""PHASE-27 — the structural calibration of ``swing_atr_multiple``.

What these tests defend is not the *value* 0.75; it is that the value was arrived at by a rule that
uses no returns, that the rule's own failure modes are visible, and that the probe refuses rather than
inventing an answer when nothing qualifies.

The last point matters most. A calibration that always produces a number is indistinguishable from a
guess, and the first real run of this probe *did* produce "no viable theta" twice — once because every
candidate was being judged against windows too short to hold a pivot, and once because the spacing
target was derived per window instead of per timeframe (F-038). Both were real mistakes caught by the
probe declining to answer.
"""
from pathlib import Path

import numpy as np
import pytest

from config.parameters import DEFAULT_PATH, load_registry
from core.errors import ConfigError
from normalize.canonical import Bar, Product, Timeframe
from probes import swing_calibration as sc

T0 = 1767225600000
H = 3_600_000
ASSET = "B-BTC_USDT"
REGISTRY = load_registry()
PARAMS = {p.name: p.value for p in REGISTRY.parameters}


def synthetic_bars(n=600, *, swing_every=12, amplitude=0.02, seed=7, tf=Timeframe.H1):
    """A price path with a **known** swing period, so spacing can be checked against a fact.

    A sine wave of period ``swing_every * 2`` bars (a full cycle is two consecutive pivots) plus mild
    noise. Synthetic rather than real history because the point is to assert that the measurement
    recovers a known structure; real bars would only let the test assert that the code ran.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    level = 100 * (1 + amplitude * np.sin(2 * np.pi * t / (swing_every * 2)))
    noise = rng.normal(0, amplitude * 100 * 0.05, n)
    closes = level + noise
    # Each bar's wick is drawn independently rather than derived from the neighbouring close. Deriving
    # it as ``max(prev_close, close) * 1.0005`` puts the *same* high on the two bars either side of
    # every turn, so no bar is a **strict** local maximum and ``find_pivots`` — which requires a strict
    # inequality — returns nothing at any theta. A fixture bug that looked exactly like a broken pivot
    # finder.
    wick = np.abs(rng.normal(0, amplitude * 100 * 0.02, (n, 2))) + 1e-4
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        hi, lo = max(o, c) + wick[i, 0], min(o, c) - wick[i, 1]
        bars.append(Bar(asset=ASSET, product=Product.FUTURES, timeframe=tf,
                        open_time_ms=T0 + i * tf.ms, open=str(o), high=str(hi), low=str(lo),
                        close=str(c), volume_base="100", is_final=True, fetched_at_ms=T0))
    return bars


class FakeStore:
    """Returns the same series for every asset, so cross-asset dispersion is exactly zero."""

    def __init__(self, bars_by_tf):
        self.bars_by_tf = bars_by_tf

    def get_bars(self, asset, product, timeframe, start_ms, end_ms, *, final_only=True,
                 as_of_ms=None):
        return [b for b in self.bars_by_tf.get(timeframe, []) if start_ms <= b.open_time_ms <= end_ms]


# ---- the measurement ----------------------------------------------------------------------

def test_the_measured_spacing_recovers_a_known_swing_period():
    """Hand-constructed: a sine wave with pivots every 12 bars should measure a median spacing near
    12. If this drifts, every spacing number in the grid is meaningless."""
    bars = synthetic_bars(n=800, swing_every=12, amplitude=0.03)
    pair = sc.MeasuredPair(Timeframe.H1, "medium", pivots_of_each_kind=2, consumer="test")
    result = sc.measure(bars, pair, theta=0.5, k=3, asset=ASSET, lookback=168, points=40)
    assert result.median_spacing_bars is not None
    assert 9 <= result.median_spacing_bars <= 15, result.median_spacing_bars


def test_a_larger_theta_finds_strictly_fewer_pivots():
    """The monotonicity the whole calibration rests on: θ is a significance threshold, so raising it
    can only remove pivots. If it did not, the availability ceiling would be meaningless."""
    bars = synthetic_bars(n=800)
    pair = sc.MeasuredPair(Timeframe.H1, "medium", pivots_of_each_kind=2, consumer="test")
    counts = [sc.measure(bars, pair, theta=t, k=3, asset=ASSET, lookback=168,
                         points=30).pivots_total
              for t in (0.25, 0.5, 1.0, 2.0, 4.0)]
    assert counts == sorted(counts, reverse=True), counts
    assert counts[0] > counts[-1]


def test_availability_is_judged_against_what_each_consumer_needs():
    """``1h/short`` needs one pivot of each kind; ``1h/medium`` needs two.

    Measuring both against one rule would either excuse a θ that leaves ``STR_SEQ`` undefined or
    reject one that serves ``STR_SWING`` perfectly well. On the same bars at the same θ, the stricter
    requirement can only be met less often.
    """
    bars = synthetic_bars(n=800)
    lenient = sc.MeasuredPair(Timeframe.H1, "medium", pivots_of_each_kind=1, consumer="test")
    strict = sc.MeasuredPair(Timeframe.H1, "medium", pivots_of_each_kind=3, consumer="test")
    a = sc.measure(bars, lenient, theta=1.0, k=3, asset=ASSET, lookback=168, points=40)
    b = sc.measure(bars, strict, theta=1.0, k=3, asset=ASSET, lookback=168, points=40)
    assert a.availability >= b.availability
    assert a.required_of_each_kind == 1 and b.required_of_each_kind == 3


def test_a_window_shorter_than_the_lookback_measures_nothing_rather_than_guessing():
    bars = synthetic_bars(n=20)
    pair = sc.MeasuredPair(Timeframe.H1, "medium", pivots_of_each_kind=2, consumer="test")
    result = sc.measure(bars, pair, theta=0.5, k=3, asset=ASSET, lookback=168, points=10)
    assert result.windows_evaluated == 0
    assert result.median_spacing_bars is None
    assert result.availability == 0.0


# ---- the derived target -------------------------------------------------------------------

def test_the_spacing_target_is_derived_from_the_registry_not_written_down():
    """``lookback_1h_short / 2`` = 24 / 2 = 12 bars. Hand-computed from specs/02 §4.

    Derived rather than hardcoded so the target cannot drift from the lookbacks the features use; a
    test asserting a literal 12 would pass while the two definitions silently diverged.
    """
    target = sc.target_spacing(PARAMS)
    assert target == {"1h": 12.0}
    assert target["1h"] == PARAMS["lookback_1h_short"] / sc.SPACINGS_PER_CYCLE


def test_the_target_is_per_timeframe_not_per_window():
    """The first run's second mistake (F-038).

    Keying the target by horizon gave ``1h/medium`` a target of 84 bars, which is wrong: pivot spacing
    is a property of the price process and θ, so looking back 168 bars reveals *more* swings, not
    longer ones. Both horizons on one timeframe must share one target.
    """
    target = sc.target_spacing(PARAMS)
    assert set(target) == {"1h"}, "the target must key on timeframe alone"
    assert "1h/medium" not in target and "1h/short" not in target


# ---- refusing to answer -------------------------------------------------------------------

def test_a_window_too_short_to_hold_a_pivot_is_skipped_with_the_arithmetic():
    """``2k + 2 = 8`` bars minimum. The 4h and 1d short lookbacks are 6 and 7, so they cannot host a
    pivot for any θ — and measuring them drove the first run's availability to zero everywhere."""
    assert sc.MIN_PIVOT_BARS == 8
    from features.registry import lookback_for

    assert lookback_for(PARAMS, Timeframe.H4, "short") < sc.MIN_PIVOT_BARS
    assert lookback_for(PARAMS, Timeframe.D1, "short") < sc.MIN_PIVOT_BARS
    # And they are not in the measured set, so the grid is not polluted by an impossibility.
    assert all(not (p.timeframe in (Timeframe.H4, Timeframe.D1) and p.horizon == "short")
               for p in sc.MEASURED)


def test_calibration_refuses_when_no_pair_can_hold_a_pivot():
    """Not a value to pick by hand: it means the lookbacks or ``k`` are the problem."""
    store = FakeStore({Timeframe.H1: synthetic_bars(n=400)})
    crippled = {**PARAMS, "lookback_1h_short": 4, "lookback_1h_medium": 5}
    with pytest.raises(ConfigError, match="long enough to hold a pivot"):
        sc.calibrate(store, crippled, candidates=(0.5,), points=5)


def test_calibration_reports_no_chosen_value_when_nothing_clears_the_floor():
    """``chosen`` is ``None`` and the grid is still written, because the grid is the diagnosis.

    θ = 50 ATR finds no pivots at all, so availability is zero everywhere. The probe must say so
    rather than returning its least-bad candidate — a calibration that always answers is a guess.
    """
    store = FakeStore({Timeframe.H1: synthetic_bars(n=600)})
    report = sc.calibrate(store, PARAMS, candidates=(50.0,), points=10)
    assert report["chosen"] is None
    assert report["candidates"][0]["viable"] is False
    assert report["candidates"][0]["availability_worst_cell"] == 0.0


def test_the_cli_exits_non_zero_when_it_cannot_calibrate(monkeypatch, tmp_path):
    """An operator must not read "calibrated" from a run that calibrated nothing."""
    monkeypatch.setattr(sc, "CANDIDATES", (50.0,))
    monkeypatch.setattr(sc, "OUT", tmp_path / "grid.json")
    assert sc.main(["--points", "5"]) == 1
    assert (tmp_path / "grid.json").exists(), "the grid is the diagnosis and must still be written"


# ---- the tension is reported, not averaged away -------------------------------------------

def test_the_criteria_conflict_is_named_rather_than_split_down_the_middle():
    """specs/02's lookbacks and the spacing target cannot both be satisfied, and that is a finding.

    Availability caps θ around 1.0 — beyond it a 24-bar window stops containing a confirmed pivot of
    each kind and C2-A's structural stop has nothing to reference. Spacing wants θ near 2.0, where
    swings reach the 12-bar scale. The probe computes the gap and says so; presenting the compromise
    as a measurement would be the dishonest option.
    """
    report = _real_grid()
    conflict = report["tension"]
    assert conflict is not None, "the known conflict stopped being reported"
    assert conflict["availability_ceiling_theta"] < conflict["spacing_optimum_theta"]
    assert conflict["availability_at_spacing_optimum"] < sc.MIN_AVAILABILITY
    assert "lookback_1h_short" in conflict["detail"]
    assert "open question" in conflict["resolution"]


def test_the_chosen_value_respects_availability_and_is_reproducible():
    """The selection rule, re-derived from the written grid rather than trusting ``chosen``."""
    report = _real_grid()
    chosen = report["chosen"]
    assert chosen is not None
    assert chosen["availability_worst_cell"] >= sc.MIN_AVAILABILITY

    viable = [c for c in report["candidates"] if c["viable"]]
    best = min(viable, key=lambda c: (round(c["spacing_error"], 3), c["cross_asset_dispersion"]))
    assert best["theta"] == chosen["theta"]
    assert chosen["theta"] == PARAMS["swing_atr_multiple"], \
        "the registry and the calibration grid disagree about theta"


def test_the_grid_records_that_no_returns_were_used():
    """The claim that matters, asserted where a reader will look for it."""
    report = _real_grid()
    assert "No returns, no performance, no P&L" in report["method"]
    entry = next(p for p in load_registry(DEFAULT_PATH).parameters
                 if p.name == "swing_atr_multiple")
    assert "No returns were used" in entry.set_by
    assert entry.status == "provisional", "a value that has never seen a backtest is not fixed"


def _real_grid():
    path = Path("data/probe_results/swing_calibration.json")
    if not path.exists():
        pytest.skip("run `python -m probes.swing_calibration` first")
    import json

    return json.loads(path.read_text(encoding="utf-8"))
