"""The error taxonomy, logging redaction and health classification.

These are failure-path tests by construction: the subject *is* the failure machinery.
"""
import json
import logging

import pytest

from core import errors as E
from core.health import ComponentHealth, HealthReport, SystemStatus
from core.logging import get_logger, register_secrets, setup_logging

# ---- error taxonomy ------------------------------------------------------------------------

def test_error_message_names_component_operation_context_and_cause():
    try:
        raise ValueError("disk went away")
    except ValueError as cause:
        err = E.StoreError(
            "could not write bar batch",
            component="store.sqlite",
            operation="upsert_bars",
            context={"asset": "B-BTC_USDT", "rows": 500},
            likely_cause="the database file is on a full or read-only volume",
            remediation="check free space and file permissions on data/state.db",
            cause=cause,
        )
    text = str(err)
    assert "[STORE_FAILURE]" in text
    assert "store.sqlite.upsert_bars" in text
    assert "could not write bar batch" in text
    assert '"asset": "B-BTC_USDT"' in text and '"rows": 500' in text
    assert "likely cause: the database file is on a full or read-only volume" in text
    assert "fix: check free space" in text
    assert "caused by: ValueError: disk went away" in text


def test_every_error_class_declares_a_stable_code_and_owner():
    seen: dict[str, type] = {}
    for cls in E.ALL_ERRORS:
        assert cls.code != "UNCLASSIFIED", cls.__name__
        assert isinstance(cls.problem_class, E.ProblemClass), cls.__name__
        # codes must be unique so they can be counted and alerted on
        if cls.code in seen:
            assert issubclass(cls, seen[cls.code]) or issubclass(seen[cls.code], cls), f"duplicate code {cls.code}"
        seen.setdefault(cls.code, cls)


def test_problem_class_separates_the_four_kinds_of_trouble():
    assert E.ConfigError.problem_class is E.ProblemClass.CONFIGURATION
    assert E.TransportFailure.problem_class is E.ProblemClass.EXTERNAL
    assert E.StoreError.problem_class is E.ProblemClass.INFRASTRUCTURE
    assert E.PointInTimeViolation.problem_class is E.ProblemClass.APPLICATION   # look-ahead is OUR bug
    assert E.StaleDataError.problem_class is E.ProblemClass.DATA_QUALITY


def test_retryable_is_declared_not_guessed():
    assert E.TransportFailure.retryable is True
    assert E.RateLimited.retryable is True
    assert E.ExchangeMaintenance.retryable is True
    assert E.ConfigError.retryable is False          # retrying a bad config forever is a hang
    assert E.UnitError.retryable is False


def test_http_status_error_carries_status_and_per_instance_retryability():
    e429 = E.HttpStatusError("rate limited", component="ingest", operation="poll", status=429, retryable=True)
    e422 = E.HttpStatusError("bad param", component="ingest", operation="poll", status=422, retryable=False)
    assert e429.status == 429 and e429.retryable is True
    assert e422.retryable is False
    assert e422.context["http_status"] == 422


def test_unit_error_is_an_application_bug_and_critical():
    # The 100x fee error: the data was fine, our conversion was not.
    assert E.UnitError.problem_class is E.ProblemClass.APPLICATION
    assert E.UnitError.severity is E.Severity.CRITICAL


def test_to_dict_is_json_serialisable_for_logs_and_audit():
    err = E.GapError("window contains an unconfirmed gap", component="features.windows",
                     operation="extract", context={"missing_bars": 3})
    d = err.to_dict()
    json.dumps(d)                                  # must not raise
    assert d["code"] == "DQ_GAP" and d["problem_class"] == "data_quality"
    assert d["context"] == {"missing_bars": 3}


# ---- logging ------------------------------------------------------------------------------

def test_error_fields_are_unpacked_into_structured_log_fields(capsys):
    setup_logging(None, console_level="INFO", json_console=True)
    err = E.ExchangeMaintenance("503 from api.coindcx.com", component="ingest.client",
                                operation="get", context={"host": "api.coindcx.com"})
    get_logger("t").warning("poll failed", error=err)
    out = capsys.readouterr().out
    rec = json.loads(out.strip().splitlines()[-1])
    assert rec["error_code"] == "EXTERNAL_MAINTENANCE"
    assert rec["problem_class"] == "external_dependency"
    assert rec["component"] == "ingest.client" and rec["operation"] == "get"
    assert rec["retryable"] is True


def test_secrets_are_redacted_from_every_log_field(capsys):
    register_secrets("super-secret-key-value")
    setup_logging(None, console_level="INFO", json_console=True)
    get_logger("t").error("auth failed", detail="header was super-secret-key-value")
    out = capsys.readouterr().out
    assert "super-secret-key-value" not in out
    assert "<redacted>" in out


def test_short_values_are_not_registered_as_secrets_blanking_everything(capsys):
    register_secrets("ab")                        # too short: redacting it would corrupt all text
    setup_logging(None, console_level="INFO", json_console=True)
    get_logger("t").info("about")
    assert "about" in capsys.readouterr().out


def test_console_stays_quiet_at_info_while_file_keeps_debug(tmp_path, capsys):
    setup_logging(tmp_path, console_level="INFO", file_level="DEBUG", json_console=True, log_name="t")
    log = get_logger("t")
    log.debug("per_poll_detail", status=200)       # the noisy kind: 230/hour
    log.warning("gap_detected", seconds=900)
    console = capsys.readouterr().out
    assert "per_poll_detail" not in console, "DEBUG must not reach the console or real failures drown"
    assert "gap_detected" in console
    contents = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "per_poll_detail" in contents and "gap_detected" in contents
    logging.getLogger().handlers.clear()


# ---- health -------------------------------------------------------------------------------

def test_unhealthy_component_must_name_its_owner():
    with pytest.raises(ValueError, match="declares no problem_class"):
        ComponentHealth(name="ingest", status=SystemStatus.DOWN, detail="no responses")
    ComponentHealth(name="ingest", status=SystemStatus.DOWN, detail="no responses",
                    problem_class=E.ProblemClass.EXTERNAL)          # fine


def test_overall_status_is_the_worst_component_and_lists_owners():
    r = HealthReport()
    r.add(ComponentHealth.healthy("store"))
    r.add(ComponentHealth(name="spot_book", status=SystemStatus.STALE, detail="10.5 min old",
                          problem_class=E.ProblemClass.EXTERNAL))
    r.add(ComponentHealth(name="features", status=SystemStatus.WARMING, detail="431/1440 bars"))
    assert r.status is SystemStatus.STALE
    assert r.problem_classes == ["external_dependency"]
    assert any("spot_book: stale" in x for x in r.degraded_reasons)


def test_warming_is_not_a_failure():
    r = HealthReport().add(ComponentHealth(name="features", status=SystemStatus.WARMING,
                                           detail="accumulating bars"))
    assert r.status is SystemStatus.WARMING
    assert r.problem_classes == []                 # nobody needs to fix warming


def test_health_from_error_preserves_classification():
    err = E.MigrationError("schema version 3 missing", component="store.sqlite", operation="migrate",
                           remediation="run migrations")
    c = ComponentHealth.from_error("store", err)
    assert c.status is SystemStatus.DOWN
    assert c.problem_class is E.ProblemClass.INFRASTRUCTURE
    assert c.context["code"] == "STORE_MIGRATION_FAILED"
    assert c.remediation == "run migrations"


def test_down_and_quiet_market_are_distinguishable_in_the_report():
    quiet = HealthReport().add(ComponentHealth.healthy("ingest")).add(ComponentHealth.healthy("store"))
    broken = HealthReport().add(ComponentHealth(name="ingest", status=SystemStatus.DOWN,
                                                detail="no successful poll in 22 min",
                                                problem_class=E.ProblemClass.EXTERNAL))
    assert quiet.to_dict()["status"] == "HEALTHY" and quiet.to_dict()["degraded_reasons"] == []
    assert broken.to_dict()["status"] == "DOWN" and broken.to_dict()["degraded_reasons"]
