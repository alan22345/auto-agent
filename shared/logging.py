"""Shared structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys

import structlog

from shared.config import settings


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
