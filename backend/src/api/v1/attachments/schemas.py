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
    (`format`/`text`/`truncationNote`) and deck (`pdfFileId`) extras appear only for those kinds.

    `pageCount` WENT WITH THE PAGE CAP. It was the only one of the optional fields nothing read:
    its siblings still render historic messages whose parts carry them, while a page count was
    only ever produced by the admission check that is gone. Kept, it would be a field the upload
    route can never populate and no client can act on."""

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


class UploadResponse(CamelModel):
    """The 201 upload body: `{"attachment": {…}}`."""

    attachment: AttachmentRef
