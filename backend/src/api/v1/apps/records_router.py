"""Per-app records API (R22, R24) — create/list/search/distinct/get/patch/delete
under the X-App-Key chain (U4). App-scoped on the VERIFIED `ctx.app_id` (never the
body): every query carries `WHERE app_id = :app_id`; a dropped predicate is a
cross-app leak (ADR-0004).

Ported from Express `app-data.js` + `data-records-repo.js`: reserved-key stripping,
`$`/`.` key rejection at any depth, depth cap, the derived `search_text` projection,
the 256 KB per-record body cap, and the atomic conditional quota reserve. Writes are
audited (create/update/delete); reads are not. Response envelopes match Express
exactly, and `project()` never leaks `app_id`, `bytes`, or `search_text`.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from src.api.deps import DbSession
from src.core.errors import AppApiError
from src.db.models.app_registry import (
    APP_DATA_BYTES_CAP,
    APP_RECORD_COUNT_CAP,
    AppRegistry,
    AppStatus,
)
from src.db.models.data_record import DataRecord
from src.services.appkey.chain import (
    InjectedUser,
    RequireAppKey,
    make_per_app_limiter,
    require_login_if_required,
)
from src.services.audit.log import append_audit

# --- ported bounds -------------------------------------------------------------

MAX_DATA_DEPTH = 8
MAX_SEARCH_BLOB = 8192
MAX_SEARCH_Q = 200
MAX_RECORD_BODY_BYTES = 256 * 1024  # the ported express.json({limit:'256kb'}) cap
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FIELD_RE = re.compile(r"^[^$.][^.]*$")  # no leading '$', no '.' anywhere
# Server-owned columns the client `data` may never set (silently stripped).
_RESERVED_KEYS = frozenset(
    {
        "_id",
        "appId",
        "collection",
        "createdBy",
        "createdInDraft",
        "bytes",
        "_search",
        "createdAt",
        "updatedAt",
    }
)

_records_limiter = make_per_app_limiter(
    limit=120, window_seconds=60, message="Too many requests for this app. Please slow down."
)

# The whole records surface sits behind the live loginRequired gate + the per-app
# limiter (require_app_key runs first as their shared dependency).
router = APIRouter(
    prefix="/apps/{app_id}/records",
    tags=["records"],
    dependencies=[Depends(require_login_if_required), Depends(_records_limiter)],
)


# --- schemas -------------------------------------------------------------------


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CreateRequest(_CamelModel):
    collection: str | None = None
    data: Any = None


class PatchRequest(_CamelModel):
    data: Any = None


class RecordOut(_CamelModel):
    """The client-facing projection — NEVER app_id/bytes/search_text."""

    id: uuid.UUID
    collection: str
    data: dict[str, Any]
    created_by: uuid.UUID | None
    created_in_draft: bool
    created_at: datetime
    updated_at: datetime


class ListResponse(_CamelModel):
    records: list[RecordOut]


class SearchResponse(_CamelModel):
    items: list[RecordOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class DistinctResponse(_CamelModel):
    values: list[Any]


class OkResponse(_CamelModel):
    ok: bool


# --- validation + derivation helpers ------------------------------------------


def _project(rec: DataRecord) -> RecordOut:
    return RecordOut(
        id=rec.id,
        collection=rec.collection,
        data=rec.data,
        created_by=rec.created_by,
        created_in_draft=rec.created_in_draft,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


def _sanitize_collection(collection: str | None) -> str:
    if collection is None:
        return "default"
    if not isinstance(collection, str) or not _COLLECTION_RE.match(collection):
        raise AppApiError(400, "collection must match ^[A-Za-z0-9_-]{1,64}$")
    return collection


def _check_keys_and_depth(value: Any, depth: int) -> None:
    if depth > MAX_DATA_DEPTH:
        raise AppApiError(400, "record is nested too deeply")
    if isinstance(value, dict):
        for key, sub in value.items():
            if not isinstance(key, str) or key.startswith("$") or "." in key:
                raise AppApiError(400, f"invalid field name: {key}")
            _check_keys_and_depth(sub, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_keys_and_depth(item, depth + 1)


def _sanitize_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AppApiError(400, "data must be a JSON object.")
    _check_keys_and_depth(data, 1)
    # Reserved keys are server-owned → silently ignored if sent.
    return {k: v for k, v in data.items() if k not in _RESERVED_KEYS}


def _record_bytes(data: dict[str, Any]) -> int:
    return len(json.dumps(data, separators=(",", ":")).encode("utf-8"))


def _build_search_text(data: Any) -> str:
    """Ported `buildSearchBlob`: every scalar leaf, lowercased, space-joined, capped
    (depth-limited). Matched by substring ILIKE in search."""
    parts: list[str] = []

    def walk(value: Any, depth: int) -> None:
        if depth > MAX_DATA_DEPTH or value is None:
            return
        if isinstance(value, bool):
            parts.append(str(value).lower())
        elif isinstance(value, (str, int, float)):
            parts.append(str(value).lower())
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, dict):
            for sub in value.values():
                walk(sub, depth + 1)

    walk(data, 1)
    return " ".join(parts)[:MAX_SEARCH_BLOB]


def _sanitize_field_name(field: str) -> str:
    if not field or not _FIELD_RE.match(field) or field in _RESERVED_KEYS:
        raise AppApiError(400, "invalid field name.")
    return field


# --- quota (atomic conditional reserve; transaction rollback compensates) ------


async def _reserve_data(db: DbSession, app_id: uuid.UUID, *, d_count: int, d_bytes: int) -> None:
    """Atomically reserve quota for an INCREASE, or raise 413. The UPDATE only
    matches when both counters have room, so there is no read-then-write race."""
    reserved = (
        await db.execute(
            sa.update(AppRegistry)
            .where(
                AppRegistry.id == app_id,
                AppRegistry.data_count <= APP_RECORD_COUNT_CAP - d_count,
                AppRegistry.data_bytes <= APP_DATA_BYTES_CAP - d_bytes,
            )
            .values(
                data_count=AppRegistry.data_count + d_count,
                data_bytes=AppRegistry.data_bytes + d_bytes,
            )
            .returning(AppRegistry.id)
        )
    ).first()
    if reserved is None:
        raise AppApiError(
            413, "This app has reached its data storage limit.", code="RECORD_QUOTA_EXCEEDED"
        )


async def _adjust_data(db: DbSession, app_id: uuid.UUID, *, d_count: int, d_bytes: int) -> None:
    """Unconditional counter adjust for a RELEASE / decrease (always fits)."""
    await db.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == app_id)
        .values(
            data_count=AppRegistry.data_count + d_count,
            data_bytes=AppRegistry.data_bytes + d_bytes,
        )
    )


# --- endpoints -----------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_record(
    body: CreateRequest, ctx: RequireAppKey, user: InjectedUser, db: DbSession
) -> RecordOut:
    collection = _sanitize_collection(body.collection)
    data = _sanitize_data(body.data)
    record_bytes = _record_bytes(data)
    if record_bytes > MAX_RECORD_BODY_BYTES:
        raise AppApiError(413, "Record exceeds the size limit.")
    # Reserve BEFORE the insert; a failed insert rolls back the reserve with the txn.
    await _reserve_data(db, ctx.app_id, d_count=1, d_bytes=record_bytes)
    record = DataRecord(
        app_id=ctx.app_id,
        collection=collection,
        data=data,
        created_by=user.id if user else None,
        created_in_draft=ctx.status is not AppStatus.APPROVED,
        bytes=record_bytes,
        search_text=_build_search_text(data),
    )
    db.add(record)
    await db.flush()
    await db.refresh(record)
    await append_audit(
        db,
        actor_id=user.id if user else None,
        action="create",
        resource_type="record",
        resource_id=str(record.id),
        detail={"appId": str(ctx.app_id), "collection": collection},
    )
    await db.commit()
    return _project(record)


@router.get("")
async def list_records(
    ctx: RequireAppKey,
    db: DbSession,
    collection: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = DEFAULT_LIST_LIMIT,
) -> ListResponse:
    capped = min(max(1, limit), MAX_LIST_LIMIT)
    where = [DataRecord.app_id == ctx.app_id]
    if collection is not None:
        where.append(DataRecord.collection == _sanitize_collection(collection))
    rows = (
        await db.execute(
            sa.select(DataRecord)
            .where(*where)
            .order_by(DataRecord.created_at.desc())
            .limit(capped)
        )
    ).scalars()
    return ListResponse(records=[_project(r) for r in rows])


def _build_data_filter(raw: str | None) -> list[Any]:
    """Parse the `filter` JSON into `data @> {...}` scalar-equality predicates."""
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AppApiError(400, "filter must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise AppApiError(400, "filter must be a JSON object.")
    predicates: list[Any] = []
    for key, value in parsed.items():
        _sanitize_field_name(key)
        if isinstance(value, (dict, list)):
            raise AppApiError(400, "filter values must be scalars.")
        predicates.append(DataRecord.data.contains({key: value}))
    return predicates


def _sort_column(sort: str | None) -> Any:
    if sort is None or sort == "createdAt":
        return DataRecord.created_at
    if sort == "updatedAt":
        return DataRecord.updated_at
    return DataRecord.data[_sanitize_field_name(sort)].astext


@router.get("/search")
async def search_records(
    ctx: RequireAppKey,
    db: DbSession,
    collection: Annotated[str | None, Query()] = None,
    q: Annotated[str, Query()] = "",
    filter: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
    order: Annotated[str, Query()] = "desc",
    page: Annotated[int, Query()] = 1,
    page_size: Annotated[int, Query(alias="pageSize")] = DEFAULT_PAGE_SIZE,
) -> SearchResponse:
    q = q.strip()
    if len(q) > MAX_SEARCH_Q:
        raise AppApiError(400, "q must be 200 characters or fewer.")
    page = max(1, page)
    capped_size = min(max(1, page_size), MAX_PAGE_SIZE)

    where = [DataRecord.app_id == ctx.app_id]
    if collection is not None:
        where.append(DataRecord.collection == _sanitize_collection(collection))
    if q:
        where.append(DataRecord.search_text.icontains(q, autoescape=True))
    where.extend(_build_data_filter(filter))

    column = _sort_column(sort)
    ordering = column.asc() if order == "asc" else column.desc()

    total = (
        await db.execute(sa.select(sa.func.count()).select_from(DataRecord).where(*where))
    ).scalar_one()
    rows = (
        await db.execute(
            sa.select(DataRecord)
            .where(*where)
            .order_by(ordering)
            .offset((page - 1) * capped_size)
            .limit(capped_size)
        )
    ).scalars()
    total_pages = (total + capped_size - 1) // capped_size if capped_size else 0
    return SearchResponse(
        items=[_project(r) for r in rows],
        total=total,
        page=page,
        page_size=capped_size,
        total_pages=total_pages,
    )


@router.get("/distinct")
async def distinct_values(
    ctx: RequireAppKey,
    db: DbSession,
    field: Annotated[str, Query()],
    collection: Annotated[str | None, Query()] = None,
) -> DistinctResponse:
    field = _sanitize_field_name(field)
    where = [DataRecord.app_id == ctx.app_id]
    if collection is not None:
        where.append(DataRecord.collection == _sanitize_collection(collection))
    expr = DataRecord.data[field]
    rows = (
        await db.execute(sa.select(sa.distinct(expr)).where(*where, expr.isnot(None)))
    ).scalars()
    return DistinctResponse(values=[v for v in rows if v is not None])


async def _owned_record_or_404(
    db: DbSession, app_id: uuid.UUID, record_id: uuid.UUID
) -> DataRecord:
    record = (
        await db.execute(
            sa.select(DataRecord).where(DataRecord.id == record_id, DataRecord.app_id == app_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise AppApiError(404, "Record not found.")
    return record


@router.get("/{record_id}")
async def get_record(record_id: uuid.UUID, ctx: RequireAppKey, db: DbSession) -> dict[str, Any]:
    record = await _owned_record_or_404(db, ctx.app_id, record_id)
    return {"record": _project(record).model_dump(by_alias=True)}


@router.patch("/{record_id}")
async def patch_record(
    record_id: uuid.UUID,
    body: PatchRequest,
    ctx: RequireAppKey,
    user: InjectedUser,
    db: DbSession,
) -> dict[str, Any]:
    clean = _sanitize_data(body.data)
    record = await _owned_record_or_404(db, ctx.app_id, record_id)
    merged = {**record.data, **clean}  # shallow merge, last-write-wins
    new_bytes = _record_bytes(merged)
    if new_bytes > MAX_RECORD_BODY_BYTES:
        raise AppApiError(413, "Record exceeds the size limit.")
    delta = new_bytes - record.bytes
    if delta > 0:
        await _reserve_data(db, ctx.app_id, d_count=0, d_bytes=delta)
    elif delta < 0:
        await _adjust_data(db, ctx.app_id, d_count=0, d_bytes=delta)
    record.data = merged
    record.bytes = new_bytes
    record.search_text = _build_search_text(merged)
    await db.flush()
    await db.refresh(record)
    await append_audit(
        db,
        actor_id=user.id if user else None,
        action="update",
        resource_type="record",
        resource_id=str(record.id),
        detail={"appId": str(ctx.app_id), "collection": record.collection},
    )
    await db.commit()
    return {"record": _project(record).model_dump(by_alias=True)}


@router.delete("/{record_id}")
async def delete_record(
    record_id: uuid.UUID, ctx: RequireAppKey, user: InjectedUser, db: DbSession
) -> OkResponse:
    record = await _owned_record_or_404(db, ctx.app_id, record_id)
    await db.delete(record)
    await _adjust_data(db, ctx.app_id, d_count=-1, d_bytes=-record.bytes)
    await append_audit(
        db,
        actor_id=user.id if user else None,
        action="delete",
        resource_type="record",
        resource_id=str(record_id),
        detail={"appId": str(ctx.app_id), "collection": record.collection},
    )
    await db.commit()
    return OkResponse(ok=True)
