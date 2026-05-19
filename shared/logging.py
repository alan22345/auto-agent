"""Shared structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

import structlog

from shared.config import settings

# ---------------------------------------------------------------------------
# Secret-value redactor
# ---------------------------------------------------------------------------
# A process-local set of plaintext secret values that must never appear in
# structured log output. Call register_secret() whenever a secret is read
# from the store so it is automatically scrubbed from every subsequent log
# event.

_secret_lock = threading.Lock()
_known_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Register a plaintext secret value so it is redacted from log events.

    Call this immediately after reading a secret from the store. Values
    shorter than 4 characters are not registered (too short to redact
    reliably without collateral damage).
    """
    if value and len(value) >= 4:
        with _secret_lock:
            _known_secrets.add(value)


def _redact_value(v: Any) -> Any:
    """Recursively replace known secret strings inside dicts/lists/strings."""
    if isinstance(v, str):
        with _secret_lock:
            secrets = frozenset(_known_secrets)
        for secret in secrets:
            if secret in v:
                v = v.replace(secret, "[REDACTED]")
        return v
    if isinstance(v, dict):
        return {k: _redact_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        redacted = [_redact_value(item) for item in v]
        return type(v)(redacted)
    return v


def _redact_processor(logger: Any, method: str, event_dict: dict) -> dict:
    """structlog processor that scrubs known secret values from every event."""
    return {k: _redact_value(v) for k, v in event_dict.items()}


def setup_logging(service_name: str) -> structlog.BoundLogger:
    """Configure structured JSON logging for a service.

    Call once at service startup. Returns a bound logger with the service name.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging to go through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )

    return structlog.get_logger(service=service_name)
