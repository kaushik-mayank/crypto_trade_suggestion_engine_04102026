"""The error taxonomy.

Every deliberate failure in this system raises a ``CryptoTraderError`` subclass. Each one carries
enough to act on without reading the code:

* **code** — a stable string (``STORE_POINT_IN_TIME_VIOLATION``) that can be grepped, alerted on and
  counted, and which never changes when the message is reworded;
* **problem_class** — *whose* problem it is: an application bug, a configuration mistake, local
  infrastructure, an external dependency, or data quality. This is what makes a 3 a.m. page
  actionable, and what will tell us on Azure whether to look at the code, the config, the VM or
  CoinDCX;
* **component / operation** — where it happened;
* **context** — the values that decided it (never secrets);
* **likely_cause** and **remediation** — what probably went wrong and what to do;
* **retryable** — whether retrying can possibly help. Set on the class, so no caller has to guess.

Two rules the rest of the codebase follows:

1. **Never swallow.** Catch a specific exception, wrap it with context, re-raise. A bare
   ``except Exception: pass`` is a defect.
2. **Never make a vague message.** "Something went wrong" cannot be acted on; every message here
   names the component, the operation and the values involved.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, ClassVar


class ProblemClass(str, Enum):
    """Who owns the problem. The single most useful field when triaging."""

    APPLICATION = "application_bug"        # our code is wrong — a defect, not an environment issue
    CONFIGURATION = "configuration"        # the operator's settings/parameters are wrong or missing
    INFRASTRUCTURE = "infrastructure"      # disk, database file, permissions, clock, process, host
    EXTERNAL = "external_dependency"       # CoinDCX: HTTP status, transport, malformed or stale data
    DATA_QUALITY = "data_quality"          # data arrived intact but is not fit to compute on
    MODELLING = "modelling"                # the data and the code are both fine, but a model's
                                           # assumption is not supported by this market -- e.g. the
                                           # regime model failing its validity tests (specs/12 §10).
                                           # Its own class because the triage answer is "read the
                                           # finding", not "check the feed" or "fix the code": paging
                                           # someone would waste the page.


class Severity(str, Enum):
    WARNING = "warning"      # degraded, still functioning
    ERROR = "error"          # this operation failed
    CRITICAL = "critical"    # the process cannot continue, or data loss is accruing


class CryptoTraderError(Exception):
    """Base for every deliberate failure. Subclasses set the class-level metadata."""

    code: ClassVar[str] = "UNCLASSIFIED"
    problem_class: ClassVar[ProblemClass] = ProblemClass.APPLICATION
    severity: ClassVar[Severity] = Severity.ERROR
    #: Whether retrying can possibly help. A class default, overridable per instance where the
    #: answer depends on the response (see :class:`HttpStatusError`).
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        component: str,
        operation: str,
        context: dict[str, Any] | None = None,
        likely_cause: str | None = None,
        remediation: str | None = None,
        cause: BaseException | None = None,
    ):
        self.message = message
        self.component = component
        self.operation = operation
        self.context = dict(context or {})
        self.likely_cause = likely_cause
        self.remediation = remediation
        self.__cause__ = cause
        super().__init__(str(self))

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.component}.{self.operation}: {self.message}"]
        if self.context:
            parts.append("context=" + json.dumps(self.context, default=str, sort_keys=True))
        if self.likely_cause:
            parts.append(f"likely cause: {self.likely_cause}")
        if self.remediation:
            parts.append(f"fix: {self.remediation}")
        if self.__cause__ is not None:
            parts.append(f"caused by: {type(self.__cause__).__name__}: {self.__cause__}")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """For structured logs, ``dq_events`` rows, the audit trail and API error bodies."""
        return {
            "code": self.code,
            "problem_class": self.problem_class.value,
            "severity": self.severity.value,
            "component": self.component,
            "operation": self.operation,
            "message": self.message,
            "context": self.context,
            "likely_cause": self.likely_cause,
            "remediation": self.remediation,
            "retryable": self.retryable,
            "cause": f"{type(self.__cause__).__name__}: {self.__cause__}" if self.__cause__ else None,
        }


# ---- configuration -------------------------------------------------------------------------

class ConfigError(CryptoTraderError):
    """Settings, parameters or config files are wrong. Raised at startup, before any work."""

    code = "CONFIG_INVALID"
    problem_class = ProblemClass.CONFIGURATION
    severity = Severity.CRITICAL


class ParameterError(ConfigError):
    code = "CONFIG_PARAMETER_INVALID"


class StartupError(ConfigError):
    """A precondition for running was not met. Deterministic and loud, never a partial start."""

    code = "STARTUP_FAILED"


# ---- external: the exchange ----------------------------------------------------------------

class ExternalError(CryptoTraderError):
    code = "EXTERNAL_FAILURE"
    problem_class = ProblemClass.EXTERNAL


class TransportFailure(ExternalError):
    """No HTTP response: DNS, TCP, TLS, timeout, proxy."""

    code = "EXTERNAL_TRANSPORT"
    retryable = True


class HttpStatusError(ExternalError):
    """A non-2xx response.

    ``retryable`` is decided per instance, because a 500 is worth retrying and a 422 never is: the
    422 means *our* request is wrong. It shadows the class default deliberately.
    """

    code = "EXTERNAL_HTTP_STATUS"

    def __init__(self, *args: Any, status: int, retryable: bool = False, **kw: Any):
        self.status = status
        kw["context"] = {"http_status": status, **(kw.get("context") or {})}
        super().__init__(*args, **kw)
        self.retryable = retryable          # instance attribute, shadowing the ClassVar default


class RateLimited(ExternalError):
    code = "EXTERNAL_RATE_LIMITED"
    retryable = True
    severity = Severity.WARNING


class ExchangeMaintenance(ExternalError):
    """503 — documented maintenance. Distinct from a server error: back off long, stay alive."""

    code = "EXTERNAL_MAINTENANCE"
    retryable = True
    severity = Severity.WARNING


class MalformedPayload(ExternalError):
    """2xx with a body that is not the JSON we can parse. The raw bytes are preserved."""

    code = "EXTERNAL_MALFORMED_PAYLOAD"


# ---- normalization and data quality --------------------------------------------------------

class NormalizationError(CryptoTraderError):
    """The payload parsed but does not match the contract. **Reject, never coerce.**"""

    code = "NORMALIZE_REJECTED"
    problem_class = ProblemClass.EXTERNAL


class SchemaError(NormalizationError):
    code = "NORMALIZE_SCHEMA"


class UnitError(NormalizationError):
    """A value is outside the range its declared unit permits — e.g. a fee ≥ 1%, which means the
    percent→fraction conversion was skipped (the 100× error of specs/10 §5)."""

    code = "NORMALIZE_UNIT"
    problem_class = ProblemClass.APPLICATION
    severity = Severity.CRITICAL


class DataQualityError(CryptoTraderError):
    """Data is intact but not fit to compute on. Gates, not crashes."""

    code = "DQ_FAILED"
    problem_class = ProblemClass.DATA_QUALITY
    severity = Severity.WARNING


class StaleDataError(DataQualityError):
    code = "DQ_STALE"


class GapError(DataQualityError):
    code = "DQ_GAP"


# ---- the snapshot boundary -----------------------------------------------------------------

class SnapshotError(CryptoTraderError):
    """The snapshot could not be built, or was asked for something it must not provide."""

    code = "SNAPSHOT_FAILURE"
    problem_class = ProblemClass.APPLICATION


class SnapshotIncomplete(SnapshotError):
    """A series or Tier B source the caller asked for is not fit to use at this ``as_of``.

    Data quality, not a bug: the right response is to go quiet for that asset, not to crash.
    """

    code = "SNAPSHOT_INCOMPLETE"
    problem_class = ProblemClass.DATA_QUALITY
    severity = Severity.WARNING


# ---- storage -------------------------------------------------------------------------------

class StoreError(CryptoTraderError):
    code = "STORE_FAILURE"
    problem_class = ProblemClass.INFRASTRUCTURE


class MigrationError(StoreError):
    code = "STORE_MIGRATION_FAILED"
    severity = Severity.CRITICAL


class PointInTimeViolation(StoreError):
    """A query would have returned data newer than ``as_of``. This is look-ahead: an application
    bug, and the most dangerous class of failure in a backtest."""

    code = "STORE_POINT_IN_TIME_VIOLATION"
    problem_class = ProblemClass.APPLICATION
    severity = Severity.CRITICAL


class IntegrityError(StoreError):
    code = "STORE_INTEGRITY"


class ClockError(CryptoTraderError):
    """This host's clock disagrees with recorded data or with the venue beyond tolerance.

    Its own class because every freshness gate, the 2-second basis tolerance and ``as_of`` itself
    rest on the clock: a skewed clock makes every other verdict meaningless, and the fix is on the
    host (NTP), not in the code or at the exchange.
    """

    code = "CLOCK_SKEWED"
    problem_class = ProblemClass.INFRASTRUCTURE
    severity = Severity.CRITICAL


# ---- features, regime, strategies, risk ----------------------------------------------------

class FeatureError(CryptoTraderError):
    code = "FEATURE_FAILURE"
    problem_class = ProblemClass.APPLICATION


class FeatureNotAvailable(FeatureError):
    """A caller read a feature whose status is not OK. The status carries the reason; conflating
    "0.3" with "could not be computed" is how a data outage becomes a trade (specs/12 §2)."""

    code = "FEATURE_NOT_OK"
    problem_class = ProblemClass.DATA_QUALITY
    severity = Severity.WARNING


class InvalidOHLC(FeatureError):
    """Negative Garman-Klass variance is mathematically impossible on valid OHLC (specs/12 §4.1):
    the bar is invalid and normalization failed to catch it. Never clamped."""

    code = "FEATURE_INVALID_OHLC"
    severity = Severity.CRITICAL


class RegimeError(CryptoTraderError):
    code = "REGIME_FAILURE"
    problem_class = ProblemClass.APPLICATION


class StrategyError(CryptoTraderError):
    code = "STRATEGY_FAILURE"
    problem_class = ProblemClass.APPLICATION


class SetupNotReady(StrategyError):
    """The strategy is fine and the inputs are not there yet. A **gate, not a failure.**

    Raised where a strategy cannot proceed because data is warming, holed or absent — an ACT unit with
    no 15m window, a trigger with no closed 1m bar. Separated from :class:`StrategyError` because the
    two demand opposite responses: an ``application_bug`` means read the code, a ``data_quality`` gate
    means wait for the recorder. Reporting the second as the first is how a fresh deployment looks
    broken on its first evaluation, and how a genuine bug hides in the noise of fifty warming notices.

    Still a ``StrategyError`` subclass, so a condition body absorbs it into ``passed=False`` with the
    reason attached exactly as before.
    """

    code = "STRATEGY_NOT_READY"
    problem_class = ProblemClass.DATA_QUALITY
    severity = Severity.WARNING


class RiskError(CryptoTraderError):
    code = "RISK_FAILURE"
    problem_class = ProblemClass.APPLICATION


class ImmutabilityViolation(RiskError):
    """The risk engine altered a strategy-produced level. specs/08 §1 forbids this absolutely."""

    code = "RISK_LEVELS_MUTATED"
    severity = Severity.CRITICAL


# ---- audit, api, rendering, backtest -------------------------------------------------------

class AuditError(CryptoTraderError):
    code = "AUDIT_FAILURE"
    problem_class = ProblemClass.INFRASTRUCTURE


class ReplayMismatch(AuditError):
    """A stored signal did not reproduce. Either a bug, or a recorded bar mutation."""

    code = "AUDIT_REPLAY_MISMATCH"
    problem_class = ProblemClass.APPLICATION


class ApiError(CryptoTraderError):
    code = "API_FAILURE"
    problem_class = ProblemClass.APPLICATION


class ServiceUnavailable(ApiError):
    """A dependency the request needed is down. Must render as DOWN, never as an empty result —
    "no data" and "no signals" are different answers (specs/14 §10)."""

    code = "API_SERVICE_UNAVAILABLE"
    problem_class = ProblemClass.INFRASTRUCTURE


class RenderError(CryptoTraderError):
    code = "RENDER_FAILURE"
    problem_class = ProblemClass.APPLICATION


class BacktestError(CryptoTraderError):
    code = "BACKTEST_FAILURE"
    problem_class = ProblemClass.APPLICATION


class ProtocolViolation(BacktestError):
    """A pre-registration rule was broken: the holdout touched twice, a lock edited after results,
    the harness reading data it must not see (specs/09 §1)."""

    code = "BACKTEST_PROTOCOL_VIOLATION"
    severity = Severity.CRITICAL


ALL_ERRORS: tuple[type[CryptoTraderError], ...] = (
    ConfigError, ParameterError, StartupError,
    ExternalError, TransportFailure, HttpStatusError, RateLimited, ExchangeMaintenance, MalformedPayload,
    NormalizationError, SchemaError, UnitError,
    DataQualityError, StaleDataError, GapError,
    SnapshotError, SnapshotIncomplete,
    StoreError, MigrationError, PointInTimeViolation, IntegrityError, ClockError,
    FeatureError, FeatureNotAvailable, InvalidOHLC,
    RegimeError, StrategyError, SetupNotReady, RiskError, ImmutabilityViolation,
    AuditError, ReplayMismatch, ApiError, ServiceUnavailable, RenderError,
    BacktestError, ProtocolViolation,
)
