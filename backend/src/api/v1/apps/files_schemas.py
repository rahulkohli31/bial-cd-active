"""Per-app file-storage request/response schemas.

Client-facing projections that NEVER leak `app_id`, `blob_key`, or `status`
(`FileOut`), plus the upload request and list/url/envelope wrappers — all on the
shared `CamelModel` base, matching the Express `app-files.js` wire envelopes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from src.schemas import CamelModel


class UploadRequest(CamelModel):
    collection: str | None = None
    filename: str
    content_type: str
    base64: str


class FileOut(CamelModel):
    """The client-facing projection — NEVER app_id/blob_key/status."""

    file_id: uuid.UUID
    collection: str
    filename: str
    content_type: str
    size: int
    created_by: uuid.UUID | None
    created_in_draft: bool
    created_at: datetime
    updated_at: datetime


class ListResponse(CamelModel):
    files: list[FileOut]


class DownloadUrlResponse(CamelModel):
    url: str
    expires_at: datetime


class FileEnvelope(CamelModel):
    file: FileOut
