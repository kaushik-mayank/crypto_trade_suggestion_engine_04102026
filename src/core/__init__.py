"""Cross-cutting foundations: the error taxonomy, structured logging, and health reporting."""
from .errors import (
    CryptoTraderError,
    ProblemClass,
    Severity,
)
from .health import ComponentHealth, HealthReport, SystemStatus
from .logging import get_logger, register_secrets, setup_logging

__all__ = [
    "CryptoTraderError", "ProblemClass", "Severity",
    "ComponentHealth", "HealthReport", "SystemStatus",
    "get_logger", "register_secrets", "setup_logging",
]
