"""The classification review surface (U7): ensure a review exists for the current saved
version, and read it — the browser is never the source of what the review said.

TWO PROJECT-SCOPED ROUTES, the deploy router's shape deliberately.
`POST /projects/{id}/classification-review` ENSURES: it resolves the current saved
commit and hands it to the service's claim-or-return — the stored answer comes back for
an unchanged version without a run (R6), a failed attempt is re-claimed by asking again
on this SAME route (R19's "ask again without re-saving"; there is no separate retry
verb), bounded by the service's three-runs-per-version cap, and a new version claims a
fresh run, detached. The route never waits for the run: a review can take up to two
minutes and the edge gives a request twenty seconds, so the answer is the current state
— 202 while a run is in flight, 200 when the state is settled — and the client polls
`GET .../classification-review`, which reads and NEVER starts, downloads, or writes.

OWNERSHIP FIRST — the opposite of the deploy routes' unconfigured-503-first ordering,
and the inversion is the plan's requirement rather than drift: a cross-user project id
must be a non-leaking 404 EVEN when storage is unbound, so the ownership read runs
before any service or storage question is asked.

THE VERSION IS A METADATA QUESTION. Both routes resolve the current saved commit from
the snapshot blob's stored `head_sha` stamp and its `last_modified` time — one `head()`
call, the save-state reader's exact move — NEVER by extracting the bundle. The extract
helper unconditionally downloads the whole bundle before consulting its SHA-keyed cache,
and the GET here is polled by a dialog that stays open for up to a minute; only the
detached runner extracts (and it fails closed with `version_drift` if the tree it pulls
turns out to be a different commit than the stamp claimed here).

EVIDENCE NEVER LEAVES THE ROW (R4/OD-B). The stored documents carry things the citizen
must not see — cited locations, the scan block, the per-question scan agreement and the
downgrade marker (the administrator's dispute presentation, U13's concern). The response
is built through `ReviewAnswers.of`, which projects verdict + reason and nothing else.

Storage is the one dependency that can be unconfigured, and it arrives through the
EXISTING shared `OptionalStorage` provider — a `None`-yielding seam, because an eagerly
raising provider resolves BEFORE the route body and would pre-empt the documented 503
with an undocumented 500 in the wrong envelope (see `deps.py` for both documented burns).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from fastapi import APIRouter, Response, status

from src.api.deps import CurrentUser, DbSession, OptionalStorage
from src.api.deps_csrf import RequireCsrf
from src.api.v1.classification.deps import ReviewService
from src.api.v1.classification.schemas import ClassificationReviewResponse, ReviewAnswers
from src.core.errors import AppApiError
from src.db.models.classification_review import ClassificationReviewStatus
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.classification.service import (
    FAIL_ABANDONED,
    FAIL_BUNDLE_UNREADABLE,
    FAIL_NO_APP,
    FAIL_REVIEW,
    FAIL_STORAGE,
    FAIL_VERSION_DRIFT,
    MAX_MODEL_RUNS_PER_VERSION,
)
from src.services.classification.store import ReviewRecord
from src.services.deploy.resolve import deploy_target
from src.services.projects.resolve import owned_project_or_404
from src.services.storage import (
    ObjectStorage,
    StorageError,
    head_sha_from_metadata,
    snapshot_key,
)

_log = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["classification"])

_REVIEW_PATH = "/{project_id}/classification-review"

# R19's "unavailable" is five distinct citizen-facing states (the plan's failure
# taxonomy) plus the drift code U6 added. The CITIZEN sentence for each stored bucket
# lives here — U7 owns the copy, the stored `failure_code` stays the stable, greppable
# operator string — and an unknown code fails loudly at the subscript rather than
# rendering a sentence nobody wrote.
_FAILURE_SENTENCES: Final[dict[str, str]] = {
    FAIL_NO_APP: "There's nothing saved to check yet — press Save first.",
    FAIL_BUNDLE_UNREADABLE: "Your saved app couldn't be read. Tell an administrator.",
    FAIL_STORAGE: "We can't reach your saved app right now. Please try again in a moment.",
    FAIL_REVIEW: "The automatic check couldn't run.",
    # The taxonomy's own rule: the same sentence as review-failed, a DISTINCT code.
    FAIL_ABANDONED: "The automatic check couldn't run.",
    FAIL_VERSION_DRIFT: (
        "Your app was saved again while the check was running, so the result doesn't "
        "match what's saved now. Ask for a fresh check."
    ),
}

# The taxonomy's "retry offered" column. An unreadable bundle cannot succeed twice and
# nothing-saved is fixed by Save, not by asking again; everything else is worth a
# re-check — including drift, where a re-ask claims a fresh review for the commit that
# actually exists now. The presented flag additionally respects the attempt cap: once
# the service will no longer run a model for this version, offering a re-check would
# offer a button that returns the same stored failure.
_RETRYABLE: Final[dict[str, bool]] = {
    FAIL_NO_APP: False,
    FAIL_BUNDLE_UNREADABLE: False,
    FAIL_STORAGE: True,
    FAIL_REVIEW: True,
    FAIL_ABANDONED: True,
    FAIL_VERSION_DRIFT: True,
}


@dataclass(frozen=True)
class _SavedVersion:
    """The snapshot blob's answer to "what is saved right now": the commit stamped in
    its user metadata (None for a bundle written before the stamp existed) and the
    store's own write time — the "version X, saved at Y" pair the dialog leads with."""

    head_sha: str | None
    saved_at: datetime | None


async def _saved_version(storage: ObjectStorage, app_id: uuid.UUID) -> _SavedVersion | None:
    """HEAD the snapshot blob — metadata only, never the bytes. None means nothing was
    ever saved (R21's nothing-to-review). A store that will not answer is the documented
    503, not "nothing saved": unknown must never render as an empty state, and per ASM21
    publishing reads the same bundle, so nobody is stranded behind this refusal."""
    try:
        meta = await storage.head(snapshot_key(app_id))
    except StorageError as exc:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _FAILURE_SENTENCES[FAIL_STORAGE],
            code=FAIL_STORAGE,
        ) from exc
    if meta is None:
        return None
    return _SavedVersion(
        head_sha=head_sha_from_metadata(meta.metadata), saved_at=meta.last_modified
    )


def _nothing_to_review() -> ClassificationReviewResponse:
    """R21: no saved code — no answers, and nothing for a review to read."""
    return ClassificationReviewResponse(status="nothing_to_review")


def _unreadable_stamp(app_id: uuid.UUID, saved: _SavedVersion) -> ClassificationReviewResponse:
    """A bundle with no `head_sha` stamp (written before the stamp existed). The commit
    cannot be resolved without downloading the whole bundle — exactly what these routes
    must never do — so no review can be claimed for it. Presented as the unreadable
    bucket (not retryable: asking again cannot mint a stamp; the next Save writes one),
    with no stored row behind it."""
    _log.warning("classification_review_bundle_has_no_stamp", app_id=str(app_id))
    return ClassificationReviewResponse(
        status="failed",
        saved_at=saved.saved_at,
        verdicts=ReviewAnswers.all_unanswered(),
        failure_code=FAIL_BUNDLE_UNREADABLE,
        failure_message=_FAILURE_SENTENCES[FAIL_BUNDLE_UNREADABLE],
        retryable=False,
    )


def _presented(
    record: ReviewRecord, *, saved: _SavedVersion, aged_out: bool
) -> ClassificationReviewResponse:
    """One stored row → the citizen's view of it.

    A RUNNING row past the wall-clock ceiling (`aged_out`) is presented as the
    review-abandoned failure, never as still-in-flight: a restart kills the detached
    runner but leaves the row RUNNING, and `start` un-wedges it on the next ask — so
    the presentation must invite that ask rather than show an immortal spinner.

    The row's own stamp rides as `reviewed_sha` even when it differs from the current
    `head_sha`: U11 filters by the stamp it asked for, so surfacing both is what lets
    the client ignore an answer about a version this dialog never named."""
    if record.status is ClassificationReviewStatus.RUNNING and not aged_out:
        return ClassificationReviewResponse(
            status="running",
            head_sha=saved.head_sha,
            saved_at=saved.saved_at,
            reviewed_sha=record.head_sha,
        )
    if record.status is ClassificationReviewStatus.COMPLETE:
        if record.verdicts is None:
            # The store's terminal write always carries the document; a COMPLETE row
            # without one is a broken invariant, not a state to present around.
            raise RuntimeError(f"complete review {record.review_id} has no verdicts document")
        return ClassificationReviewResponse(
            status="complete",
            head_sha=saved.head_sha,
            saved_at=saved.saved_at,
            reviewed_sha=record.head_sha,
            verdicts=ReviewAnswers.of(record.verdicts["questions"]),
        )
    code = FAIL_ABANDONED if aged_out else record.failure_code
    if code is None:
        raise RuntimeError(f"failed review {record.review_id} carries no failure code")
    # A failed row usually carries no verdicts (a failure is never stored as an answer,
    # R19) and presents as six unanswered questions. The one exception is the Tier A
    # floor (P8): the model never returned, but a complete scan holds a high-confidence
    # credential hit strong enough to stand as the credentials answer — stored on the
    # row, projected here like any other answer set.
    verdicts = (
        ReviewAnswers.of(record.verdicts["questions"])
        if record.verdicts is not None
        else ReviewAnswers.all_unanswered()
    )
    return ClassificationReviewResponse(
        status="failed",
        head_sha=saved.head_sha,
        saved_at=saved.saved_at,
        reviewed_sha=record.head_sha,
        verdicts=verdicts,
        failure_code=code,
        failure_message=_FAILURE_SENTENCES[code],
        retryable=_RETRYABLE[code] and record.attempt < MAX_MODEL_RUNS_PER_VERSION,
    )


@router.post(
    _REVIEW_PATH,
    response_model=ClassificationReviewResponse,
    dependencies=[RequireCsrf],
    responses={
        202: {
            "model": ClassificationReviewResponse,
            "description": "A review run is in flight — poll the GET for the result",
        },
        **error_responses(
            (403, ErrorEnvelope, "CSRF check failed"),
            AUTH_401,
            (404, ErrorEnvelope, "Project not found"),
            (503, ErrorEnvelope, "Object storage is unavailable — and so is publishing"),
        ),
    },
)
async def ensure_review(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    storage: OptionalStorage,
    service: ReviewService,
    response: Response,
) -> ClassificationReviewResponse:
    """Ensure a review exists for the app's current saved version, and answer with it.

    Opening the publish dialog calls this. An unchanged version gets the stored answers
    back with no run (R6); a version the stored row does not match claims a fresh run,
    detached; a failed attempt is re-claimed by calling this same route again, until the
    service's per-version attempt cap returns the stored failure instead. 202 says a run
    is in flight (poll the GET); 200 says the enclosed state is settled.

    The service resolves nothing itself: the CALLER owns the version question, answered
    here from the blob's metadata stamp — and if a Save lands between this read and the
    runner's extraction, the runner fails closed with `version_drift` rather than
    reviewing a tree this route never named."""
    # Ownership before anything — a cross-user id is a non-leaking 404 even when
    # storage is unbound, so no storage (or service) question may precede this read.
    await owned_project_or_404(db, user.id, project_id)
    if storage is None:
        # The in-body 503 seam (see the module docstring): storage-off is a supported
        # posture outside production, not a deploy bug.
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _FAILURE_SENTENCES[FAIL_STORAGE],
            code=FAIL_STORAGE,
        )
    # Read-only resolution, deliberately (the build path's resolver UPSERTS a draft
    # app row; a review request must not mint one).
    target = await deploy_target(db, user_id=user.id, project_id=project_id)
    if target is None:
        return _nothing_to_review()
    saved = await _saved_version(storage, target.app_id)
    if saved is None:
        return _nothing_to_review()
    if saved.head_sha is None:
        return _unreadable_stamp(target.app_id, saved)

    record = await service.start(
        db, app_id=target.app_id, user_id=user.id, head_sha=saved.head_sha
    )
    # `start` renews `started_at` on every claim, so a record it hands back cannot be
    # aged out; a stale RUNNING row on this path was already settled and re-claimed.
    presented = _presented(record, saved=saved, aged_out=False)
    if presented.status == "running":
        response.status_code = status.HTTP_202_ACCEPTED
    return presented


@router.get(
    _REVIEW_PATH,
    response_model=ClassificationReviewResponse,
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (503, ErrorEnvelope, "Object storage is unavailable — and so is publishing"),
    ),
)
async def read_review(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    storage: OptionalStorage,
    service: ReviewService,
) -> ClassificationReviewResponse:
    """The current review state — what the dialog polls while a run is in flight.

    READS ONLY: never starts a run, never writes a row, and never touches the bundle's
    bytes — the one storage call is the metadata `head()`, because this is polled every
    few seconds by a dialog that can stay open a minute, and pulling the app's whole
    tree per poll is exactly what the metadata stamp exists to avoid."""
    await owned_project_or_404(db, user.id, project_id)
    if storage is None:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _FAILURE_SENTENCES[FAIL_STORAGE],
            code=FAIL_STORAGE,
        )
    target = await deploy_target(db, user_id=user.id, project_id=project_id)
    if target is None:
        return _nothing_to_review()
    saved = await _saved_version(storage, target.app_id)
    if saved is None:
        return _nothing_to_review()
    if saved.head_sha is None:
        return _unreadable_stamp(target.app_id, saved)

    readout = await service.read(db, app_id=target.app_id)
    if readout is None:
        # Saved code, no review ever claimed — a normal state (the dialog's POST is
        # what claims one), answered with the version facts and no verdicts.
        return ClassificationReviewResponse(
            status="not_reviewed", head_sha=saved.head_sha, saved_at=saved.saved_at
        )
    return _presented(readout.review, saved=saved, aged_out=readout.aged_out)
