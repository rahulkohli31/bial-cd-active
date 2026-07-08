"""Per-app records request/response schemas.

Client-facing projections that NEVER leak `app_id`, `bytes`, or `search_text`
(`RecordOut`), plus the list/search/distinct/envelope wrappers — all on the shared
`CamelModel` base, matching the Express `app-data.js` wire envelopes exactly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from src.schemas import CamelModel


class CreateRequest(CamelModel):
    collection: str | None = None
    data: Any = None


class PatchRequest(CamelModel):
    data: Any = None


class RecordOut(CamelModel):
    """The client-facing projection — NEVER app_id/bytes/search_text."""

    id: uuid.UUID
    collection: str
    data: dict[str, Any]
    created_by: uuid.UUID | None
    created_in_draft: bool
    created_at: datetime
    updated_at: datetime


class ListResponse(CamelModel):
    records: list[RecordOut]


class SearchResponse(CamelModel):
    items: list[RecordOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class DistinctResponse(CamelModel):
    values: list[Any]


class OkResponse(CamelModel):
    ok: bool


class RecordEnvelope(CamelModel):
    record: RecordOut
