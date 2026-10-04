"""Arbitration (specs/07 §3-§5, specs/13 §7).

Six stages, composed in a fixed order:

    collect -> resolve_conflicts -> merge_same_direction -> dedup_class4
            -> rank_within_class -> expose_portfolio

**The order is load-bearing and enforced by the composition, not by convention.** specs/07 §3 warns
that running Class 4 dedup before conflict resolution silently loses good signals: dedup would pick a
winner from a set that still contains a signal about to be suppressed, and the survivor of the conflict
would never be considered.

Two rules deserve naming:

**Opposing signals are resolved by evidence first.** Tier A beats Tier B, because Tier A *can* be
validated and Tier B cannot — the only criterion grounded in evidence rather than design preference.
Then timeframe seniority (1d > 1h). Otherwise **both are suppressed**: genuine ambiguity between
equally-supported opposing readings means the honest answer is no trade.

**Same-direction signals merge; they never stack.** Two strategies describing one economic position
must become one suggestion with one risk allocation. The corroborators are listed, and
**strength is not additive** — two correlated readings of the same move are not twice the evidence.

Every stage writes an event, so the audit can answer why a signal is absent as well as why one is
present.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from core.logging import get_logger
from normalize.canonical import Timeframe
from risk.engine import Signal
from strategy.act import Direction

log = get_logger("arbitration.pipeline")
COMPONENT = "arbitration.pipeline"

#: Timeframe seniority (specs/07 §4 criterion 2): higher-timeframe structure dominates lower.
SENIORITY: dict[Timeframe, int] = {Timeframe.D1: 2, Timeframe.H1: 1}


@dataclass
class ArbitrationEvent:
    """One decision, with both sides' evidence — written to ``arbitration_events``."""

    kind: str                       # conflict | merge | suppression | dedup | rank
    asset: str
    at_ms: int
    detail: str
    winner: str | None = None
    losers: tuple[str, ...] = ()
    criterion: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "asset": self.asset, "at_ms": self.at_ms, "detail": self.detail,
                "winner": self.winner, "losers": list(self.losers), "criterion": self.criterion,
                "context": dict(self.context)}


@dataclass
class ArbitrationResult:
    """What survived, what was suppressed, and why."""

    published: tuple[Signal, ...]
    suppressed: tuple[tuple[Signal, str], ...]
    events: tuple[ArbitrationEvent, ...]
    #: Per-asset merge record: primary signal id -> corroborating signal ids.
    corroborations: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def conflicting_assets(self) -> tuple[str, ...]:
        """Assets where both sides were suppressed — the UI shows "NO VALID TRADE - conflicting
        evidence" for these, which is different from showing nothing."""
        return tuple(sorted({sig.raw.asset for sig, reason in self.suppressed
                             if "conflicting evidence" in reason}))

    def to_dict(self) -> dict[str, Any]:
        return {"published": [s.raw.signal_id for s in self.published],
                "suppressed": [{"signal_id": s.raw.signal_id, "reason": r}
                               for s, r in self.suppressed],
                "conflicting_assets": list(self.conflicting_assets),
                "corroborations": {k: list(v) for k, v in self.corroborations.items()},
                "events": [e.to_dict() for e in self.events]}


def _standing(signal: Signal) -> tuple[int, int, int]:
    """``(evidential standing, timeframe seniority, bespoke over benchmark)`` — higher wins.

    Evidential standing is ``validated`` first (nothing is, before Phase 16), then Tier A over Tier B.
    At launch this often ties, pushing conflicts to seniority and then to suppression, which specs/07 §4
    calls acceptable and expected.

    The third component is the specs/06 §3 deduplication rule, and it is a *tiebreak* rather than a
    separate pass: a Class 4 signal surfaces only when no Class 1-3 signal is active on the same asset
    in the same direction. Without it, a Class 4 and a Class 2 signal on the same side would tie on
    the first two components and ``max()`` would pick whichever happened to be evaluated first — the
    benchmark would sometimes suppress the strategy it exists to measure, and nothing about the output
    would look wrong.
    """
    validated = 1 if signal.raw.detail.get("validated") else 0
    tier_a = 1 if signal.raw.detail.get("tier") == "A" else 0
    bespoke = 0 if _is_class4(signal) else 1
    return validated * 2 + tier_a, SENIORITY.get(signal.raw.signal_timeframe, 0), bespoke


def _is_class4(signal: Signal) -> bool:
    return (signal.raw.detail.get("strategy_class") == 4
            or signal.raw.strategy_id.startswith("C4"))


def resolve_conflicts(signals: Sequence[Signal], *, at_ms: int
                      ) -> tuple[list[Signal], list[tuple[Signal, str]], list[ArbitrationEvent]]:
    """Case A: opposing directions on the same asset (specs/07 §4)."""
    survivors: list[Signal] = []
    suppressed: list[tuple[Signal, str]] = []
    events: list[ArbitrationEvent] = []

    by_asset: dict[str, list[Signal]] = {}
    for s in signals:
        by_asset.setdefault(s.raw.asset, []).append(s)

    for asset, group in by_asset.items():
        longs = [s for s in group if s.raw.direction is Direction.LONG]
        shorts = [s for s in group if s.raw.direction is Direction.SHORT]
        if not longs or not shorts:
            survivors.extend(group)
            continue

        best_long = max(longs, key=_standing)
        best_short = max(shorts, key=_standing)
        long_rank, short_rank = _standing(best_long), _standing(best_short)

        if long_rank[0] != short_rank[0]:
            criterion = "evidential standing (Tier A can be validated; Tier B cannot)"
            winner, loser_side = ((best_long, shorts) if long_rank[0] > short_rank[0]
                                  else (best_short, longs))
        elif long_rank[1] != short_rank[1]:
            criterion = "timeframe seniority (higher-timeframe structure dominates)"
            winner, loser_side = ((best_long, shorts) if long_rank[1] > short_rank[1]
                                  else (best_short, longs))
        else:
            # Genuine ambiguity between equally-supported opposing readings: the honest answer is no
            # trade, and the asset shows "NO VALID TRADE - conflicting evidence".
            for s in group:
                suppressed.append((s, "conflicting evidence: an equally-supported opposing setup "
                                      "exists, so no direction is published"))
            events.append(ArbitrationEvent(
                kind="conflict", asset=asset, at_ms=at_ms,
                detail=(f"{best_long.raw.strategy_id} LONG and {best_short.raw.strategy_id} SHORT have "
                        "equal standing and equal seniority, so both are suppressed"),
                losers=tuple(s.raw.signal_id for s in group),
                criterion="ambiguity -> suppress both",
                context={"long": best_long.raw.to_dict()["conditions_summary"],
                         "short": best_short.raw.to_dict()["conditions_summary"]}))
            continue

        kept = [s for s in group if s not in loser_side]
        survivors.extend(kept)
        for s in loser_side:
            suppressed.append((s, f"superseded by {winner.raw.strategy_id} "
                                  f"({winner.raw.direction.value}) on {criterion}"))
        events.append(ArbitrationEvent(
            kind="conflict", asset=asset, at_ms=at_ms,
            detail=(f"{winner.raw.strategy_id} {winner.raw.direction.value} beat "
                    f"{', '.join(s.raw.strategy_id for s in loser_side)} on {criterion}. That a "
                    "credible opposite case existed is decision-relevant and is surfaced on the "
                    "surviving signal."),
            winner=winner.raw.signal_id,
            losers=tuple(s.raw.signal_id for s in loser_side), criterion=criterion))
    return survivors, suppressed, events


def merge_same_direction(signals: Sequence[Signal], *, at_ms: int
                         ) -> tuple[list[Signal], list[tuple[Signal, str]],
                                    list[ArbitrationEvent], dict[str, tuple[str, ...]]]:
    """Case B: two strategies describing one economic position become **one** suggestion.

    The primary is chosen by the same hierarchy; its entry, stop and targets govern. One risk
    allocation, not two — which is the entire point, and the fix for the concentration risk specs/05 §6
    flags.
    """
    survivors: list[Signal] = []
    suppressed: list[tuple[Signal, str]] = []
    events: list[ArbitrationEvent] = []
    corroborations: dict[str, tuple[str, ...]] = {}

    grouped: dict[tuple[str, Direction], list[Signal]] = {}
    for s in signals:
        grouped.setdefault((s.raw.asset, s.raw.direction), []).append(s)

    for (asset, direction), group in grouped.items():
        if len(group) == 1:
            survivors.extend(group)
            continue
        primary = max(group, key=_standing)
        others = [s for s in group if s is not primary]
        survivors.append(primary)
        corroborations[primary.raw.signal_id] = tuple(s.raw.signal_id for s in others)
        for s in others:
            # The superseding signal id is named, not just the strategy: specs/06 §10 requires a
            # deduplicated Class 4 signal to record `suppressed_by`, and a strategy id alone would not
            # identify *which* evaluation superseded it.
            suppressed.append((s, f"merged into {primary.raw.strategy_id}: one economic position "
                                  f"receives one risk allocation, not two "
                                  f"[suppressed_by={primary.raw.signal_id}]"))
        events.append(ArbitrationEvent(
            kind="merge", asset=asset, at_ms=at_ms,
            detail=(f"{len(group)} {direction.value} signals merged into "
                    f"{primary.raw.strategy_id}; corroborated by "
                    f"{', '.join(s.raw.strategy_id for s in others)}. Strength is **not** additive: "
                    "two correlated readings of the same move are not twice the evidence."),
            winner=primary.raw.signal_id,
            losers=tuple(s.raw.signal_id for s in others),
            criterion="same asset, same direction"))
    return survivors, suppressed, events, corroborations


def dedup_class4(signals: Sequence[Signal], *, at_ms: int
                 ) -> tuple[list[Signal], list[tuple[Signal, str]], list[ArbitrationEvent]]:
    """One Class 4 signal per asset and direction, keeping the strongest standing.

    Runs **after** conflict resolution, deliberately: deduplicating first would pick a winner from a set
    still containing a signal about to be suppressed, and the conflict's survivor would never be seen
    (specs/07 §3).
    """
    survivors: list[Signal] = []
    suppressed: list[tuple[Signal, str]] = []
    events: list[ArbitrationEvent] = []
    seen: dict[tuple[str, Direction], Signal] = {}

    for s in signals:
        if s.raw.detail.get("strategy_class") != 4 and not s.raw.strategy_id.startswith("C4"):
            survivors.append(s)
            continue
        key = (s.raw.asset, s.raw.direction)
        incumbent = seen.get(key)
        if incumbent is None:
            seen[key] = s
            continue
        winner, loser = (s, incumbent) if _standing(s) > _standing(incumbent) else (incumbent, s)
        seen[key] = winner
        suppressed.append((loser, f"a stronger Class 4 signal exists for {key[0]} "
                                  f"{key[1].value}: {winner.raw.strategy_id}"))
        events.append(ArbitrationEvent(kind="dedup", asset=key[0], at_ms=at_ms,
                                       detail=f"kept {winner.raw.strategy_id} over "
                                              f"{loser.raw.strategy_id}",
                                       winner=winner.raw.signal_id,
                                       losers=(loser.raw.signal_id,),
                                       criterion="one Class 4 signal per asset and direction"))
    survivors.extend(seen.values())
    return survivors, suppressed, events


def rank_within_class(signals: Sequence[Signal]) -> list[Signal]:
    """Rank by standing, then by cost-adjusted reward-to-risk. Ordering only — nothing is dropped."""
    def key(s: Signal) -> tuple[int, int, int, float]:
        k_net = s.detail.get("k_net")
        return (*_standing(s), float(k_net) if k_net else 0.0)

    return sorted(signals, key=key, reverse=True)


def expose_portfolio(signals: Sequence[Signal]) -> dict[str, Any]:
    """The portfolio view: what is published, and the concentration the correlation family warns about.

    Three simultaneous long signals on 0.7-0.9 correlated assets are **one leveraged position**
    (specs/02 Family 5), so the count by direction is part of the output rather than something a reader
    has to notice.
    """
    longs = [s for s in signals if s.raw.direction is Direction.LONG]
    shorts = [s for s in signals if s.raw.direction is Direction.SHORT]
    return {
        "published": len(signals),
        "long": len(longs), "short": len(shorts),
        "assets": sorted({s.raw.asset for s in signals}),
        "same_direction_assets": sorted({s.raw.asset for s in (longs if len(longs) >= len(shorts)
                                                               else shorts)}),
        "concentration_note": ("BTC, ETH and SOL run 0.7-0.9 correlated: simultaneous same-direction "
                              "signals are closer to one leveraged position than to diversification "
                              "(specs/02 Family 5)") if max(len(longs), len(shorts)) > 1 else None,
    }


def arbitrate(signals: Sequence[Signal], *, at_ms: int,
              benchmark_mode: bool = False) -> ArbitrationResult:
    """The whole pipeline, in the one order that is correct.

    ``benchmark_mode`` Class 4 signals bypass arbitration entirely and are recorded uncensored
    (specs/06 §3): the benchmark exists to measure what the strategies would have done without the
    system's own filtering, and filtering it would defeat that.
    """
    approved = [s for s in signals if s.approved]
    refused = [(s, "the risk engine refused it: "
                   + ", ".join(v.name for v in s.fired_vetoes)) for s in signals if not s.approved]

    if benchmark_mode:
        log.info("arbitration_bypassed", reason="benchmark mode", signals=len(signals))
        return ArbitrationResult(published=tuple(approved), suppressed=tuple(refused),
                                 events=(ArbitrationEvent(
                                     kind="rank", asset="*", at_ms=at_ms,
                                     detail="benchmark mode: Class 4 signals bypass the pipeline and "
                                            "are recorded uncensored (specs/06 §3)"),))

    after_conflict, s1, e1 = resolve_conflicts(approved, at_ms=at_ms)
    after_merge, s2, e2, corroborations = merge_same_direction(after_conflict, at_ms=at_ms)
    after_dedup, s3, e3 = dedup_class4(after_merge, at_ms=at_ms)
    ranked = rank_within_class(after_dedup)

    result = ArbitrationResult(published=tuple(ranked),
                               suppressed=tuple(refused + s1 + s2 + s3),
                               events=tuple(e1 + e2 + e3), corroborations=corroborations)
    if result.suppressed:
        log.info("arbitration_complete", published=len(result.published),
                 suppressed=len(result.suppressed),
                 conflicting_assets=list(result.conflicting_assets))
    return result
