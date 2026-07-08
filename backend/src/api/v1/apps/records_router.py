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
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, status

from src.api.deps import DbSession
from src.api.v1.apps.records_schemas import (
    CreateRequest,
    DistinctResponse,
    ListResponse,
    OkResponse,
    PatchRequest,
    RecordEnvelope,
    RecordOut,
    SearchResponse,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import (
    APP_DATA_BYTES_CAP,
    APP_RECORD_COUNT_CAP,
    AppRegistry,
    AppStatus,
)
from src.db.models.data_record import DataRecord
from src.schemas import ErrorEnvelope, error_responses
from src.services.appkey.chain import (
    InjectedUser,
    RequireAppKey,
    make_per_app_limiter,
    require_login_if_required,
)
from src.services.appserving.quota import adjust_app_quota, reserve_app_quota
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


# All records routes sit behind the X-App-Key chain: `require_app_key` (401 missing/
# unknown key, 403 inactive app, 404 URL/key mismatch), `require_login_if_required`
# (401 when the app's live loginRequired gate is on), and the per-app limiter (429) —
# every failure is `AppApiError` -> `ErrorEnvelope`. This shared tuple is spread into
# each route's `responses=` alongside that route's own explicit 400/413.
_DATA_PLANE = (
    (401, ErrorEnvelope, "Missing or invalid app key, or failed login"),
    (403, ErrorEnvelope, "This app is not available"),
    (404, ErrorEnvelope, "App or record not found"),
    (429, ErrorEnvelope, "Too many requests for this app"),
)


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
    """Atomically reserve data quota for an INCREASE, or raise 413 (shared helper)."""
    await reserve_app_quota(
        db,
        app_id,
        count_col=AppRegistry.data_count,
        bytes_col=AppRegistry.data_bytes,
        count_cap=APP_RECORD_COUNT_CAP,
        bytes_cap=APP_DATA_BYTES_CAP,
        d_count=d_count,
        d_bytes=d_bytes,
        error_code="RECORD_QUOTA_EXCEEDED",
        error_message="This app has reached its data storage limit.",
    )


async def _adjust_data(db: DbSession, app_id: uuid.UUID, *, d_count: int, d_bytes: int) -> None:
    """Unconditional counter adjust for a RELEASE / decrease (shared helper)."""
    await adjust_app_quota(
        db,
        app_id,
        count_col=AppRegistry.data_count,
        bytes_col=AppRegistry.data_bytes,
        d_count=d_count,
        d_bytes=d_bytes,
    )


# --- endpoints -----------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid collection or record data"),
        (413, ErrorEnvelope, "Record too large or app data quota exceeded"),
        *_DATA_PLANE,
    ),
)
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


@router.get(
    "",
    responses=error_responses((400, ErrorEnvelope, "Invalid collection"), *_DATA_PLANE),
)
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


@router.get(
    "/search",
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid query, filter, sort, or collection"), *_DATA_PLANE
    ),
)
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


@router.get(
    "/distinct",
    responses=error_responses((400, ErrorEnvelope, "Invalid field or collection"), *_DATA_PLANE),
)
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
    db: DbSession, app_id: uuid.UUID, record_id: uuid.UUID, *, for_update: bool = False
) -> DataRecord:
    stmt = sa.select(DataRecord).where(DataRecord.id == record_id, DataRecord.app_id == app_id)
    if for_update:
        # Lock the row for the read-modify-write so concurrent PATCHes serialize and the
        # byte delta is always computed against the committed base (no lost update).
        stmt = stmt.with_for_update()
    record = (await db.execute(stmt)).scalar_one_or_none()
    if record is None:
        raise AppApiError(404, "Record not found.")
    return record


@router.get("/{record_id}", responses=error_responses(*_DATA_PLANE))
async def get_record(record_id: uuid.UUID, ctx: RequireAppKey, db: DbSession) -> RecordEnvelope:
    record = await _owned_record_or_404(db, ctx.app_id, record_id)
    return RecordEnvelope(record=_project(record))


@router.patch(
    "/{record_id}",
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid record data"),
        (413, ErrorEnvelope, "Record too large or app data quota exceeded"),
        *_DATA_PLANE,
    ),
)
async def patch_record(
    record_id: uuid.UUID,
    body: PatchRequest,
    ctx: RequireAppKey,
    user: InjectedUser,
    db: DbSession,
) -> RecordEnvelope:
    clean = _sanitize_data(body.data)
    record = await _owned_record_or_404(db, ctx.app_id, record_id, for_update=True)
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
    return RecordEnvelope(record=_project(record))


@router.delete("/{record_id}", responses=error_responses(*_DATA_PLANE))
async def delete_record(
    record_id: uuid.UUID, ctx: RequireAppKey, user: InjectedUser, db: DbSession
) -> OkResponse:
    # DELETE ... RETURNING gates the counter release on a row this txn actually removed:
    # a concurrent double-DELETE matches zero rows the second time → 404, no double
    # decrement of the quota counters.
    deleted = (
        await db.execute(
            sa.delete(DataRecord)
            .where(DataRecord.id == record_id, DataRecord.app_id == ctx.app_id)
            .returning(DataRecord.bytes, DataRecord.collection)
        )
    ).first()
    if deleted is None:
        raise AppApiError(404, "Record not found.")
    await _adjust_data(db, ctx.app_id, d_count=-1, d_bytes=-deleted.bytes)
    await append_audit(
        db,
        actor_id=user.id if user else None,
        action="delete",
        resource_type="record",
        resource_id=str(record_id),
        detail={"appId": str(ctx.app_id), "collection": deleted.collection},
    )
    await db.commit()
    return OkResponse(ok=True)
