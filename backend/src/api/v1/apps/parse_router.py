"""Per-app parse endpoint (R26) — stored fileId OR inline base64 → structured rows /
document text, under the X-App-Key chain (U4). Ported from Express `app-parse.js`.

Bound (1), the decoded-size cap, is enforced HERE at the route (inline mode; stored
files were already capped at upload); bounds (2)-(4) live in the parse governor.
Errors carry the ported status + code (413 `FILE_TOO_LARGE`/`PARSE_TIMEOUT`, 415
`UNSUPPORTED_TYPE`, 400 `FILE_PARSE_ERROR`, …).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends

from src.api.deps import DbSession
from src.api.v1.apps.files_router import APP_FILE_MAX_BYTES, Storage
from src.api.v1.apps.parse_schemas import ParseRequest, ParseResponse
from src.core.errors import AppApiError
from src.db.models.app_file import AppFile, AppFileStatus
from src.schemas import ErrorEnvelope, error_responses
from src.services.appkey.chain import (
    RequireAppKey,
    make_per_app_limiter,
    require_login_if_required,
)
from src.services.extract.zip_safety import FileParseError
from src.services.parse import parse_kind_for, run_parse

# Parse is heavier than a metadata read → a lower ceiling than files (Express 60/min).
_parse_limiter = make_per_app_limiter(
    limit=60, window_seconds=60, message="Too many parse requests for this app. Please slow down."
)

router = APIRouter(
    prefix="/apps/{app_id}/parse",
    tags=["parse"],
    dependencies=[Depends(require_login_if_required), Depends(_parse_limiter)],
)

# Parse sits behind the same X-App-Key chain (401/403/404 + login-gate 401 + the
# per-app limiter 429) — every failure is `AppApiError` -> `ErrorEnvelope`.
_DATA_PLANE = (
    (401, ErrorEnvelope, "Missing or invalid app key, or failed login"),
    (403, ErrorEnvelope, "This app is not available"),
    (404, ErrorEnvelope, "App or file not found"),
    (429, ErrorEnvelope, "Too many parse requests for this app"),
)


def _kind_or_415(content_type: str, filename: str) -> str:
    kind = parse_kind_for(content_type, filename)
    if kind is None:
        raise AppApiError(
            415, "Supported: Excel (.xlsx/.xls), CSV, Word (.docx).", code="UNSUPPORTED_TYPE"
        )
    return kind


async def _from_stored(
    file_id: uuid.UUID, ctx: RequireAppKey, db: DbSession, storage: Storage
) -> tuple[bytes, str, str]:
    file = (
        await db.execute(
            sa.select(AppFile).where(
                AppFile.id == file_id,
                AppFile.app_id == ctx.app_id,
                AppFile.status == AppFileStatus.READY,
            )
        )
    ).scalar_one_or_none()
    if file is None:
        raise AppApiError(404, "File not found.")
    from src.services.storage import StorageNotFoundError

    try:
        data = await storage.get(file.blob_key)
    except StorageNotFoundError as exc:
        raise AppApiError(404, "File not found.") from exc
    return data, file.content_type, file.filename


def _from_inline(body: ParseRequest) -> tuple[bytes, str, str]:
    if not body.content_type:
        raise AppApiError(400, "contentType is required.")
    if not body.base64:
        raise AppApiError(400, "No file bytes to parse.")
    try:
        data = base64.b64decode(body.base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppApiError(400, "File payload is not valid base64.") from exc
    if not data:
        raise AppApiError(400, "No file bytes to parse.")
    # Bound (1): decoded-size cap at the route (inline only; stored was capped at upload).
    if len(data) > APP_FILE_MAX_BYTES:
        raise AppApiError(413, "File exceeds the size limit.", code="FILE_TOO_LARGE")
    return data, body.content_type, body.filename or ""


@router.post(
    "",
    # Enforced discriminated-union response_model (the route returns a plain dict; FastAPI
    # validates + serializes it per `kind`). Emit order preserved -> byte-identical output.
    response_model=ParseResponse,
    responses=error_responses(
        (400, ErrorEnvelope, "Missing or invalid parse input"),
        (413, ErrorEnvelope, "File too large or parse timed out"),
        (415, ErrorEnvelope, "Unsupported file type"),
        *_DATA_PLANE,
    ),
)
async def parse_file(
    body: ParseRequest, ctx: RequireAppKey, db: DbSession, storage: Storage
) -> dict[str, Any]:
    if body.file_id is not None:
        data, content_type, filename = await _from_stored(body.file_id, ctx, db, storage)
    else:
        data, content_type, filename = _from_inline(body)

    kind = _kind_or_415(content_type, filename)
    try:
        return await run_parse(data, kind, filename, body.sheet)
    except FileParseError as exc:
        raise AppApiError(exc.status, str(exc), code=exc.code) from exc
