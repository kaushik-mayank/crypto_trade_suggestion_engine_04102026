"""The holdout guard (specs/09 §1.2, specs/16 Stage 10).

> The holdout may be used **exactly once**, after all decisions are final. A second use burns it —
> it becomes in-sample forever and a new holdout must be carved from future data that does not
> yet exist.

Enforced mechanically rather than by discipline:

* the directory is **physically separate** from ``data/parquet`` and ``data/state.db``;
* reading requires an explicit ``unlock(reason=...)``, which **appends an access record** to
  ``data/holdout/ACCESS_LOG.jsonl`` with the timestamp, the protocol hash and the reason;
* a **second** unlock raises :class:`ProtocolViolation` unless ``allow_burn=True`` is passed, which
  itself records that the holdout is burned.

The access log is committed evidence: `specs/16` acceptance 7 requires proving the holdout was used
once, after Stage 9.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import ProtocolViolation
from core.logging import get_logger

log = get_logger("store.holdout")

ACCESS_LOG = "ACCESS_LOG.jsonl"
BURNED_MARKER = "BURNED"


@dataclass
class HoldoutGuard:
    directory: Path
    protocol_hash: str = ""

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self._unlocked = False

    # ---- state -----------------------------------------------------------------------------

    @property
    def access_log_path(self) -> Path:
        return self.directory / ACCESS_LOG

    def accesses(self) -> list[dict[str, Any]]:
        if not self.access_log_path.exists():
            return []
        return [json.loads(line) for line in self.access_log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    @property
    def used(self) -> bool:
        return any(a.get("event") == "unlock" for a in self.accesses())

    @property
    def burned(self) -> bool:
        return (self.directory / BURNED_MARKER).exists()

    # ---- use -------------------------------------------------------------------------------

    def unlock(self, *, reason: str, allow_burn: bool = False) -> HoldoutGuard:
        """Permit reads. Records the access; refuses a second unlock unless it is a deliberate burn."""
        if not reason.strip():
            raise ProtocolViolation(
                "unlocking the holdout requires a stated reason",
                component="store.holdout", operation="unlock", context={"directory": str(self.directory)},
                likely_cause="an automated read attempted without recording why",
                remediation="pass reason='Stage 10 holdout evaluation of the promoted set'",
            )
        if self.used and not allow_burn:
            first = next(a for a in self.accesses() if a.get("event") == "unlock")
            raise ProtocolViolation(
                "the holdout has already been used once and may not be read again",
                component="store.holdout", operation="unlock",
                context={"first_access": first, "directory": str(self.directory)},
                likely_cause="a second evaluation, or a re-run after seeing the first result",
                remediation="a second use BURNS the holdout: it becomes in-sample forever. If that is "
                            "genuinely intended, pass allow_burn=True and carve a new holdout from "
                            "future data (specs/09 §1.2)",
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {"event": "unlock", "at_ms": int(time.time() * 1000), "reason": reason,
                  "protocol_hash": self.protocol_hash, "burn": bool(self.used and allow_burn)}
        with open(self.access_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if self.used and allow_burn:
            (self.directory / BURNED_MARKER).write_text(
                f"burned at {record['at_ms']}: {reason}\n", encoding="utf-8")
            log.critical("holdout_burned", reason=reason, directory=str(self.directory))
        else:
            log.warning("holdout_unlocked", reason=reason, protocol_hash=self.protocol_hash,
                        directory=str(self.directory))
        self._unlocked = True
        return self

    def path(self, *parts: str) -> Path:
        """A path inside the holdout. Raises unless unlocked."""
        if not self._unlocked:
            raise ProtocolViolation(
                "the holdout is locked: reading it requires an explicit, logged unlock",
                component="store.holdout", operation="path",
                context={"directory": str(self.directory), "requested": "/".join(parts),
                         "previously_used": self.used},
                likely_cause="the harness tried to read holdout data during normal operation",
                remediation="holdout data is for specs/16 Stage 10 only, after all decisions are "
                            "final; call unlock(reason=...) there and nowhere else",
            )
        return self.directory.joinpath(*parts)

    def status(self) -> dict[str, Any]:
        """For the health endpoint and the validation report."""
        return {"directory": str(self.directory), "exists": self.directory.exists(),
                "used": self.used, "burned": self.burned, "accesses": len(self.accesses()),
                "unlocked_in_this_process": self._unlocked}
