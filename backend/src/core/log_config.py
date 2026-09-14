"""The one structlog configuration every process installs: JSON in production, console in dev.

A logged exception is rendered as its signature (`core.error_signature`) in both, never as a
traceback: exception text and frame locals can hold a supervisor bearer, a DSN or what a citizen
typed, and a log line leaves the process."""

from __future__ import annotations

import sys
from typing import cast

import structlog
from structlog.typing import EventDict, WrappedLogger

from src.core.error_signature import error_signature


def render_exc_info_as_signature(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Replace `exc_info` with `exc_signature`, in whichever form `exc_info` arrived: `True`
    (the exception being handled), the exception itself, or a `sys.exc_info()` tuple."""
    exc_info: object = event_dict.pop("exc_info", None)
    exc: BaseException | None = None
    if exc_info is True:
        exc = sys.exc_info()[1]
    elif isinstance(exc_info, BaseException):
        exc = exc_info
    elif isinstance(exc_info, tuple):
        parts = cast("tuple[object, ...]", exc_info)
        if len(parts) == 3 and isinstance(parts[1], BaseException):
            exc = parts[1]
    if exc is not None:
        event_dict["exc_signature"] = error_signature(exc)
    return event_dict


def configure_logging(*, production: bool) -> None:
    """Install the process-wide chain. Called once, when an entry point is imported."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            render_exc_info_as_signature,
            structlog.processors.JSONRenderer() if production else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
