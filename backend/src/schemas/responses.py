"""Reusable OpenAPI error-response machinery: the body models for the API's two
live error envelopes, plus a builder that turns explicit `(code, model,
description)` tuples into the `responses=` mapping FastAPI expects.

The API has two error envelopes, deliberately NOT unified (a merge would reshape
existing bodies — a behavior change):

* `ErrorEnvelope` — `{"error": {"message", "code?"}}` — rendered by
  `app_api_error_handler` for every `AppApiError` (app lifecycle, admin, build
  sessions) and by the rate-limiter's handler.
* `DetailBody` — `{"detail": str}` — the bare `HTTPException` raises from the
  `current_user` (401 / suspension 403) and `requires_superadmin` (403)
  dependencies, and the global unhandled-exception 500.

`DailyTokenLimitBody` documents claude's daily-token 429, which carries
`limit`/`used`/`remaining` beyond the plain envelope (`DailyTokenLimitExceededError`)
— documenting it as a plain `ErrorEnvelope` would understate the real body.

These models exist for the OpenAPI schema only; the actual responses are rendered
by the exception handlers / hand-built `JSONResponse`s. Keys are single-word, so a
plain `BaseModel` reproduces the wire shape without the camelCase base.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.schemas.base import CamelModel


class OkResponse(CamelModel):
    """The shared `{"ok": true}` success envelope — the single definition for every
    mutation route that returns it (admin delete, conversations patch/delete,
    attachments delete, feedback submit). `ok` is single-word, so
    the camel base is a no-op and the wire body is `{"ok": true}`."""

    ok: bool


class ErrorDetail(BaseModel):
    """The inner object of the `AppApiError` envelope. `code` is present only when
    the raiser set one (e.g. `FILE_QUOTA_EXCEEDED`, `PARSE_TIMEOUT`)."""

    message: str
    code: str | None = None


class ErrorEnvelope(BaseModel):
    """`{"error": {"message", "code?"}}` — every `AppApiError` and the rate limiter."""

    error: ErrorDetail


class DetailBody(BaseModel):
    """`{"detail": str}` — bare-`HTTPException` deps (401/403) and the unhandled 500."""

    detail: str


# The single shared "401 Not authenticated" spec — spread into the `responses=` of every
# route whose 401 originates in a bare-`HTTPException` auth dependency (`current_user` /
# `requires_superadmin`), i.e. the `{"detail"}` shape. One definition so the tuple value is
# byte-identical everywhere and the generated `responses=` mapping stays unchanged.
AUTH_401: tuple[int, type[BaseModel], str] = (401, DetailBody, "Not authenticated")

# The single shared "403 Account suspended" spec — `current_user` refuses a suspended
# account on EVERY authenticated route (deps.py, R11), the same bare-`HTTPException`
# `{"detail"}` shape as the 401. Spread ONCE into the v1-router-level defaults
# (api/v1/router.py); a route that declares its own 403 (admin's superadmin gate)
# overrides the description, not the shape.
AUTH_403_SUSPENDED: tuple[int, type[BaseModel], str] = (403, DetailBody, "Account suspended")

# The shared superadmin-gate pair, spread into the `responses=` of every route behind
# `requires_superadmin`. That dependency layers after `current_user`: an unauthenticated
# caller gets 401 and a non-super-admin 403, both bare `HTTPException` -> `{"detail"}`
# (documented as `DetailBody`), while the routes' own raises are `AppApiError` ->
# `ErrorEnvelope`. Lives here beside `AUTH_401` for the reason stated above it — one
# definition so the tuple is byte-identical everywhere — now that the gate has consumers in
# more than one router module (`admin/router.py`, `deploy/router.py`'s `unpublish`).
ADMIN_AUTH: tuple[tuple[int, type[BaseModel], str], ...] = (
    AUTH_401,
    (403, DetailBody, "Super-admin privileges required"),
)


class DailyTokenLimitDetail(BaseModel):
    """claude's daily-token 429 inner object — the five keys the SPA interceptor reads."""

    message: str
    code: str
    limit: int
    used: int
    remaining: int


class DailyTokenLimitBody(BaseModel):
    """`{"error": {message, code, limit, used, remaining}}` — the daily-token 429."""

    error: DailyTokenLimitDetail


def error_responses(
    *specs: tuple[int, type[BaseModel], str],
) -> dict[int | str, dict[str, Any]]:
    """Build the FastAPI `responses=` mapping from explicit `(status_code, body_model,
    description)` tuples — one entry per documented non-2xx code, mirroring
    `health/router.py`'s `{503: {"model": HealthStatus, "description": ...}}`.

    Each route passes the body model it actually returns, so a route can document a
    401 as `DetailBody` and another as `ErrorEnvelope` in the same layer without the
    helper guessing.
    """
    responses: dict[int | str, dict[str, Any]] = {}
    for code, model, description in specs:
        if code in responses:
            raise ValueError(f"duplicate status {code} in error_responses()")
        responses[code] = {"model": model, "description": description}
    return responses
