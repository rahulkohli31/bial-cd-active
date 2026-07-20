"""Projects HTTP endpoints — user-scoped CRUD for the parent container (R1, R3, R5–R7).

A project is the home a citizen developer builds one tool inside (KD-4). Identity is always
the authenticated caller; every query is scoped by `user_id` (a dropped predicate is a
cross-user leak — a cross-user id is a 404, never a leak, ADR-0004). List is keyset-paginated
+ searchable (KD-1); delete cascades through the blob-aware, rollback-safe U6 service (KD-3).

Errors use the ported `{"error": {"message": ...}}` shape (`AppApiError`), documented with
the shared `error_responses(...)` + `AUTH_401` builders (KD-7).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm.exc import StaleDataError

from src.api.deps import CurrentUser, DbSession
from src.api.v1.attachments.router import storage_dependency
from src.api.v1.claude.router import ModelDep
from src.api.v1.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorQuery,
    LimitQuery,
    SearchQuery,
    clean_limit,
    clean_search,
    parse_cursor,
    split_keyset,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.project import Project
from src.schemas import (
    AUTH_401,
    DailyTokenLimitBody,
    ErrorEnvelope,
    OkResponse,
    ProjectCreate,
    ProjectListResponse,
    ProjectPatch,
    ProjectResponse,
    error_responses,
)
from src.services.audit.log import append_audit
from src.services.projects import (
    delete_project_cascade,
    extract_source,
    generate_project_description,
    owned_project_or_404,
    resweep_submission_prefixes,
)
from src.services.storage import (
    AppContainerStore,
    ObjectStorage,
    get_app_container_store,
    sweep_app_containers,
    sweep_blobs,
)
from src.services.usage.gate import DailyTokenLimitExceededError, enforce_daily_limit

logger = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["projects"])

# A project-owned storage handle for the cascade blob sweep (swappable in tests).
StorageDep = Annotated[ObjectStorage, Depends(storage_dependency)]


def container_store_dependency() -> AppContainerStore | None:
    """The per-app container store for the cascade container sweep, or `None` when object storage
    is unconfigured (dev/test). Deliberately NOT mirroring `storage_dependency` (which raises via
    `get_storage()`): the sweep is None-tolerant so a delete still succeeds with storage off
    (KTD-2); in prod `_require_storage_in_production` guarantees a store. A dependency (not a bare
    call) so tests swap a fake via `dependency_overrides`."""
    return get_app_container_store()


# `| None`-tolerant, unlike StorageDep — the container sweep no-ops when storage is disabled.
ContainerStoreDep = Annotated[AppContainerStore | None, Depends(container_store_dependency)]


def _to_response(
    project: Project,
    app_id: uuid.UUID | None = None,
    app_status: AppStatus | None = None,
) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        app_id=str(app_id) if app_id is not None else None,
        app_status=app_status.value if app_status is not None else None,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


async def _project_app(
    db: DbSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[uuid.UUID | None, AppStatus | None]:
    """The project's ONE app's (id, status) — read-only discovery for the response
    (one app per project, KD-4) — or (None, None) for a fresh project. Owner-scoped
    like every query (ADR-0004)."""
    row = (
        await db.execute(
            sa.select(AppRegistry.id, AppRegistry.status).where(
                AppRegistry.project_id == project_id, AppRegistry.user_id == user_id
            )
        )
    ).one_or_none()
    return (row.id, row.status) if row is not None else (None, None)


@router.post("", status_code=status.HTTP_201_CREATED, responses=error_responses(AUTH_401))
async def create_project(body: ProjectCreate, user: CurrentUser, db: DbSession) -> ProjectResponse:
    """Create a project owned by the caller. `name` is stripped/bounded and an
    empty/whitespace `description` is normalized to NULL at the schema boundary (KD-8)."""
    project = Project(user_id=user.id, name=body.name, description=body.description)
    db.add(project)
    await db.flush()
    await db.refresh(project)  # load server defaults (id, timestamps) before projecting
    await db.commit()
    return _to_response(project)


@router.get(
    "",
    responses=error_responses(
        AUTH_401, (422, ErrorEnvelope, "Invalid pagination cursor or over-long q")
    ),
)
async def list_projects(
    user: CurrentUser,
    db: DbSession,
    cursor: CursorQuery = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    q: SearchQuery = None,
) -> ProjectListResponse:
    """Keyset page of the caller's projects, newest-first, optionally filtered by a
    case-insensitive name/description substring (R6). Stable under concurrent inserts (R5)."""
    after = parse_cursor(cursor)
    search = clean_search(q)
    limit = clean_limit(limit)
    # LEFT-JOIN the project's ONE app (uq_app_registry_project) so the page carries the
    # read-only appId/appStatus discovery without an N+1; the outer join keeps app-less
    # projects, and the app side carries its own owner scope (ADR-0004).
    query = (
        sa.select(Project, AppRegistry.id, AppRegistry.status)
        .outerjoin(
            AppRegistry,
            sa.and_(AppRegistry.project_id == Project.id, AppRegistry.user_id == user.id),
        )
        .where(Project.user_id == user.id)
    )
    if search is not None:
        query = query.where(
            sa.or_(
                Project.name.icontains(search, autoescape=True),
                Project.description.icontains(search, autoescape=True),
            )
        )
    if after is not None:
        query = query.where(Project.id < after)
    query = query.order_by(Project.id.desc()).limit(limit + 1)
    rows = (await db.execute(query)).all()
    page, next_cursor, has_more = split_keyset(rows, limit, key=lambda row: row[0].id)
    return ProjectListResponse(
        items=[_to_response(project, app_id, app_status) for project, app_id, app_status in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/{project_id}",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def get_project(project_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ProjectResponse:
    project = await owned_project_or_404(db, user.id, project_id)
    return _to_response(project, *await _project_app(db, user.id, project.id))


@router.patch(
    "/{project_id}",
    responses=error_responses(
        (400, ErrorEnvelope, "name cannot be cleared"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
    ),
)
async def patch_project(
    project_id: uuid.UUID, body: ProjectPatch, user: CurrentUser, db: DbSession
) -> ProjectResponse:
    """Apply only the fields present in the body (absent ≠ null). `description` may be
    cleared to NULL; `name` (NOT NULL) may not."""
    project = await owned_project_or_404(db, user.id, project_id)
    fields = body.model_fields_set
    if "name" in fields:
        if body.name is None:
            raise AppApiError(status.HTTP_400_BAD_REQUEST, "name cannot be cleared.")
        project.name = body.name
    if "description" in fields:
        project.description = body.description
    try:
        await db.commit()
    except StaleDataError:
        # The project was deleted between our load and this flush — the loser of that
        # race gets the same non-leaking 404 a PATCH one second later would, not a 500
        # (mirrors conversations' patch-vs-delete handling).
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.") from None
    await db.refresh(project)
    return _to_response(project, *await _project_app(db, user.id, project.id))


@router.delete(
    "/{project_id}",
    response_model=OkResponse,
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def delete_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    container_store: ContainerStoreDep,
) -> OkResponse:
    """Cascade-delete the project and every child it owns. Rows are deleted inside the
    transaction and committed; object-store blobs AND each app's per-app Blob container are swept
    only AFTER commit, best-effort, so a rolled-back delete never destroys a blob/container a
    restored row still points at (KD-3). The two sweeps hit two different stores (KTD-7).

    The submissions prefixes are re-enumerated AFTER the commit and folded into the sweep list
    (R8/R12), so a bundle written between the cascade's pre-commit gather and the commit is
    still swept instead of surviving under an app id whose row is gone. The narrower residual —
    a write landing after that re-walk — is NOT closed here; see `delete_project_cascade`."""
    project = await owned_project_or_404(db, user.id, project_id)
    cleanup = await delete_project_cascade(db, project, storage, user_id=user.id)
    await append_audit(
        db,
        actor_id=user.id,
        action="project:delete",
        resource_type="project",
        resource_id=str(project_id),
    )
    await db.commit()
    # Post-commit, pre-sweep: re-walk the submission prefixes so the sweep list reflects the
    # store as it is NOW. `app_container_ids` are plain UUIDs captured pre-commit, so reading
    # them here triggers no `expire_on_commit` lazy I/O (KD-8). Dedup preserves order and keeps
    # the pre-commit list in play even if the re-walk fails (it logs rather than raising).
    resweep = await resweep_submission_prefixes(storage, cleanup.app_container_ids)
    await sweep_blobs(storage, list(dict.fromkeys([*cleanup.blob_keys, *resweep])))
    await sweep_app_containers(container_store, cleanup.app_container_ids)
    return OkResponse(ok=True)


@router.post(
    "/{project_id}/description:generate",
    response_model=ProjectResponse,
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (409, ErrorEnvelope, "Nothing to generate from yet (no app / no code)"),
        (429, DailyTokenLimitBody, "Daily token limit exceeded"),
        (500, ErrorEnvelope, "The description generation failed"),
        (503, ErrorEnvelope, "Claude client not configured"),
    ),
)
async def generate_description(
    project_id: uuid.UUID, user: CurrentUser, db: DbSession, model: ModelDep
) -> ProjectResponse | JSONResponse:
    """Generate (or revise) the project description from its app's code (KD-5). Reads the
    project's ONE app's `current_code` (KD-4/9); a fresh project (no app / NULL code) is a
    409 "nothing to generate from yet". Bills against the daily gate like a chat turn (Q5);
    if a description already exists it is fed in so generation revises it (R19). The result
    is length-capped (KD-8) and stored on the project."""
    project = await owned_project_or_404(db, user.id, project_id)
    if model is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "Claude client not configured.")

    app = await db.scalar(
        sa.select(AppRegistry).where(
            AppRegistry.project_id == project.id, AppRegistry.user_id == user.id
        )
    )
    source = extract_source(app.current_code) if app is not None else ""
    if app is None or not source:
        raise AppApiError(
            status.HTTP_409_CONFLICT, "Nothing to generate from yet — build the app first."
        )

    # Bills like a normal turn (Q5): gate BEFORE the model call, 429 with the 5-key body.
    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError as exc:
        return exc.as_response()

    try:
        project.description = await generate_project_description(
            db, model, user.id, source=source, current_description=project.description
        )
    except Exception as exc:
        # A Foundry/model failure is this route's own explicit 500 envelope, never the
        # generic `{detail}` handler (mirrors the chat relay's drain failure).
        logger.exception("project_description_generation_failed")
        raise AppApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "The description generation failed."
        ) from exc
    try:
        await db.commit()
    except StaleDataError:
        # The project was deleted mid-generate. The usage row rides this commit, so the
        # 404 rolls the billing back too — an accepted, bounded loss on this rare race
        # (not worth rewiring billing into its own transaction).
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.") from None
    await db.refresh(project)
    return _to_response(project, app.id, app.status)
