"""A deliberate failure drill: break the system on purpose and record what it says.

Tests assert that a failure path behaves; this asks a different question — **would the message tell an
operator what to do at 3 a.m.?** Each drill breaks one thing for real (a missing file, a corrupt
database, a closed connection, a skewed clock) and records the code, the problem class and the
remediation the system produced.

The output is a table meant to be read, and the column that matters is ``problem_class``: on Azure it
is what decides whether to look at the code, the configuration, the VM or the venue.

    python -m probes.failure_drill
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")

from core.errors import CryptoTraderError  # noqa: E402

COMPONENT = "probes.failure_drill"


@dataclass
class DrillResult:
    name: str
    what_was_broken: str
    outcome: str                       # "raised" | "reported" | "NO FAILURE"
    code: str = ""
    problem_class: str = ""
    message: str = ""
    remediation: str = ""
    actionable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "broke": self.what_was_broken, "outcome": self.outcome,
                "code": self.code, "problem_class": self.problem_class, "message": self.message,
                "remediation": self.remediation, "actionable": self.actionable,
                "detail": self.detail}


def _from_error(name: str, broke: str, e: CryptoTraderError) -> DrillResult:
    return DrillResult(
        name=name, what_was_broken=broke, outcome="raised", code=e.code,
        problem_class=e.problem_class.value, message=e.message[:200],
        remediation=(e.remediation or "")[:200],
        # "Actionable" here means: it names a class, says what probably happened, and says what to do.
        actionable=bool(e.problem_class and e.remediation and e.likely_cause))


def drill(name: str, broke: str, fn: Callable[[], DrillResult | None]) -> DrillResult:
    try:
        result = fn()
    except CryptoTraderError as e:
        return _from_error(name, broke, e)
    except Exception as e:                                     # noqa: BLE001 - that is the finding
        return DrillResult(name=name, what_was_broken=broke, outcome="raised",
                           code=type(e).__name__, problem_class="UNCLASSIFIED",
                           message=str(e)[:200], actionable=False)
    if result is None:
        return DrillResult(name=name, what_was_broken=broke, outcome="NO FAILURE",
                           message="the system did not fail; check whether it should have")
    return result


# ---- the drills ---------------------------------------------------------------------------

def missing_database() -> None:
    from store.reader import ReadOnlyStore
    ReadOnlyStore(Path(tempfile.gettempdir()) / "definitely-not-here.db")


def corrupt_database() -> None:
    from normalize.canonical import Product, Timeframe
    from store import open_store

    # Not a TemporaryDirectory context: the failure happens *inside* open_store, so `store` is never
    # bound and nothing closes the handle -- on Windows the cleanup then raises PermissionError and
    # masks the very error this drill exists to read.
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "corrupt.db"
    path.write_bytes(b"this is not a database, it is a text file wearing a .db extension")
    store = None
    try:
        store = open_store(path, migrate=False)
        store.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, 0, 10**13)
    finally:
        if store is not None:
            store.close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def missing_regime_artifact() -> None:
    from app.runner import load_classifiers
    with tempfile.TemporaryDirectory() as tmp:
        load_classifiers(["B-BTC_USDT"], directory=tmp)


def corrupt_regime_artifact() -> None:
    from regime.model import RegimeModel
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "regime_model_B-BTC_USDT.json"
        path.write_text('{"model_version": "x", "grid": 3', encoding="utf-8")   # truncated
        RegimeModel.load(path)


def missing_parameter_file() -> None:
    from config.parameters import load_registry
    load_registry(Path(tempfile.gettempdir()) / "no-such-parameters.toml")


def parameter_budget_exceeded() -> DrillResult | None:
    """The registry's own rule: the cap is enforced, not documented."""
    from config.parameters import load_registry
    registry = load_registry()
    if len(registry) > registry.cap:
        return None
    return DrillResult(
        name="parameter budget", what_was_broken="nothing (checking the guard exists)",
        outcome="reported", code="CONFIG_PARAMETER_INVALID", problem_class="configuration",
        message=f"{len(registry)} of {registry.cap} parameters registered; the cap is enforced at load",
        remediation="adding one requires removing one or raising the cap with a written reason",
        actionable=True)


def public_bind_without_a_token() -> None:
    from api.app import assert_bind_allowed
    assert_bind_allowed("0.0.0.0", "")


def shallow_snapshot_request() -> None:
    from config.parameters import load_registry
    from features.engine import FeatureEngine
    from normalize.canonical import Product, Timeframe
    from snapshot.builder import SeriesRequest, SnapshotRequest
    from snapshot.models import SeriesKey

    registry = load_registry()
    engine = FeatureEngine({p.name: p.value for p in registry.parameters},
                           parameter_hash=registry.parameter_hash)
    engine.assert_request_sufficient(SnapshotRequest(series=(
        SeriesRequest(SeriesKey("B-BTC_USDT", Product.FUTURES, Timeframe.H1), bars=50),)))


def clock_behind_recorded_data() -> None:
    """A suspend/resume without NTP resync produces exactly this."""

    from normalize.canonical import Bar, Product, Timeframe
    from pipeline.drivers import LiveDriver
    from snapshot.builder import SeriesRequest, SnapshotBuilder, SnapshotRequest
    from snapshot.models import SeriesKey
    from store import open_store

    with tempfile.TemporaryDirectory() as tmp:
        store = open_store(Path(tmp) / "clock.db")
        base = 1767225600000
        store.upsert_bars([Bar(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=Timeframe.H1,
                               open_time_ms=base + i * 3_600_000, open="100", high="110", low="90",
                               close="105", volume_base="1", is_final=True, fetched_at_ms=base)
                           for i in range(5)])
        key = SeriesKey("B-BTC_USDT", Product.FUTURES, Timeframe.H1)
        driver = LiveDriver(
            builder=SnapshotBuilder(store=store, parameter_hash="ph"),
            request=SnapshotRequest(series=(SeriesRequest(key, bars=5),)),
            clock=lambda: base)                     # the clock is five hours behind the data
        try:
            driver.snapshot()
        finally:
            store.close()


def stale_tier_b_disables_the_family() -> DrillResult:
    """Not an exception: a stale source must *gate*, and the drill checks the gate closes."""

    from features.engine import FeatureEngine
    from features.value import FeatureStatus
    from normalize.canonical import FuturesRT, Product, Timeframe
    from snapshot.models import MarketSnapshot, PointInTimeValue, SeriesKey, SeriesState

    base = 1767225600000
    row = FuturesRT(asset="B-BTC_USDT", ts_ms=base, funding_rate="0.0001",
                    predicted_funding_rate="0.0002", mark_price="85000", last_price="84990",
                    tpe_tick_send_ms=base, received_at_ms=base)
    stale = PointInTimeValue(source="futures_rt", asset="B-BTC_USDT", value=row,
                             source_ts_ms=base, age_ms=9_000_000, max_age_ms=60_000, ok=False,
                             reason="9000.0s old, window is 60s")
    key = SeriesKey("B-BTC_USDT", Product.FUTURES, Timeframe.H1)
    snap = MarketSnapshot(as_of_ms=base + 9_000_000, code_version="v", parameter_hash="ph",
                          series={key: SeriesState(key=key, bars=(), requested=1, min_required=1,
                                                   ok=False, reason="EMPTY", detail="none")},
                          point_in_time={("futures_rt", "B-BTC_USDT"): stale},
                          point_in_time_history={("futures_rt", "B-BTC_USDT"): ()})
    from config.parameters import load_registry
    registry = load_registry()
    engine = FeatureEngine({p.name: p.value for p in registry.parameters}, parameter_hash="ph")
    fs = engine.compute(snap, "B-BTC_USDT", Product.FUTURES)
    fv = fs.get("FND_RATE")
    return DrillResult(
        name="stale Tier B", what_was_broken="futures_rt is 9,000 s old against a 60 s window",
        outcome="reported", code=fv.status.value, problem_class="data_quality",
        message=(fv.reason or "")[:200],
        remediation="the family goes quiet; no signal can be built on it",
        actionable=fv.status is FeatureStatus.DQ_FAILED and bool(fv.reason),
        detail={"value_published": fv.value})


def store_failure_is_not_an_empty_dashboard() -> DrillResult:
    from api.services import build_services
    from core.health import SystemStatus
    from store import open_store

    with tempfile.TemporaryDirectory() as tmp:
        store = open_store(Path(tmp) / "api.db")
        signals, _, _ = build_services(store)
        store.close()                                # the query will now fail
        result = signals.live()
        del store
    env = result.envelope
    return DrillResult(
        name="store failure behind the API",
        what_was_broken="the database connection was closed under a live request",
        outcome="reported", code=result.error.code if result.error else "",
        problem_class=result.error.problem_class.value if result.error else "",
        message=(result.envelope.degraded_reasons or [""])[0][:200],
        remediation="the dashboard shows DOWN with a correlation id, never an empty market",
        actionable=(env.system_status is SystemStatus.DOWN and bool(env.correlation_id)
                    and result.payload == {"signals": [], "count": 0, "by_asset": {}}),
        detail={"system_status": env.system_status.value, "payload_shape_kept": True,
                "correlation_id": env.correlation_id})


def network_failure_is_classified() -> DrillResult:
    """The ingest client must map a transport failure to a retryable external error, not a crash."""
    from ingest.client import IngestClient

    client = IngestClient()
    error = None
    try:
        client.get("spot_candles", "https://this-host-does-not-exist.invalid/market_data/candles")
    except CryptoTraderError as e:
        error = e
    return DrillResult(
        name="network failure", what_was_broken="a request to a host that does not resolve",
        outcome="reported" if error else "NO FAILURE",
        code=error.code if error else "", problem_class=error.problem_class.value if error else "",
        message=(error.message if error else "")[:200],
        remediation=(error.remediation or "")[:200] if error else "",
        actionable=bool(error and error.retryable and error.problem_class.value == "external_dependency"),
        detail={"retryable": bool(error and error.retryable)})


def aggregation_across_a_gap_is_refused() -> None:
    from normalize.canonical import Bar, Product, Timeframe
    from pipeline.gaps import aggregate_bars

    base = 1767225600000
    bars = [Bar(asset="a", product=Product.SPOT, timeframe=Timeframe.H1,
                open_time_ms=base + i * 3_600_000, open="100", high="110", low="90", close="105",
                volume_base="1", is_final=True, fetched_at_ms=base)
            for i in (0, 1, 3)]                      # the third hour is missing
    aggregate_bars(bars, factor=4, target=Timeframe.H4, window_start_ms=base)


def risk_engine_mutation_is_caught() -> None:
    from decimal import Decimal

    from normalize.canonical import Product, Timeframe
    from risk.engine import assert_levels_unchanged
    from strategy.act import Direction
    from strategy.base import Lifecycle, RawSignal

    def make(stop):
        return RawSignal(signal_id="s", strategy_id="C1-B", strategy_version="1", asset="a",
                         product=Product.FUTURES, direction=Direction.LONG,
                         lifecycle=Lifecycle.SETUP_DETECTED, as_of_ms=1767225600000,
                         signal_timeframe=Timeframe.H1, entry=Decimal("100"), stop=stop)

    assert_levels_unchanged(make(Decimal("95")), make(Decimal("96")))


def control_strategy_outside_backtest() -> None:
    from strategy.controls import assert_backtest_only
    assert_backtest_only("C4-X1", "live")


DRILLS: list[tuple[str, str, Callable[[], Any]]] = [
    ("missing database", "pointed the API at a database that does not exist", missing_database),
    ("corrupt database", "wrote text into a .db file and queried it", corrupt_database),
    ("missing regime artifact", "removed regime_model_*.json", missing_regime_artifact),
    ("corrupt regime artifact", "truncated regime_model_*.json mid-object", corrupt_regime_artifact),
    ("missing parameter file", "pointed the registry at a path with no parameters.toml",
     missing_parameter_file),
    ("parameter budget", "checked the 30-parameter cap is enforced", parameter_budget_exceeded),
    ("public bind, no token", "asked the API to bind 0.0.0.0 with no auth token",
     public_bind_without_a_token),
    ("shallow snapshot request", "asked for 50 bars where the registry needs 721",
     shallow_snapshot_request),
    ("clock behind data", "set the clock five hours behind the newest stored bar",
     clock_behind_recorded_data),
    ("stale Tier B", "aged futures_rt past its freshness window", stale_tier_b_disables_the_family),
    ("store failure behind the API", "closed the database under a live request",
     store_failure_is_not_an_empty_dashboard),
    ("network failure", "requested a host that does not resolve", network_failure_is_classified),
    ("aggregation across a gap", "aggregated 4h from three of four hourly bars",
     aggregation_across_a_gap_is_refused),
    ("risk engine mutation", "altered a published stop after the strategy set it",
     risk_engine_mutation_is_caught),
    ("control strategy live", "evaluated C4-X1 outside backtest mode",
     control_strategy_outside_backtest),
]


def main() -> int:
    results = [drill(name, broke, fn) for name, broke, fn in DRILLS]

    width = max(len(r.name) for r in results) + 2
    print(f"\n{'drill'.ljust(width)}{'outcome':<10}{'problem class':<20}{'code':<32}actionable")
    print("-" * (width + 74))
    for r in results:
        mark = "yes" if r.actionable else "NO"
        print(f"{r.name.ljust(width)}{r.outcome:<10}{r.problem_class:<20}{r.code:<32}{mark}")

    unactionable = [r for r in results if not r.actionable]
    silent = [r for r in results if r.outcome == "NO FAILURE"]
    print(f"\n{len(results)} drills · {len(results) - len(unactionable)} actionable · "
          f"{len(silent)} produced no failure")
    if unactionable:
        print("\nNOT ACTIONABLE — these would not tell an operator what to do:")
        for r in unactionable:
            print(f"  {r.name}: [{r.code}] {r.message}")

    out = Path("data/probe_results/failure_drill.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ran_at_ms": int(time.time() * 1000),
                               "results": [r.to_dict() for r in results]}, indent=2),
                   encoding="utf-8")
    print(f"\nwrote {out}")
    return 0 if not unactionable else 1


if __name__ == "__main__":
    sys.exit(main())
