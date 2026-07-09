"""Feedback request schema.

`FeedbackRequest` is documentation-only: the route parses the raw JSON body itself and
returns a pre-built `JSONResponse`, so it can emit the ported `400 {"error":{"message"}}`
envelope (not FastAPI's `422 {"detail"}`) without enabling runtime validation. The
`201 {"ok": true}` success body is the shared `OkResponse` (`src/schemas/responses.py`).
"""

from __future__ import annotations

from src.schemas import CamelModel


class FeedbackRequest(CamelModel):
    """The `POST /feedback` body: a required `message` and an advisory `page`."""

    message: str
    page: str | None = None
