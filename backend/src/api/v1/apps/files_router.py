"""Per-app file storage (R25) — upload/list/metadata/delete + both download paths,
under the X-App-Key chain (U4). App-scoped on the VERIFIED `ctx.app_id`; the blob key
is `apps/{app_id}/{id}` (server-minted). Ported from Express `app-files.js` +
`app-files-repo.js`.

Two-store integrity: reserve quota → insert `pending` → put blob → mark `ready`, with
a compensating blob delete + transactional rollback on any failure (no orphan blob, no
stuck reserve). Uploads pass an allowlist + magic-byte + size + per-app quota gateway.
Downloads take the signed-URL path when the store can sign (bytes bypass the app),
else a hardened same-origin byte proxy (`nosniff` + `default-src 'none'; sandbox`).

Ownership here is APP-scoped via the verified app context — NOT `assert_owned`, which
is hardcoded to the `att/{user_id}/` attachment prefix and would always raise on an
`apps/{app_id}/` key; a local app-prefix guard is used instead.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Response, status

from src.api.deps import DbSession
from src.api.v1.apps.files_schemas import (
    DownloadUrlResponse,
    FileEnvelope,
    FileOut,
    ListResponse,
    OkResponse,
    UploadRequest,
)
from src.core.errors import AppApiError
from src.db.models.app_file import MAX_FILENAME_LEN, AppFile, AppFileStatus
from src.db.models.app_registry import (
    APP_FILE_BYTES_CAP,
    APP_FILE_COUNT_CAP,
    AppRegistry,
    AppStatus,
)
from src.schemas import ErrorEnvelope, error_responses
from src.services.appkey.chain import (
    InjectedUser,
    RequireAppKey,
    make_per_app_limiter,
    require_login_if_required,
)
from src.services.appserving.quota import adjust_app_quota, reserve_app_quota
from src.services.audit.log import append_audit
from src.services.storage import (
    ObjectStorage,
    StorageError,
    StorageNotFoundError,
    StorageSignError,
    UnsupportedCapabilityError,
    app_file_key,
    get_storage,
)

_log = structlog.get_logger()

# --- ported bounds -------------------------------------------------------------

APP_FILE_MAX_BYTES = 18 * 1024 * 1024  # 18 MiB decoded
FILE_SAS_TTL_SECONDS = 120
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500

_FILENAME_RE = re.compile(rf"^[A-Za-z0-9._-]{{1,{MAX_FILENAME_LEN}}}$")
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Content-type allowlist (Express `DEFAULT_ALLOWED_TYPES`). SVG is deliberately
# excluded (script-execution vector).
_ALLOWED_TYPES = frozenset(
    {
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/json",
        "text/plain",
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)
# Magic-byte prefixes (Express `MAGIC`). Types absent here (xls/csv/txt/json) are
# declared-type-only. webp additionally requires "WEBP" at offset 8.
_MAGIC: dict[str, bytes] = {
    "image/png": b"\x89PNG",
    "image/jpeg": b"\xff\xd8",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
    "application/pdf": b"%PDF",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": b"PK\x03\x04",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": b"PK\x03\x04",
}
# The hardened byte-proxy CSP (Express verbatim) — the real content-type-independent
# XSS mitigation for downloaded bytes.
_PROXY_CSP = "default-src 'none'; sandbox"

_files_limiter = make_per_app_limiter(
    limit=120, window_seconds=60, message="Too many requests for this app. Please slow down."
)

router = APIRouter(
    prefix="/apps/{app_id}/files",
    tags=["files"],
    dependencies=[Depends(require_login_if_required), Depends(_files_limiter)],
)


def storage_dependency() -> ObjectStorage:
    """The configured object store as a dependency so tests swap an in-memory fake
    (mirrors the attachments router)."""
    return get_storage()


Storage = Annotated[ObjectStorage, Depends(storage_dependency)]


# All files routes sit behind the X-App-Key chain: `require_app_key` (401 missing/
# unknown key, 403 inactive app, 404 URL/key mismatch), `require_login_if_required`
# (401), and the per-app limiter (429) — every failure is `AppApiError` ->
# `ErrorEnvelope`. This shared tuple is spread into each route's `responses=`.
_DATA_PLANE = (
    (401, ErrorEnvelope, "Missing or invalid app key, or failed login"),
    (403, ErrorEnvelope, "This app is not available"),
    (404, ErrorEnvelope, "App or file not found"),
    (429, ErrorEnvelope, "Too many requests for this app"),
)


# --- validation / sniffing helpers --------------------------------------------


def _project(file: AppFile) -> FileOut:
    return FileOut(
        file_id=file.id,
        collection=file.collection,
        filename=file.filename,
        content_type=file.content_type,
        size=file.size,
        created_by=file.created_by,
        created_in_draft=file.created_in_draft,
        created_at=file.created_at,
        updated_at=file.updated_at,
    )


def _sanitize_collection(collection: str | None) -> str:
    if collection is None:
        return "default"
    if not _COLLECTION_RE.match(collection):
        raise AppApiError(400, "collection must match ^[A-Za-z0-9_-]{1,64}$")
    return collection


def _sanitize_filename(filename: str) -> str:
    if not _FILENAME_RE.match(filename):
        raise AppApiError(400, "filename must match ^[A-Za-z0-9._-]{1,200}$")
    return filename


def _sniff_magic(content_type: str, data: bytes) -> bool:
    """Magic-byte gate: True if the declared type has no magic OR the bytes match."""
    magic = _MAGIC.get(content_type)
    if magic is None:
        return True
    if len(data) < len(magic) or not data.startswith(magic):
        return False
    if content_type == "image/webp":
        # RIFF also matches WAV/AVI — require the WEBP form.
        return len(data) >= 12 and data[8:12] == b"WEBP"
    return True


def _sniff_image_type(data: bytes) -> str | None:
    """Reverse-sniff a stored blob's image type (for inline content-type)."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _disposition_name(file: AppFile) -> str:
    try:
        return _sanitize_filename(file.filename)
    except AppApiError:
        return f"file-{file.id}"


def _assert_app_scoped(blob_key: str, app_id: uuid.UUID) -> None:
    """App-scoped key-boundary guard (the app-file analogue of `assert_owned`): the
    stored key must live strictly under this app's `apps/{app_id}/` prefix."""
    prefix = f"apps/{app_id}/"
    if not blob_key.startswith(prefix) or len(blob_key) <= len(prefix):
        raise AppApiError(404, "File not found.")


# --- quota ---------------------------------------------------------------------


async def _reserve_files(db: DbSession, app_id: uuid.UUID, size: int) -> None:
    await reserve_app_quota(
        db,
        app_id,
        count_col=AppRegistry.file_count,
        bytes_col=AppRegistry.file_bytes,
        count_cap=APP_FILE_COUNT_CAP,
        bytes_cap=APP_FILE_BYTES_CAP,
        d_count=1,
        d_bytes=size,
        error_code="FILE_QUOTA_EXCEEDED",
        error_message="This app has reached its file storage limit.",
    )


async def _release_files(db: DbSession, app_id: uuid.UUID, size: int) -> None:
    await adjust_app_quota(
        db,
        app_id,
        count_col=AppRegistry.file_count,
        bytes_col=AppRegistry.file_bytes,
        d_count=-1,
        d_bytes=-size,
    )


async def _safe_delete_blob(storage: ObjectStorage, blob_key: str) -> None:
    try:
        await storage.delete(blob_key)
    except StorageError:
        _log.warning("app file compensating blob delete failed", blob_key=blob_key)


async def _file_or_404(db: DbSession, app_id: uuid.UUID, file_id: uuid.UUID) -> AppFile:
    """Composite metadata read (id + app_id + ready) — the invariant guarding the
    signer/proxy: a non-owned/guessed/non-ready id 404s before any bytes are touched."""
    file = (
        await db.execute(
            sa.select(AppFile).where(
                AppFile.id == file_id,
                AppFile.app_id == app_id,
                AppFile.status == AppFileStatus.READY,
            )
        )
    ).scalar_one_or_none()
    if file is None:
        raise AppApiError(404, "File not found.")
    _assert_app_scoped(file.blob_key, app_id)
    return file


# --- endpoints -----------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid filename, type, or payload"),
        (413, ErrorEnvelope, "File too large or app file quota exceeded"),
        *_DATA_PLANE,
    ),
)
async def upload_file(
    body: UploadRequest, ctx: RequireAppKey, user: InjectedUser, db: DbSession, storage: Storage
) -> FileOut:
    collection = _sanitize_collection(body.collection)
    filename = _sanitize_filename(body.filename)
    if body.content_type not in _ALLOWED_TYPES:
        raise AppApiError(400, "This file type is not allowed.")
    if not body.base64:
        raise AppApiError(400, "No file bytes to upload.")
    try:
        data = base64.b64decode(body.base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppApiError(400, "File payload is not valid base64.") from exc
    if not data:
        raise AppApiError(400, "No file bytes to upload.")
    if len(data) > APP_FILE_MAX_BYTES:
        raise AppApiError(413, "File exceeds the size limit.")
    if not _sniff_magic(body.content_type, data):
        raise AppApiError(400, "File contents do not match the declared type.")

    # Reserve quota BEFORE the blob write; the txn rollback (get_db) undoes it on any
    # pre-commit failure. blob_key needs the id, so the id is minted up front.
    await _reserve_files(db, ctx.app_id, len(data))
    file_id = uuid.uuid7()
    blob_key = app_file_key(ctx.app_id, file_id)
    file = AppFile(
        id=file_id,
        app_id=ctx.app_id,
        collection=collection,
        filename=filename,
        content_type=body.content_type,
        size=len(data),
        blob_key=blob_key,
        status=AppFileStatus.PENDING,
        created_by=user.id if user else None,
        created_in_draft=ctx.status is not AppStatus.APPROVED,
    )
    db.add(file)
    await db.flush()
    try:
        await storage.put(blob_key, data, content_type=body.content_type)
        file.status = AppFileStatus.READY
        await append_audit(
            db,
            actor_id=user.id if user else None,
            action="file:create",
            resource_type="file",
            resource_id=str(file_id),
            detail={"appId": str(ctx.app_id), "collection": collection},
        )
        await db.commit()
    except Exception:
        # Compensate the external side effect: drop the (possibly written) blob. The
        # DB reserve + pending row are rolled back by `get_db` on the propagating
        # exception (no orphan blob, no stuck reserve) — an explicit rollback here
        # would double-roll-back the request-scoped session.
        await _safe_delete_blob(storage, blob_key)
        raise
    # Load the server-defaulted timestamps for the projection (the newly-inserted row
    # was never SELECTed) — a bare attribute access would otherwise trigger a sync
    # lazy-load in the async context.
    await db.refresh(file)
    return _project(file)


@router.get(
    "",
    responses=error_responses((400, ErrorEnvelope, "Invalid collection"), *_DATA_PLANE),
)
async def list_files(
    ctx: RequireAppKey,
    db: DbSession,
    collection: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = DEFAULT_LIST_LIMIT,
) -> ListResponse:
    capped = min(max(1, limit), MAX_LIST_LIMIT)
    where = [AppFile.app_id == ctx.app_id, AppFile.status == AppFileStatus.READY]
    if collection is not None:
        where.append(AppFile.collection == _sanitize_collection(collection))
    rows = (
        await db.execute(
            sa.select(AppFile).where(*where).order_by(AppFile.created_at.desc()).limit(capped)
        )
    ).scalars()
    return ListResponse(files=[_project(f) for f in rows])


@router.get(
    "/{file_id}/url",
    responses=error_responses(
        (501, ErrorEnvelope, "Signed downloads unavailable; use the content endpoint"),
        *_DATA_PLANE,
    ),
)
async def download_url(
    file_id: uuid.UUID, ctx: RequireAppKey, user: InjectedUser, db: DbSession, storage: Storage
) -> DownloadUrlResponse:
    file = await _file_or_404(db, ctx.app_id, file_id)  # composite read BEFORE signing
    try:
        url = await storage.signed_read_url(
            file.blob_key, expires_in=timedelta(seconds=FILE_SAS_TTL_SECONDS)
        )
    except (StorageSignError, UnsupportedCapabilityError) as exc:
        # The store can't sign — the client falls back to the /content byte proxy.
        raise AppApiError(
            501,
            "Signed downloads are not available for this deployment. Use the content endpoint.",
        ) from exc
    await append_audit(
        db,
        actor_id=user.id if user else None,
        action="file:url",
        resource_type="file",
        resource_id=str(file_id),
        detail={"appId": str(ctx.app_id)},
    )
    await db.commit()
    expires_at = datetime.now(UTC) + timedelta(seconds=FILE_SAS_TTL_SECONDS)
    return DownloadUrlResponse(url=url, expires_at=expires_at)


@router.get(
    "/{file_id}/content",
    # Returns raw bytes (`Response`) — no response_model. Errors still documented.
    responses=error_responses(*_DATA_PLANE),
)
async def download_content(
    file_id: uuid.UUID, ctx: RequireAppKey, db: DbSession, storage: Storage
) -> Response:
    file = await _file_or_404(db, ctx.app_id, file_id)
    try:
        data = await storage.get(file.blob_key)
    except StorageNotFoundError as exc:
        raise AppApiError(404, "File not found.") from exc

    safe_name = _disposition_name(file)
    image_type = _sniff_image_type(data)
    if image_type is not None:
        content_type = image_type
        disposition = f'inline; filename="{safe_name}"'
    else:
        content_type = "application/octet-stream"
        disposition = f'attachment; filename="{safe_name}"'
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private",
            "Content-Security-Policy": _PROXY_CSP,
            "Content-Disposition": disposition,
        },
    )


@router.get("/{file_id}", responses=error_responses(*_DATA_PLANE))
async def file_metadata(file_id: uuid.UUID, ctx: RequireAppKey, db: DbSession) -> FileEnvelope:
    file = await _file_or_404(db, ctx.app_id, file_id)
    return FileEnvelope(file=_project(file))


@router.delete("/{file_id}", responses=error_responses(*_DATA_PLANE))
async def delete_file(
    file_id: uuid.UUID, ctx: RequireAppKey, user: InjectedUser, db: DbSession, storage: Storage
) -> OkResponse:
    file = await _file_or_404(db, ctx.app_id, file_id)
    # Order: blob → metadata → quota. A genuine store error (not a missing blob, which
    # is idempotent) propagates → 500, keeping the row + quota (retryable).
    await storage.delete(file.blob_key)
    size = file.size
    await db.delete(file)
    await _release_files(db, ctx.app_id, size)
    await append_audit(
        db,
        actor_id=user.id if user else None,
        action="file:delete",
        resource_type="file",
        resource_id=str(file_id),
        detail={"appId": str(ctx.app_id)},
    )
    await db.commit()
    return OkResponse(ok=True)
