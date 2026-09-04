"""Application-wide exception handlers.

Two guarantees:
1. Validation errors never reflect submitted input back to the client. FastAPI's
   default 422 body includes an ``input`` field that echoes the rejected value —
   for a password field that means leaking the plaintext (and it may be logged).
   We return only ``type``/``loc``/``msg``.
2. Any unhandled exception returns a generic 500 with no internal detail; the real
   error is logged server-side only (`.claude/rules/security.md`: NEVER expose
   internal errors to the frontend).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


class AppApiError(Exception):
    """An app-lifecycle / platform error rendered as the ported
    ``{"error": {"message": ...}}`` body the SPA already consumes (distinct from the
    auth endpoints' ``{"detail": ...}`` shape).

    Carries its own HTTP status so the lifecycle, admin, and build-session routers can
    fail closed with a stable, non-leaking message. An optional machine-readable
    ``code`` is surfaced under ``error.code`` so the SPA can branch on it rather than
    string-matching the message, and an optional structured ``detail`` is surfaced
    under ``error.detail`` for the refusals a client must RENDER rather than merely
    branch on (R15b's waiting-for-review 409 carries the pending state, the submitted
    version and the rejection note, so neither citizen surface needs a second call).
    ``detail`` must be JSON-ready — plain strings/numbers/bools/None only, never an
    un-serialisable object and never internal identifiers a citizen must not see.
    Raised from a dependency or a route; rendered by ``app_api_error_handler``.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.detail = detail


def app_api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Typed as Exception to match Starlette's handler signature; narrows before use.
    if not isinstance(exc, AppApiError):
        raise TypeError(
            f"app_api_error_handler received {type(exc).__name__}, expected AppApiError"
        )
    error: dict[str, Any] = {"message": exc.message}
    if exc.code is not None:
        error["code"] = exc.code
    if exc.detail is not None:
        error["detail"] = exc.detail
    return JSONResponse(status_code=exc.status_code, content={"error": error})


# Pydantic prefixes every `ValueError` a validator raises with "Value error, ". That
# prefix is machine vocabulary, and this envelope is not machine-only: `apiError.ts`
# resolves a 422 as `error.message` -> `detail[].msg` -> ... and renders the result, so
# whatever a validator says reaches a citizen's screen verbatim. Before this, renaming a
# project too long read:
#
#     Value error, name must be at most 120 characters
#
# The validators now write for a person (#158 §14), and the prefix is the one part they
# cannot remove themselves — it is added after they raise. So it comes off HERE, at the
# boundary that already exists to curate what leaves.
#
# Only the prefix goes. Pydantic's own messages ("Field required", "Input should be a
# valid string") do not carry it and are untouched, and the `type`/`loc` a client matches
# on are unchanged.
_VALUE_ERROR_PREFIX = "Value error, "


def _user_facing(msg: object) -> object:
    if isinstance(msg, str) and msg.startswith(_VALUE_ERROR_PREFIX):
        return msg[len(_VALUE_ERROR_PREFIX) :]
    return msg


def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Typed as Exception to match Starlette's handler signature; Starlette only
    # dispatches RequestValidationError here.
    if not isinstance(exc, RequestValidationError):
        raise TypeError(
            f"validation_exception_handler received {type(exc).__name__}, "
            "expected RequestValidationError"
        )
    # Keep field location and message; drop `input` and `ctx`, which can carry the
    # submitted value (e.g. a plaintext password) or the whole request body.
    safe_errors = [
        {"type": err.get("type"), "loc": err.get("loc"), "msg": _user_facing(err.get("msg"))}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": safe_errors},
    )


def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled_exception",
        method=request.method,
        path=request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # AppApiError renders the ported {"error": {"message"}} data-plane shape; Starlette
    # dispatches it ahead of the Exception catch-all via the MRO lookup.
    app.add_exception_handler(AppApiError, app_api_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
