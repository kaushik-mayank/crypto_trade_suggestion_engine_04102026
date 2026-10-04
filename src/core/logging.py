"""Structured logging (specs/10 §6, §8).

Two things this must get right:

1. **Logs are not the audit trail.** Logs are operational diagnostics: rotated, disposable. The
   audit trail is SQLite and is never rotated away. Do not put a signal's derivation in a log line.
2. **Noise hides failures.** The recorder polls ~230 times an hour; if every poll logs at INFO the
   one line that matters is invisible. So: **per-operation detail at DEBUG, state changes and
   failures at INFO/WARNING/ERROR.** The console defaults to INFO, the file to DEBUG.

Every ``CryptoTraderError`` logs as structured fields — ``code``, ``problem_class``, ``component``,
``operation``, ``context`` — so failures can be counted and filtered by kind rather than grepped by
message.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

from .errors import CryptoTraderError

#: Values that must never reach a log line, set once at startup from the loaded credentials.
_SECRETS: set[str] = set()


def register_secrets(*values: str) -> None:
    """Register values to be redacted from every log line (specs/10 §9, two-layer redaction)."""
    for v in values:
        if v and len(v) >= 6:          # ignore empty/short values: redacting "" would blank everything
            _SECRETS.add(v)


def _redact(_logger: Any, _name: str, event_dict: dict) -> dict:
    if not _SECRETS:
        return event_dict
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            for secret in _SECRETS:
                if secret in value:
                    event_dict[key] = value.replace(secret, "<redacted>")
    return event_dict


def _unpack_error(_logger: Any, _name: str, event_dict: dict) -> dict:
    """Flatten a ``CryptoTraderError`` passed as ``error=`` into queryable fields."""
    err = event_dict.pop("error", None)
    if isinstance(err, CryptoTraderError):
        d = err.to_dict()
        event_dict.setdefault("error_code", d["code"])
        event_dict.setdefault("problem_class", d["problem_class"])
        event_dict.setdefault("component", d["component"])
        event_dict.setdefault("operation", d["operation"])
        event_dict.setdefault("error_message", d["message"])
        if d["context"]:
            event_dict.setdefault("error_context", d["context"])
        for k in ("likely_cause", "remediation", "retryable", "cause"):
            if d[k] is not None:
                event_dict.setdefault(k, d[k])
    elif isinstance(err, BaseException):
        event_dict.setdefault("error_code", "UNHANDLED")
        event_dict.setdefault("problem_class", "application_bug")
        event_dict.setdefault("error_message", f"{type(err).__name__}: {err}")
    elif err is not None:
        event_dict["error"] = err
    return event_dict


def setup_logging(
    log_dir: Path | None = None,
    *,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    json_console: bool = False,
    log_name: str = "app",
) -> None:
    """Configure structlog + stdlib logging. Safe to call twice (handlers are replaced)."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _unpack_error,
        _redact,
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)

    console_renderer: Any = (
        structlog.processors.JSONRenderer() if json_console
        else structlog.dev.ConsoleRenderer(colors=False, exception_formatter=structlog.dev.plain_traceback)
    )
    if sys.stdout is not None:                       # None under a fully detached launch
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(console_level.upper())
        ch.setFormatter(structlog.stdlib.ProcessorFormatter(processor=console_renderer, foreign_pre_chain=shared))
        root.addHandler(ch)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / f"{log_name}.jsonl", maxBytes=20_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(file_level.upper())
        fh.setFormatter(structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(), foreign_pre_chain=shared))
        root.addHandler(fh)

    # Third-party noise: urllib3 logs every connection at DEBUG, which drowns our own DEBUG lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
