"""Attachment request/response schemas.

The upload + delete routes return pre-built `JSONResponse`s (to emit the ported
`{"attachment": …}` / `{"ok": true}` bodies and the `{"error":{"message","code?"}}`
envelope), so `UploadResponse`/`OkResponse` are DOCUMENTED-ONLY — the characterization
tests are the byte-identical guard. The download route returns raw bytes, so it has no
model.
"""

from __future__ import annotations

from src.schemas import CamelModel


class AttachmentRef(CamelModel):
    """A stored-attachment file part. The core fields are always present; the office
    (`format`/`text`/`truncationNote`) and deck (`pdfFileId`/`pageCount`) extras appear
    only for those kinds."""

    attachment_id: str
    key: str
    media_type: str
    size: int
    name: str
    kind: str
    format: str | None = None
    text: str | None = None
    truncated: bool | None = None
    truncation_note: str | None = None
    pdf_file_id: str | None = None
    page_count: int | None = None


class UploadResponse(CamelModel):
    """The 201 upload body: `{"attachment": {…}}`."""

    attachment: AttachmentRef


class OkResponse(CamelModel):
    """The 200 delete body: `{"ok": true}`."""

    ok: bool
