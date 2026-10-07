"""PHASE-23/24/25 — the service layer, the HTTP boundary and the dashboard.

The tests that matter here are the ones about *not misleading*: a store failure must not render as an
empty dashboard, a cached response must not claim to be fresher than its data, and a control-strategy
signal must never reach a response.
"""
import json
import time
from pathlib import Path

import pytest

from api.app import ResponseCache, assert_bind_allowed, create_app, status_code_for
from api.render import (
    age_class,
    age_text,
    render_action_bar,
    render_empty_panel,
    render_exposure_strip,
    render_health,
    render_page,
    render_signals,
)
from api.services import FreshnessEnvelope, ServiceResult, build_services
from core.errors import StartupError, StoreError
from core.health import ComponentHealth, HealthReport, SystemStatus
from normalize.canonical import Product
from store import open_store

T0 = 1767225600000


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "api.db")
    yield s
    s.close()


def insert_signal(store, *, signal_id="sig-1", strategy="C1-B", asset="B-BTC_USDT",
                  state="SETUP_DETECTED", tradeable=True, as_of=T0, validated=False):
    payload = {"raw": {"rationale": "mean reversion is operative and price is at structure",
                       "direction": "LONG"},
               "tradeable": tradeable,
               "detail": {"k_net": "2.4", "breakeven_hit_rate": "0.31"}}
    store.conn.execute(
        """INSERT INTO signals (signal_id, strategy_id, strategy_version, strategy_class, asset,
               product, direction, state, state_since_ms, tier, validated, mode, armed_at_ms,
               entry, stop, targets, k_net, breakeven_hit_rate, parameter_hash, code_version,
               snapshot_id, as_of_ms, payload, created_at_ms, updated_at_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (signal_id, strategy, "1.0.0", 1, asset, "futures", "LONG", state, as_of, "A",
         int(validated), "suggestion", as_of, "85000", "84500", json.dumps(["86500"]),
         "2.4", "0.31", "ph", "cv", "snap", as_of, json.dumps(payload), as_of, as_of))
    store.conn.commit()


def insert_rejection(store, *, strategy="C1-B", asset="B-BTC_USDT", binding="mean_reversion_operative"):
    store.conn.execute(
        """INSERT INTO rejections (at_ms, as_of_ms, strategy_id, asset, product, binding_condition,
               passed_count, total_count, conditions, rolled_up)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        (T0, T0, strategy, asset, "futures", binding, 5, 6,
         json.dumps([{"name": binding, "passed": False, "value": 1.18, "threshold": 1.0}])))
    store.conn.commit()


def healthy_report():
    return HealthReport(components=[ComponentHealth.healthy("store", detail="ok")])


# ---- the envelope -------------------------------------------------------------------------

def test_age_is_computed_from_the_data_not_from_when_it_was_served():
    env = FreshnessEnvelope(served_at_ms=T0 + 5000, data_as_of_ms=T0,
                            system_status=SystemStatus.HEALTHY)
    assert env.age_seconds == 5.0
    assert env.to_dict()["age_seconds"] == 5.0


def test_age_renders_as_age_never_as_a_timestamp():
    """"4s ago" is actionable; "14:32:07" makes the reader do arithmetic to find out if it is alive."""
    assert age_text(4) == "4s ago"
    assert age_text(125) == "2m ago"
    assert age_text(7200) == "2.0h ago"
    assert age_text(None) == "age unknown"
    assert age_class(10) == "age-ok" and age_class(90) == "age-amber" and age_class(600) == "age-red"


def test_a_cached_response_reports_data_age_not_cache_age():
    """specs/14 §3's cache trap. A cached response that reports itself fresh turns a visible outage
    into an invisible one."""
    cache = ResponseCache(ttl_seconds=5.0)
    stale_data_ms = int(time.time() * 1000) - 120_000          # the data is two minutes old
    result = ServiceResult(payload={"signals": []},
                           envelope=FreshnessEnvelope(served_at_ms=int(time.time() * 1000),
                                                      data_as_of_ms=stale_data_ms,
                                                      system_status=SystemStatus.HEALTHY))
    cache.put("live", result)
    time.sleep(0.05)
    served = cache.get("live")
    assert served is not None and cache.hits == 1
    assert served.envelope.data_as_of_ms == stale_data_ms      # unchanged by caching
    assert served.envelope.age_seconds >= 120                  # reports the data's age, not ~0
    assert served.envelope.served_at_ms >= result.envelope.served_at_ms


def test_a_failed_result_is_not_cached():
    """Caching a failure would delay recovery and make an outage look steadier than it is."""
    cache = ResponseCache()
    failed = ServiceResult(payload=None,
                           envelope=FreshnessEnvelope(served_at_ms=T0, data_as_of_ms=None,
                                                      system_status=SystemStatus.DOWN),
                           error=StoreError("db is locked", component="store", operation="query"))
    cache.put("live", failed)
    assert cache.get("live") is None


# ---- services -----------------------------------------------------------------------------

def test_live_signals_are_returned_with_their_envelope(store):
    insert_signal(store)
    signals, _, _ = build_services(store, health_source=healthy_report)
    result = signals.live()
    assert result.ok and result.payload["count"] == 1
    assert result.payload["by_asset"] == {"B-BTC_USDT": ["sig-1"]}
    assert result.envelope.system_status is SystemStatus.HEALTHY
    assert result.envelope.correlation_id


def test_a_control_signal_never_reaches_a_response(store):
    """specs/13 §13: three layers guard this, and the API boundary is the last one."""
    insert_signal(store, signal_id="ctl-1", strategy="C4-X1", tradeable=False)
    insert_signal(store, signal_id="real-1", strategy="C1-B", tradeable=True)
    signals, _, _ = build_services(store)
    ids = [s["signal_id"] for s in signals.live().payload["signals"]]
    assert ids == ["real-1"]
    # and only an explicit opt-in (used by backtest tooling, never by a handler) shows it
    all_ids = [s["signal_id"] for s in signals.live(include_untradeable=True).payload["signals"]]
    assert set(all_ids) == {"ctl-1", "real-1"}


def test_a_store_failure_renders_as_down_not_as_an_empty_market(store):
    """specs/14 §10: the single most dangerous thing this API can do is report a blind system as a
    quiet one."""
    signals, _, _ = build_services(store, health_source=healthy_report)
    store.close()                                    # the query will now fail
    result = signals.live()
    assert not result.ok
    assert result.envelope.system_status is SystemStatus.DOWN
    assert result.payload == {"signals": [], "count": 0, "by_asset": {}}   # shape kept
    body = result.to_dict()
    assert body["error"]["correlation_id"] == result.envelope.correlation_id
    assert "signals.live" in result.envelope.degraded_reasons[0]


def test_an_unknown_signal_id_is_a_structured_error_not_a_crash(store):
    signals, _, _ = build_services(store)
    result = signals.detail("nope")
    assert not result.ok and result.error.code == "API_SERVICE_UNAVAILABLE"
    assert "history" in result.error.remediation


def test_history_is_cursor_paginated(store):
    for i in range(5):
        insert_signal(store, signal_id=f"sig-{i}", as_of=T0 + i * 1000)
    signals, _, _ = build_services(store)
    page = signals.history(limit=2)
    assert len(page.payload["signals"]) == 2
    cursor = page.payload["next_cursor"]
    assert cursor is not None
    second = signals.history(cursor=cursor, limit=2)
    first_ids = {s["signal_id"] for s in page.payload["signals"]}
    second_ids = {s["signal_id"] for s in second.payload["signals"]}
    assert not (first_ids & second_ids)              # no overlap, no skips


def test_rejections_answer_why_there_is_no_signal(store):
    """specs/14 §7 — the endpoint most systems cannot offer, because they never recorded this."""
    insert_rejection(store)
    insert_rejection(store)
    _, market, _ = build_services(store)
    result = market.rejections("B-BTC_USDT")
    assert result.ok and len(result.payload["rejections"]) == 2
    assert result.payload["most_binding"][0]["binding_condition"] == "mean_reversion_operative"
    assert result.payload["most_binding"][0]["n"] == 2
    assert result.payload["rejections"][0]["conditions"][0]["threshold"] == 1.0


def test_health_without_a_source_says_so_rather_than_claiming_healthy(store):
    _, _, health = build_services(store)              # no health_source attached
    result = health.health()
    assert not result.ok
    assert "no health source" in result.error.message
    assert result.envelope.system_status is SystemStatus.DOWN


def test_warmup_is_a_state_not_an_error(store):
    from features.value import FeatureKey, FeatureSet, FeatureStatus, FeatureValue
    from normalize.canonical import Timeframe

    warming = FeatureValue(name="TRD_RET", version="1", status=FeatureStatus.WARMING,
                           reason="need 721 1h bars, have 300", timeframe=Timeframe.H1,
                           horizon="long", detail={"hours_to_warm": 421.0})
    fs = FeatureSet(asset="B-BTC_USDT", product=Product.FUTURES, as_of_ms=T0,
                    values={FeatureKey("TRD_RET", Timeframe.H1, "long"): warming})
    _, _, health = build_services(store)
    result = health.warmup({"B-BTC_USDT": fs})
    assert result.ok                                   # warming is not a failure
    assert result.envelope.system_status is SystemStatus.WARMING
    assert result.payload["assets"]["B-BTC_USDT"]["hours_to_warm"] == 421.0


# ---- the HTTP boundary ---------------------------------------------------------------------

def test_the_app_refuses_to_bind_a_public_address_without_a_token():
    """specs/10 §9: exposure without authentication is not a supported configuration."""
    assert_bind_allowed("127.0.0.1", "")               # localhost is always fine
    assert_bind_allowed("0.0.0.0", "a-token")          # authenticated is fine
    with pytest.raises(StartupError) as ei:
        assert_bind_allowed("0.0.0.0", "")
    assert "refusing to bind" in ei.value.message
    assert "SSH tunnel" in ei.value.remediation
    assert ei.value.problem_class.value == "configuration"


def test_a_service_failure_answers_503_and_a_healthy_one_200():
    ok = ServiceResult(payload={}, envelope=FreshnessEnvelope(T0, T0, SystemStatus.HEALTHY))
    degraded = ServiceResult(payload={}, envelope=FreshnessEnvelope(T0, T0, SystemStatus.DEGRADED))
    failed = ServiceResult(payload={}, envelope=FreshnessEnvelope(T0, None, SystemStatus.DOWN),
                           error=StoreError("x", component="c", operation="o"))
    assert status_code_for(ok) == 200
    assert status_code_for(degraded) == 200            # degraded still answers, with its reasons
    assert status_code_for(failed) == 503


def test_the_http_routes_serve_the_same_service_results(store, tmp_path):
    """Served through ReadOnlyStore, because a sync handler runs in a worker thread and a sqlite3
    connection belongs to the thread that made it — sharing the writer's raises on the first request."""
    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    insert_signal(store)
    insert_rejection(store)
    reader = ReadOnlyStore(store.path)
    signals, market, health = build_services(reader, health_source=healthy_report)
    app = create_app(signals=signals, market=market, health=health)
    client = TestClient(app)

    live = client.get("/api/signals/live")
    assert live.status_code == 200
    body = live.json()
    assert body["data"]["count"] == 1
    assert body["envelope"]["age_seconds"] is not None
    assert body["envelope"]["system_status"] == "HEALTHY"

    assert client.get("/api/signals/sig-1").status_code == 200
    assert client.get("/api/signals/nope").status_code == 503
    assert client.get("/api/assets/B-BTC_USDT/rejections").json()["data"]["most_binding"]
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_an_authenticated_app_rejects_an_unauthenticated_request(store):
    from fastapi.testclient import TestClient

    from store.reader import ReadOnlyStore

    signals, market, health = build_services(ReadOnlyStore(store.path),
                                             health_source=healthy_report)
    app = create_app(signals=signals, market=market, health=health, host="0.0.0.0",
                     auth_token="secret-token")
    client = TestClient(app)
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"x-auth-token": "secret-token"}).status_code == 200


def test_no_route_accepts_a_parameter_that_reaches_an_engine(store):
    """specs/14 §11: the API is read-only in the strongest sense — it cannot influence a signal."""
    signals, market, health = build_services(store)

    app = create_app(signals=signals, market=market, health=health)
    forbidden = {"delta", "threshold", "risk_per_trade", "param", "size", "equity", "override"}
    for route in app.routes:
        params = set(getattr(route, "param_convertors", {}) or {})
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            import inspect
            params |= set(inspect.signature(endpoint).parameters)
        assert not (params & forbidden), f"{route.path} accepts {params & forbidden}"


# ---- the dashboard --------------------------------------------------------------------------

def result_with(signals, *, status=SystemStatus.HEALTHY, error=None, age_ms=1000):
    env = FreshnessEnvelope(served_at_ms=T0 + age_ms, data_as_of_ms=T0, system_status=status,
                            degraded_reasons=["store: locked"] if error else [],
                            correlation_id="abc123")
    return ServiceResult(payload={"signals": signals, "count": len(signals)}, envelope=env,
                         error=error)


def card(**kw):
    base = {"signal_id": "sig-1", "strategy_id": "C1-B", "asset": "B-BTC_USDT", "direction": "LONG",
            "state": "SETUP_DETECTED", "entry": "85000", "stop": "84500", "targets": ["86500"],
            "validated": False, "k_net": "2.4", "breakeven_hit_rate": "0.31",
            "payload": {"raw": {"rationale": "the variance ratio is below 1"}}}
    return {**base, **kw}


def test_a_quiet_market_and_a_broken_system_look_different():
    """Failure mode 1 (specs/15 §1): both render as empty panels by default."""
    quiet = render_signals(result_with([]))
    blind = render_signals(result_with([], status=SystemStatus.DOWN,
                                       error=StoreError("locked", component="s", operation="q")))
    assert "no setups match right now" in quiet
    assert "SYSTEM DOWN" not in quiet
    assert "SYSTEM DOWN" in blind
    assert "not</strong> an empty market" in blind
    assert "ref abc123" in blind                       # the correlation id reaches the screen


def test_enter_now_relocates_and_spells_out_the_word():
    """specs/15 §6: position, size and a word — three cues before colour."""
    bar = render_action_bar([card(state="ENTER_NOW"), card(signal_id="s2")])
    assert "action-bar live" in bar
    assert ">ENTER<" in bar
    assert "85000" in bar


def test_an_empty_action_bar_says_so_rather_than_being_blank():
    bar = render_action_bar([card(), card(signal_id="s2")])
    assert "no entries live — 2 setups waiting" in bar
    assert "action-bar empty" in bar


def test_an_unvalidated_signal_is_a_labelled_section_not_a_badge():
    """Failure mode 3: a badge among many gets scanned past."""
    html = render_signals(result_with([card(validated=False)]))
    assert "not yet backtested" in html
    assert "unvalidated" in html                        # dashed border class
    assert "not-backtested-section" in html


def test_confidence_is_never_blank_and_offers_the_breakeven_rate_instead():
    """Failure mode 2: nulls render as blank by default."""
    html = render_signals(result_with([card()]))
    assert "not estimated" in html
    assert "31% win rate to break even" in html


def test_three_correlated_longs_are_described_as_one_position():
    """Failure mode 5: three cards imply three opportunities."""
    signals = [card(signal_id=f"s{i}", asset=a)
               for i, a in enumerate(("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"))]
    strip = render_exposure_strip(signals)
    assert "3 long · 0 short" in strip
    assert "approximately one position" in strip
    assert "0.7-0.9 correlated" in strip


def test_an_empty_panel_states_the_binding_condition():
    """Failure mode 1 again, and the visible payoff of evaluating every condition."""
    panel = render_empty_panel("C1-B", {"binding_condition": "mean_reversion_operative",
                                        "passed_count": 5, "total_count": 6})
    assert "no setup — mean_reversion_operative" in panel
    assert "(5 of 6 conditions pass)" in panel
    assert "no evaluation recorded yet" in render_empty_panel("C1-A", None)


def test_a_stale_envelope_is_visibly_different():
    """Failure mode 6: subtle greying is not sufficient."""
    fresh = render_signals(result_with([card()], age_ms=2_000))
    stale = render_signals(result_with([card()], age_ms=400_000))
    assert "age-ok" in fresh and "age-red" in stale
    assert "7m ago" in stale      # 400 s of envelope age plus the fixture offset


def test_the_page_renders_and_escapes_its_input():
    signals = result_with([card(strategy_id="<script>alert(1)</script>")])
    page = render_page(signals, ServiceResult(payload={"components": []},
                                              envelope=FreshnessEnvelope(T0, T0,
                                                                         SystemStatus.HEALTHY)))
    assert "<!doctype html>" in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "never places orders" in page


def test_health_renders_the_problem_class_so_triage_is_possible():
    report = HealthReport(components=[
        ComponentHealth(name="regime:B-BTC_USDT", status=SystemStatus.DEGRADED,
                        detail="failed validity tests",
                        problem_class=__import__("core.errors", fromlist=["ProblemClass"])
                        .ProblemClass.MODELLING)])
    html = render_health(ServiceResult(payload=report.to_dict(),
                                       envelope=FreshnessEnvelope(T0, T0, SystemStatus.DEGRADED)))
    assert "modelling" in html
    assert "regime:B-BTC_USDT" in html
    assert "DEGRADED" in html


def test_the_api_reads_through_a_thread_safe_read_only_connection(store, tmp_path):
    """Two properties at once: a connection per thread (the bug this caught), and read-only enforced
    by SQLite rather than by convention."""
    import sqlite3
    import threading

    from store.reader import ReadOnlyStore

    insert_signal(store)
    reader = ReadOnlyStore(store.path)
    seen = {}

    def read():
        seen[threading.current_thread().name] = reader.conn.execute(
            "SELECT COUNT(*) AS n FROM signals").fetchone()["n"]

    threads = [threading.Thread(target=read, name=f"w{i}") for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(seen.values()) == {1}, seen
    assert reader.stats()["read_connections"] >= 3

    with pytest.raises(sqlite3.OperationalError):
        reader.conn.execute("DELETE FROM signals")          # read-only is enforced by SQLite


def test_pointing_the_reader_at_a_missing_database_fails_clearly(tmp_path):
    from store.reader import ReadOnlyStore

    with pytest.raises(StoreError) as ei:
        ReadOnlyStore(tmp_path / "absent.db")
    assert "never creates the database" in ei.value.remediation


# ---- exposure through a tunnel (the Vercel dashboard) --------------------------------------

def test_a_tunnel_in_front_of_loopback_still_demands_a_token():
    """The guard that the hosted dashboard made necessary.

    Binding ``127.0.0.1`` used to *mean* unreachable, so the token was only demanded for a
    non-loopback bind. With a Cloudflare tunnel the process still binds loopback and is reachable
    from the internet, so the original check would have passed while the API sat open. The operator
    declares the tunnel, because only they know what is in front of the process.
    """
    from api.app import assert_bind_allowed
    from core.errors import StartupError

    # Loopback with no tunnel: fine, and unchanged.
    assert_bind_allowed("127.0.0.1", "")

    with pytest.raises(StartupError, match="without an auth token"):
        assert_bind_allowed("127.0.0.1", "", public_via_tunnel=True)

    assert_bind_allowed("127.0.0.1", "a-long-random-token", public_via_tunnel=True)


def test_the_settings_guard_agrees_with_the_api_guard():
    """Two guards, one rule. They are separate because one runs at configuration load and the other
    at app construction, and a host should fail at the earliest of the two."""
    from config.settings import Settings

    tunnelled = Settings(server={"host": "127.0.0.1", "auth_token": "", "public_via_tunnel": True})
    with pytest.raises(RuntimeError, match="without CT_SERVER__AUTH_TOKEN"):
        tunnelled.assert_bind_allowed()

    ok = Settings(server={"host": "127.0.0.1", "auth_token": "t" * 40, "public_via_tunnel": True})
    ok.assert_bind_allowed()


def test_the_token_is_compared_in_constant_time():
    """A plain ``!=`` returns as soon as two bytes differ, so the time taken leaks how much of the
    token was guessed. Cheap to exploit if measurable, and one import to fix."""
    source = (Path(__file__).resolve().parents[1] / "src" / "api" / "app.py").read_text(
        encoding="utf-8")
    assert "secrets.compare_digest" in source
    assert "supplied != auth_token" not in source
