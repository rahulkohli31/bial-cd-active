"""App-lifecycle endpoints — owner-facing withdraw / status (R18, R4; U8: R15a, P6).

The citizen-callable submit route is RETIRED (ASM18). It was the only backend writer
of the pending status, and leaving it reachable while the publish flow became the
route into the queue would let a queue item arrive with no declaration attached —
the second differently-worded way in that R15a forbids. The submit body lives on,
verbatim plus its documented divergence, as `services/approvals/submit.py`; its only
callers are the publish gate (U9) and any future admin-initiated entry. The manual
go-live runbook lineage consequently gets NO new entrants — apps already in it keep
their controls and their address, which is R15a's accepted cost.

What remains here is owner-scoped and authenticated via `current_user`, scoped by
`user_id` (ADR-0004) — a cross-user read or write is a 404, never a leak:

* `withdraw` — the P6 escape hatch that replaced the re-submit refresh: an owner
  pulls their own PENDING submission back to draft, clearing the pin, the
  declaration and the lineage. The queue item is REMOVED, never replaced — an
  administrator mid-review sees it disappear rather than change underneath them.
* `status` — the owner-scoped lifecycle read.

The app ROW is minted by the build session (`build_sessions/appdata.resolve_app_for_project`),
not by a client call: the standalone `POST /apps/provision` and `GET /apps/{id}/source`
endpoints had zero production callers and were removed in U6.

Errors use the ported `{"error": {"message": ...}}` shape (`AppApiError`) the SPA
already consumes, not the auth endpoints' `{"detail": ...}`.
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
    """Load an app scoped to its owner, or fail closed with a non-leaking 404 (a
    cross-user id is indistinguishable from a missing one)."""
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
    """Pull the owner's own PENDING submission back out of the queue (P6, audited).

    pending→draft through the same guarded-UPDATE shape as the admin transitions —
    `STATUS_TRANSITIONS[DRAFT]` is the source set — plus the `user_id` ownership
    predicate the admin helper deliberately omits (an admin acts across owners; an
    owner must not). Zero rows updated is a refused withdrawal (409), never a no-op.

    What clears, and why: the submission pin (`source_submission_id` /
    `source_commit_sha` / `submitted_at`) — the queue item is REMOVED, not left
    dangling; the `declaration` — it described the withdrawn submission, and the next
    submit attaches a fresh one; and the LINEAGE (`approval_route` → NULL) — a
    withdrawn submission entered through a route that no longer describes it, and
    NULL is the documented "no current submission" state (`ApprovalRoute`'s NULL
    semantics), so a later submit stamps its own lineage rather than inheriting one.

    What deliberately survives: the APPROVED pin (`approved_submission_id` et al.) —
    same rule as reject: status governs liveness, the pin governs WHICH artifact —
    and the immutable submission BLOB, because submissions are retained and their ids
    never reused (R2); withdrawal removes the queue item, not the audit trail's
    artifact. An administrator racing this with an approve conflicts safely: their
    approve names a submission id the row no longer carries, updates zero rows → 409
    (purpose-written admin copy for that moment is U13's).
    """
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
            # The ownership predicate (ADR-0004) — a dropped user_id clause is a
            # cross-user leak, not a style nit.
            AppRegistry.user_id == user.id,
            AppRegistry.status.in_(tuple(STATUS_TRANSITIONS[AppStatus.DRAFT])),
        )
        .values(
            status=AppStatus.DRAFT,
            source_submission_id=None,
            source_commit_sha=None,
            submitted_at=None,
            approval_route=None,
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
    """Owner-scoped lifecycle read; an absent or cross-user app is the same non-leaking 404 its
    sibling `withdraw` returns.

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
