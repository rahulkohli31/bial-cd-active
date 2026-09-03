"""Projects HTTP endpoints — user-scoped CRUD for the parent container (R1, R3, R5–R7).

A project is the home a citizen developer builds one tool inside (KD-4). Identity is always
the authenticated caller; every query is scoped by `user_id` (a dropped predicate is a
cross-user leak — a cross-user id is a 404, never a leak, ADR-0004). List is keyset-paginated
+ searchable (KD-1); delete cascades through the blob-aware, rollback-safe U6 service (KD-3).

Errors use the ported `{"error": {"message": ...}}` shape (`AppApiError`), documented with
the shared `error_responses(...)` + `AUTH_401` builders (KD-7).
"""

from __future__ import annotations

import math
import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from src.api.deps import CurrentUser, DbSession
from src.api.v1.attachments.router import storage_dependency
from src.api.v1.conversations._shared import ModelDep
from src.api.v1.live_build import refuse_while_build_session_live
from src.api.v1.offset_pagination import PageQuery, clean_page
from src.api.v1.pagination import (
    DEFAULT_PAGE_SIZE,
    LimitQuery,
    SearchQuery,
    clean_limit,
    clean_search,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.conversation import Conversation
from src.db.models.deleted_project import DeletedProject
from src.db.models.project import Project
from src.schemas import (
    AUTH_401,
    DailyTokenLimitBody,
    ErrorEnvelope,
    OkResponse,
    ProjectCountsResponse,
    ProjectCreate,
    ProjectDeleteRequest,
    ProjectListResponse,
    ProjectPatch,
    ProjectResponse,
    error_responses,
)
from src.services.appdb.provision import ensure_project_database
from src.services.appdb.teardown import salt_the_earth, teardown_handles
from src.services.audit.log import append_audit
from src.services.build_sessions.manager import restorable_presence
from src.services.deploy.liveness import live_app_ids
from src.services.deploy.teardown import sweep_published_apps
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
    has_relaunchable_snapshot: bool | None = None,
    *,
    is_serving: bool,
) -> ProjectResponse:
    """Project a row onto the wire shape.

    `is_serving` IS REQUIRED, AND KEYWORD-ONLY, because a default here is a silent wrong
    answer. It shipped as `= False` and three of the five call sites simply never passed it,
    so `GET /{id}` reported a live app as not serving while its own field docstring says it
    IS the server's answer. A default is what let the omission type-check; without one, a new
    endpoint cannot forget it, and `_serving_now` is the one way to work it out.
    """
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        app_id=str(app_id) if app_id is not None else None,
        app_status=app_status.value if app_status is not None else None,
        has_relaunchable_snapshot=has_relaunchable_snapshot,
        is_serving=is_serving,
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


async def _serving_now(db: DbSession, app_id: uuid.UUID | None) -> bool:
    """Is this ONE app live right now?

    The single-row form of the list's collapse, reading the SAME `live_app_ids` definition
    rather than re-deriving it — the drift `liveness.py` exists to prevent is not only
    between surfaces, it is between the list and the detail view of the same project.

    `None` means the project has no app at all, which is a confirmed False rather than an
    unknown: nothing can be serving.
    """
    if app_id is None:
        return False
    live = live_app_ids().subquery()
    return bool(await db.scalar(sa.select(sa.exists().where(live.c.app_id == app_id))))


@router.post("", status_code=status.HTTP_201_CREATED, responses=error_responses(AUTH_401))
async def create_project(body: ProjectCreate, user: CurrentUser, db: DbSession) -> ProjectResponse:
    """Create a project owned by the caller, then provision its own database (ADR-0028).

    `name` is stripped/bounded and an empty/whitespace `description` is normalized to NULL
    at the schema boundary (KD-8).

    The provision runs AFTER the commit and is BEST-EFFORT, both deliberately.
    After, because `ensure_project_database` commits its own claim and its own terminal
    marker — running it first would commit this request's half-built transaction.
    Best-effort, because a substrate hiccup must never strand or 500 a project the user
    already owns: the response is a normal 201 and the next build's lazy ensure
    (`provision_app_database`) re-runs the idempotent sequence.

    The app row is NOT minted here — it stays lazily created at first build, so a fresh
    project still reports `appId: null` (`test_app_discovery_null_for_fresh_project…`).
    """
    project = Project(user_id=user.id, name=body.name, description=body.description)
    db.add(project)
    await db.flush()
    await db.refresh(project)  # load server defaults (id, timestamps) before projecting
    project_id = project.id  # a plain scalar for the post-commit work (no expired-attribute I/O)
    await db.commit()
    # A project one statement old owns no app, so nothing of its can be serving. Passed
    # explicitly rather than defaulted: this is an answer, not an omission.
    response = _to_response(project, is_serving=False)
    await _provision_database_or_shrug(db, project_id)
    return response


async def _provision_database_or_shrug(db: DbSession, project_id: uuid.UUID) -> None:
    """Provision the project's database; on failure log and carry on (never 500).

    Resolved lazily INSIDE the body rather than through a `Depends`, so an unconfigured or
    unreachable substrate can never turn create-project into a dependency-solve 500
    (commit 6be7a9c closed exactly that class of bug).

    Only the exception TYPE is logged, never its message: a failing `CREATE ROLE` surfaces
    as a SQLAlchemy `DBAPIError` whose string carries the offending `[SQL: ...]` — which
    for that one statement contains the role's password literal.
    """
    try:
        await ensure_project_database(db, project_id)
    except Exception as exc:  # noqa: BLE001 — degraded state, not a failed create (R4)
        logger.warning(
            "project_database_provision_failed",
            project_id=str(project_id),
            error_type=type(exc).__name__,
            hint="the next build start re-runs the idempotent provision",
        )


@router.get(
    "",
    responses=error_responses(
        AUTH_401, (422, ErrorEnvelope, "Invalid page/pageSize or over-long q")
    ),
)
async def list_projects(
    user: CurrentUser,
    db: DbSession,
    page: PageQuery = 1,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    q: SearchQuery = None,
) -> ProjectListResponse:
    """One NUMBERED page of the caller's projects, newest-first, optionally filtered by a
    case-insensitive name/description substring (R6).

    IT PAGES BY OFFSET, and `pagination.py` says the platform does not. #158 §2 specifies
    numbered pages and a rows-per-page selector — `Showing 1-8 of 12`, `Page 1 of 2` — and
    neither is expressible without a `total`, which keyset deliberately does not provide.

    THE MARKETPLACE'S ARGUMENT DOES NOT TRANSFER, and reaching for it would be the quiet
    kind of wrong. That one reads: "KD-1's keyset rule protects a list you are writing to,
    this catalog is read-only and small". This list is written to — `create` and `delete`
    both act on it, and under `ORDER BY id DESC` a new project lands at position 0, which is
    the worst case for OFFSET rather than a benign one.

    What makes it acceptable here is different and narrower: the list is OWNER-SCOPED and
    effectively SINGLE-WRITER. Every row is `WHERE user_id = :me`, and the only person who
    inserts or deletes rows in it is the person reading it. So the skew KD-1 guards against
    — a busy shared table shifting under a stranger's page walk — is here a citizen with two
    tabs open, creating a project in one while paging in the other. That is a real window
    and it is bounded by one person's own actions, which is a different risk from the one
    the rule was written for.

    `total` is a SEPARATE READ from the page under READ COMMITTED, not one snapshot, so a
    create landing between them can make the count and the rows disagree for one render.
    The client is expected to say something true when they do, rather than assert either
    number over the other.

    A page past the end returns an empty `items` with the real `total`, not a 404: paging
    past the end while a project is deleted elsewhere is ordinary, not an error.
    """
    page = clean_page(page)
    search = clean_search(q)
    limit = clean_limit(limit)
    # LEFT-JOIN the project's ONE app (uq_app_registry_project) so the page carries the
    # read-only appId/appStatus discovery without an N+1; the outer join keeps app-less
    # projects, and the app side carries its own owner scope (ADR-0004).
    # ONE JOIN, not one request per row. The status column needs to know whether each app is
    # SERVING, and "live = deployed / published, with a url" is a deployment fact rather than
    # a lifecycle one (#158). `PublishStatusChip` gets it from `getDeployment(projectId)`,
    # which is fine for one project page and is an N-way fan-out on a list — so the list
    # reads the same definition set-wise instead, via the shared `live_app_ids` collapse.
    # SCOPED to this owner, and the scoping happens INSIDE the collapse (round-4 fix): an
    # unscoped `live_app_ids()` filtered afterward by `user.id` still evaluates the
    # `DISTINCT ON` over every deployment row the PLATFORM has, because the join here cannot
    # tell the collapse to narrow first. Measured at 25,245 apps / 112,045 deployments as a
    # >300x cost on the first screen after sign-in — see `live_app_ids`'s docstring.
    live = live_app_ids(owner_user_id=user.id).subquery()
    query = (
        sa.select(Project, AppRegistry.id, AppRegistry.status, live.c.app_id.is_not(None))
        .outerjoin(
            AppRegistry,
            sa.and_(AppRegistry.project_id == Project.id, AppRegistry.user_id == user.id),
        )
        # OUTER on the liveness side too: a project with no app, or an app that has never
        # deployed, has no row here and is simply not live — it must still be listed.
        .outerjoin(live, live.c.app_id == AppRegistry.id)
        .where(Project.user_id == user.id)
    )
    if search is not None:
        query = query.where(
            sa.or_(
                Project.name.icontains(search, autoescape=True),
                Project.description.icontains(search, autoescape=True),
            )
        )
    # THE COUNT DOES NOT NEED EITHER JOIN, and carrying them was the other half of the same
    # cost: neither can change how many rows match. `AppRegistry.project_id` is unique
    # (`uq_app_registry_project`, KD-4 — one app per project), and `live.c.app_id` is unique
    # per collapse, so a project row survives an outer join to either exactly once. The count
    # runs over the SAME predicate as the page (owner + search), just without the columns
    # that predicate does not need — a total computed over a different predicate is the
    # failure that would render page numbers the user can click and find empty; a total
    # computed over a WIDER one just to reuse a query object is a cost with no such payoff.
    count_query = sa.select(Project).where(Project.user_id == user.id)
    if search is not None:
        count_query = count_query.where(
            sa.or_(
                Project.name.icontains(search, autoescape=True),
                Project.description.icontains(search, autoescape=True),
            )
        )
    count_stmt = sa.select(sa.func.count()).select_from(count_query.subquery())
    total = int(await db.scalar(count_stmt) or 0)
    rows = (
        await db.execute(query.order_by(Project.id.desc()).limit(limit).offset((page - 1) * limit))
    ).all()
    return ProjectListResponse(
        items=[
            _to_response(project, app_id, app_status, is_serving=is_serving)
            for project, app_id, app_status, is_serving in rows
        ],
        page=page,
        page_size=limit,
        total=total,
        total_pages=math.ceil(total / limit) if total else 0,
    )


@router.get("/counts", responses=error_responses(AUTH_401))
async def project_counts(user: CurrentUser, db: DbSession) -> ProjectCountsResponse:
    """The three numbers above the project list (#158 §1).

    DECLARED BEFORE `/{project_id}`, and that ordering is load-bearing: FastAPI matches in
    declaration order, so a `/counts` registered after the parameterised route would be
    swallowed by it and answer 422 on a UUID parse instead.

    Owner-scoped like every route here (ADR-0004) — these are the citizen's own projects,
    unlike `/admin/apps/counts`, which counts across owners.

    Three aggregates over one owner's rows, no row projection and no per-app probing. The
    liveness half reads the SHARED `live_app_ids` collapse, which is the whole reason this
    is not three ad-hoc queries: the list's status column reads the same definition, so
    "3 in production" above a list showing two live apps is not expressible.
    """
    # SCOPED to this owner inside the collapse — see `live_app_ids`'s docstring and
    # `list_projects`'s identical fix; this route had the same unscoped-collapse cost.
    live = live_app_ids(owner_user_id=user.id).subquery()

    # PROJECTS, not `app_registry` rows. The product calls a project an application — the
    # page is headed "Your apps" and its subtitle reads "each project is one tool" — and a
    # project exists before anything is built inside it. Counting app rows put "Total
    # applications 0" above a list showing 18 projects, which reads as broken rather than as
    # a subtle distinction, and the mockup shows the two numbers agreeing for that reason.
    total = sa.select(sa.func.count()).select_from(Project).where(Project.user_id == user.id)
    in_production = (
        sa.select(sa.func.count())
        .select_from(AppRegistry)
        .join(live, live.c.app_id == AppRegistry.id)
        .where(AppRegistry.user_id == user.id)
    )
    # In the pipeline: submitted or decided, but not yet serving. PENDING and REJECTED are
    # unambiguous. APPROVED belongs here only while it is NOT live — an approved app that is
    # serving is counted by `in_production`, and counting it twice would make the three
    # numbers sum to more than the citizen has.
    in_pipeline = (
        sa.select(sa.func.count())
        .select_from(AppRegistry)
        .outerjoin(live, live.c.app_id == AppRegistry.id)
        .where(
            AppRegistry.user_id == user.id,
            AppRegistry.status.in_((AppStatus.PENDING, AppStatus.REJECTED, AppStatus.APPROVED)),
            live.c.app_id.is_(None),
        )
    )
    return ProjectCountsResponse(
        in_production=(await db.execute(in_production)).scalar_one(),
        total_applications=(await db.execute(total)).scalar_one(),
        in_pipeline=(await db.execute(in_pipeline)).scalar_one(),
    )


@router.get(
    "/{project_id}",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def get_project(project_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ProjectResponse:
    project = await owned_project_or_404(db, user.id, project_id)
    app_id, app_status = await _project_app(db, user.id, project.id)
    # N7 — the ONE surface that offers Relaunch, so the one that pays for the head-check.
    # No app row means no bundle can exist, and that is a CONFIRMED absent rather than an
    # unknown: skipping the store call here is an answer, not an omission.
    #
    # `restorable_presence`, NOT `snapshot_presence` (R18): the saved bundle alone missed the
    # builder who worked for an hour and never pressed Save, and told them their project had
    # nothing to restore while the platform sat on their entire workspace. This is also the
    # exact predicate `preview-state` answers with, so a cold page load and the 45-second poll
    # can never disagree about whether a restore is on offer.
    relaunchable = False if app_id is None else await restorable_presence(app_id)
    return _to_response(
        project, app_id, app_status, relaunchable, is_serving=await _serving_now(db, app_id)
    )


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
    app_id, app_status = await _project_app(db, user.id, project.id)
    return _to_response(project, app_id, app_status, is_serving=await _serving_now(db, app_id))


# Names the LIVE SESSION as the reason and the action that clears it (R9/D4: refuse, never
# force — forcing would destroy every file change since the last snapshot, and snapshots are
# written only at finalize, so the user would get no signal their work was unsaved).
_BUILD_LIVE_DELETE_MSG = (
    "A build session is still running for this project — end it before deleting."
)


@router.delete(
    "/{project_id}",
    response_model=OkResponse,
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (409, ErrorEnvelope, "A build session is live for this project's app"),
        (503, ErrorEnvelope, "Build coordination temporarily unavailable"),
    ),
)
async def delete_project(
    project_id: uuid.UUID,
    body: ProjectDeleteRequest,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    container_store: ContainerStoreDep,
) -> OkResponse:
    """Cascade-delete the project and every child it owns.

    IT TAKES A BODY, which is unusual for DELETE and worth naming. #158 §13.2 requires the
    person deleting to state WHY, in 5-50 words, and a 50-word reason does not belong in a
    query string. RFC 9110 says content on a DELETE has no defined semantics, and httpx
    declines to offer `json=` on `.delete()` for that reason — tests use `.request("DELETE",
    ...)`. nginx and the container ingress both forward the body, and the portal is the only
    client, so this is safe here; it is recorded rather than assumed. The alternative, a
    `POST /{id}/delete` matching `disable`/`unpublish`, is a bigger contract change than
    adding a required field to the route that already exists. Rows are deleted inside the
    transaction and committed; object-store blobs AND each app's per-app Blob container are swept
    only AFTER commit, best-effort, so a rolled-back delete never destroys a blob/container a
    restored row still points at (KD-3). The two sweeps hit two different stores (KTD-7).

    The submissions prefixes are re-enumerated AFTER the commit and folded into the sweep list
    (R8/R12), so a bundle written between the cascade's pre-commit gather and the commit is
    still swept instead of surviving under an app id whose row is gone. The narrower residual —
    a write landing after that re-walk — is NOT closed here; see `delete_project_cascade`.

    A live build session for THIS project's app refuses the delete (409, R9) rather than
    racing it. The guard is app-scoped, so a build in one project never blocks the delete of
    another. It does NOT cover a relaunched preview, which holds no lock by design — that
    container keeps serving after the delete; see `api/v1/live_build.py` for the open gap.
    That gap is exactly why the project's own database is torn down with `salt_the_earth`
    (sever, then `DROP DATABASE ... WITH (FORCE)`): a preview or a deployed container can
    still be holding live connections at delete time, and the force-drop — not the guard —
    is what guarantees they stop reading. It runs post-commit and never raises: the rows are
    already gone, so a failed drop is a logged orphan for the reconciler, never a 500 on a
    delete that in fact succeeded."""
    project = await owned_project_or_404(db, user.id, project_id)
    # THE TOMBSTONE, written before the cascade removes what it describes (#158 §13.3).
    # Inside the caller's transaction, so a rolled-back delete leaves no record of a
    # deletion that did not happen — and a committed one always has its reason.
    #
    # Values, not foreign keys: the project row is gone a few lines below, so anything this
    # references by id would be unreadable. The counts are captured HERE because they cannot
    # be reconstructed once the children are deleted.
    chats_deleted = int(
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(Conversation)
            # BOTH predicates, matching `delete_project_cascade` exactly. Counting on
            # `project_id` alone is not exploitable — ownership is already checked above —
            # but it makes the recorded number and the rows actually deleted two different
            # sets by construction, on a table whose only job is to be accurate about what
            # went with the project.
            .where(Conversation.project_id == project.id, Conversation.user_id == user.id)
        )
        or 0
    )
    # R9: refuse while this project's app is being built. Owner-scoped discovery (ADR-0004);
    # a project with no app row can have no build session, so the guard is skipped rather
    # than fired — an app-less project must not inherit another project's live build.
    app_id, _app_status = await _project_app(db, user.id, project.id)
    if app_id is not None:
        await refuse_while_build_session_live(
            user.id, conflict_message=_BUILD_LIVE_DELETE_MSG, app_id=app_id
        )
    # The database handles, as plain scalars, BEFORE the cascade: deleting the project
    # cascades its `project_databases` row away, so post-commit there is nothing left to
    # read them from — the same reason `app_container_ids` are plain UUIDs (KD-8).
    handles = await teardown_handles(db, project.id)
    # Captured before the cascade, for the same reason as the chat count: `handles` is read
    # from a row the cascade deletes.
    db.add(
        DeletedProject(
            project_id=project.id,
            project_name=project.name,
            owner_id=project.user_id,
            owner_email=user.email,
            deleted_by=user.id,
            # BOTH from the session, never the body. `deleted_by` is the durable key and
            # this is its readable label, so they must name the same person by construction;
            # a client-supplied name could not be trusted by the administrator who reads it.
            # `display_name` is nullable — Entra does not always give one — and the email
            # identifies the account just as well, so it stands in rather than leaving the
            # one human-readable field on the row blank.
            deleted_by_name=user.display_name or user.email,
            remark=body.remark,
            chats_deleted=chats_deleted,
            had_app=app_id is not None,
            had_database=handles is not None,
        )
    )
    # EVERYTHING FROM HERE THROUGH THE COMMIT IS THE GUARDED SECTION. The tombstone insert
    # above is only PENDING — SQLAlchemy autoflushes it at the next query that needs a
    # consistent view of the database, and `delete_project_cascade` issues exactly that kind
    # of query. The loser of a race therefore hits `deleted_projects.project_id`'s unique
    # index INSIDE the cascade's own autoflush, not at the explicit `db.commit()` below —
    # confirmed by running this without the wider try: the IntegrityError surfaced from
    # `delete_project_cascade`, not from the commit call.
    try:
        cleanup = await delete_project_cascade(db, project, storage, user_id=user.id)
        await append_audit(
            db,
            actor_id=user.id,
            action="project:delete",
            resource_type="project",
            resource_id=str(project_id),
        )
        if handles is not None:
            # NAMES only (D11) — never the DSN. `appId` is what makes this project-scoped
            # row visible in the app's audit drawer (`admin.read_audit` matches on it); an
            # app-less project simply has no app to file it under.
            detail: dict[str, str] = {"dbName": handles.db_name, "roleName": handles.role_name}
            if app_id is not None:
                detail["appId"] = str(app_id)
            await append_audit(
                db,
                actor_id=user.id,
                action="db:drop",
                resource_type="project",
                resource_id=str(project_id),
                detail=detail,
            )
        await db.commit()
    except IntegrityError:
        # THE LOSER OF A DOUBLE-SUBMIT OR A RETRY. `owned_project_or_404` takes no row lock
        # and this cascade deletes through Core `sa.delete()`, so no ORM staleness check
        # fires the way `patch_project`'s does — the first signal either request gets that
        # it lost the race is `deleted_projects.project_id`'s unique index refusing the
        # second tombstone. By then the winner's transaction has already committed and the
        # project is genuinely gone, so this mirrors `patch_project`'s own StaleDataError
        # handling: the loser gets the same non-leaking 404 a request one second later
        # would, not a 500 for a delete that in fact succeeded.
        #
        # THE EXPLICIT ROLLBACK IS LOAD-BEARING HERE IN A WAY IT ISN'T FOR StaleDataError.
        # Postgres aborts the whole transaction the instant a real constraint violation
        # reaches it — every statement after this one would answer "current transaction is
        # aborted" until something rolls it back — whereas `StaleDataError` is SQLAlchemy
        # catching a zero-row UPDATE/DELETE before any failing SQL is sent, so that
        # connection was never poisoned. `get_db`'s own `except Exception` rolls back too,
        # but only for what escapes this function; nothing downstream of this handler
        # (including a caller sharing this session) should have to know that.
        await db.rollback()
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.") from None
    if handles is not None:
        # FIRST of the post-commit sweeps, because it is the one that stops data being read:
        # sever, then force-drop the database, then drop the role. Never raises.
        await salt_the_earth(db_name=handles.db_name, role_name=handles.role_name)
    # Post-commit, pre-sweep: re-walk the submission prefixes so the sweep list reflects the
    # store as it is NOW. `app_container_ids` are plain UUIDs captured pre-commit, so reading
    # them here triggers no `expire_on_commit` lazy I/O (KD-8). Dedup preserves order and keeps
    # the pre-commit list in play even if the re-walk fails (it logs rather than raising).
    resweep = await resweep_submission_prefixes(storage, cleanup.app_container_ids)
    await sweep_blobs(storage, list(dict.fromkeys([*cleanup.blob_keys, *resweep])))
    await sweep_app_containers(container_store, cleanup.app_container_ids)
    # Last: the published container app itself. Same pre-commit id list — the published name
    # is a pure function of the app id — because after the cascade there is nothing left in
    # the database that names the running container, and the sandbox reaper cannot see it
    # (it sweeps the Redis registry, which a published app is never written to).
    await sweep_published_apps(cleanup.app_container_ids)
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
        # generic `{detail}` handler.
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
    return _to_response(project, app.id, app.status, is_serving=await _serving_now(db, app.id))
