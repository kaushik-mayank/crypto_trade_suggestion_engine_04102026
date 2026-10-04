"""The risk engine (specs/08, specs/13 §8).

Three rules shape this module, and each is enforced structurally rather than by discipline:

**1. It wraps; it never rewrites.** ``Signal`` holds the strategy's ``RawSignal`` unchanged and adds
sizing and vetoes alongside. specs/08 criterion 1 forbids the risk engine altering a strategy-produced
level, and :func:`assert_levels_unchanged` checks the wrapped raw signal against what the strategy
produced, so a mutation is a test failure rather than a subtle divergence.

**2. Every veto is evaluated and logged, with no short-circuit.** A signal failing four checks is
diagnostically different from one failing one, and short-circuiting hides that — including the case
where a data outage masks a trade that would have been rejected on economics anyway.

**3. Size is ``min(size_stop, size_vol)``.** Stop-based sizing alone "fails badly for C3-A", whose stop
is the narrow boundary of a volatility *contraction*: it would produce the largest position immediately
before an expansion, giving the most dangerous moment in the strategy's life the biggest size. So
``sigma_forward`` is the volatility expected **during the hold**, and it has its own named function so
the substitution cannot be made by accident.

Equity is never read from the exchange. Sizing is a fraction of equity the user optionally supplies
locally; no balance or wallet endpoint is called anywhere in this codebase and the API key holds no
permission to reach one (specs/08 §2.3). The endpoint paths are deliberately not written here either:
`tests/test_normalize_hazards.py` greps the source for them, and that guard is worth more than the
convenience of naming them in prose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from core.errors import ImmutabilityViolation, RiskError
from core.logging import get_logger
from features.value import FeatureSet, FeatureStatus
from normalize.canonical import InstrumentMeta, Timeframe
from regime.model import RegimeState
from snapshot.models import MarketSnapshot
from strategy.act import Direction
from strategy.base import RawSignal

log = get_logger("risk.engine")
COMPONENT = "risk.engine"


#: Floors on the **cost-adjusted** reward-to-risk ratio (specs/08 §7.2). Both come from the breakeven
#: identity ``p* = (1 + c/R) / (k + 1)`` by choosing the worst hit rate the class should survive:
#: ~27.5% for Class 3, ~40% for the others. **Neither is a fitted parameter**, so neither lives in the
#: registry -- putting them there would invite tuning something that was derived.
K_NET_FLOOR: dict[int, Decimal] = {1: Decimal("1.8"), 2: Decimal("1.8"), 3: Decimal("3.0"),
                                   4: Decimal("1.8")}

#: Funding intervals in the two-day hold specs/08 §7.3 reasons about (3 per day).
FUNDING_INTERVALS_PER_HOLD = 6


def k_net(*, entry: Decimal, target: Decimal, risk: Decimal, cost_price: Decimal) -> Decimal:
    """``(|T1 - entry| - c_price) / (R + c_price)`` — specs/08 §7.2.

    One gate, not two: every reward-to-risk comparison in the system runs against this figure and never
    against the gross ratio. The cost appears twice, subtracted from the reward and added to the risk,
    because a round trip is paid on the way in and on the way out.
    """
    denominator = risk + cost_price
    if denominator <= 0:
        raise RiskError(
            "risk plus cost is not positive, so a cost-adjusted ratio is undefined",
            component=COMPONENT, operation="k_net",
            context={"risk": str(risk), "cost_price": str(cost_price)},
            likely_cause="R collapsed to zero, or a negative cost was passed",
            remediation="the signal is refused; a ratio computed here would be meaningless",
        )
    return (abs(target - entry) - cost_price) / denominator


def breakeven_hit_rate(k: Decimal, *, risk: Decimal, cost_price: Decimal) -> Decimal | None:
    """``p* = (1 + c/R) / (k + 1)`` — published on every signal (specs/07 §2).

    The honest framing of a reward-to-risk figure: not "how good is this", but "how often must it work
    to break even". A k of 1.8 needs roughly 40%; a k of 3.0 roughly 27.5%.
    """
    if k <= -1 or risk <= 0:
        return None
    return (1 + cost_price / risk) / (k + 1)


class Tier(int, Enum):
    DATA_INTEGRITY = 1
    ECONOMIC = 2
    MARKET = 3
    PORTFOLIO = 4


@dataclass(frozen=True, slots=True)
class VetoResult:
    """One veto, with the inputs that decided it — logged whether or not an earlier one fired."""

    name: str
    tier: Tier
    fired: bool
    detail: str
    value: Any = None
    threshold: Any = None
    #: True when the veto could not be evaluated. Distinct from "did not fire": an unevaluated
    #: data-integrity veto is itself a reason to refuse, and the ladder treats it as one.
    unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tier": int(self.tier), "fired": self.fired,
                "detail": self.detail, "unavailable": self.unavailable,
                "value": _plain(self.value), "threshold": _plain(self.threshold)}


def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


@dataclass(frozen=True)
class SizingResult:
    """What size the risk budget permits, and which constraint bound it."""

    size: Decimal | None
    size_stop: Decimal | None
    size_vol: Decimal | None
    basis: str                      # "stop" | "vol" | "refused"
    sigma_forward: Decimal | None
    class_multiplier: Decimal
    validation_multiplier: Decimal
    notional: Decimal | None = None
    min_equity_required: Decimal | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"size": _plain(self.size), "size_stop": _plain(self.size_stop),
                "size_vol": _plain(self.size_vol), "basis": self.basis,
                "sigma_forward": _plain(self.sigma_forward),
                "class_multiplier": str(self.class_multiplier),
                "validation_multiplier": str(self.validation_multiplier),
                "notional": _plain(self.notional),
                "min_equity_required": _plain(self.min_equity_required), "reason": self.reason}


@dataclass(frozen=True)
class Signal:
    """A raw signal **wrapped** with risk output. The raw signal is never rebuilt."""

    raw: RawSignal
    sizing: SizingResult
    vetoes: tuple[VetoResult, ...]
    approved: bool
    equity: Decimal | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def fired_vetoes(self) -> tuple[VetoResult, ...]:
        return tuple(v for v in self.vetoes if v.fired)

    @property
    def tradeable(self) -> bool:
        """Whether this may reach a user. The control strategies set ``tradeable=False`` on the raw
        signal's detail, and the API filters on it (specs/13 §13)."""
        return bool(self.raw.detail.get("tradeable", True))

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw.to_dict(), "sizing": self.sizing.to_dict(),
                "vetoes": [v.to_dict() for v in self.vetoes],
                "fired_vetoes": [v.name for v in self.fired_vetoes],
                "approved": self.approved, "tradeable": self.tradeable,
                "equity": _plain(self.equity), "detail": dict(self.detail)}


def assert_levels_unchanged(original: RawSignal, wrapped: RawSignal) -> None:
    """specs/08 criterion 1: the risk engine may veto or scale, never alter a level.

    Compared field by field rather than by identity, so a copy that *looks* equal still has to be.
    """
    for field_name in ("entry", "stop", "targets", "direction", "asset", "signal_id"):
        if getattr(original, field_name) != getattr(wrapped, field_name):
            raise ImmutabilityViolation(
                f"the risk engine altered {field_name} on {original.signal_id}",
                component=COMPONENT, operation="assert_levels_unchanged",
                context={"field": field_name, "before": str(getattr(original, field_name)),
                         "after": str(getattr(wrapped, field_name))},
                likely_cause="a level was recomputed instead of wrapped",
                remediation="the risk engine wraps a RawSignal and adds sizing and vetoes beside it; "
                            "rounding or adjusting a published level belongs to the strategy",
            )


def sigma_forward(strategy_id: str, features: FeatureSet, *, expansion_seeking: bool) -> Decimal:
    """The volatility expected **during the hold**, not the volatility observed now.

    Its own function precisely so the substitution cannot be made by accident (specs/13 §8.3). For an
    expansion-seeking strategy — C3-A, which arms *inside* a contraction — the forward volatility is the
    **long-horizon** ATR-equivalent the setup expects to return to. Using the contracted value would
    reproduce the exact bug ``min(size_stop, size_vol)`` exists to prevent: the largest position
    immediately before an expansion.
    """
    horizon = "long" if expansion_seeking else "medium"
    try:
        value = features.value("VOL_CC", Timeframe.H1, horizon)
    except Exception as e:                                       # noqa: BLE001 - re-raised classified
        raise RiskError(
            f"forward volatility is unavailable for {strategy_id}: {e}",
            component=COMPONENT, operation="sigma_forward",
            context={"strategy_id": strategy_id, "horizon": horizon,
                     "expansion_seeking": expansion_seeking},
            likely_cause="VOL_CC is not OK at this as_of",
            remediation="no size can be computed without forward volatility; the signal is refused "
                        "rather than sized on the observed value",
            cause=e,
        ) from e
    return Decimal(repr(value))


@dataclass
class RiskEngine:
    """Sizes and vets signals. Holds no strategy names — behaviour comes from declared flags."""

    params: dict[str, Any]
    #: Optional, local-only, display-convenience equity (specs/08 §2.3). Without it, sizing is
    #: expressed as a fraction and the notional checks that need an absolute size are reported as
    #: unavailable rather than guessed.
    equity: Decimal | None = None
    #: Concurrent Class 3 positions, supplied by the caller from open state (Tier 4).
    open_class3: int = 0
    #: Portfolio volatility already committed, as an annualised fraction.
    committed_portfolio_vol: Decimal = Decimal(0)

    def evaluate(self, signal: RawSignal, *, snapshot: MarketSnapshot, features: FeatureSet,
                 regime: RegimeState, instrument: InstrumentMeta | None,
                 expansion_seeking: bool, strategy_class: int) -> Signal:
        """Size, run all four veto tiers, and wrap. Never raises on a veto — that is the answer."""
        vetoes: list[VetoResult] = []
        vetoes += self._tier1(signal, snapshot, features, instrument)
        sizing = self._size(signal, features, instrument, expansion_seeking=expansion_seeking,
                            strategy_class=strategy_class)
        vetoes += self._tier2(signal, features, instrument, sizing, strategy_class=strategy_class)
        vetoes += self._tier3(signal, features, regime, instrument=instrument,
                              expansion_seeking=expansion_seeking, strategy_class=strategy_class)
        vetoes += self._tier4(signal, sizing, strategy_class=strategy_class)

        fired = [v for v in vetoes if v.fired]
        approved = not fired and sizing.size is not None
        k, cost_price, p_star = self._k_net(signal, instrument)
        wrapped = Signal(raw=signal, sizing=sizing, vetoes=tuple(vetoes), approved=approved,
                         equity=self.equity,
                         detail={"tiers_evaluated": sorted({int(v.tier) for v in vetoes}),
                                 "vetoes_total": len(vetoes), "vetoes_fired": len(fired),
                                 "k_net": None if k is None else str(round(k, 4)),
                                 "k_net_floor": str(K_NET_FLOOR.get(strategy_class, Decimal("1.8"))),
                                 "round_trip_cost_price": str(cost_price),
                                 # specs/07 §2: published on every signal, because "how often must
                                 # this work to break even" is the honest reading of a k figure.
                                 "breakeven_hit_rate": (None if p_star is None
                                                        else str(round(p_star, 4)))})
        assert_levels_unchanged(signal, wrapped.raw)
        if fired:
            log.info("signal_vetoed", signal_id=signal.signal_id, strategy=signal.strategy_id,
                     asset=signal.asset, fired=[v.name for v in fired],
                     tiers=[int(v.tier) for v in fired])
        return wrapped

    # ---- Tier 1: data integrity ----------------------------------------------------------

    def _tier1(self, signal: RawSignal, snapshot: MarketSnapshot, features: FeatureSet,
               instrument: InstrumentMeta | None) -> list[VetoResult]:
        out: list[VetoResult] = []

        stale = [f"{s}:{a}" for (s, a), pv in snapshot.point_in_time.items()
                 if a == signal.asset and not pv.ok]
        out.append(VetoResult(
            name="DQ_STALE", tier=Tier.DATA_INTEGRITY, fired=bool(stale),
            detail=("; ".join(stale) if stale else "every point-in-time source for this asset is fresh"),
            value=len(stale), threshold=0))

        # DQ_SKW is deliberately NOT a veto: F-004 measured skw as one value shared by every fresh row,
        # drifting over time, so rejecting on it would discard all live data. Recorded as not applicable
        # rather than silently omitted, because specs/08 §8 lists it and the deviation must be visible.
        out.append(VetoResult(
            name="DQ_SKW", tier=Tier.DATA_INTEGRITY, fired=False, unavailable=True,
            detail="not implemented as a veto: skw is not a per-row health flag (F-004); freshness is "
                   "judged on btST/bmST age, which DQ_STALE covers"))

        series_bad = [str(k) for k, st in snapshot.series.items()
                      if k.asset == signal.asset and not st.ok]
        out.append(VetoResult(
            name="DQ_SCHEMA", tier=Tier.DATA_INTEGRITY, fired=False,
            detail=("normalization rejects malformed payloads before storage; unusable series here: "
                    + (", ".join(series_bad) if series_bad else "none")),
            value=len(series_bad)))

        basis = features.get("BAS_BPS")
        if basis.status is FeatureStatus.OK and basis.value is not None:
            out.append(VetoResult(
                name="DQ_DISAGREE", tier=Tier.DATA_INTEGRITY, fired=abs(basis.value) > 50.0,
                detail=f"spot/futures basis {basis.value:+.2f} bps",
                value=round(basis.value, 3), threshold=50.0))
        else:
            out.append(VetoResult(
                name="DQ_DISAGREE", tier=Tier.DATA_INTEGRITY, fired=False, unavailable=True,
                detail=f"basis unavailable ({basis.status.value}): {basis.reason}"))

        out.append(VetoResult(
            name="DQ_META", tier=Tier.DATA_INTEGRITY, fired=instrument is None,
            detail=("instrument metadata is unavailable, so costs cannot be computed and no signal is "
                    "possible" if instrument is None else
                    f"tick={instrument.tick}, taker fee={instrument.taker_fee.fraction}")))

        clock = snapshot.dq.get("clock_skew_ms")
        tolerance = float(self.params.get("clock_skew_tolerance_s", 3.0)) * 1000
        out.append(VetoResult(
            name="DQ_CLOCK", tier=Tier.DATA_INTEGRITY,
            fired=clock is not None and abs(float(clock)) > tolerance,
            unavailable=clock is None,
            detail=(f"measured skew {clock} ms against a {tolerance:.0f} ms tolerance"
                    if clock is not None else "no clock measurement in this snapshot's dq block"),
            value=clock, threshold=tolerance))
        return out

    # ---- sizing ---------------------------------------------------------------------------

    def _size(self, signal: RawSignal, features: FeatureSet, instrument: InstrumentMeta | None,
              *, expansion_seeking: bool, strategy_class: int) -> SizingResult:
        class_mult = Decimal(str(self.params.get("class3_multiplier", 1)
                                 if strategy_class == 3 else 1))
        # Nothing is validated until Phase 16 says so, so the judgment multiplier applies to everything
        # the system currently produces (specs/08 §11, D-11).
        validation_mult = Decimal(str(self.params.get("unvalidated_multiplier", 1)))
        r = signal.risk_per_unit

        if r is None or r == 0:
            return SizingResult(size=None, size_stop=None, size_vol=None, basis="refused",
                                sigma_forward=None, class_multiplier=class_mult,
                                validation_multiplier=validation_mult,
                                reason="R is zero or undefined, so risk-based sizing is impossible")
        try:
            sigma = sigma_forward(signal.strategy_id, features,
                                  expansion_seeking=expansion_seeking)
        except RiskError as e:
            return SizingResult(size=None, size_stop=None, size_vol=None, basis="refused",
                                sigma_forward=None, class_multiplier=class_mult,
                                validation_multiplier=validation_mult, reason=e.message)

        risk_fraction = Decimal(str(self.params["risk_per_trade"]))
        vol_budget = Decimal(str(self.params["portfolio_vol_target"]))
        price = signal.entry or Decimal(0)

        if self.equity is None:
            # Sizes are expressed per unit of equity: a fraction is honest and still usable, and the
            # system never needs the user's balance in order to suggest anything.
            size_stop = risk_fraction / r
            size_vol = (vol_budget / (price * sigma)) if price > 0 and sigma > 0 else None
        else:
            size_stop = (self.equity * risk_fraction) / r
            size_vol = ((self.equity * vol_budget) / (price * sigma)
                        if price > 0 and sigma > 0 else None)

        candidates = [c for c in (size_stop, size_vol) if c is not None]
        chosen = min(candidates)
        basis = "stop" if chosen == size_stop else "vol"
        size = chosen * class_mult * validation_mult
        notional = size * price if self.equity is not None else None
        min_equity = None
        if instrument is not None and self.equity is None and price > 0:
            # specs/08 §9: publish what min_notional implies rather than over-risking to satisfy it.
            min_equity = instrument.min_notional * r / (price * risk_fraction * class_mult
                                                        * validation_mult)
        return SizingResult(size=size, size_stop=size_stop, size_vol=size_vol, basis=basis,
                            sigma_forward=sigma, class_multiplier=class_mult,
                            validation_multiplier=validation_mult, notional=notional,
                            min_equity_required=min_equity,
                            reason=None if self.equity is not None else
                            "expressed as a fraction of equity: no balance is read from the exchange")

    # ---- Tier 2: economic viability -------------------------------------------------------

    def _k_net(self, signal: RawSignal, instrument: InstrumentMeta | None
               ) -> tuple[Decimal | None, Decimal, Decimal | None]:
        """``(k_net, cost_price, breakeven_hit_rate)``; ``k_net`` is None when it cannot be computed."""
        risk = signal.risk_per_unit
        if instrument is None or signal.entry is None or risk is None or risk <= 0 \
                or not signal.targets:
            return None, Decimal(0), None
        cost_price = 2 * instrument.taker_fee.fraction * signal.entry
        try:
            k = k_net(entry=signal.entry, target=signal.targets[0], risk=risk, cost_price=cost_price)
        except RiskError:
            return None, cost_price, None
        return k, cost_price, breakeven_hit_rate(k, risk=risk, cost_price=cost_price)

    def _tier2(self, signal: RawSignal, features: FeatureSet, instrument: InstrumentMeta | None,
               sizing: SizingResult, *, strategy_class: int) -> list[VetoResult]:
        out: list[VetoResult] = []
        floor = K_NET_FLOOR.get(strategy_class, Decimal("1.8"))
        k, cost_price, p_star = self._k_net(signal, instrument)
        out.append(VetoResult(
            name="RR_FLOOR", tier=Tier.ECONOMIC,
            fired=k is not None and k < floor, unavailable=k is None,
            detail=(f"k_net {k:.3f} against the Class {strategy_class} floor of {floor} "
                    f"(round-trip cost {cost_price}); breakeven hit rate "
                    f"{float(p_star) * 100:.1f}%" if k is not None and p_star is not None else
                    "k_net needs a published target and instrument metadata"),
            value=None if k is None else round(float(k), 4), threshold=float(floor)))

        if instrument is not None and self.equity is not None and sizing.notional is not None:
            out.append(VetoResult(
                name="MIN_NOTIONAL", tier=Tier.ECONOMIC,
                fired=sizing.notional < instrument.min_notional,
                detail=(f"risk-correct notional {sizing.notional:.2f} against a minimum of "
                        f"{instrument.min_notional}; taking it at the minimum would over-risk "
                        "silently, which is how small accounts are destroyed (specs/08 §9)"),
                value=str(sizing.notional), threshold=str(instrument.min_notional)))
        else:
            out.append(VetoResult(
                name="MIN_NOTIONAL", tier=Tier.ECONOMIC, fired=False, unavailable=True,
                detail=("no equity is configured, so an absolute notional cannot be compared with "
                        f"min_notional; min_equity_required = {sizing.min_equity_required}")))

        degenerate = False
        detail = "no instrument metadata"
        if instrument is not None and signal.entry is not None and signal.stop is not None:
            tick = instrument.tick
            r_ticks = abs(signal.entry - signal.stop) / tick
            long = signal.direction is Direction.LONG
            crosses = (signal.stop >= signal.entry) if long else (signal.stop <= signal.entry)
            degenerate = crosses or r_ticks < 1
            detail = (f"R is {r_ticks:.1f} ticks; stop {'crosses' if crosses else 'is clear of'} entry")
        out.append(VetoResult(name="TICK_DEGENERATE", tier=Tier.ECONOMIC, fired=degenerate,
                              detail=detail, unavailable=instrument is None))

        band_fired, band_detail = False, "no price band metadata"
        if instrument is not None and instrument.multiplier_up is not None and signal.entry:
            last = features.get("LIQ_SPREAD").detail.get("mid")
            if last:
                limit = Decimal(repr(last)) * instrument.multiplier_up
                floor_price = Decimal(repr(last)) * (instrument.multiplier_down or Decimal(1))
                band_fired = not (floor_price <= signal.entry <= limit)
                band_detail = (f"entry {signal.entry} against the exchange band "
                               f"[{floor_price:.2f}, {limit:.2f}]")
        out.append(VetoResult(name="PRICE_BAND", tier=Tier.ECONOMIC, fired=band_fired,
                              detail=band_detail,
                              unavailable=instrument is None or instrument.multiplier_up is None))
        return out

    # ---- Tier 3: market conditions --------------------------------------------------------

    def _tier3(self, signal: RawSignal, features: FeatureSet, regime: RegimeState, *,
               instrument: InstrumentMeta | None, expansion_seeking: bool,
               strategy_class: int) -> list[VetoResult]:
        out: list[VetoResult] = []
        spread = features.get("LIQ_SPREAD")
        r = signal.risk_per_unit
        if spread.status is FeatureStatus.OK and spread.value is not None and r and signal.entry:
            half_spread_price = signal.entry * Decimal(repr(spread.value)) / 2 / Decimal(10_000)
            fraction = half_spread_price / r
            out.append(VetoResult(
                name="SPREAD", tier=Tier.MARKET, fired=fraction > Decimal("0.10"),
                detail=(f"half-spread is {float(fraction) * 100:.2f}% of R -- expressed against the "
                        "trade's own risk so it scales across assets and regimes"),
                value=round(float(fraction), 5), threshold=0.10))
        else:
            out.append(VetoResult(name="SPREAD", tier=Tier.MARKET, fired=False, unavailable=True,
                                  detail=f"spread unavailable: {spread.reason}"))

        depth = features.get("LIQ_DEPTH")
        if depth.status is FeatureStatus.OK:
            bands = depth.detail.get("bands", {})
            at_25 = bands.get("25bps")
            out.append(VetoResult(
                name="DEPTH", tier=Tier.MARKET, fired=False, unavailable=at_25 is None,
                detail=("depth within 25 bps is not observable at this venue: the futures book spans "
                        f"~{depth.detail.get('visible_reach_bps')} bps (F-032). Visible-book notional "
                        f"is {depth.detail.get('visible_notional')}, which is what a size check can "
                        "use" if at_25 is None else f"25 bps depth {at_25}"),
                value=at_25 if at_25 is not None else depth.detail.get("visible_notional")))
        else:
            out.append(VetoResult(name="DEPTH", tier=Tier.MARKET, fired=False, unavailable=True,
                                  detail=f"book unavailable: {depth.reason}"))

        vol_ratio = regime.axis_values.get("vol_ratio")
        extreme = 2.0            # structural: short-horizon vol at twice the long-horizon norm
        out.append(VetoResult(
            name="VOL_EXTREME", tier=Tier.MARKET,
            fired=(vol_ratio is not None and vol_ratio >= extreme and not expansion_seeking),
            unavailable=vol_ratio is None,
            detail=("exempt: this strategy declares itself expansion-seeking" if expansion_seeking
                    else f"VOL_RATIO {vol_ratio} against an extreme threshold of {extreme}"
                    if vol_ratio is not None else "VOL_RATIO unavailable"),
            value=vol_ratio, threshold=extreme))

        # specs/08 §7.3: funding is a veto, not only a cost. Rather than invent a threshold, the
        # projected carry over a two-day hold is added to the round-trip cost and k_net is recomputed:
        # if the carry pushes the trade below its class floor, the trade is uneconomic. At the verified
        # +/-2% cap that is 12% over two days, which no swing target survives -- and the arithmetic says
        # so without a number anyone chose.
        funding = features.get("FND_RATE")
        floor = K_NET_FLOOR.get(strategy_class, Decimal("1.8"))
        k_with_carry: Decimal | None = None
        adverse = False
        carry = Decimal(0)
        if (funding.status is FeatureStatus.OK and funding.value is not None
                and instrument is not None and signal.entry is not None and signal.targets
                and signal.risk_per_unit):
            rate = Decimal(repr(funding.value))
            paying = (rate > 0) if signal.direction is Direction.LONG else (rate < 0)
            carry = (abs(rate) * FUNDING_INTERVALS_PER_HOLD * signal.entry) if paying else Decimal(0)
            base_cost = 2 * instrument.taker_fee.fraction * signal.entry
            try:
                k_with_carry = k_net(entry=signal.entry, target=signal.targets[0],
                                     risk=signal.risk_per_unit, cost_price=base_cost + carry)
                adverse = k_with_carry < floor
            except RiskError:
                k_with_carry = None
        out.append(VetoResult(
            name="FUNDING_ADVERSE", tier=Tier.MARKET, fired=adverse,
            unavailable=k_with_carry is None,
            detail=(f"projected carry {carry} over {FUNDING_INTERVALS_PER_HOLD} intervals takes k_net "
                    f"to {k_with_carry:.3f} against a floor of {floor}"
                    if k_with_carry is not None else
                    f"funding or cost inputs unavailable: {funding.reason or 'no target'}"),
            value=None if k_with_carry is None else round(float(k_with_carry), 4),
            threshold=float(floor)))
        return out

    # ---- Tier 4: portfolio ----------------------------------------------------------------

    def _tier4(self, signal: RawSignal, sizing: SizingResult, *,
               strategy_class: int) -> list[VetoResult]:
        target = Decimal(str(self.params["portfolio_vol_target"]))
        projected = self.committed_portfolio_vol
        out = [
            VetoResult(name="PORTFOLIO_VOL", tier=Tier.PORTFOLIO, fired=projected > target,
                       detail=(f"committed portfolio volatility {projected} against a target of "
                               f"{target}"),
                       value=str(projected), threshold=str(target)),
            VetoResult(name="CLASS3_CONCURRENT", tier=Tier.PORTFOLIO,
                       fired=strategy_class == 3 and self.open_class3 >= 2,
                       detail=(f"{self.open_class3} Class 3 position(s) already open against a "
                               "structural ceiling of 2"),
                       value=self.open_class3, threshold=2),
            VetoResult(name="LIQ_DISTANCE", tier=Tier.PORTFOLIO, fired=False, unavailable=True,
                       detail="liquidation distance needs margin tiers and a leverage choice; it is "
                              "reported unavailable rather than assumed satisfied"),
            VetoResult(name="CROSS_MARGIN", tier=Tier.PORTFOLIO, fired=False, unavailable=True,
                       detail="cross-margin interaction needs open-position state this system does "
                              "not read from the exchange"),
        ]
        return out
