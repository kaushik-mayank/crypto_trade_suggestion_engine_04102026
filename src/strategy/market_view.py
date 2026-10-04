"""The cross-asset view, for the strategies that genuinely need one.

Most strategies see one asset. Three do not:

* **C1-C** is a two-leg relative-value trade and needs BTC's features alongside the dislocated
  asset's.
* **C2-B** selects *which* of the three assets best expresses a BTC-driven factor move, so it has to
  compare all three before it can arm anything.
* **C2-B** also carries a hard constraint — at most one position across all assets — which specs/04
  §4 requires enforced **in the strategy engine, not only in the risk engine**, so the strategy needs
  to know whether a sibling asset already has one armed.

Rather than widen the ``Strategy`` protocol for every strategy, a strategy *declares*
``needs_market_view = True`` and the runner sets :attr:`market_view` on it before evaluating. The
view is built once per pass, after features and regimes exist for every asset and before any strategy
runs — so a strategy reading a peer's features reads them at the same ``as_of`` as its own, which is
the whole point. There is no store handle here and nothing is fetched on access: the view is a
snapshot of a snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.errors import StrategyError
from features.value import FeatureSet
from regime.model import RegimeState

COMPONENT = "strategy.market_view"


@dataclass(frozen=True)
class MarketView:
    """Features and regime for every asset in this pass, plus which setups are already armed."""

    as_of_ms: int
    features: Mapping[str, FeatureSet] = field(default_factory=dict)
    regimes: Mapping[str, RegimeState] = field(default_factory=dict)
    #: ``strategy_id -> assets that currently hold an active setup for it``.
    armed: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(sorted(self.features))

    def features_for(self, asset: str) -> FeatureSet:
        """A peer's features, or a refusal naming what was available.

        Raises rather than returning ``None`` because every caller here is about to compute a
        cross-asset quantity, and a silently missing leg would produce a number that looks like a
        two-asset measurement and is not one.
        """
        fs = self.features.get(asset)
        if fs is None:
            raise StrategyError(
                f"no features for {asset} in this pass, so a cross-asset condition cannot be formed",
                component=COMPONENT, operation="features_for",
                context={"asset": asset, "available": list(self.assets)},
                likely_cause="that asset failed its own evaluation, or it is not in the runner's "
                             "asset list",
                remediation="the strategy declines; a leg computed from one asset only is a "
                            "directional bet, not a relative-value trade",
            )
        return fs

    def regime_for(self, asset: str) -> RegimeState:
        regime = self.regimes.get(asset)
        if regime is None:
            raise StrategyError(
                f"no regime state for {asset} in this pass",
                component=COMPONENT, operation="regime_for",
                context={"asset": asset, "available": list(self.regimes)},
                likely_cause="that asset failed its own evaluation",
                remediation="the strategy declines rather than treating a missing regime as open",
            )
        return regime

    def armed_assets(self, strategy_id: str) -> tuple[str, ...]:
        return tuple(self.armed.get(strategy_id, ()))

    def holds_a_position(self, strategy_id: str, *, other_than: str | None = None) -> bool:
        """Whether this strategy already has an active setup somewhere.

        ``other_than`` excludes the asset being evaluated, so re-evaluating an asset that already
        holds the single permitted position does not read as a conflict with itself.
        """
        return any(a != other_than for a in self.armed_assets(strategy_id))

    def to_dict(self) -> dict[str, Any]:
        return {"as_of_ms": self.as_of_ms, "assets": list(self.assets),
                "armed": {k: list(v) for k, v in self.armed.items()}}


def requires_view(strategy: Any) -> bool:
    return bool(getattr(strategy, "needs_market_view", False))
