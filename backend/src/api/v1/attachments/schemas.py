"""Attachment request/response schemas.

The upload and delete routes RETURN these models, so what the declaration promises and what
the route sends are one thing. They used to return a pre-built `JSONResponse`, which meant
FastAPI validated and filtered nothing and the schema was documentation that could drift.
Both routes serialize with `exclude_none`, which is what keeps the ported wire body exact:
the legacy fields below are absent rather than null. The download route returns raw bytes, so
it has no model.
"""

from __future__ import annotations

from src.schemas import CamelModel


class AttachmentRef(CamelModel):
    """A stored-attachment file part: six fields this route fills, and five it never can.

    ★ `format`, `text`, `truncated`, `truncationNote` AND `pdfFileId` ARE RENDER-ONLY, and the
    distinction is the point. They were produced by the extractor and the deck-to-PDF step, both
    of which are gone: an upload today can populate none of them. They stay on the model because
    a message sent before that change still carries them in its stored payload, and the browser
    draws those chips from exactly these names — removing them would blank a historic
    transcript. Nothing new ever sets one, so they are absent from every response this route
    sends.

    `pageCount` went with the page cap for the opposite reason: nothing read it. It was only
    ever produced by the admission check that is gone, so keeping it would have been a field no
    client could act on."""

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
