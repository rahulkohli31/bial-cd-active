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
from fastapi import APIRouter, Depends, status

from src.api.deps import CurrentUser, DbSession
from src.api.v1.attachments.router import storage_dependency
from src.api.v1.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorQuery,
    LimitQuery,
    SearchQuery,
    clean_search,
    parse_cursor,
    split_keyset,
)
from src.core.errors import AppApiError
from src.db.models.project import Project
from src.schemas import (
    AUTH_401,
    ErrorEnvelope,
    OkResponse,
    ProjectCreate,
    ProjectListResponse,
    ProjectPatch,
    ProjectResponse,
    error_responses,
)
from src.services.audit.log import append_audit
from src.services.projects import delete_project_cascade
from src.services.storage import ObjectStorage, StorageError

router = APIRouter(prefix="/projects", tags=["projects"])

# A project-owned storage handle for the cascade blob sweep (swappable in tests).
StorageDep = Annotated[ObjectStorage, Depends(storage_dependency)]


def _to_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


async def _owned_project_or_404(
    db: DbSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> Project:
    """Load a project scoped to its owner, or fail closed with a non-leaking 404 (a
    cross-user id is indistinguishable from a missing one, ADR-0004)."""
    project = await db.get(Project, project_id)
    if project is None or project.user_id != user_id:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.")
    return project


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


@router.get("", responses=error_responses(AUTH_401))
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
    query = sa.select(Project).where(Project.user_id == user.id)
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
    rows = (await db.execute(query)).scalars().all()
    page, next_cursor, has_more = split_keyset(rows, limit, key=lambda p: p.id)
    return ProjectListResponse(
        items=[_to_response(p) for p in page], next_cursor=next_cursor, has_more=has_more
    )


@router.get(
    "/{project_id}",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def get_project(project_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ProjectResponse:
    return _to_response(await _owned_project_or_404(db, project_id, user.id))


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
    project = await _owned_project_or_404(db, project_id, user.id)
    fields = body.model_fields_set
    if "name" in fields:
        if body.name is None:
            raise AppApiError(status.HTTP_400_BAD_REQUEST, "name cannot be cleared.")
        project.name = body.name
    if "description" in fields:
        project.description = body.description
    await db.commit()
    await db.refresh(project)
    return _to_response(project)


@router.delete(
    "/{project_id}",
    response_model=OkResponse,
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def delete_project(
    project_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: StorageDep
) -> OkResponse:
    """Cascade-delete the project and every child it owns. Rows are deleted inside the
    transaction and committed; object-store blobs are swept only AFTER commit, best-effort,
    so a rolled-back delete never destroys a blob a restored row still points at (KD-3)."""
    project = await _owned_project_or_404(db, project_id, user.id)
    blob_keys = await delete_project_cascade(db, project, user_id=user.id)
    await append_audit(
        db,
        actor_id=user.id,
        action="project:delete",
        resource_type="project",
        resource_id=str(project_id),
    )
    await db.commit()
    for key in blob_keys:
        try:
            await storage.delete(key)
        except StorageError:
            # Best-effort post-commit: a missing object / store error must not surface.
            pass
    return OkResponse(ok=True)
