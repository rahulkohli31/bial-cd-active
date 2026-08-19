"""Submit an app into the admin approve queue — the extracted body of the retired
citizen `POST /apps/{app_id}/submit` route (U8: R15a, R15b, ASM9, ASM18).

The shipped ordering is preserved step for step, with each step's original rationale
kept at the step (do not reorder — every line of D3/D8/D9 reasoning still applies):

1. refuse while a build session is live (D8),
2. a non-authoritative status pre-check (D3's window-narrowing),
3. the submit's own fail-closed bundle read (D9),
4. bundle validation + HEAD SHA parse (R3/R4),
5. the blob copy BEFORE the row write (D3),
6. one guarded UPDATE carrying the `user_id` ownership predicate (ADR-0004),
   logging the orphan blob when the guard refuses after the copy landed.

ONE deliberate divergence from the shipped route — the build-session guard is
APP-scoped, not user-wide. The route's guard refused while the citizen was building
ANYTHING, and its own comment recorded that narrowing it was a separate call. That
coarse refusal was tolerable for a button beside the status card; it is not tolerable
now that this service is the only route into the queue: a citizen building project A
would be refused when publishing project B, on a screen where they pressed Publish,
while an unflagged app publishes fine in the same moment. The deploy route's own
guard was already app-scoped (`deploy/router.py` passes `app_id`), so submitting and
publishing now refuse on the same axis — the one D8 actually protects (this app's
snapshot being overwritten mid-copy). Do not restore the coarse call: the user-wide
refusal guarded nothing the app-scoped one does not, it only over-refused.

Two changes of policy travel with the extraction, both this feature's point:

* PENDING is no longer a legal submit source (R15b): re-submitting over an item an
  administrator may be reading is forbidden; the way out is withdrawal (P6), which
  removes the queue item rather than replacing it.
* The guarded UPDATE writes the submission's LINEAGE (`approval_route`) and the
  DECLARATION alongside the pin — this service is the only writer of both (U4 made
  the columns; the publish gate assembles the declaration dict and hands it over
  opaquely).

COMMIT-LESS, like the admin router's `_transition` and `append_audit` itself: every
write (the UPDATE and the audit row) lands in the caller's transaction, and the
caller owns the commit — so the gate's own decision record and the submit share one
fate. The blob copy is external and lands regardless; a caller that never commits
leaves the same accepted D3 orphan class as a crash between UPDATE and commit did on
the retired route.

Raises `AppApiError` with the retired route's exact statuses and copy (the withdraw
route and the publish gate surface them unchanged); the caller owns the 404 for an
absent/cross-user app, and this service re-checks ownership fail-closed anyway.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

# `live_build` is build-session domain logic parked under `api/v1/` for cycle-freedom
# (its docstring owns that story); importing it here adds no cycle — it never imports
# routers or `api.deps`.
from src.api.v1.live_build import refuse_while_build_session_live
from src.core.errors import AppApiError
from src.db.models.app_registry import (
    STATUS_TRANSITIONS,
    AppRegistry,
    ApprovalRoute,
    AppStatus,
)
from src.services.audit.log import append_audit
from src.services.storage import (
    BUNDLE_CONTENT_TYPE,
    BundleValidationError,
    ObjectStorage,
    StorageError,
    StorageNotFoundError,
    parse_bundle_head_sha,
    snapshot_key,
    submission_key,
)

_log = structlog.get_logger()

# A submit is legal ONLY from the pending-transition sources — draft, rejected,
# approved (the resubmit paths). PENDING itself is deliberately OUT of the set
# (R15b): the retired route treated a submit from pending as a refresh; overwriting
# an item an administrator may be mid-review on is exactly what R15b forbids, and the
# stranded-wrong-build case has withdrawal (P6) instead.
_SUBMIT_FROM = STATUS_TRANSITIONS[AppStatus.PENDING]

_ILLEGAL_STATE_MSG = "This app cannot be submitted in its current state."
_PENDING_MSG = "This app is already waiting for review — withdraw it to submit again."
_NO_BUNDLE_MSG = "Nothing to submit — generate an app first."
_STORAGE_DOWN_MSG = "Storage is temporarily unavailable. Please try again."
_BUILD_LIVE_SUBMIT_MSG = (
    "A build session is still running for this app — end it before submitting."
)


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    """Plain scalars from the guarded UPDATE (never an ORM instance across the
    caller's later commit) — what the gate reports back to the citizen."""

    submission_id: uuid.UUID
    commit_sha: str
    submitted_at: datetime


async def submit_app_for_review(
    db: AsyncSession,
    storage: ObjectStorage | None,
    *,
    user_id: uuid.UUID,
    app: AppRegistry,
    declaration: dict[str, Any],
    route: ApprovalRoute,
) -> SubmissionReceipt:
    """Fork an immutable copy of the app's bundle and move it to pending (audited),
    carrying `route` + `declaration` onto the row. Commit-less — the caller commits.

    `app` is the row the caller already resolved through its own owner-scoped 404;
    `declaration` is opaque here (the publish gate assembles it: both answer sets,
    the differences, the redacted explanation — R15).
    """
    # Fail-closed ownership re-check (ADR-0004): the caller's `_owned_app_or_404`
    # normally guarantees this, but a service that trusts its caller with the
    # ownership predicate is one refactor away from a cross-user write. Same
    # non-leaking 404 the resolvers return.
    if app.user_id != user_id:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "App not found.")

    # 1. D8 — never copy out from under a live build session: the copy would capture
    #    the PREVIOUS build's bundle (valid bytes, wrong tree — undetectable by any
    #    header check) or torn bytes under a concurrent finalize overwrite. APP-scoped
    #    (`app_id=`): the deliberate divergence documented in the module docstring.
    await refuse_while_build_session_live(
        user_id, conflict_message=_BUILD_LIVE_SUBMIT_MSG, app_id=app.id
    )

    # 2. Non-authoritative status pre-check on the already-owned row: narrows the
    #    orphan-blob window (D3) — the guarded UPDATE below is the real gate. A
    #    pending row gets its own copy: the remedy (withdraw, P6) is different from
    #    the dead-end statuses'.
    if app.status is AppStatus.PENDING:
        raise AppApiError(status.HTTP_409_CONFLICT, _PENDING_MSG)
    if app.status not in _SUBMIT_FROM:
        raise AppApiError(status.HTTP_409_CONFLICT, _ILLEGAL_STATE_MSG)

    # 3. Submit's OWN fail-closed read (D9) — deliberately NOT `_snapshot_exists`,
    #    whose transient-error-means-absent bias would tell someone whose app is
    #    fully built to go build it. Absent → 409; transient → 503. An UNCONFIGURED
    #    store arrives as `None` (the caller's None-tolerant `OptionalStorage`) and
    #    answers with the SAME documented 503 as a transient blip.
    if storage is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN_MSG)
    try:
        raw = await storage.get(snapshot_key(app.id))
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
    key = submission_key(app.id, submission_id)
    try:
        await storage.put(key, raw, content_type=BUNDLE_CONTENT_TYPE)
    except StorageError as exc:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN_MSG) from exc

    now = datetime.now(UTC)
    moved = await db.execute(
        sa.update(AppRegistry)
        .where(
            AppRegistry.id == app.id,
            # The ownership predicate (ADR-0004) — a dropped user_id clause is a
            # cross-user leak, not a style nit.
            AppRegistry.user_id == user_id,
            AppRegistry.status.in_(tuple(_SUBMIT_FROM)),
        )
        .values(
            status=AppStatus.PENDING,
            source_submission_id=submission_id,
            source_commit_sha=commit_sha,
            submitted_at=now,
            # The queue item carries how it got here and what was declared (U4's
            # columns; this service is their only writer).
            approval_route=route,
            declaration=declaration,
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
            app_id=str(app.id),
            key=key,
        )
        raise AppApiError(status.HTTP_409_CONFLICT, _ILLEGAL_STATE_MSG)

    await append_audit(
        db,
        actor_id=user_id,
        action="submit",
        resource_type="app",
        resource_id=str(app.id),
        detail={
            "submissionId": str(submission_id),
            "commitSha": commit_sha,
            "route": route.value,
        },
    )
    return SubmissionReceipt(submission_id=submission_id, commit_sha=commit_sha, submitted_at=now)
