"""The classification review surface: ensure a review exists for the current saved
version, and read it — the browser is never the source of what the review said.

WHY THIS EXISTS

TWO PROJECT-SCOPED ROUTES, the deploy router's shape deliberately. The POST ENSURES: it
resolves the current saved commit and the live configuration and hands both to the service's
claim-or-return. An unchanged commit under unchanged class definitions comes back without a run;
a failed attempt is re-claimed by asking again on this SAME route — there is no separate retry
verb — to the three-runs cap; anything else claims a fresh run, detached. The route never waits
(a review takes minutes, the edge gives twenty seconds): it answers the current state, 202 in
flight and 200 when settled, and the client polls the GET, which reads and NEVER starts,
downloads, or writes. Both carry the live policy and classes the dialog scores with.

OWNERSHIP FIRST, inverting the deploy routes' unconfigured-503-first ordering: a cross-user
project id must be a non-leaking 404 EVEN when storage is unbound.

THE VERSION IS A METADATA QUESTION. Both routes resolve the saved commit from the snapshot
blob's `head_sha` stamp and its `last_modified` — one `head()` call — NEVER by extracting
the bundle, which downloads the whole thing before consulting its SHA-keyed cache. The GET
here is polled by a dialog open for up to a minute; only the detached runner extracts.

EVIDENCE NEVER LEAVES THE ROW, and neither does a class description. The response goes through
`ClassReview.all_of` (verdict + reason) and `ReviewClass` (key, title, kind, weight)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Response, status

from src.api.deps import CurrentUser, DbSession, OptionalStorage
from src.api.deps_csrf import RequireCsrf
from src.api.v1.classification.deps import ReviewService
from src.api.v1.classification.schemas import (
    ClassificationReviewResponse,
    ClassReview,
    ReviewClass,
    ReviewPolicy,
)
from src.core.errors import AppApiError
from src.db.models.classification_review import ClassificationReviewStatus
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.classification.config import LiveConfig, load_live_config
from src.services.classification.service import (
    FAIL_ABANDONED,
    FAIL_BUNDLE_UNREADABLE,
    FAIL_NO_APP,
    FAIL_REVIEW,
    FAIL_STORAGE,
    FAIL_VERSION_DRIFT,
    MAX_MODEL_RUNS_PER_VERSION,
)
from src.services.classification.store import ReviewRecord, is_for
from src.services.deploy.resolve import deploy_target
from src.services.projects.resolve import owned_project_or_404
from src.services.ratelimit import rate_limit
from src.services.storage import (
    ObjectStorage,
    StorageError,
    head_sha_from_metadata,
    snapshot_key,
)

_log = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["classification"])

_REVIEW_PATH = "/{project_id}/classification-review"

# THE REVIEW'S ONLY PER-USER SPEND BOUND, and the reason it needs one: review spend is
# deliberately exempt from the daily token gate (a heavy build day must not make an
# app unpublishable), and the stated bound, MAX_MODEL_RUNS_PER_VERSION, is per VERSION.
# A version costs one Save, so `save -> ensure -> save -> ensure` mints fresh runs
# forever: three runs x 25 requests x 8k tokens each, on the premium deployment, from a
# citizen who has already exhausted their build budget. Uncapped premium spend sits badly
# against a platform whose token meter is a stated client requirement.
#
# A REFUSAL HERE IS NOT A GATE HOLE. It only stops a review from being STARTED; with no
# complete review for the version the gate routes the app to an administrator. The failure
# direction is toward a human, never toward an unattended publish.
REVIEW_RATE_LIMIT = 12
REVIEW_RATE_WINDOW_SECONDS = 15 * 60


async def _review_rate_key(user: CurrentUser) -> str:
    # Per-user bucket, matching the feedback/attachment limiters. Declaring `CurrentUser`
    # here resolves identity BEFORE the limiter runs — the "limiter after key" ordering.
    return f"classification-review:{user.id}"


_review_limiter = rate_limit(
    _review_rate_key,
    limit=REVIEW_RATE_LIMIT,
    window_seconds=REVIEW_RATE_WINDOW_SECONDS,
    message=(
        "Too many automatic checks started in a short time. "
        "Please wait a few minutes and try again."
    ),
)

# The owner sentence for each stored bucket lives here — this module owns the copy, the stored
# `failure_code` stays the stable, greppable operator string — and an unknown code fails loudly
# at the subscript rather than rendering a sentence nobody wrote.
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
    """HEAD the snapshot blob — metadata only, never the bytes. `None` means nothing was ever
    saved. A store that will not answer raises the documented 503 instead of reporting the same
    `None`: folding the two together would render an unknown as an empty state, telling a citizen
    there is nothing to review while their saved app sits in a store that is merely unreachable."""
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


def _nothing_to_review(config: LiveConfig) -> ClassificationReviewResponse:
    """No saved code — no answers, and nothing for a review to read."""
    return ClassificationReviewResponse(
        status="nothing_to_review",
        policy=ReviewPolicy.of(config),
        classes=ReviewClass.in_display_order(config.classes),
    )


def _unreadable_stamp(
    app_id: uuid.UUID, saved: _SavedVersion, config: LiveConfig
) -> ClassificationReviewResponse:
    """A bundle with no `head_sha` stamp (written before the stamp existed). The commit
    cannot be resolved without downloading the whole bundle — exactly what these routes
    must never do — so no review can be claimed for it. Presented as the unreadable
    bucket (not retryable: asking again cannot mint a stamp; the next Save writes one),
    with no stored row behind it."""
    _log.warning("classification_review_bundle_has_no_stamp", app_id=str(app_id))
    return ClassificationReviewResponse(
        status="failed",
        policy=ReviewPolicy.of(config),
        classes=ReviewClass.in_display_order(config.classes),
        saved_at=saved.saved_at,
        failure_code=FAIL_BUNDLE_UNREADABLE,
        failure_message=_FAILURE_SENTENCES[FAIL_BUNDLE_UNREADABLE],
        retryable=False,
    )


def _presented(
    record: ReviewRecord, *, saved: _SavedVersion, config: LiveConfig, aged_out: bool
) -> ClassificationReviewResponse:
    """One stored row → the owner's view of it.

    A RUNNING row past the wall-clock ceiling (`aged_out`) is presented as the review-abandoned
    failure, never as still-in-flight, so the next ask un-wedges it. The row's own stamp rides
    as `reviewed_sha` even when it differs from the current `head_sha`; answers ride only on a
    current, complete review."""
    current = saved.head_sha is not None and is_for(
        record, head_sha=saved.head_sha, fingerprint=config.fingerprint
    )
    policy = ReviewPolicy.of(config)
    classes = ReviewClass.in_display_order(config.classes)
    if record.status is ClassificationReviewStatus.RUNNING and not aged_out:
        return ClassificationReviewResponse(
            status="running",
            policy=policy,
            classes=classes,
            head_sha=saved.head_sha,
            saved_at=saved.saved_at,
            reviewed_sha=record.head_sha,
            current=current,
        )
    if record.status is ClassificationReviewStatus.COMPLETE:
        if record.verdicts is None:
            # The store's terminal write always carries the document; a COMPLETE row
            # without one is a broken invariant, not a state to present around.
            raise RuntimeError(f"complete review {record.review_id} has no verdicts document")
        return ClassificationReviewResponse(
            status="complete",
            policy=policy,
            classes=classes,
            head_sha=saved.head_sha,
            saved_at=saved.saved_at,
            reviewed_sha=record.head_sha,
            checked_at=record.finished_at,
            current=current,
            verdicts=ClassReview.all_of(record.verdicts["classes"]) if current else None,
        )
    code = FAIL_ABANDONED if aged_out else record.failure_code
    if code is None:
        raise RuntimeError(f"failed review {record.review_id} carries no failure code")
    return ClassificationReviewResponse(
        status="failed",
        policy=policy,
        classes=classes,
        head_sha=saved.head_sha,
        saved_at=saved.saved_at,
        reviewed_sha=record.head_sha,
        checked_at=record.finished_at,
        current=current,
        failure_code=code,
        failure_message=_FAILURE_SENTENCES[code],
        retryable=_RETRYABLE[code] and record.attempt < MAX_MODEL_RUNS_PER_VERSION,
    )


@router.post(
    _REVIEW_PATH,
    response_model=ClassificationReviewResponse,
    dependencies=[RequireCsrf, Depends(_review_limiter)],
    responses={
        202: {
            "model": ClassificationReviewResponse,
            "description": "A review run is in flight — poll the GET for the result",
        },
        **error_responses(
            (403, ErrorEnvelope, "CSRF check failed"),
            AUTH_401,
            (404, ErrorEnvelope, "Project not found"),
            (429, ErrorEnvelope, "Too many review runs started — try again shortly"),
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

    Opening the publish dialog calls this. An unchanged version under unchanged class
    definitions gets the stored answers back with no run; anything else claims a fresh run,
    detached; a failed attempt is re-claimed by calling this same route again, until the
    service's attempt cap returns the stored failure instead. 202 says a run is in flight (poll
    the GET); 200 says the enclosed state is settled."""
    # The service resolves nothing itself: the CALLER owns the version question, answered here
    # from the blob's metadata stamp and one configuration read — and if a Save lands between
    # this read and the runner's extraction, the runner fails closed with `version_drift`.
    # Ownership before anything — a cross-user id is a non-leaking 404 even when
    # storage is unbound, so no storage (or service) question may precede this read.
    await owned_project_or_404(db, user.id, project_id)
    if storage is None:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _FAILURE_SENTENCES[FAIL_STORAGE],
            code=FAIL_STORAGE,
        )
    config = await load_live_config(db)
    # Read-only resolution, deliberately (the build path's resolver UPSERTS a draft
    # app row; a review request must not mint one).
    target = await deploy_target(db, user_id=user.id, project_id=project_id)
    if target is None:
        return _nothing_to_review(config)
    saved = await _saved_version(storage, target.app_id)
    if saved is None:
        return _nothing_to_review(config)
    if saved.head_sha is None:
        return _unreadable_stamp(target.app_id, saved, config)

    record = await service.start(
        db, app_id=target.app_id, user_id=user.id, head_sha=saved.head_sha, config=config
    )
    # `start` renews `started_at` on every claim, so a record it hands back cannot be
    # aged out; a stale RUNNING row on this path was already settled and re-claimed.
    presented = _presented(record, saved=saved, config=config, aged_out=False)
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
    config = await load_live_config(db)
    target = await deploy_target(db, user_id=user.id, project_id=project_id)
    if target is None:
        return _nothing_to_review(config)
    saved = await _saved_version(storage, target.app_id)
    if saved is None:
        return _nothing_to_review(config)
    if saved.head_sha is None:
        return _unreadable_stamp(target.app_id, saved, config)

    readout = await service.read(db, app_id=target.app_id)
    if readout is None:
        # Saved code, no review ever claimed — a normal state (the dialog's POST is
        # what claims one), answered with the version facts and no verdicts.
        return ClassificationReviewResponse(
            status="not_reviewed",
            policy=ReviewPolicy.of(config),
            classes=ReviewClass.in_display_order(config.classes),
            head_sha=saved.head_sha,
            saved_at=saved.saved_at,
        )
    return _presented(readout.review, saved=saved, config=config, aged_out=readout.aged_out)
