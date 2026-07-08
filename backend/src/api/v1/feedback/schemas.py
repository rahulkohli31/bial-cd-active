"""Feedback request/response schemas.

Both are documentation-only. The route parses the raw JSON body itself and returns a
pre-built `JSONResponse`, so that it can emit the ported `400 {"error":{"message"}}`
envelope (not FastAPI's `422 {"detail"}`) and the `201 {"ok": true}` success body —
these models describe those shapes in OpenAPI without enabling runtime validation.
"""

from __future__ import annotations

from src.schemas import CamelModel


class FeedbackRequest(CamelModel):
    """The `POST /feedback` body: a required `message` and an advisory `page`."""

    message: str
    page: str | None = None


class FeedbackResponse(CamelModel):
    """The 201 success body: `{"ok": true}`."""

    ok: bool
