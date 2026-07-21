"""App-lifecycle endpoints — owner-facing provision / submit / status (R18, R4).

The builder flow: `provision` mints (or reuses) the project's ONE draft + publishable
appKey (one app per project, KD-4); `submit` forks an immutable copy of the app's
git-bundle snapshot and moves draft→pending; `status` is an owner-scoped read.
All three authenticate the owner via `current_user` and are scoped by `user_id`
(ADR-0004) — a cross-user read is a 404, never a leak.

The artifact is server-side (APPROVAL R19): the open-sandbox build finalizes an
overwrite-latest snapshot bundle, and submit copies it to a versioned, per-submission
key so the artifact under review can never move (R1). Approval (admin) pins the exact
submission the admin reviewed (D5).

Errors use the ported `{"error": {"message": ...}}` shape (`AppApiError`) the SPA
already consumes, not the auth endpoints' `{"detail": ...}`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from src.api.deps import CurrentUser, DbSession, OptionalStorage
from src.api.v1.apps.schemas import (
    AppSourceResponse,
    AppStatusResponse,
    LifecycleResponse,
    ProvisionRequest,
    SubmitResponse,
)
from src.api.v1.live_build import refuse_while_build_session_live
from src.core.errors import AppApiError
from src.db.models.app_registry import (
    STATUS_TRANSITIONS,
    AppRegistry,
    AppStatus,
    mint_app_key,
)
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
from src.services.projects import extract_source, owned_project_or_404
from src.services.storage import (
    BUNDLE_CONTENT_TYPE,
    BundleValidationError,
    StorageError,
    StorageNotFoundError,
    parse_bundle_head_sha,
    snapshot_key,
    submission_key,
)

_log = structlog.get_logger()

router = APIRouter(prefix="/apps", tags=["apps"])


# All three routes authenticate via `current_user` (bare HTTPException 401 ->
# `{"detail"}`), so each documents 401 via the shared `AUTH_401` (DetailBody) spec; the
# routes' own raises are `AppApiError` -> `ErrorEnvelope`.


@router.post(
    "/provision",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found (or not owned by the caller)"),
        (409, ErrorEnvelope, "Project app is owned by another user"),
    ),
)
async def provision(body: ProvisionRequest, user: CurrentUser, db: DbSession) -> LifecycleResponse:
    """Mint (or reuse) the project's ONE draft + appKey — one app per project (KD-4).

    Provision targets a PROJECT, not a conversation: `project_id` is REQUIRED
    (project-first — every app lives in a caller-owned project) and is the idempotency
    key. If the project already has an app (`uq_app_registry_project`) the SAME row + its
    original appKey are returned (the continuity case — a new builder session builds against
    the existing app), and the acting conversation is recorded as the head/last-builder
    pointer; otherwise the single project app is minted. The SPA addresses submit/status by
    the RETURNED appId. The appKey is a publishable `secrets.token_urlsafe` label, never a
    raw UUID (ADR-0006)."""
    project = await owned_project_or_404(db, user.id, body.project_id)
    # Upsert on the one-app-per-project constraint: a first provision INSERTs the app; a
    # repeat in the same project DO-UPDATEs (advancing the head conversation), so provision
    # stays idempotent per project and races collapse onto the single row. The owner-guarded
    # WHERE means the DO-UPDATE only touches the caller's own app (defense in depth — the
    # project is already owner-validated).
    upsert = (
        pg_insert(AppRegistry)
        .values(
            user_id=user.id,
            project_id=project.id,
            conversation_id=body.conversation_id,
            app_key=mint_app_key(),
            status=AppStatus.DRAFT,
        )
        .on_conflict_do_update(
            constraint="uq_app_registry_project",
            set_={"conversation_id": body.conversation_id, "updated_at": sa.func.now()},
            where=(AppRegistry.user_id == user.id),
        )
        .returning(
            AppRegistry.id,
            AppRegistry.app_key,
            AppRegistry.login_required,
            AppRegistry.status,
        )
    )
    try:
        row = (await db.execute(upsert)).one_or_none()
    except IntegrityError as exc:
        # The project was deleted between `owned_project_or_404` and this INSERT — the
        # loser of that race gets the same non-leaking 404 a provision one second later
        # would, not a 500. Any other violation is a real bug: re-raise.
        if "app_registry_project_id_fkey" in str(exc.orig):
            raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.") from exc
        raise
    if row is None:
        # The project's app belongs to another user (an ownership-invariant violation):
        # fail closed rather than touch or reveal it (ADR-0004).
        raise AppApiError(status.HTTP_409_CONFLICT, "Project app is owned by another user.")
    await db.commit()
    return LifecycleResponse(
        app_id=row.id,
        app_key=row.app_key,
        login_required=row.login_required,
        status=row.status,
    )


async def _owned_app_or_404(db: DbSession, app_id: uuid.UUID, user_id: uuid.UUID) -> AppRegistry:
    """Load an app scoped to its owner, or fail closed with a non-leaking 404 (a
    cross-user id is indistinguishable from a missing one)."""
    app = await db.get(AppRegistry, app_id)
    if app is None or app.user_id != user_id:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


# A submit is allowed from the pending-transition sources PLUS pending itself (a
# re-submit refresh). Disabled falls outside → 409.
_SUBMIT_FROM = frozenset(STATUS_TRANSITIONS[AppStatus.PENDING] | {AppStatus.PENDING})

_ILLEGAL_STATE_MSG = "This app cannot be submitted in its current state."
_NO_BUNDLE_MSG = "Nothing to submit — generate an app first."
_STORAGE_DOWN_MSG = "Storage is temporarily unavailable. Please try again."
# Deliberately COARSE (no `app_id`): submit refuses while this user is building ANYTHING,
# which is the behaviour that shipped and the copy that matches it. Narrowing it to the
# submitted app is a separate call, not a side effect of lifting the guard (U8).
_BUILD_LIVE_SUBMIT_MSG = "A build session is still running — end it before submitting."


@router.post(
    "/{app_id}/submit",
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "No bundle, invalid bundle, build session live, or illegal state"),
        (503, ErrorEnvelope, "Storage or build coordination temporarily unavailable"),
    ),
)
async def submit(
    app_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: OptionalStorage
) -> SubmitResponse:
    """Fork an immutable copy of the app's bundle and move draft→pending (audited).

    Copies `snapshots/{app_id}/app.bundle` (overwrite-latest, mutable) to
    `submissions/{app_id}/{submission_id}.bundle` (immutable by key derivation, R1)
    after validating it is a real git bundle and parsing its HEAD commit SHA (R3/R4).
    A submit is legal from draft/rejected/approved (resubmit paths) and is a refresh
    from pending — every re-submit mints a FRESH submission id (R2); from disabled it
    is rejected (409). The transition is a single atomic guarded UPDATE carrying the
    `user_id` ownership predicate — an illegal source state updates zero rows."""
    app = await _owned_app_or_404(db, app_id, user.id)  # existence + ownership (404)

    # 1. D8 — never copy out from under a live build session: the copy would capture the
    #    PREVIOUS build's bundle (valid bytes, wrong tree — undetectable by any header
    #    check) or torn bytes under a concurrent finalize overwrite.
    await refuse_while_build_session_live(user.id, conflict_message=_BUILD_LIVE_SUBMIT_MSG)

    # 2. Non-authoritative status pre-check on the already-owned row: narrows the
    #    orphan-blob window (D3) — the guarded UPDATE below is the real gate.
    if app.status not in _SUBMIT_FROM:
        raise AppApiError(status.HTTP_409_CONFLICT, _ILLEGAL_STATE_MSG)

    # 3. Submit's OWN fail-closed read (D9) — deliberately NOT `_snapshot_exists`,
    #    whose transient-error-means-absent bias would tell someone whose app is
    #    fully built to go build it. Absent → 409; transient → 503.
    #    An UNCONFIGURED store arrives here as `None` (the None-tolerant `OptionalStorage`)
    #    and answers with the SAME documented 503 as a transient blip — an eager `Storage`
    #    dependency would instead raise at solve time, before this seam or even the 404 above
    #    could run, and the client would get an undocumented 500.
    if storage is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN_MSG)
    try:
        raw = await storage.get(snapshot_key(app_id))
    except StorageNotFoundError as exc:
        raise AppApiError(status.HTTP_409_CONFLICT, _NO_BUNDLE_MSG) from exc
    except StorageError as exc:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN_MSG) from exc

    # 4. R3/R4 — a real git bundle, and its HEAD SHA for provenance. The header is
    #    attacker-writable; the parser returns only a validated 40-hex token.
    try:
        commit_sha = parse_bundle_head_sha(raw)
    except BundleValidationError as exc:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "The app's snapshot is not a valid build bundle — rebuild and try again.",
        ) from exc

    # 5. D3 — the blob lands FIRST, the row second. put→DB-fail leaves an orphan
    #    blob nothing references (harmless, logged below); DB→put-fail would leave
    #    a ref whose artifact does not exist — an app could reach APPROVED with
    #    nothing to deploy. Do not "tidy" this ordering.
    submission_id = uuid.uuid7()
    key = submission_key(app_id, submission_id)
    try:
        await storage.put(key, raw, content_type=BUNDLE_CONTENT_TYPE)
    except StorageError as exc:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN_MSG) from exc

    now = datetime.now(UTC)
    moved = await db.execute(
        sa.update(AppRegistry)
        .where(
            AppRegistry.id == app_id,
            # The ownership predicate (ADR-0004) — a dropped user_id clause is a
            # cross-user leak, not a style nit.
            AppRegistry.user_id == user.id,
            AppRegistry.status.in_(tuple(_SUBMIT_FROM)),
        )
        .values(
            status=AppStatus.PENDING,
            source_submission_id=submission_id,
            source_commit_sha=commit_sha,
            submitted_at=now,
            # A stale "rejected because X" note must not survive the re-submit —
            # `read_status` returns it straight to the citizen.
            rejection_note=None,
        )
        .returning(AppRegistry.id)
    )
    if moved.first() is None:
        # The accepted D3 residual: the blob above is now an orphan no row
        # references. Log it structured so the deferred reconciling blob-GC has a
        # trail (its exclusion contract lives in the plan's D3).
        _log.warning(
            "submit orphan blob: status guard refused the row after the copy landed",
            app_id=str(app_id),
            key=key,
        )
        raise AppApiError(status.HTTP_409_CONFLICT, _ILLEGAL_STATE_MSG)

    await append_audit(
        db,
        actor_id=user.id,
        action="submit",
        resource_type="app",
        resource_id=str(app_id),
        detail={"submissionId": str(submission_id), "commitSha": commit_sha},
    )
    await db.commit()
    return SubmitResponse(
        app_id=app_id,
        status=AppStatus.PENDING,
        submission_id=submission_id,
        commit_sha=commit_sha,
        submitted_at=now,
    )


@router.get(
    "/{app_id}/status",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "App not found")),
)
async def read_status(app_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AppStatusResponse:
    """Owner-scoped lifecycle read; an absent or cross-user app is the same non-leaking 404 its
    sibling `submit` returns.

    The old `200 {status: null}` "not provisioned" signal was the appId==conversationId polling
    shim: the SPA could hold an id the server had never minted. Provision now mints a server-side
    appId, so a client can only hold an id we issued — `status: null` could then only mask a
    client bug."""
    app = await _owned_app_or_404(db, app_id, user.id)
    return AppStatusResponse(
        app_id=app.id,
        status=app.status,
        app_key=app.app_key,
        login_required=app.login_required,
        rejection_note=app.rejection_note,
        submission_id=app.source_submission_id,
        commit_sha=app.source_commit_sha,
        submitted_at=app.submitted_at,
        deployed_at=app.deployed_at,
        deployed_url=app.deployed_url,
    )


@router.get(
    "/{app_id}/source",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "App not found")),
)
async def read_source(app_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AppSourceResponse:
    """Serve the project's ONE durable app code (KD-9) by appId, owner-scoped.

    This is the read that lets ANY builder chat in a project render the existing app —
    not just the chat that first generated it. Code is written per-conversation AND mirrored
    into `app_registry.current_code`, so a second chat (or a casual 'hi' turn) has no snapshot
    of its own; without this read its preview would render blank over a fully-built app.

    A never-built app resolves to `source: ""` ("nothing to render", the SPA's empty state),
    NOT a 404 — the app row exists, it just has no code yet. A cross-user or absent id is the
    same non-leaking 404 its `status`/`submit` siblings return (ADR-0004)."""
    app = await _owned_app_or_404(db, app_id, user.id)
    current = app.current_code.get("current") if isinstance(app.current_code, dict) else None
    entry = current.get("entry") if isinstance(current, dict) else None
    return AppSourceResponse(
        app_id=app.id,
        source=extract_source(app.current_code),
        entry=entry if isinstance(entry, str) and entry else "PreviewApp",
    )
