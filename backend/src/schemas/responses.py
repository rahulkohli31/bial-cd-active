"""Reusable OpenAPI error-response machinery: the body models for the API's two
live error envelopes, plus a builder that turns explicit `(code, model,
description)` tuples into the `responses=` mapping FastAPI expects.

The API has two error envelopes, deliberately NOT unified (a merge would reshape
existing bodies — a behavior change):

* `ErrorEnvelope` — `{"error": {"message", "code?"}}` — rendered by
  `app_api_error_handler` for every `AppApiError` (data-plane app-key chain,
  lifecycle, admin, records/files/parse quota) and by the rate-limiter's handler.
* `DetailBody` — `{"detail": str}` — the bare `HTTPException` raises from the
  `current_user` (401) and `requires_superadmin` (403) dependencies, and the
  global unhandled-exception 500.

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
    mutation route that returns it (admin delete, conversations patch/delete, records
    delete, files delete, attachments delete). `ok` is single-word, so the camel base is
    a no-op and the wire body is `{"ok": true}`."""

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
        responses[code] = {"model": model, "description": description}
    return responses
