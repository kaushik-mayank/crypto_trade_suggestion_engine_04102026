"""Pre-registration (specs/09 §1) — the mechanism, not the aspiration.

> "A protocol that can be edited after seeing results is not a protocol. Enforce it mechanically."

So this module is the enforcement, and it is deliberately unhelpful in one direction: it will not let
a run produce results unless a lock file exists and matches, and it will not quietly accept a lock
file that has changed since the last run.

**Window boundaries are absolute UTC dates.** Relative offsets ("the last 20%") silently shift as
data accumulates, which is a slow-motion way of moving the goalposts — the kind that leaves no trace
because nobody edited anything.

**The parameter set is captured whole**, all thirty with their tested bands, because a result is only
interpretable against the parameters that produced it. :func:`verify` compares the live
``parameter_hash`` against the locked one and **refuses** on a mismatch rather than warning: a
backtest run under different parameters than it registered is not the backtest that was registered.

**A protocol change is a version bump, and seeing results first burns the holdout.** specs/09 §1.3
calls that the intended consequence rather than a penalty to work around, so :func:`amend` records
the previous hash, demands a reason, and marks the lock as post-results when the holdout has already
been read.

There is no ``git_commit`` field because this repository is not a git checkout
(:mod:`core.version` exists for exactly this reason); ``code_version`` carries a digest of
``src/**/*.py`` instead, which is strictly more precise about what ran — a commit identifies a tree,
a digest identifies the tree that was actually executed, including uncommitted edits.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.errors import ProtocolViolation
from core.logging import get_logger
from core.version import code_version

log = get_logger("backtest.protocol")
COMPONENT = "backtest.protocol"

DEFAULT_PATH = Path("validation/protocol.lock.json")

#: The fields the hash covers, in a fixed order. Fixed because a hash over a dict whose key order can
#: vary is a hash that changes without the content changing.
HASHED_FIELDS = ("protocol_version", "created_utc", "code_version", "window_boundaries",
                 "parameter_set", "parameter_hash", "metric_definitions", "decision_rules",
                 "hypotheses", "trial_count_declared", "fill_model", "cost_model")


@dataclass
class ProtocolLock:
    """The specs/09 §1.1 lock file. ``sha256`` is self-referencing over :data:`HASHED_FIELDS`."""

    protocol_version: int
    created_utc: str
    code_version: str
    #: ``{"name": {"start_utc": "...", "end_utc": "..."}}`` — absolute dates, never offsets.
    window_boundaries: dict[str, dict[str, str]]
    #: All thirty parameters with value, status and tested band.
    parameter_set: dict[str, dict[str, Any]]
    parameter_hash: str
    metric_definitions: dict[str, str]
    decision_rules: dict[str, str]
    hypotheses: list[dict[str, str]]
    trial_count_declared: int
    fill_model: dict[str, str]
    cost_model: dict[str, str]
    sha256: str = ""
    #: Set when this version was created after results were seen, which burns the holdout (§1.3).
    amended_after_results: bool = False
    previous_sha256: str | None = None
    amendment_reason: str | None = None

    def digest(self) -> str:
        payload = {f: getattr(self, f) for f in HASHED_FIELDS}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def seal(self) -> ProtocolLock:
        self.sha256 = self.digest()
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def window(self, name: str) -> tuple[int, int]:
        """A named window as ``(start_ms, end_ms)``, parsed from its absolute UTC dates."""
        if name not in self.window_boundaries:
            raise ProtocolViolation(
                f"the protocol declares no window named {name!r}",
                component=COMPONENT, operation="window",
                context={"requested": name, "declared": sorted(self.window_boundaries)},
                likely_cause="a run asked for a window that was never pre-registered",
                remediation="windows are pre-registered with absolute dates; add it as a new "
                            "protocol version rather than passing it ad hoc",
            )
        bounds = self.window_boundaries[name]
        return _to_ms(bounds["start_utc"]), _to_ms(bounds["end_utc"])


def _to_ms(iso: str) -> int:
    try:
        parsed = dt.datetime.fromisoformat(iso)
    except ValueError as e:
        raise ProtocolViolation(
            f"window boundary {iso!r} is not an ISO-8601 datetime",
            component=COMPONENT, operation="to_ms", context={"value": iso},
            likely_cause="a relative offset or a loose date string was written into the lock file",
            remediation="use absolute UTC, e.g. 2024-01-01T00:00:00+00:00; relative offsets shift "
                        "as data accumulates, which moves the goalposts without an edit",
            cause=e,
        ) from e
    if parsed.tzinfo is None:
        raise ProtocolViolation(
            f"window boundary {iso!r} has no timezone",
            component=COMPONENT, operation="to_ms", context={"value": iso},
            likely_cause="a naive datetime, which means something different on every host",
            remediation="append +00:00; every boundary in this system is UTC and says so",
        )
    return int(parsed.timestamp() * 1000)


#: The formulas, written out. In the lock file so a metric cannot be redefined after the fact and
#: still look like the same metric.
METRIC_DEFINITIONS = {
    "r_multiple": "(exit_price - entry_price) * sign(direction) / |entry_price - initial_stop|, "
                  "weighted across partial exits; initial_stop is fixed at entry and never updated",
    "hit_rate": "count(r_multiple > 0) / n, published with its 95% Wilson score interval and n",
    "wilson_interval": "((p + z^2/2n) +/- z*sqrt(p(1-p)/n + z^2/4n^2)) / (1 + z^2/n), z = 1.96",
    "mean_r": "arithmetic mean of r_multiple over closed trades",
    "median_r": "median of r_multiple over closed trades; reported alongside the mean always, "
                "because they diverge for skewed payoffs and the mean alone hides which",
    "top5_dependency": "sum of the 5 largest r_multiples / sum of all r_multiples",
    "top_decile_share": "sum of the largest ceil(n/10) r_multiples / sum of all r_multiples",
    "max_drawdown_r": "largest peak-to-trough decline of the cumulative r_multiple curve, ordered "
                      "by exit time; expressed in R, not percent",
    "sharpe_per_trade": "mean_r / stdev_r over closed trades. REJECTED as sufficient output",
    "sortino_per_trade": "mean_r / stdev of negative r_multiples only",
    "mar": "sum of r_multiples / max_drawdown_r",
    "loss_tail_p95_r": "95th percentile of |r_multiple| over losing trades, linear interpolation",
    "stopped_mean_r": "mean r_multiple over trades exited by stop or trail. If materially worse "
                      "than -1.0R, real risk exceeds published risk and liq_safety is re-derived",
    "percentile": "linear interpolation on the sorted sample: v[floor(q(n-1))] blended with the "
                  "next value by the fractional part",
}

#: specs/09 §12, in the file that decides them.
DECISION_RULES = {
    "PROMOTE": "validated: true and validation_multiplier -> 1.0. Requires passing the §7.1 test "
               "against C4-X1, the §10 benchmark comparison, and the §8 sensitivity check",
    "REVISE": "permitted ONCE, and only for a defect — never because the result disappointed",
    "CUT": "a bespoke strategy that fails its benchmark comparison is cut, NOT tuned. Tuning until "
           "it beats the benchmark is fitting the benchmark, which is worse than having none",
    "random_entry_rule": "if a strategy cannot beat C4-X1 with statistical significance, its signal "
                         "contributes nothing and the strategy is cut. Applies to all four classes",
    "harness_rule": "if C4-X2 backtests well, the BACKTEST is suspect, not the strategy. Audit for "
                    "look-ahead, survivorship in level definitions, cost under-modelling and fill "
                    "logic before accepting any other result from the same harness",
    "confidence": "no confidence value is published anywhere until Phase 16 says so",
}

#: specs/09 §11 and specs/06 §4.1 — the falsifiable ones, recorded before anything runs so they
#: cannot be softened into observations afterwards.
HYPOTHESES = [
    {"id": "H1", "prediction": "C4-A performs poorly in Axis-P mean-reverting regime cells",
     "source": "specs/06 §4.1",
     "if_false": "the regime model, the strategy, or the backtest is wrong. Determining which is "
                 "more valuable than the headline result and is a required deliverable"},
    {"id": "H2", "prediction": "C4-C shows a high hit rate and a poor tail",
     "source": "specs/06 §4.3",
     "if_false": "if the hit rate is low the mechanism is absent; if the tail is good, check the "
                 "fill model before believing it"},
    {"id": "H3", "prediction": "Class 2 and Class 3 show high profit concentration; Class 1 and "
                               "C4-C show low concentration",
     "source": "specs/09 §5.2",
     "if_false": "for Class 2/3, the trailing exit is cutting winners; for Class 1/C4-C, the edge "
                 "rests on a few lucky events"},
    {"id": "H4", "prediction": "the mean realised R on stopped trades is worse than -1.0R",
     "source": "specs/09 §3.2",
     "if_false": "the gap rule is not being applied, or the sample contains no gap events yet"},
    {"id": "H5", "prediction": "C4-X2 (unconditioned Bollinger touch) does NOT backtest well",
     "source": "specs/06 §5.2",
     "if_false": "the harness is suspect: audit before accepting any other result from it"},
]

FILL_MODEL = {
    "entry": "the WORSE of {trigger price, next 1m bar open}, plus slippage. Never the trigger",
    "stop": "if the bar's range gaps beyond the stop, the fill is the bar's OPEN, not the stop",
    "target": "fills at the target price when the bar's range contains it; no slippage (limit "
              "order). A favourable gap beyond the target is deliberately not credited",
    "trail": "follows the stop gap rule",
    "intrabar_ambiguity": "when one bar contains both the stop and the target, the STOP is assumed "
                          "first. Always, and never resolved by selectively consulting finer data",
    "open_at_window_end": "marked at the final close and EXCLUDED from realised statistics",
}

COST_MODEL = {
    "fees": "round-trip taker fee from live InstrumentMeta, 2 * taker_fee.fraction * price. Never "
            "hardcoded: the vendor doc's sample values are wrong for BTC (hard invariant 1)",
    "slippage": "a fraction of price, applied adversely on entries and stops; never on targets",
    "sensitivity": "every headline result is re-run at 2x slippage. An edge that disappears there "
                   "is not robust enough to deploy, because the estimate itself carries error",
    "funding": "accrued per 8h interval crossed, from the live funding rate",
    "shared_with_live": "the same formula the risk engine charges, asserted by test rather than "
                        "assumed (specs/08 acceptance criterion 10)",
}


def build(*, windows: dict[str, dict[str, str]], parameters: Any, trial_count: int,
          protocol_version: int = 1) -> ProtocolLock:
    """Construct a sealed lock from the live parameter registry.

    Built from the registry rather than from a hand-written list so the locked parameter set cannot
    disagree with the one the run will use — the disagreement that makes a result uninterpretable.
    """
    parameter_set = {
        p.name: {"id": p.id, "value": p.value, "status": p.status, "band": p.band,
                 "unit": p.unit, "source_spec": p.source_spec}
        for p in parameters.parameters
    }
    return ProtocolLock(
        protocol_version=protocol_version,
        created_utc=dt.datetime.now(dt.UTC).isoformat(),
        code_version=code_version(),
        window_boundaries=windows,
        parameter_set=parameter_set,
        parameter_hash=parameters.parameter_hash,
        metric_definitions=dict(METRIC_DEFINITIONS),
        decision_rules=dict(DECISION_RULES),
        hypotheses=[dict(h) for h in HYPOTHESES],
        trial_count_declared=trial_count,
        fill_model=dict(FILL_MODEL),
        cost_model=dict(COST_MODEL),
    ).seal()


def write(lock: ProtocolLock, path: Path = DEFAULT_PATH, *, overwrite: bool = False) -> Path:
    """Write the lock, refusing to overwrite an existing one.

    Refusing is the whole point. A lock that can be silently replaced is not a pre-registration, so
    replacing one requires :func:`amend`, which records what it replaced and why.
    """
    path = Path(path)
    if path.exists() and not overwrite:
        existing = load(path)
        raise ProtocolViolation(
            f"a protocol lock already exists at {path} (version {existing.protocol_version}, "
            f"sha256 {existing.sha256[:12]})",
            component=COMPONENT, operation="write",
            context={"path": str(path), "existing_version": existing.protocol_version,
                     "existing_sha256": existing.sha256},
            likely_cause="a second pre-registration, or a re-run that regenerated the lock",
            remediation="use amend() with a stated reason, which bumps the version and records the "
                        "previous hash. A lock that can be silently replaced is not a protocol",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    log.info("protocol_locked", path=str(path), version=lock.protocol_version,
             sha256=lock.sha256[:12], windows=sorted(lock.window_boundaries))
    return path


def load(path: Path = DEFAULT_PATH) -> ProtocolLock:
    path = Path(path)
    if not path.exists():
        raise ProtocolViolation(
            f"no protocol lock at {path}",
            component=COMPONENT, operation="load", context={"path": str(path)},
            likely_cause="a backtest was started before the protocol was pre-registered",
            remediation="run `python -m backtest --preregister` first. specs/09 §1 requires the lock "
                        "to exist BEFORE the first backtest, because a protocol written afterwards "
                        "is a description of what happened, not a prediction",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProtocolViolation(
            f"the protocol lock at {path} is not valid JSON",
            component=COMPONENT, operation="load", context={"path": str(path), "error": str(e)},
            likely_cause="the file was hand-edited and left malformed",
            remediation="restore it from version control; a lock nobody can read enforces nothing",
            cause=e,
        ) from e
    known = {f.name for f in ProtocolLock.__dataclass_fields__.values()}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ProtocolViolation(
            f"the protocol lock has unknown field(s): {', '.join(unknown)}",
            component=COMPONENT, operation="load",
            context={"unknown": unknown, "known": sorted(known)},
            likely_cause="a field was added by hand, or the lock came from a newer schema",
            remediation="a lock this code cannot fully interpret must not be treated as satisfied",
        )
    return ProtocolLock(**raw)


def verify(lock: ProtocolLock, *, parameter_hash: str,
           allow_code_drift: bool = True) -> dict[str, Any]:
    """Check the lock against what is about to run. **Raises** on a mismatch.

    Three checks, in order of what they mean:

    * the lock's own hash, which catches an edited file;
    * the parameter hash, which catches a run under parameters other than the registered ones —
      refused, because such a result is not the backtest that was registered;
    * the code version, which is reported as drift rather than refused by default. Code changes
      between pre-registration and execution are normal during development; the version travels on
      every result so a reader can see exactly what produced it.
    """
    if lock.sha256 != lock.digest():
        raise ProtocolViolation(
            "the protocol lock's contents do not match its own sha256",
            component=COMPONENT, operation="verify",
            context={"recorded": lock.sha256, "computed": lock.digest()},
            likely_cause="the lock file was edited after it was sealed",
            remediation="a protocol that can be edited after seeing results is not a protocol "
                        "(specs/09 §1). Restore it, or amend() it explicitly and accept that an "
                        "amendment made after seeing results burns the holdout",
        )
    if lock.parameter_hash != parameter_hash:
        raise ProtocolViolation(
            "the parameter registry has changed since the protocol was locked",
            component=COMPONENT, operation="verify",
            context={"locked": lock.parameter_hash, "current": parameter_hash,
                     "protocol_version": lock.protocol_version},
            likely_cause="a parameter value or status was edited after pre-registration",
            remediation="a result is only interpretable against the parameters that produced it. "
                        "Either restore the registered values, or amend() the protocol to a new "
                        "version with the reason recorded (specs/09 §1.3)",
        )
    drift = lock.code_version != code_version()
    if drift and not allow_code_drift:
        raise ProtocolViolation(
            "the code has changed since the protocol was locked",
            component=COMPONENT, operation="verify",
            context={"locked": lock.code_version, "current": code_version()},
            likely_cause="src/ was edited after pre-registration",
            remediation="re-lock, or pass allow_code_drift=True and accept that the result is "
                        "attributed to the current code version rather than the locked one",
        )
    result = {"protocol_version": lock.protocol_version, "sha256": lock.sha256,
              "parameter_hash": parameter_hash, "locked_code_version": lock.code_version,
              "running_code_version": code_version(), "code_drift": drift,
              "amended_after_results": lock.amended_after_results}
    if drift:
        log.info("protocol_code_drift", locked=lock.code_version, running=code_version(),
                 detail="the code changed since pre-registration; the result is attributed to the "
                        "running version, which is recorded on every output")
    if lock.amended_after_results:
        log.warning("protocol_amended_after_results", version=lock.protocol_version,
                    reason=lock.amendment_reason,
                    detail="this protocol was changed after results were seen, which burns the "
                           "holdout (specs/09 §1.3). The holdout is in-sample from here on")
    return result


def amend(lock: ProtocolLock, *, reason: str, after_results: bool,
          parameters: Any | None = None) -> ProtocolLock:
    """A new protocol version. ``after_results=True`` records that the holdout is burned.

    specs/09 §1.3: "Changes made *after* seeing results **burn the holdout**. This is the intended
    consequence, not a penalty to be worked around." So the flag is a required argument rather than
    an inferred default — whoever amends has to state which situation they are in.
    """
    if not reason.strip():
        raise ProtocolViolation(
            "amending the protocol requires a stated reason",
            component=COMPONENT, operation="amend",
            context={"protocol_version": lock.protocol_version},
            likely_cause="an automated edit with no justification recorded",
            remediation="specs/09 §1.3 requires the reason in docs/findings.md; pass it here so the "
                        "lock itself carries it too",
        )
    updated = ProtocolLock(
        protocol_version=lock.protocol_version + 1,
        created_utc=dt.datetime.now(dt.UTC).isoformat(),
        code_version=code_version(),
        window_boundaries=dict(lock.window_boundaries),
        parameter_set=({p.name: {"id": p.id, "value": p.value, "status": p.status, "band": p.band,
                                 "unit": p.unit, "source_spec": p.source_spec}
                        for p in parameters.parameters}
                       if parameters is not None else dict(lock.parameter_set)),
        parameter_hash=(parameters.parameter_hash if parameters is not None
                        else lock.parameter_hash),
        metric_definitions=dict(lock.metric_definitions),
        decision_rules=dict(lock.decision_rules),
        hypotheses=[dict(h) for h in lock.hypotheses],
        trial_count_declared=lock.trial_count_declared,
        fill_model=dict(lock.fill_model),
        cost_model=dict(lock.cost_model),
        amended_after_results=after_results,
        previous_sha256=lock.sha256,
        amendment_reason=reason,
    ).seal()
    log.warning("protocol_amended", from_version=lock.protocol_version,
                to_version=updated.protocol_version, reason=reason,
                burns_holdout=after_results)
    return updated
