"""App-lifecycle endpoints — the owner's own withdraw and status reads.

Owner-scoped and authenticated via `current_user`; every query carries the `user_id` predicate
the v1 router's header states. The submit body lives as `services/approvals/submit.py`, called
by the publish gate, and the app ROW is minted by the build session, never by a client call.

* `withdraw` — an owner pulls their own PENDING submission back to draft.
* `status` — the owner-scoped lifecycle read.

Errors use the `{"error": {"message": ...}}` shape (`AppApiError`) the SPA already consumes,
not the auth endpoints' `{"detail": ...}`.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, status

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.apps.schemas import AppStatusResponse, WithdrawResponse
from src.core.errors import AppApiError
from src.db.models.app_registry import STATUS_TRANSITIONS, AppRegistry, AppStatus
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit

router = APIRouter(prefix="/apps", tags=["apps"])


# Both routes authenticate via `current_user` (bare HTTPException 401 ->
# `{"detail"}`), so each documents 401 via the shared `AUTH_401` (DetailBody) spec; the
# routes' own raises are `AppApiError` -> `ErrorEnvelope`.


async def _owned_app_or_404(db: DbSession, app_id: uuid.UUID, user_id: uuid.UUID) -> AppRegistry:
    app = await db.get(AppRegistry, app_id)
    if app is None or app.user_id != user_id:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


_NOT_PENDING_WITHDRAW_MSG = "Only a submission that is waiting for review can be withdrawn."


@router.post(
    "/{app_id}/withdraw",
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only a pending submission can be withdrawn"),
    ),
)
async def withdraw(app_id: uuid.UUID, user: CurrentUser, db: DbSession) -> WithdrawResponse:
    """Pull the owner's own PENDING submission back out of the queue (audited).

    pending→draft through the same guarded-UPDATE shape as the admin transitions
    (`STATUS_TRANSITIONS[DRAFT]` is the source set), plus the ownership predicate the admin
    helper omits because an admin acts across owners. Zero rows updated is a refused withdrawal
    (409), never a no-op. Clearing the submission pin and the declaration REMOVES
    the queue item; the APPROVED pin and the immutable submission blob survive, because
    withdrawal takes back the queue item, not the artifact already decided on."""
    app = await _owned_app_or_404(db, app_id, user.id)

    # Non-authoritative pre-check for the honest copy; the guarded UPDATE below is
    # the real gate. Approved/rejected/disabled/draft all refuse — withdrawal only
    # ever un-queues, it never un-decides an administrator.
    if app.status is not AppStatus.PENDING:
        raise AppApiError(status.HTTP_409_CONFLICT, _NOT_PENDING_WITHDRAW_MSG)

    # Captured BEFORE the UPDATE (never read ORM attributes across a commit): the
    # audit trail names the exact submission that left the queue.
    submission_id, commit_sha = app.source_submission_id, app.source_commit_sha

    moved = await db.execute(
        sa.update(AppRegistry)
        .where(
            AppRegistry.id == app_id,
            AppRegistry.user_id == user.id,
            # THIS IS WHY `STATUS_TRANSITIONS[DRAFT]` STAYS `{PENDING}`. The row is
            # shared with the admin state machine, and this reader is citizen-facing with
            # nothing but an ownership check in front of it: add DISABLED to it and the
            # owner of an app an administrator switched off walks it back to draft from
            # their own workspace. Whatever un-sticks a disabled app, it is not this set.
            AppRegistry.status.in_(tuple(STATUS_TRANSITIONS[AppStatus.DRAFT])),
        )
        .values(
            status=AppStatus.DRAFT,
            source_submission_id=None,
            source_commit_sha=None,
            submitted_at=None,
            declaration=None,
        )
        .returning(AppRegistry.id)
    )
    if moved.first() is None:
        # Raced by an admin decision between the pre-check and here — the row is no
        # longer pending, so there is nothing left to withdraw.
        raise AppApiError(status.HTTP_409_CONFLICT, _NOT_PENDING_WITHDRAW_MSG)

    await append_audit(
        db,
        actor_id=user.id,
        action="withdraw",
        resource_type="app",
        resource_id=str(app_id),
        detail={
            "submissionId": str(submission_id) if submission_id else None,
            "commitSha": commit_sha,
        },
    )
    await db.commit()
    return WithdrawResponse(app_id=app_id, status=AppStatus.DRAFT)


@router.get(
    "/{app_id}/status",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "App not found")),
)
async def read_status(app_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AppStatusResponse:
    """Owner-scoped lifecycle read; an unknown or cross-user app is a 404.

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
    )
