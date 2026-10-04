"""The feature engine: snapshot in, :class:`FeatureSet` out (specs/12 §1, §8).

**Recompute from a window; never accumulate.** No feature holds state between calls, so live and
backtest cannot diverge through accumulator seeding — the same reasoning that chose an SMA for ATR,
applied at the engine level. The cost is recomputation, which at this data volume is negligible.

Caching is keyed exactly as specs/12 §8 requires — ``(series, name, version, as_of, parameter_hash)``
— so a formula change that bumps the version invalidates its own entries. A formula change that does
*not* bump the version would serve values computed by the old formula, which is why the version lives
on the spec and is asserted in the tests.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

import numpy as np

from core.errors import ConfigError, FeatureError, StartupError
from core.logging import get_logger
from features.registry import FEATURES, lookback_for, lookback_name
from features.spec import NO_HORIZON, ComputeContext, FeatureSpec, evaluate
from features.value import FeatureKey, FeatureSet, FeatureStatus, FeatureValue
from features.windows import Window, WindowOutcome, extract
from normalize.canonical import Product, Timeframe
from snapshot.models import MarketSnapshot, SeriesKey

log = get_logger("features.engine")
COMPONENT = "features.engine"


class FeatureEngine:
    """Computes every registered feature for one asset from one snapshot."""

    def __init__(self, params: dict[str, Any], *, specs: Sequence[FeatureSpec] = FEATURES,
                 parameter_hash: str = "", cache_entries: int = 4096,
                 unset_params: frozenset[str] = frozenset()):
        self.params = dict(params)
        self.unset_params = unset_params
        self.specs = tuple(specs)
        self.parameter_hash = parameter_hash
        self._cache: OrderedDict[tuple[Any, ...], FeatureValue] = OrderedDict()
        self._cache_entries = cache_entries
        self.cache_hits = 0
        self.cache_misses = 0
        self.undefined = self._undefined_combinations()
        if self.undefined:
            log.info("features_undefined_at_horizon", count=len(self.undefined),
                     detail="these (feature, timeframe, horizon) combinations are undefined by their "
                            "own formula at that lookback and are not computed",
                     combinations=[f"{k} (lookback {n} < {m})" for k, n, m in self.undefined])

    def _undefined_combinations(self) -> list[tuple[str, int, int]]:
        """(key, lookback, minimum) for every combination the formula cannot support."""
        out: list[tuple[str, int, int]] = []
        for spec in self.specs:
            if not spec.min_lookback:
                continue
            for tf in spec.timeframes:
                for horizon in spec.horizons:
                    lookback = self._lookback(spec, tf, horizon)
                    if lookback < spec.min_lookback:
                        out.append((str(FeatureKey(spec.name, tf, horizon)), lookback,
                                    spec.min_lookback))
        return out

    # ---- what the engine needs to be given ---------------------------------------------------

    def required_bars(self, timeframe: Timeframe) -> int:
        """The deepest window any registered feature needs on this timeframe.

        The app calls this at startup to size its ``SnapshotRequest``: a request that is too shallow
        would leave features permanently WARMING, which looks like a data problem and is a
        configuration one.
        """
        deepest = 0
        for spec in self.specs:
            if timeframe not in spec.timeframes or spec.windowless:
                continue
            for horizon in spec.horizons:
                if self._lookback(spec, timeframe, horizon) < spec.min_lookback:
                    continue
                deepest = max(deepest, self._need(spec, timeframe, horizon))
        return deepest

    def assert_request_sufficient(self, request: Any) -> None:
        """Deterministic startup check: every registered feature's series is present and deep enough.

        Raised as a configuration failure, naming what to change — not discovered later as dozens of
        WARMING features with no obvious cause.
        """
        by_key = {s.key: s for s in request.series}
        problems: list[str] = []
        timeframes = {tf for s in self.specs for tf in s.timeframes if tf is not None}
        assets = {k.asset for k in by_key}
        for asset in sorted(assets):
            for tf in sorted(timeframes, key=lambda t: t.ms):
                need = self.required_bars(tf)
                if need == 0:
                    continue
                req = by_key.get(SeriesKey(asset, Product.FUTURES, tf)) or \
                    by_key.get(SeriesKey(asset, Product.SPOT, tf))
                if req is None:
                    problems.append(f"{asset} {tf.value}: no series requested, but features are "
                                    f"registered for it (needs {need} bars)")
                elif req.bars < need:
                    problems.append(f"{asset} {tf.value}: request loads {req.bars} bars, the deepest "
                                    f"registered feature needs {need}")
        requested_sources = {src for src, _ in getattr(request, "tier_b", ())}
        history_ms = int(getattr(request, "tier_b_history_ms", 0) or 0)
        for spec in self.specs:
            missing = [s for s in spec.tier_b_sources if s not in requested_sources]
            if missing:
                problems.append(f"{spec.name}: reads {', '.join(missing)}, which the request does not "
                                f"list in tier_b")
            if spec.needs_history and history_ms <= 0:
                problems.append(f"{spec.name}: needs a trailing Tier B series, but the request sets "
                                f"tier_b_history_ms=0")
        if problems:
            raise StartupError(
                f"the snapshot request cannot support the registered features ({len(problems)} problems)",
                component=COMPONENT, operation="assert_request_sufficient",
                context={"problems": problems[:10]},
                likely_cause="the request was assembled with shallower windows than the feature "
                             "registry needs",
                remediation="deepen the SnapshotRequest, add the missing tier_b sources, or set "
                            "tier_b_history_ms (SnapshotRequest.standard does all three)",
            )

    # ---- computation -------------------------------------------------------------------------

    def compute(self, snapshot: MarketSnapshot, asset: str, product: Product, *,
                timeframes: Sequence[Timeframe] | None = None) -> FeatureSet:
        """Every registered feature for ``asset``, in registry order so dependencies resolve."""
        started = time.perf_counter()
        computed: dict[FeatureKey, FeatureValue] = {}
        windows: dict[SeriesKey, Window] = {}
        window_cache: dict[tuple[SeriesKey, int], WindowOutcome] = {}
        # A separate cache: cross-series windows are extracted at a different depth, and sharing one
        # dict would let a shallow entry satisfy a deep feature -- which is how a 720-bar statistic
        # quietly gets computed on 300 bars.
        cross_cache: dict[tuple[SeriesKey, int], WindowOutcome] = {}
        now = snapshot.as_of_ms

        for spec in self.specs:
            for tf in spec.timeframes:
                # A Tier B feature has no timeframe, so a timeframe filter never excludes it: the
                # caller asking for "1h features" still needs funding and the book.
                if timeframes is not None and tf is not None and tf not in timeframes:
                    continue
                for horizon in spec.horizons:
                    key = FeatureKey(spec.name, tf, horizon)
                    lookback = self._lookback(spec, tf, horizon)
                    if lookback < spec.min_lookback:
                        continue          # undefined here by construction; reported at startup
                    cache_key = (asset, product.value, tf.value if tf else "pit", spec.name,
                                 spec.version, horizon, snapshot.as_of_ms, self.parameter_hash)
                    hit = self._cache.get(cache_key)
                    if hit is not None:
                        self.cache_hits += 1
                        computed[key] = hit
                        continue
                    self.cache_misses += 1

                    if spec.windowless:
                        ctx = ComputeContext(
                            asset=asset, product=product, timeframe=tf, horizon=horizon,
                            lookback=0, window=None, params=self.params, unset=self.unset_params,
                            window_provider=self._window_provider(snapshot, cross_cache),
                            computed=computed, snapshot=snapshot, series_provider=None)
                        value = evaluate(spec, ctx, computed_at_ms=now)
                        computed[key] = value
                        self._remember(cache_key, value)
                        continue

                    if tf is None:
                        raise FeatureError(
                            f"{spec.name} has no timeframe but is not marked windowless",
                            component=COMPONENT, operation="compute",
                            context={"feature": spec.name},
                            likely_cause="a Tier B spec was registered without windowless=True",
                            remediation="set windowless=True, or give the spec its timeframes",
                        )
                    outcome = self._window(snapshot, asset, product, tf, spec, horizon, window_cache)
                    if outcome.window is None:
                        computed[key] = FeatureValue(
                            name=spec.name, version=spec.version, status=outcome.status,
                            reason=outcome.reason, timeframe=tf, horizon=horizon, lookback=lookback,
                            bars_used=outcome.bars_available, computed_at_ms=now,
                            detail=outcome.detail or {})
                        self._remember(cache_key, computed[key])
                        continue
                    window = outcome.window
                    windows.setdefault(SeriesKey(asset, product, tf), window)
                    ctx = ComputeContext(
                        asset=asset, product=product, timeframe=tf, horizon=horizon,
                        lookback=lookback, window=window, params=self.params,
                        unset=self.unset_params,
                        window_provider=self._window_provider(snapshot, cross_cache),
                        computed=computed, snapshot=snapshot,
                        series_provider=self._series_provider(window, tf),
                    )
                    value = evaluate(spec, ctx, computed_at_ms=now)
                    computed[key] = value
                    self._remember(cache_key, value)

        fs = FeatureSet(asset=asset, product=product, as_of_ms=snapshot.as_of_ms, values=computed,
                        parameter_hash=self.parameter_hash, code_version=snapshot.code_version)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if fs.ok_count < len(fs):
            log.debug("features_incomplete", asset=asset, as_of_ms=snapshot.as_of_ms,
                      ok=fs.ok_count, total=len(fs), statuses=fs.status_counts(), ms=elapsed_ms)
        return fs

    # ---- internals ---------------------------------------------------------------------------

    def _lookback(self, spec: FeatureSpec, tf: Timeframe | None, horizon: str) -> int:
        if tf is None or spec.windowless:
            return 0                    # point-in-time: there is no lookback to resolve
        h = spec.lookback_horizon if horizon == "-" else horizon
        return lookback_for(self.params, tf, h)

    def _need(self, spec: FeatureSpec, tf: Timeframe | None, horizon: str) -> int:
        if tf is None or spec.windowless:
            return 0
        need = self._lookback(spec, tf, horizon) + spec.extra_bars
        if spec.extra_horizon:
            need += lookback_for(self.params, tf, spec.extra_horizon)
        return need

    def _window(self, snapshot: MarketSnapshot, asset: str, product: Product, tf: Timeframe,
                spec: FeatureSpec, horizon: str,
                cache: dict[tuple[SeriesKey, int], WindowOutcome]) -> WindowOutcome:
        key = SeriesKey(asset, product, tf)
        need = self._need(spec, tf, horizon)
        cache_key = (key, need)
        if cache_key in cache:
            return cache[cache_key]
        state = snapshot.series.get(key)
        if state is None:
            outcome = WindowOutcome(
                None, FeatureStatus.DQ_FAILED,
                f"series {key} is not in the snapshot, so {spec.name} cannot be computed",
                detail={"fix": "add the series to the SnapshotRequest"})
        else:
            lookback = self._lookback(spec, tf, horizon)
            extra = need - lookback
            outcome = extract(state, lookback=lookback, extra_bars=extra, min_bars=need)
        cache[cache_key] = outcome
        return outcome

    def _window_provider(self, snapshot: MarketSnapshot,
                         cache: dict[tuple[SeriesKey, int], WindowOutcome]) -> Any:
        """Extracts another series' window at the depth the consumer asks for.

        Depth is part of the cache key, because a window is only interchangeable with another of the
        same depth — and gap checking is per window, so the same series can legitimately be usable at
        24 bars and refused at 720.
        """
        def provide(key: SeriesKey, lookback: int, extra_bars: int) -> WindowOutcome:
            need = lookback + extra_bars
            cache_key = (key, need)
            hit = cache.get(cache_key)
            if hit is not None:
                return hit
            state = snapshot.series.get(key)
            if state is None:
                outcome = WindowOutcome(
                    None, FeatureStatus.DQ_FAILED,
                    f"series {key} is not in this snapshot, so a cross-series feature cannot be formed",
                    detail={"fix": "add the series to the SnapshotRequest"})
            else:
                outcome = extract(state, lookback=lookback, extra_bars=extra_bars, min_bars=need)
            cache[cache_key] = outcome
            return outcome

        return provide

    def _series_provider(self, window: Window, tf: Timeframe):
        """Serves a dependency's vectorised series, so a series-valued dependency uses the *same*
        implementation the parity test pins to the scalar one."""
        from features.registry import BY_NAME

        def provide(name: str, horizon: str, length: int) -> np.ndarray:
            spec = BY_NAME.get(name)
            if spec is None or spec.vectorised is None:
                raise FeatureError(
                    f"{name} has no vectorised implementation, so it cannot be read as a series",
                    component=COMPONENT, operation="dep_series",
                    context={"name": name, "horizon": horizon},
                    likely_cause="a feature depends on another feature's series but that feature only "
                                 "has a scalar path",
                    remediation="add a vectorised implementation for it (specs/12 §7 requires the two "
                                "to agree)",
                )
            lookback = lookback_for(self.params, tf, horizon) if horizon != "-" else \
                lookback_for(self.params, tf, spec.lookback_horizon)
            series = spec.vectorised(window, lookback)
            return series[-length:]

        return provide

    def _remember(self, key: tuple[Any, ...], value: FeatureValue) -> None:
        self._cache[key] = value
        while len(self._cache) > self._cache_entries:
            self._cache.popitem(last=False)

    def stats(self) -> dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {"cache_entries": len(self._cache), "hits": self.cache_hits,
                "misses": self.cache_misses, "undefined_combinations": len(self.undefined),
                "hit_rate": round(self.cache_hits / total, 4) if total else None,
                "features_registered": len(self.specs)}


def engine_from_registry(registry: Any, **kw: Any) -> FeatureEngine:
    """Build an engine from the parameter registry, checking the lookbacks it needs exist."""
    params = {p.name: p.value for p in registry.parameters}
    unset = frozenset(p.name for p in registry.parameters if p.status == "unset")
    missing = [lookback_name(tf, h)
               for spec in FEATURES for tf in spec.timeframes if tf is not None and not spec.windowless
               for h in (spec.horizons if spec.horizons != NO_HORIZON else (spec.lookback_horizon,))
               if lookback_name(tf, h) not in params]
    if missing:
        raise ConfigError(
            f"{len(set(missing))} lookback parameter(s) the feature registry needs are not registered",
            component=COMPONENT, operation="engine_from_registry",
            context={"missing": sorted(set(missing))},
            likely_cause="features are registered for a timeframe whose lookbacks are absent from "
                         "config/parameters.toml",
            remediation="add the lookbacks, or unregister those timeframes",
        )
    return FeatureEngine(params, parameter_hash=registry.parameter_hash, unset_params=unset, **kw)
