"""Class 1 — correction strategies (specs/03).

**C1-B is the reference strategy for the whole system**: Tier A, fully backtestable, and the first
thing that can be validated. Its edge is *entirely* in the conditioning — an unconditional
mean-reversion strategy on crypto at this horizon is close to a coin flip after costs, because the
asset class trends. Condition 2 (``MR_VR < 1``) is not one condition among six: specs/03 §4 says it
"carries almost all of the load", and if the variance ratio proves unreliable in backtest **C1-B has no
edge and should be cut rather than patched**.

**C1-A is Tier B and cannot be backtested at launch** — no funding history exists. Extremity is
anchored to multiples of *observed baseline* rather than a z-score, because the ±2% cap is a structural
constant while ``FND_Z`` needs accumulated history. Which anchor was used is recorded on every signal.

Both enter through ACT and both manage their lifecycle through :mod:`strategy.lifecycle`, so neither
contains entry mechanics or state-machine logic of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.errors import StrategyError
from features.f4_deviation import VR_HORIZONS
from features.value import FeatureSet, FeatureStatus
from normalize.canonical import Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot
from strategy.act import Direction
from strategy.base import (
    Lifecycle,
    SetupState,
    StrategyOutcome,
    no_setup,
    setup,
    terminal,
    update,
)
from strategy.conditions import (
    Condition,
    ConditionContext,
    ConditionResult,
    dwell_stable,
    evaluate_all,
    threshold,
)
from strategy.evaluation import (
    Levels,
    atr_1h,
    build_signal,
    compute_act,
    detail_of,
    price_now,
    signal_id,
)
from strategy.lifecycle import Invalidation, advance

COMPONENT = "strategy.c1"

#: The modal observed funding rate on these instruments (specs/03 §3 gives baseline 0.005-0.01% per 8 h;
#: measured at exactly 0.0001 throughout the recorded window). It is the denominator of ``FND_MULT``
#: while ``FND_Z`` has no history. **Not a tunable parameter** — an observed property of the venue,
#: recorded with its measurement so a change is a deliberate edit rather than a silent drift.
FUNDING_BASELINE = Decimal("0.0001")

#: ``FND_Z`` becomes the preferred extremity anchor once this many observations exist (specs/03 §3).
FND_Z_MIN_OBSERVATIONS = 500

#: C1-A expires after 3 funding intervals (specs/03 §3).
C1A_EXPIRY_MS = 3 * 8 * 3_600_000
#: C1-B expires after ``N_exp`` 1h bars; the spec leaves N_exp unnamed, so the shared short lookback
#: (24 bars = 1 day) is used, which matches C1-A's window and adds no parameter.
C1B_EXPIRY_BARS = 24


# ---- condition bodies --------------------------------------------------------------------

def _vr_majority_below_one(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """C1-B condition 2: ``MR_VR(k) < 1`` for a majority of k in {2, 4, 8} on 1h.

    Reported as the fraction of *available* horizons below 1, naming the ones that could not be
    computed: a majority of two available horizons is weaker evidence than a majority of three, and
    the audit needs to see which it was.
    """
    below, available, missing = 0, 0, []
    for k in VR_HORIZONS:
        fv = ctx.features.get(f"MR_VR_K{k}", Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            missing.append(f"k={k}: {fv.status.value}")
            continue
        available += 1
        if fv.value < 1.0:
            below += 1
    if available == 0:
        raise StrategyError(
            f"no variance-ratio horizon is available ({'; '.join(missing)})",
            component=COMPONENT, operation="vr_majority",
            context={"missing": missing},
            likely_cause="the 1h short window is warming or contains a gap",
            remediation="the strategy declines; condition 2 carries almost all of C1-B's load and has "
                        "no substitute",
        )
    ctx.scratch["vr_below_one"] = below
    ctx.scratch["vr_available"] = available
    ctx.scratch["vr_missing"] = missing
    return below * 2 > available, f"{below}/{available} below 1", "a majority"


def _structural_confluence(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """C1-B condition 4: price within ``c * ATR_1h`` of a confirmed swing level or prior extreme.

    Either source satisfies it. ``STR_SWING`` is preferred; ``STR_DIST_PDH`` is the fallback, which
    matters because Family 3's pivots are dormant until ``swing_atr_multiple`` is calibrated while the
    prior-day extreme is available today.
    """
    limit = float(ctx.param("act_trigger_multiple"))
    tried: list[str] = []
    for name in ("STR_SWING", "STR_DIST_PDH"):
        fv = ctx.features.get(name, Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            tried.append(f"{name}: {fv.status.value}")
            continue
        distance = abs(fv.value)
        ctx.scratch["structural_source"] = name
        ctx.scratch["structural_level"] = (fv.detail.get("nearest_level")
                                          or fv.detail.get("prior_high"))
        ctx.scratch["structural_distance_atr"] = distance
        return distance <= limit, f"{name} at {distance:.3f} ATR", f"<= {limit} ATR"
    raise StrategyError(
        f"no structural reference is available ({'; '.join(tried)})",
        component=COMPONENT, operation="structural_confluence",
        context={"tried": tried},
        likely_cause="swing pivots need a calibrated swing_atr_multiple, and the prior-period extreme "
                     "needs the 1d series in the snapshot",
        remediation="calibrate swing_atr_multiple or include the 1d series; condition 4 is the "
                    "independent reason to arrest and is not optional",
    )


def _funding_extremity(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """C1-A condition 1, with the anchor recorded on the signal."""
    m_extreme = float(ctx.param("funding_extreme_multiple"))
    z_threshold = float(ctx.param("deviation_z_threshold"))
    z = ctx.features.get("FND_Z")
    if (z.status is FeatureStatus.OK and z.value is not None
            and int(z.detail.get("observations", 0)) >= FND_Z_MIN_OBSERVATIONS):
        ctx.scratch["funding_anchor"] = "FND_Z"
        ctx.scratch["funding_observations"] = z.detail.get("observations")
        return abs(z.value) >= z_threshold, abs(z.value), z_threshold

    rate = ctx.feature("FND_RATE")
    mult = abs(Decimal(str(rate))) / FUNDING_BASELINE
    ctx.scratch["funding_anchor"] = "FND_MULT"
    ctx.scratch["fnd_mult"] = float(mult)
    ctx.scratch["funding_rate"] = rate
    return float(mult) >= m_extreme, float(mult), m_extreme


def _funding_persisted(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """C1-A condition 2: extremity persisted across >= 2 consecutive observations — not a print spike.

    Read from the Tier B history the snapshot carries, so it is reconstructible rather than accumulated.
    """
    m_extreme = Decimal(str(ctx.param("funding_extreme_multiple")))
    if not ctx.snapshot.has_tier_b_series("futures_rt", ctx.asset):
        raise StrategyError(
            "no funding history in this snapshot, so persistence cannot be checked",
            component=COMPONENT, operation="funding_persisted",
            context={"asset": ctx.asset},
            likely_cause="the snapshot request did not ask for Tier B history",
            remediation="set tier_b_history_ms on the SnapshotRequest",
        )
    history = ctx.snapshot.tier_b_series("futures_rt", ctx.asset)
    if len(history) < 2:
        return False, f"{len(history)} observation(s)", ">= 2 consecutive"
    recent = history[-2:]
    extreme = [abs(Decimal(str(r.funding_rate))) / FUNDING_BASELINE >= m_extreme for r in recent]
    same_sign = len({Decimal(str(r.funding_rate)) > 0 for r in recent}) == 1
    return (all(extreme) and same_sign,
            f"{sum(extreme)}/2 extreme, same sign={same_sign}", "2/2 extreme, same sign")


def _funding_basis_agree(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """C1-A condition 3 — a **data-quality check only** (specs/03 §3).

    Funding and basis should share a sign; disagreement means one feed is wrong, not that the
    opportunity is better. That is exactly why it is a condition and not a strength input.
    """
    rate = ctx.feature("FND_RATE")
    basis = ctx.feature("BAS_BPS")
    agree = rate == 0 or basis == 0 or (rate > 0) == (basis > 0)
    return agree, f"fr={rate:+.6f}, basis={basis:+.2f} bps", "same sign"


def _funding_prediction_confirms(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """C1-A condition 4: ``sign(efr) == sign(fr)`` — the next interval is not already normalising."""
    rate = ctx.feature("FND_RATE")
    predicted = ctx.feature("FND_PRED")
    confirms = predicted != 0 and (predicted > 0) == (rate > 0)
    return confirms, f"fr={rate:+.6f}, efr={predicted:+.6f}", "same sign"


def _squeeze_veto(ctx: ConditionContext) -> tuple[bool, Any, Any]:
    """C1-A's regime gate: veto when volatility is expanding **and** the market is trending.

    That is the squeeze: the crowded side is crowded *because they are right*. specs/03 §3 calls this
    gate "the difference between this strategy working and not", so an unavailable regime fails the
    condition rather than waving it through.

    In degraded mode the fitted top-tercile boundary is unavailable, so "expanding" falls back to
    ``VOL_RATIO > 1`` (short-horizon volatility above long-horizon), which is the raw-axis reading
    specs/12 §10 preserves. The boundary actually used is reported.
    """
    r = ctx.regime
    vol_ratio = r.axis_values.get("vol_ratio")
    mr_vr = r.axis_values.get("mr_vr")
    if vol_ratio is None or mr_vr is None:
        raise StrategyError(
            f"the regime axes are unavailable, so the squeeze veto cannot be evaluated: "
            f"{r.reason or 'unclassified'}",
            component=COMPONENT, operation="squeeze_veto",
            context={"regime_reason": r.reason, "axis_values": dict(r.axis_values)},
            likely_cause="an axis feature was not OK at this as_of",
            remediation="the strategy declines; this gate is what stops C1-A fading a genuine trend",
        )
    boundaries = r.axis_boundaries.get("vol_ratio") or []
    expanding_from = float(boundaries[-1]) if boundaries else 1.0
    squeezing = vol_ratio >= expanding_from and mr_vr > 1.0
    ctx.scratch["squeeze_boundary"] = expanding_from
    return (not squeezing,
            f"vol_ratio={vol_ratio:.3f} (expanding at >= {expanding_from:.3f}), mr_vr={mr_vr:.3f}",
            "not (expanding and trending)")


# ---- C1-B ---------------------------------------------------------------------------------

@dataclass
class C1B:
    """C1-B — Conditional Statistical Overextension (specs/03 §4). Tier A, backtestable."""

    params: dict[str, Any] = field(default_factory=dict)
    id: str = "C1-B"
    version: str = "1.0.0"
    strategy_class: int = 1
    tier: str = "A"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = (
        "DEV_Z", "MR_VR_K2", "MR_VR_K4", "MR_VR_K8", "STR_SWING", "STR_DIST_PDH", "VLM_ZROBUST",
        "ATR",
    )

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            threshold("deviation_in_tail",
                      "the 1h deviation from its trailing mean is in the tail",
                      "DEV_Z", timeframe=Timeframe.H1, horizon="medium", op=">=",
                      limit_param="deviation_z_threshold", transform=abs),
            Condition(name="mean_reversion_operative",
                      description="a majority of variance-ratio horizons read below 1, so mean "
                                  "reversion is measurably the operative behaviour",
                      body=_vr_majority_below_one,
                      required_features=tuple(f"MR_VR_K{k}" for k in VR_HORIZONS)),
            dwell_stable(),
            Condition(name="structural_confluence",
                      description="price is close to a confirmed swing level or prior-period extreme, "
                                  "giving the deviation an independent reason to arrest",
                      body=_structural_confluence,
                      required_features=("STR_SWING", "STR_DIST_PDH")),
            threshold("daily_not_trending",
                      "the daily variance ratio is not strongly above 1",
                      "MR_VR_K2", timeframe=Timeframe.D1, horizon="short", op="<", limit=1.2),
            threshold("volume_not_climactic",
                      "volume is not in the extreme upper tail, which would suggest genuine "
                      "repricing rather than overextension",
                      "VLM_ZROBUST", timeframe=Timeframe.H1, horizon="medium", op="<", limit=3.0),
        )

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))

        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, ctx, results)

        if not all(r.passed for r in results):
            failed = [r.name for r in results if not r.passed]
            return no_setup(results, reason=f"{len(failed)} condition(s) not met: {', '.join(failed)}")

        dev_z = features.value("DEV_Z", Timeframe.H1, "medium")
        direction = Direction.LONG if dev_z < 0 else Direction.SHORT
        return self._arm(snapshot, features, regime, ctx, results, direction, dev_z)

    # ---- arming -------------------------------------------------------------------------

    def _arm(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
             ctx: ConditionContext, results: tuple[ConditionResult, ...], direction: Direction,
             dev_z: float) -> StrategyOutcome:
        armed_at = snapshot.as_of_ms
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + C1B_EXPIRY_BARS * Timeframe.H1.ms,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"structural_level": ctx.scratch.get("structural_level"),
                     "structural_source": ctx.scratch.get("structural_source"),
                     "dev_z_at_arming": dev_z})
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product, direction,
                                          armed_at, self.params)
        state.x_star, state.e_t = act_state.x_star, act_state.e_t
        levels = self._levels(features, act_state, direction)
        rationale = (
            f"DEV_Z({Timeframe.H1.value}) = {dev_z:+.2f} with "
            f"{ctx.scratch.get('vr_below_one')}/{ctx.scratch.get('vr_available')} variance-ratio "
            f"horizons below 1, so mean reversion is the operative behaviour; price is "
            f"{ctx.scratch.get('structural_distance_atr', float('nan')):.2f} ATR from a "
            f"{ctx.scratch.get('structural_source')} level. Entry ratchets with ACT at "
            f"{act_state.e_t} (delta {act_state.delta} x {act_state.unit})."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={"act_unit_bars": unit_bars,
                                      "act_unit_timeframe": Timeframe.M15.value,
                                      "vr_horizons_below_one": ctx.scratch.get("vr_below_one"),
                                      "vr_horizons_available": ctx.scratch.get("vr_available"),
                                      "vr_horizons_missing": ctx.scratch.get("vr_missing"),
                                      "structural_source": ctx.scratch.get("structural_source")})
        return setup(signal, results, state,
                     reason=f"armed {direction.value} at {snapshot.as_of_ms}")

    def _levels(self, features: FeatureSet, act_state: Any, direction: Direction) -> Levels:
        """Stop ``X* -/+ s * ATR_1h``; T1 halfway back to the SMA, T2 the SMA itself.

        The target is **where the restoring force stops acting** (specs/03 §4), not a multiple of R. If
        T1 does not clear the minimum R:R gate, the risk engine rejects the signal — which is the
        quality filter working, not a level to adjust.
        """
        s = Decimal(str(self.params["stop_buffer_atr_multiple"]))
        atr = atr_1h(features)
        sma_detail = detail_of(features, "DEV_Z", Timeframe.H1, "medium")
        sma = Decimal(repr(sma_detail["sma"])) if "sma" in sma_detail else None
        entry = act_state.e_t
        stop = (act_state.x_star - s * atr if direction is Direction.LONG
                else act_state.x_star + s * atr)
        targets: tuple[Decimal, ...] = ()
        if sma is not None:
            halfway = entry + (sma - entry) / 2
            targets = (halfway, sma)
        return Levels(entry=entry, stop=stop, targets=targets,
                      detail={"atr_1h": str(atr), "stop_buffer_atr_multiple": str(s),
                              "sma_target": str(sma) if sma is not None else None,
                              "target_basis": "T1 = half the deviation closed, T2 = the trailing mean; "
                                              "the restoring force stops acting there"})

    # ---- managing an armed setup ---------------------------------------------------------

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, ctx: ConditionContext,
                results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        levels = self._levels(features, act_state, state.direction)

        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._mechanism_invalidation(features),
                                       lambda: self._structural_invalidation(features, state, price)),
                           stop=levels.stop, targets=levels.targets)

        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results,
                              rationale=progress.reason, state=state, act_state=act_state,
                              detail={"act_unit_bars": unit_bars,
                                      "progress": progress.to_dict(),
                                      "bars_since_armed": act_state.bars_since_armed})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _mechanism_invalidation(self, features: FeatureSet) -> Invalidation:
        """``MR_VR`` crossing above 1 ends the measured mean-reverting behaviour → exit regardless of
        price (specs/03 §4). The thesis, not the price, is what died."""
        fv = features.get("MR_VR_K2", Timeframe.H1, "short")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="mechanism_mr_vr", fired=False,
                                detail=f"MR_VR is {fv.status.value}, so the mechanism cannot be "
                                       f"re-checked this bar: {fv.reason}")
        return Invalidation(name="mechanism_mr_vr", fired=fv.value > 1.0,
                            detail=("the variance ratio crossed above 1: the mean-reverting behaviour "
                                    "the setup was conditioned on has ended"
                                    if fv.value > 1.0 else "mean reversion still operative"),
                            value=round(fv.value, 4), threshold=1.0)

    def _structural_invalidation(self, features: FeatureSet, state: SetupState,
                                price: Decimal) -> Invalidation:
        """Price closing beyond the structural level by more than ``c * ATR_1h`` (specs/03 §4)."""
        level = state.context.get("structural_level")
        if level is None:
            return Invalidation(name="structural", fired=False,
                                detail="no structural level was recorded at arming")
        try:
            atr = atr_1h(features)
        except Exception as e:                                   # noqa: BLE001 - reported, not hidden
            return Invalidation(name="structural", fired=False,
                                detail=f"ATR unavailable, so the structural buffer cannot be "
                                       f"measured: {e}")
        c = Decimal(str(self.params["act_trigger_multiple"]))
        level_d = Decimal(str(level))
        buffer = c * atr
        fired = (price < level_d - buffer if state.direction is Direction.LONG
                 else price > level_d + buffer)
        return Invalidation(name="structural", fired=fired,
                            detail=(f"price {price} closed beyond the structural level {level_d} by "
                                    f"more than {c} x ATR" if fired else
                                    f"price {price} is within {c} x ATR of {level_d}"),
                            value=str(price), threshold=str(level_d))


# ---- C1-A ---------------------------------------------------------------------------------

@dataclass
class C1A:
    """C1-A — Funding Dislocation Reversion (specs/03 §3). **Tier B: not backtestable at launch.**"""

    params: dict[str, Any] = field(default_factory=dict)
    id: str = "C1-A"
    version: str = "1.0.0"
    strategy_class: int = 1
    tier: str = "B"
    tradeable: bool = True
    expansion_seeking: bool = False
    signal_timeframe: Timeframe = Timeframe.H1
    required_features: tuple[str, ...] = ("FND_RATE", "FND_PRED", "FND_TENSION", "FND_Z", "BAS_BPS",
                                          "ATR")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            Condition(name="funding_extreme",
                      description="funding is an extreme multiple of observed baseline (or an extreme "
                                  "z-score once history allows)",
                      body=_funding_extremity, required_features=("FND_RATE", "FND_Z")),
            Condition(name="extremity_persisted",
                      description="the extremity held across at least two consecutive observations, "
                                  "so it is not a print spike",
                      body=_funding_persisted, required_features=("FND_RATE",)),
            Condition(name="funding_basis_agree",
                      description="funding and basis carry the same sign -- a data-quality check, not "
                                  "a strength input",
                      body=_funding_basis_agree, required_features=("FND_RATE", "BAS_BPS")),
            Condition(name="prediction_confirms",
                      description="the predicted rate confirms persistence, so the next interval is "
                                  "not already normalising",
                      body=_funding_prediction_confirms,
                      required_features=("FND_RATE", "FND_PRED")),
            Condition(name="not_squeezing",
                      description="the market is not both expanding and trending, which would mean the "
                                  "crowded side is crowded because they are right",
                      body=_squeeze_veto),
        )

    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                 state: SetupState | None) -> StrategyOutcome:
        ctx = ConditionContext(asset=features.asset, snapshot=snapshot, features=features,
                              regime=regime, params=self.params)
        results = tuple(evaluate_all(self.conditions, ctx))

        if state is not None and state.lifecycle.active:
            return self._manage(snapshot, features, regime, state, results)

        if not all(r.passed for r in results):
            failed = [r.name for r in results if not r.passed]
            return no_setup(results, reason=f"{len(failed)} condition(s) not met: {', '.join(failed)}")

        rate = features.value("FND_RATE")
        # fr strongly positive -> longs are paying -> the perp is rich -> SHORT
        direction = Direction.SHORT if rate > 0 else Direction.LONG
        armed_at = snapshot.as_of_ms
        state = SetupState(
            signal_id=signal_id(self.id, features.asset, armed_at, snapshot.snapshot_id),
            strategy_id=self.id, asset=features.asset, product=features.product,
            direction=direction, armed_at_ms=armed_at, armed_snapshot_id=snapshot.snapshot_id,
            expires_at_ms=armed_at + C1A_EXPIRY_MS,
            parameter_hash=snapshot.parameter_hash, strategy_version=self.version,
            context={"funding_anchor": ctx.scratch.get("funding_anchor"),
                     "fnd_mult_at_arming": ctx.scratch.get("fnd_mult"),
                     "funding_rate_at_arming": rate})
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product, direction,
                                           armed_at, self.params)
        state.x_star, state.e_t = act_state.x_star, act_state.e_t
        levels = self._levels(features, act_state, direction)
        rationale = (
            f"funding is {ctx.scratch.get('fnd_mult', float('nan')):.1f}x baseline "
            f"(fr={rate:+.6f}, anchor={ctx.scratch.get('funding_anchor')}), persisted across two "
            f"observations, and the market is not squeezing "
            f"({ctx.scratch.get('squeeze_boundary')}). The crowded side pays to hold; entry ratchets "
            f"with ACT at {act_state.e_t}."
        )
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=direction, lifecycle=Lifecycle.SETUP_DETECTED,
                              levels=levels, conditions=results, rationale=rationale, state=state,
                              act_state=act_state,
                              detail={"act_unit_bars": unit_bars,
                                      "funding_anchor": ctx.scratch.get("funding_anchor"),
                                      "fnd_mult": ctx.scratch.get("fnd_mult"),
                                      "backtestable": False,
                                      "tier_b_caveat": "no funding history exists before recording "
                                                       "started, so this signal cannot be backtested "
                                                       "and carries validated=false (specs/03 §3)"})
        return setup(signal, results, state, reason=f"armed {direction.value} at {armed_at}")

    def _levels(self, features: FeatureSet, act_state: Any, direction: Direction) -> Levels:
        """Stop ``X* -/+ s * ATR_1h``; T1 is perp fair value from the basis.

        T1 = ``spot_last * (1 + median_basis)`` — where the mechanism's force dissipates, not an
        arbitrary R multiple. T2 is **unpublished** until enough funding history exists to estimate
        where ``FND_MULT`` returns to baseline (specs/03 §3), and saying so is better than inventing it.
        """
        s = Decimal(str(self.params["stop_buffer_atr_multiple"]))
        atr = atr_1h(features)
        stop = (act_state.x_star - s * atr if direction is Direction.LONG
                else act_state.x_star + s * atr)
        targets: tuple[Decimal, ...] = ()
        basis_detail = detail_of(features, "BAS_Z")
        spot_last = basis_detail.get("current_bps")
        bas = detail_of(features, "BAS_BPS")
        median_basis_bps = basis_detail.get("mean_bps")
        if median_basis_bps is not None and bas.get("spot_last") is not None:
            fair = Decimal(repr(bas["spot_last"])) * (1 + Decimal(repr(median_basis_bps)) / 10_000)
            targets = (fair,)
        return Levels(entry=act_state.e_t, stop=stop, targets=targets,
                      detail={"atr_1h": str(atr),
                              "median_basis_bps": median_basis_bps,
                              "t1_basis": "perp fair value = spot_last x (1 + median basis); the "
                                          "mechanism's force dissipates there",
                              "t2": "unpublished until funding history supports estimating where "
                                    "FND_MULT returns to baseline (specs/03 §3)",
                              "current_basis_bps": spot_last})

    def _manage(self, snapshot: MarketSnapshot, features: FeatureSet, regime: RegimeState,
                state: SetupState, results: tuple[ConditionResult, ...]) -> StrategyOutcome:
        act_state, unit_bars = compute_act(snapshot, features.asset, features.product,
                                          state.direction, state.armed_at_ms, self.params)
        price = price_now(features, snapshot)
        levels = self._levels(features, act_state, state.direction)
        progress = advance(state, act_state=act_state, now_ms=snapshot.as_of_ms, price=price,
                           predicates=(lambda: self._mechanism_stop(features, state),),
                           stop=levels.stop, targets=levels.targets)
        signal = build_signal(strategy=self, features=features, snapshot=snapshot, regime=regime,
                              direction=state.direction, lifecycle=progress.lifecycle,
                              levels=levels, conditions=results, rationale=progress.reason,
                              state=state, act_state=act_state,
                              detail={"act_unit_bars": unit_bars, "progress": progress.to_dict()})
        if progress.terminal:
            return terminal(progress.reason, results, signal, state)
        return update(signal, act_state, results, state)

    def _mechanism_stop(self, features: FeatureSet, state: SetupState) -> Invalidation:
        """Dual invalidation (specs/03 §3): if ``FND_MULT`` falls back inside the normal band before
        T1, the restoring force has switched off and **the thesis is dead independently of price**.

        A correction trade whose restoring force has switched off is no longer the trade that was
        entered — so this exits even while price is moving favourably.
        """
        fv = features.get("FND_RATE")
        if fv.status is not FeatureStatus.OK or fv.value is None:
            return Invalidation(name="mechanism_funding", fired=False,
                                detail=f"FND_RATE is {fv.status.value}, so the mechanism cannot be "
                                       f"re-checked: {fv.reason}")
        mult = abs(Decimal(str(fv.value))) / FUNDING_BASELINE
        floor = Decimal(str(self.params["funding_extreme_multiple"]))
        fired = mult < floor
        return Invalidation(
            name="mechanism_funding", fired=fired,
            detail=(f"funding returned to {mult:.1f}x baseline, inside the {floor}x band: the "
                    "restoring force has switched off and the thesis is dead independently of price"
                    if fired else f"funding still {mult:.1f}x baseline"),
            value=float(mult), threshold=float(floor))
