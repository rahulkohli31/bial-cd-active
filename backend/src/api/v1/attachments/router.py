"""Attachment HTTP endpoints — image/PDF upload / download / delete (R16, R4).

Byte-matches the Express `/api/attachments` contract (`server/attachments.js`): one base64
file per request, server-side allowlist + magic-byte validation, a 4 MB per-file cap, a 50 MB
per-user byte quota, owner-scoped object keys, and the `{error:{message}}` / `{ok:true}`
envelopes. Text is never uploaded (it travels inline); office/deck branches land in U11.

Identity is the authenticated caller; object keys are `att/{user_id}/{uuid}` (traversal-safe,
UUID axes) and every read/delete is scoped by `user_id` AND re-guarded with `assert_owned`.
The object store is injected via `storage_dependency` so tests swap an in-memory fake.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from src.api.deps import CurrentUser, DbSession
from src.api.v1.attachments.schemas import OkResponse, UploadResponse
from src.core.errors import AppApiError
from src.db.models.attachment import Attachment
from src.db.models.user import User
from src.schemas import DetailBody, ErrorEnvelope, error_responses
from src.services.extract.deck import (
    DeckConvertError,
    convert_deck_to_pdf,
    deck_attachments_enabled,
)
from src.services.extract.office import (
    OFFICE_MEDIA_TYPES,
    PPTX_MEDIA_TYPE,
    office_format_for,
)
from src.services.extract.zip_safety import FileParseError
from src.services.media.magic import ALLOWED_MEDIA, magic_matches
from src.services.parse.governor import run_parse
from src.services.ratelimit import rate_limit
from src.services.storage import (
    ObjectStorage,
    StorageError,
    StorageNotFoundError,
    assert_owned,
    attachment_key,
    get_storage,
)

router = APIRouter(prefix="/attachments", tags=["attachments"])

# Client-minted attachment id shape (Express `ID_RE`) — a safe object-key token.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Per-file decoded cap (Express `ATTACHMENT_MAX_BYTES`) and per-user total (Express
# `ATTACHMENT_TOTAL_CAP`), plus the request-body ceiling (Express mount `limit:'6mb'`).
ATTACHMENT_MAX_BYTES = 4 * 1024 * 1024
ATTACHMENT_TOTAL_CAP = 50 * 1024 * 1024
_BODY_LIMIT_BYTES = 6 * 1024 * 1024

# The allowlist + magic-byte prefixes live in `src.services.media.magic` — the SINGLE source of
# truth shared with the `/v1/claude` chat relay, so an inline block the upload path would reject
# can never slip through the relay unvalidated. `ALLOWED_MEDIA` / `magic_matches` imported above.

# Attachment limiter (Express: ~30/min, POST + DELETE only; GET is never limited).
ATTACHMENT_RATE_LIMIT = 30
ATTACHMENT_RATE_WINDOW_SECONDS = 60


def storage_dependency() -> ObjectStorage:
    """The configured object store. A dependency (not a bare `get_storage()` at the callsite)
    so tests override it with an in-memory fake via `dependency_overrides`."""
    return get_storage()


async def _attachment_rate_key(user: CurrentUser) -> str:
    return f"attachment:{user.id}"


_attachment_limiter = rate_limit(
    _attachment_rate_key,
    limit=ATTACHMENT_RATE_LIMIT,
    window_seconds=ATTACHMENT_RATE_WINDOW_SECONDS,
    message="Too many attachment requests. Please slow down.",
)

Storage = Annotated[ObjectStorage, Depends(storage_dependency)]


def _validate_attachment_bytes(media_type: str, b64: Any) -> str | None:
    """Validate an image/PDF upload against the allowlist + magic bytes (+ WebP form-type),
    matching Express `validateAttachmentBytes`. Returns the error string, or None if valid."""
    if not isinstance(b64, str) or not b64:
        return "Invalid attachment: missing bytes."
    magic = ALLOWED_MEDIA.get(media_type)
    if magic is None:
        return f"Unsupported attachment type: {media_type}. Allowed: PNG, JPEG, GIF, WebP, PDF."
    # 24 base64 chars → 18 bytes: enough for any magic prefix + the WebP form-type at offset 8.
    try:
        prefix = base64.b64decode(b64[:24])
    except (binascii.Error, ValueError):  # fmt: skip  # ruff py314 strips parens
        prefix = b""
    if not magic_matches(prefix, magic):
        return f"Attachment bytes do not match the declared type {media_type}."
    if media_type == "image/webp" and prefix[8:12] != b"WEBP":
        return "Attachment bytes do not match the declared type image/webp."
    return None


def _sniff_media_type(data: bytes) -> str | None:
    """Reverse-lookup the media type from the magic bytes (only allowlisted/validated bytes
    are ever stored), matching Express `sniffMediaType`. None → application/octet-stream."""
    for media, magic in ALLOWED_MEDIA.items():
        if magic_matches(data, magic):
            if media == "image/webp" and data[8:12] != b"WEBP":
                continue
            return media
    return None


async def _store_attachment_bytes(
    db: DbSession,
    storage: ObjectStorage,
    user_id: uuid.UUID,
    attachment_id: str,
    media_type: str,
    name: str,
    data: bytes,
) -> dict[str, Any]:
    """Enforce the per-user quota and store the bytes owner-scoped; return the Express file-part
    ref. Idempotent on a repeated id (reuses the row + key). Raises `AppApiError(413)` on an
    over-quota write (was a sentinel return). NOTE: the quota check-then-store has a
    concurrent-overspend window (as in the daily gate) — hardening deferred."""
    size = len(data)
    used_raw = await db.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(Attachment.size), 0)).where(
            Attachment.user_id == user_id
        )
    )
    used = int(used_raw or 0)
    existing = await db.scalar(
        sa.select(Attachment).where(
            Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
        )
    )
    old_size = existing.size if existing is not None else 0
    if used - old_size + size > ATTACHMENT_TOTAL_CAP:
        raise AppApiError(
            413,
            "Attachment storage is full. Remove some attachments and try again.",
            code="ATTACHMENT_STORE_FULL",
        )

    if existing is not None:
        key = existing.storage_key
    else:
        key = attachment_key(user_id, uuid.uuid7())
    # Store the bytes first; only then persist the row (a failed put leaves no dangling row).
    await storage.put(key, data, content_type=media_type)
    if existing is not None:
        existing.media_type, existing.name, existing.size = media_type, name, size
    else:
        db.add(
            Attachment(
                user_id=user_id,
                attachment_id=attachment_id,
                media_type=media_type,
                name=name,
                size=size,
                storage_key=key,
            )
        )
    await db.commit()
    return {
        "attachmentId": attachment_id,
        "key": key,
        "mediaType": media_type,
        "size": size,
        "name": name,
    }


def _decode_bounded(b64: Any) -> bytes:
    """Decode a base64 body and enforce the 4 MB decoded cap (shared by office/deck).
    Raises `AppApiError` (was a sentinel return)."""
    if not isinstance(b64, str) or not b64:
        raise AppApiError(400, "Invalid attachment: missing bytes.")
    try:
        data = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(400, "Invalid attachment: missing bytes.") from None
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise AppApiError(413, "Attachment is too large (max 4 MB).")
    return data


async def _handle_office_upload(
    db: DbSession,
    storage: ObjectStorage,
    user: User,
    attachment_id: str,
    media_type: str,
    body: dict[str, Any],
) -> JSONResponse:
    """docx/xlsx: extract to Markdown BEFORE storing (a corrupt file is rejected without orphaning
    an object), then store the original bytes and return the `kind:'office'` part.

    The extraction runs in the shared killable parse governor (`run_parse`) — NOT in-process —
    so an untrusted docx/xlsx whose compressed bytes pass the 4 MB cap but inflate to gigabytes
    can never OOM the shared API worker (A.U11 review invariant); a contained OOM/timeout maps
    to 413, a corrupt file to 400."""
    data = _decode_bounded(body.get("base64"))
    raw_name = body.get("name")
    name = raw_name if isinstance(raw_name, str) else ""
    office_format = office_format_for(media_type)
    if office_format is None:
        raise AppApiError(400, f"Unsupported Office type: {media_type}")
    kind = "extract_word" if office_format == "word" else "extract_excel"
    try:
        extracted = await run_parse(data, kind, name, None)
    except FileParseError as exc:
        raise AppApiError(exc.status, str(exc), code=exc.code) from exc
    ref = await _store_attachment_bytes(
        db, storage, user.id, attachment_id, media_type, name, data
    )
    return JSONResponse(
        status_code=201,
        content={
            "attachment": {
                **ref,
                "kind": "office",
                "format": extracted["format"],
                "text": extracted["text"],
                "truncated": extracted["truncated"],
                "truncationNote": extracted["truncationNote"],
            }
        },
    )


async def _handle_deck_upload(
    db: DbSession,
    storage: ObjectStorage,
    user: User,
    attachment_id: str,
    media_type: str,
    body: dict[str, Any],
) -> JSONResponse:
    """pptx: gated on a configured Gotenberg. Convert FIRST (validates structure/zip-bomb/page-cap
    without storing), then store the original .pptx and the derived PDF. Under Azure-hosted Foundry
    there is no Anthropic Files API, so the PDF lives in the object store and the chat path
    rehydrates + inlines it — the exact deck-part replay is finalized with the Foundry hosting-mode
    decision (ADR-0026); deck is disabled by default (unset GOTENBERG_URL) until then."""
    if not deck_attachments_enabled():
        raise AppApiError(501, "PowerPoint attachments aren't enabled.")
    data = _decode_bounded(body.get("base64"))
    raw_name = body.get("name")
    name = raw_name if isinstance(raw_name, str) else ""
    try:
        converted = await convert_deck_to_pdf(data, name=name)
    except DeckConvertError as exc:
        raise AppApiError(exc.status, str(exc), code=exc.code) from exc
    ref = await _store_attachment_bytes(
        db, storage, user.id, attachment_id, media_type, name, data
    )
    pdf_key = f"{ref['key']}.pdf"
    await storage.put(pdf_key, converted.pdf, content_type="application/pdf")
    return JSONResponse(
        status_code=201,
        content={
            "attachment": {
                **ref,
                "kind": "deck",
                "pdfFileId": pdf_key,
                "pageCount": converted.page_count,
                "truncated": False,
            }
        },
    )


@router.post(
    "",
    status_code=201,
    response_model=UploadResponse,
    dependencies=[Depends(_attachment_limiter)],
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid attachment id, type, or bytes"),
        (413, ErrorEnvelope, "Attachment too large or per-user storage full"),
        (501, ErrorEnvelope, "PowerPoint attachments are not enabled"),
        (429, ErrorEnvelope, "Too many attachment requests"),
        (401, DetailBody, "Not authenticated"),
    ),
)
async def upload_attachment(
    request: Request, user: CurrentUser, db: DbSession, storage: Storage
) -> JSONResponse:
    # Body-size ceiling (Express mount `limit:'6mb'`) — refuse a huge body before buffering it.
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > _BODY_LIMIT_BYTES
    ):
        raise AppApiError(413, "Attachment request is too large.")

    try:
        body: Any = await request.json()
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        body = {}
    if not isinstance(body, dict):
        body = {}

    attachment_id = body.get("attachmentId")
    if not isinstance(attachment_id, str) or not _ID_RE.match(attachment_id):
        raise AppApiError(400, "Invalid attachment id.")
    media_type = body.get("mediaType")
    if not isinstance(media_type, str):
        raise AppApiError(400, "mediaType is required.")
    if media_type.startswith("text/"):
        raise AppApiError(400, "Text attachments are sent inline, not uploaded.")
    if media_type in OFFICE_MEDIA_TYPES:
        return await _handle_office_upload(db, storage, user, attachment_id, media_type, body)
    if media_type == PPTX_MEDIA_TYPE:
        return await _handle_deck_upload(db, storage, user, attachment_id, media_type, body)

    b64 = body.get("base64")
    err = _validate_attachment_bytes(media_type, b64)
    if err is not None:
        raise AppApiError(400, err)
    # Validation guarantees a non-empty str; this redundant narrow satisfies the type checker.
    if not isinstance(b64, str):
        raise AppApiError(400, "Invalid attachment: missing bytes.")
    try:
        data = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(
            400, f"Attachment bytes do not match the declared type {media_type}."
        ) from None
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise AppApiError(413, "Attachment is too large (max 4 MB).")

    raw_name = body.get("name")
    name = raw_name if isinstance(raw_name, str) else ""
    ref = await _store_attachment_bytes(
        db, storage, user.id, attachment_id, media_type, name, data
    )
    kind = "document" if media_type == "application/pdf" else "image"
    return JSONResponse(status_code=201, content={"attachment": {**ref, "kind": kind}})


async def _safe_delete_pdf(storage: ObjectStorage, pdf_key: str) -> None:
    """Best-effort delete of a deck's derived `{key}.pdf` sibling — a genuine store error is
    swallowed (never fails the parent delete); a missing object is already idempotent."""
    try:
        await storage.delete(pdf_key)
    except StorageError:
        pass


async def _load_owned(db: DbSession, user_id: uuid.UUID, attachment_id: str) -> Attachment | None:
    result: Attachment | None = await db.scalar(
        sa.select(Attachment).where(
            Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
        )
    )
    return result


@router.get(
    "/{attachment_id}",
    # Returns raw bytes (`Response`) — no response_model. Errors still documented.
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid attachment id"),
        (404, ErrorEnvelope, "Attachment not found"),
        (401, DetailBody, "Not authenticated"),
    ),
)
async def download_attachment(
    attachment_id: str, user: CurrentUser, db: DbSession, storage: Storage
) -> Response:
    if not _ID_RE.match(attachment_id):
        raise AppApiError(400, "Invalid attachment id.")
    att = await _load_owned(db, user.id, attachment_id)
    if att is None:
        raise AppApiError(404, "Attachment not found.")
    # Defense in depth: the query already scoped by user_id; re-assert the key is in scope.
    assert_owned(att.storage_key, user.id)
    try:
        data = await storage.get(att.storage_key)
    except StorageNotFoundError:
        raise AppApiError(404, "Attachment not found.") from None
    # Content-Type is SNIFFED from the bytes (not the stored media_type), matching Express.
    media = _sniff_media_type(data) or "application/octet-stream"
    return Response(
        content=data, media_type=media, headers={"Cache-Control": "private, max-age=3600"}
    )


@router.delete(
    "/{attachment_id}",
    response_model=OkResponse,
    dependencies=[Depends(_attachment_limiter)],
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid attachment id"),
        (429, ErrorEnvelope, "Too many attachment requests"),
        (401, DetailBody, "Not authenticated"),
    ),
)
async def delete_attachment(
    attachment_id: str, user: CurrentUser, db: DbSession, storage: Storage
) -> JSONResponse:
    if not _ID_RE.match(attachment_id):
        raise AppApiError(400, "Invalid attachment id.")
    att = await _load_owned(db, user.id, attachment_id)
    if att is not None:
        assert_owned(att.storage_key, user.id)
        await storage.delete(att.storage_key)  # idempotent on a missing object
        # A deck attachment also wrote a derived `{key}.pdf` sibling — sweep it best-effort
        # so it doesn't leak (idempotent on a missing object; never fails the delete).
        if att.media_type == PPTX_MEDIA_TYPE:
            await _safe_delete_pdf(storage, att.storage_key + ".pdf")
        await db.delete(att)
        await db.commit()
    # A deck's internal Files-API pdfFileId release lands with U11/U13 (Files API). Delete is
    # always idempotent and 200, even when the id is unknown (Express behavior).
    return JSONResponse(content={"ok": True})
