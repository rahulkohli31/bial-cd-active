"""Feedback HTTP endpoint — citizen feedback submission (R30, R31).

`POST /v1/feedback` byte-matches the Express `POST /api/feedback` contract
(`server/feedback.js`): a required `message` (trimmed, ≤4000 UTF-8 bytes) and an advisory
`page` (sanitized to a same-origin path or empty). The author is ALWAYS the authenticated
caller — a body-supplied `username` is ignored. Success is `201 {"ok": true}`; validation
failures return the Express `400 {"error":{"message"}}` shape the SPA reads (not FastAPI's
default `422 {"detail"}`). Rate-limited per user (20 / 15 min) via the in-process limiter.

Feedback *read* is an admin surface (Plan B) — not here.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api.deps import CurrentUser, DbSession
from src.api.v1.feedback.schemas import FeedbackRequest, FeedbackResponse
from src.core.errors import AppApiError
from src.db.models.feedback import Feedback
from src.schemas import DetailBody, ErrorEnvelope, error_responses
from src.services.ratelimit import rate_limit

router = APIRouter(prefix="/feedback", tags=["feedback"])

# The raw-parse route takes a JSON body FastAPI never sees (no Pydantic param), so its
# request shape is documented explicitly from the model — without enabling the 422 path.
_REQUEST_BODY_DOC: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": FeedbackRequest.model_json_schema()}},
    }
}

# Message byte cap (UTF-8, on the trimmed value) — Express `MAX_FEEDBACK_CHARS`.
MAX_FEEDBACK_BYTES = 4000
# Page code-point cap — Express `MAX_PAGE_CHARS`.
MAX_PAGE_CODE_POINTS = 256
# A single leading slash NOT followed by another — the same-origin path shape
# `useLocation().pathname` produces. Drops absolute URLs, `javascript:`, and
# protocol-relative `//evil`. Mirrors Express `/^\/(?!\/)/`.
_SAME_ORIGIN_PATH = re.compile(r"^/(?!/)")

# Per-user rate limit (parity with Express `makeFeedbackLimiter`: 20 / 15 min).
FEEDBACK_RATE_LIMIT = 20
FEEDBACK_RATE_WINDOW_SECONDS = 15 * 60


async def _feedback_rate_key(user: CurrentUser) -> str:
    # Per-user bucket — this limiter instance is feedback-specific, so the user id alone
    # isolates callers. (Express keys by user+IP; behind the edge the IP is the proxy, so
    # the user is the meaningful axis.) Declaring `CurrentUser` here resolves identity
    # BEFORE the limiter runs — the "limiter after key" ordering.
    return f"feedback:{user.id}"


_feedback_limiter = rate_limit(
    _feedback_rate_key,
    limit=FEEDBACK_RATE_LIMIT,
    window_seconds=FEEDBACK_RATE_WINDOW_SECONDS,
    message="Too many feedback submissions. Please try again later.",
)


def _sanitize_page(value: Any) -> str:
    """A client `page` reduced to a safe same-origin path or empty, matching Express
    `validateFeedback`: non-string → ''; truncate to 256 code points; keep only a
    single-leading-slash path (else '')."""
    if not isinstance(value, str):
        return ""
    # Python str length is code points, so a plain slice IS the code-point truncation.
    page = value[:MAX_PAGE_CODE_POINTS]
    if not _SAME_ORIGIN_PATH.match(page):
        return ""
    return page


@router.post(
    "",
    status_code=201,
    response_model=FeedbackResponse,
    dependencies=[Depends(_feedback_limiter)],
    openapi_extra=_REQUEST_BODY_DOC,
    # 400 (validation) and 429 (limiter) render the AppApiError/limiter envelope; the
    # inherited `current_user` 401 is DetailBody.
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid or missing feedback message"),
        (401, DetailBody, "Not authenticated"),
        (429, ErrorEnvelope, "Too many feedback submissions"),
    ),
)
async def submit_feedback(request: Request, user: CurrentUser, db: DbSession) -> JSONResponse:
    # Raw-body parse so a non-object body / non-string message returns the EXACT Express
    # 400 shape (FastAPI's Pydantic path would 422 with a different envelope).
    try:
        body: Any = await request.json()
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(400, "Feedback message is required.") from None
    if not isinstance(body, dict):
        raise AppApiError(400, "Feedback message is required.")

    message = body.get("message")
    if not isinstance(message, str):
        raise AppApiError(400, "Feedback message is required.")
    trimmed = message.strip()
    if not trimmed:
        raise AppApiError(400, "Feedback message cannot be empty.")
    if len(trimmed.encode("utf-8")) > MAX_FEEDBACK_BYTES:
        raise AppApiError(400, "Feedback message is too long (max 4000 characters).")

    # Author from the verified session, NEVER the body. One row, then commit (the request
    # is the unit of work; get_db does not auto-commit).
    db.add(Feedback(user_id=user.id, message=trimmed, page=_sanitize_page(body.get("page"))))
    await db.commit()
    return JSONResponse(status_code=201, content={"ok": True})
