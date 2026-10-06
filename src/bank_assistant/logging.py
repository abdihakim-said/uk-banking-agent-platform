"""Structured JSON logging. Never log message content: log sizes, ids and outcomes."""

from __future__ import annotations

import logging
import sys

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, WrappedLogger


def _add_trace_ids(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    """Put the current trace and span id on every log line, so a log jumps straight to its trace in Jaeger."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def configure_logging(level: str = "INFO", service: str = "bank-assistant") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_ids,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
