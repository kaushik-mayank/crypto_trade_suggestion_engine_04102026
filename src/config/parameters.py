"""The parameter registry (specs/02 §10, specs/08 §11).

One versioned TOML file holds every tunable quantity in the system, and its hash is recorded on
every signal (`parameter_set_hash`) so a bad deploy is attributable and any signal is replayable.

Enforced here, not by discipline:

* the **cap** (30) — adding a parameter requires removing one or raising the cap in writing;
* every entry carries a justification, a source spec and an overfitting-exposure note;
* ``status`` distinguishes a *calibrated* value from a *provisional* one. Nothing downstream may
  present a provisional value as measured, so ``Parameter.is_calibrated`` is explicit and
  ``ParameterRegistry.provisional()`` lists what still needs Phase 16;
* a ``provisional`` value must lie inside its registered band — a value outside the band it will
  be swept across is a contradiction;
* ``fixed`` parameters have no band and are never swept.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.errors import ConfigError

Status = Literal["fixed", "provisional", "unset", "user", "conditional", "judgment"]
VALID_STATUS: tuple[str, ...] = ("fixed", "provisional", "unset", "user", "conditional", "judgment")

#: Parameters with a band that specs/09 §8 sweeps for sensitivity.
SWEEPABLE: tuple[str, ...] = ("provisional", "conditional", "judgment")

#: Statuses that must name the phase which sets them. A ``judgment`` is excluded deliberately:
#: specs/08 §11 registers ``unvalidated_multiplier`` as a PERMANENT judgment — it exists precisely
#: because the data that would replace it is absent, so no phase can ever set it.
NEEDS_SET_BY: tuple[str, ...] = ("provisional", "conditional", "unset")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "config" / "parameters.toml"


class RegistryError(ValueError):
    """The registry file violates a rule that the specs state as non-negotiable."""


@dataclass(frozen=True)
class Parameter:
    id: int
    name: str
    value: Any
    unit: str
    status: Status
    band: Any
    used_by: tuple[str, ...]
    source_spec: str
    justification: str
    overfitting_exposure: str
    set_by: str | None = None
    condition: str | None = None

    @property
    def is_calibrated(self) -> bool:
        """True only for values that are final: structural constants and operator choices.

        ``provisional`` and ``conditional`` values are placeholders until Phase 16; ``unset`` has
        no value at all. Callers that publish a number derived from a parameter must be able to
        say which kind it was.
        """
        return self.status in ("fixed", "user")

    @property
    def numeric_band(self) -> tuple[float, float] | None:
        b = self.band
        if isinstance(b, list) and len(b) == 2 and all(isinstance(x, (int, float)) for x in b):
            return float(b[0]), float(b[1])
        return None


class ParameterRegistry:
    def __init__(self, parameters: list[Parameter], cap: int, version: str, path: Path):
        self._by_name = {p.name: p for p in parameters}
        self._by_id = {p.id: p for p in parameters}
        self.parameters = tuple(parameters)
        self.cap = cap
        self.version = version
        self.path = path

    # ---- access ----------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.parameters)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def get(self, name: str) -> Parameter:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no registered parameter {name!r}; registry holds {sorted(self._by_name)}") from None

    def value(self, name: str) -> Any:
        """The parameter's value. Raises for ``unset`` — there is nothing to return."""
        p = self.get(name)
        if p.status == "unset":
            raise RegistryError(f"{name} is unset (set by: {p.set_by}); it has no value yet")
        return p.value

    def by_id(self, pid: int) -> Parameter:
        return self._by_id[pid]

    def provisional(self) -> tuple[Parameter, ...]:
        """Everything Phase 16 still has to calibrate, plus anything unset."""
        return tuple(p for p in self.parameters if not p.is_calibrated)

    def sweepable(self) -> tuple[Parameter, ...]:
        """Parameters with a numeric band for the specs/09 §8 sensitivity sweep."""
        return tuple(p for p in self.parameters if p.status in SWEEPABLE and p.numeric_band)

    # ---- identity --------------------------------------------------------------------------

    @property
    def parameter_hash(self) -> str:
        """sha256 over (id, name, value) for every entry, in id order.

        Deliberately covers values only — editing a justification does not invalidate a signal's
        provenance, but changing any value does.
        """
        payload = json.dumps(
            [[p.id, p.name, p.value] for p in sorted(self.parameters, key=lambda x: x.id)],
            separators=(",", ":"),
            sort_keys=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def summary(self) -> dict:
        """For the health endpoint and the operations view (specs/19 §11)."""
        counts: dict[str, int] = {}
        for p in self.parameters:
            counts[p.status] = counts.get(p.status, 0) + 1
        return {
            "version": self.version,
            "count": len(self),
            "cap": self.cap,
            "parameter_hash": self.parameter_hash,
            "by_status": counts,
            "provisional": [p.name for p in self.provisional()],
        }


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RegistryError(msg)


def load_registry(path: str | Path = DEFAULT_PATH) -> ParameterRegistry:
    path = Path(path)
    # Startup failures must be deterministic and say what to do. A bare FileNotFoundError names the
    # path and nothing else -- not which component wanted it, not what it is for, not how to fix it.
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigError(
            f"no parameter registry at {path}",
            component="config.parameters", operation="load_registry",
            context={"path": str(path), "default": str(DEFAULT_PATH)},
            likely_cause="the working directory is not the repository root, or the file was not "
                         "deployed alongside the code",
            remediation="every tunable value lives in config/parameters.toml and the system will not "
                        "start without it; copy it from the repository or pass an explicit path",
            cause=e,
        ) from e
    except OSError as e:
        raise ConfigError(
            f"the parameter registry at {path} could not be read",
            component="config.parameters", operation="load_registry",
            context={"path": str(path), "error": str(e)},
            likely_cause="a permissions problem, or the file is held by another process",
            remediation="check the file's permissions and that no editor holds a lock on it",
            cause=e,
        ) from e
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"the parameter registry at {path} is not valid TOML",
            component="config.parameters", operation="load_registry",
            context={"path": str(path), "error": str(e)},
            likely_cause="a hand edit left the file malformed",
            remediation="fix the syntax; the registry is the single source of every tunable value and "
                        "a partial parse would silently change the parameter hash",
            cause=e,
        ) from e
    cap = int(raw.get("cap", 0))
    version = str(raw.get("version", ""))
    _require(cap > 0, "registry must declare a positive cap")
    _require(bool(version), "registry must declare a version")

    entries = raw.get("parameter", [])
    _require(bool(entries), "registry contains no parameters")

    params: list[Parameter] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for entry in entries:
        for field in ("id", "name", "status", "used_by", "source_spec", "justification", "overfitting_exposure"):
            _require(field in entry, f"parameter {entry.get('name', entry.get('id'))} is missing {field!r}")
        pid, name, status = int(entry["id"]), str(entry["name"]), str(entry["status"])
        _require(status in VALID_STATUS, f"{name}: status {status!r} not in {VALID_STATUS}")
        _require(pid not in seen_ids, f"duplicate parameter id {pid}")
        _require(name not in seen_names, f"duplicate parameter name {name!r}")
        _require(bool(str(entry["justification"]).strip()), f"{name}: justification must not be empty")
        seen_ids.add(pid)
        seen_names.add(name)

        p = Parameter(
            id=pid,
            name=name,
            value=entry.get("value"),
            unit=str(entry.get("unit", "")),
            status=status,  # type: ignore[arg-type]
            band=entry.get("band"),
            used_by=tuple(entry["used_by"]),
            source_spec=str(entry["source_spec"]),
            justification=str(entry["justification"]),
            overfitting_exposure=str(entry["overfitting_exposure"]),
            set_by=entry.get("set_by"),
            condition=entry.get("condition"),
        )

        # A value that will be swept must sit inside the band it is swept across.
        if p.status in SWEEPABLE:
            nb = p.numeric_band
            if nb is not None and isinstance(p.value, (int, float)):
                lo, hi = nb
                _require(lo <= float(p.value) <= hi, f"{name}: provisional value {p.value} outside band [{lo}, {hi}]")
            pass
        if p.status in NEEDS_SET_BY:
            _require(bool(p.set_by), f"{name}: {p.status} parameters must name the phase that sets them (set_by)")
        if p.status == "judgment":
            _require(not p.set_by, f"{name}: a judgment is permanent (specs/08 §11) and must not claim a setting phase")
        if p.status == "conditional":
            _require(bool(p.condition), f"{name}: conditional parameters must state their condition")
        if p.status == "fixed":
            _require(p.numeric_band is None, f"{name}: fixed parameters are never swept and must not carry a numeric band")

        params.append(p)

    _require(len(params) <= cap, f"registry holds {len(params)} parameters, exceeding the cap of {cap}")
    return ParameterRegistry(params, cap=cap, version=version, path=path)
