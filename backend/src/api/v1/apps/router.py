"""App-lifecycle endpoints — owner-facing submit / status (R18, R4).

The builder flow: `submit` forks an immutable copy of the app's git-bundle snapshot,
scores the attached data-classification answers, and moves the app STRAIGHT to
approved or rejected (V4 Part 2 — no human review step; see `AUTO_APPROVE_AT` in
`apps/schemas.py`); `status` is an owner-scoped read. Both authenticate the owner via
`current_user` and are scoped by `user_id` (ADR-0004) — a cross-user read is a 404,
never a leak.

The app ROW is minted by the build session (`build_sessions/appdata.resolve_app_for_project`),
not by a client call: the standalone `POST /apps/provision` and `GET /apps/{id}/source`
endpoints had zero production callers and were removed in U6. The SPA renders its preview
from the live sandbox, and the durable `app_registry.current_code` is read server-side
through `extract_source` (`projects/router.generate_description`).

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

from src.api.deps import CurrentUser, DbSession, OptionalStorage
from src.api.v1.apps.schemas import (
    AUTO_APPROVE_AT,
    AppStatusResponse,
    SubmitRequest,
    SubmitResponse,
    total_weight,
)
from src.api.v1.live_build import refuse_while_build_session_live
from src.core.errors import AppApiError
from src.db.models.app_registry import STATUS_TRANSITIONS, AppRegistry, AppStatus
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
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


# Both routes authenticate via `current_user` (bare HTTPException 401 ->
# `{"detail"}`), so each documents 401 via the shared `AUTH_401` (DetailBody) spec; the
# routes' own raises are `AppApiError` -> `ErrorEnvelope`.


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
    app_id: uuid.UUID,
    body: SubmitRequest,
    user: CurrentUser,
    db: DbSession,
    storage: OptionalStorage,
) -> SubmitResponse:
    """Fork an immutable copy of the app's bundle and auto-decide approve/reject (audited).

    Copies `snapshots/{app_id}/app.bundle` (overwrite-latest, mutable) to
    `submissions/{app_id}/{submission_id}.bundle` (immutable by key derivation, R1)
    after validating it is a real git bundle and parsing its HEAD commit SHA (R3/R4).
    A submit is legal from draft/rejected/approved (resubmit paths) and is a refresh
    from pending — every re-submit mints a FRESH submission id (R2); from disabled it
    is rejected (409).

    V4 Part 2: there is no PENDING stop for a human anymore. The SAME request that
    copies the bundle scores `body.answers` (`total_weight`) and decides APPROVED
    (pinning this submission, `approved_by=None` — no human) or REJECTED (a plain
    auto-generated note) against `AUTO_APPROVE_AT`. The transition is a single atomic
    guarded UPDATE carrying the `user_id` ownership predicate — an illegal source
    state updates zero rows, exactly as before."""
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

    # V4 Part 2 — the auto-decision: no human review, decided from the same request
    # that copied the bundle. `decision_values` folds into the SAME guarded UPDATE as
    # everything else below, so the outcome lands atomically with the status change.
    score = total_weight(body.answers)
    if score >= AUTO_APPROVE_AT:
        target_status = AppStatus.APPROVED
        rejection_note = None
        decision_values: dict[str, object] = {
            # Pin EXACTLY the submission just minted — the same guarantee the old
            # human `approve` gave (D5), just decided here instead.
            "approved_submission_id": submission_id,
            "approved_commit_sha": commit_sha,
            # No human approved this — never claim a reviewer that doesn't exist.
            "approved_by": None,
            "approved_at": now,
        }
    else:
        target_status = AppStatus.REJECTED
        rejection_note = (
            f"Automatically rejected — the data-classification score ({score}) is "
            f"below the required threshold ({AUTO_APPROVE_AT}). Review your answers "
            "and resubmit, or contact an administrator."
        )
        # Deliberately does NOT touch approved_submission_id/approved_commit_sha —
        # same "reject does not clear the pin" rule the old admin `reject` followed:
        # a previously-approved app that auto-rejects on a later resubmit keeps its
        # last-good approved pin (the runbook can still deploy it).
        decision_values = {}

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
            status=target_status,
            source_submission_id=submission_id,
            source_commit_sha=commit_sha,
            submitted_at=now,
            rejection_note=rejection_note,
            # V4 Part 1: recorded atomically with the status change — never a second
            # call, so there is no window where the row has a decision but no answers.
            data_classification=body.answers.model_dump(),
            # V4 Part 2: this row's current decision came from the score gate, not a
            # human — durable and queryable independent of the audit log.
            decided_automatically=True,
            **decision_values,
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
        detail={
            "submissionId": str(submission_id),
            "commitSha": commit_sha,
            "dataClassification": body.answers.model_dump(mode="json", by_alias=True),
            "dataClassificationWeight": score,
            # The durable record of which route this submission took (V4 Part 2).
            "decision": target_status.value,
            "threshold": AUTO_APPROVE_AT,
        },
    )
    await db.commit()
    return SubmitResponse(
        app_id=app_id,
        status=target_status,
        submission_id=submission_id,
        commit_sha=commit_sha,
        submitted_at=now,
        rejection_note=rejection_note,
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
        data_classification=app.data_classification,
    )
