"""Request/response bodies for one-click deploy.

The response deliberately carries BOTH the machine-readable failure code and the prose: a
client needs the code to decide what to offer next (retry, open the chat, tell an admin),
and the citizen needs the sentence. Reporting only one of the two has to be worked around
later by whichever consumer was left short.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field

from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.classification_config import MAX_CLASS_KEY
from src.db.models.deployment import Deployment, DeploymentStatus
from src.schemas import CamelModel
from src.services.deploy.service import (
    FAIL_BUILD,
    FAIL_BUILD_UNAVAILABLE,
    FAIL_CONTEXT_TOO_LARGE,
    FAIL_INTERNAL,
    FAIL_NO_SNAPSHOT,
    FAIL_NOT_HEALTHY,
    FAIL_NOT_READY,
    FAIL_PROVISION,
    FAIL_RESTART,
    FAIL_RESTART_NOT_READY,
    FAIL_ROUTED_FOR_REVIEW,
    FAIL_SNAPSHOT_CORRUPT,
    FAIL_SNAPSHOT_MOVED,
    FAIL_SNAPSHOT_UNREADABLE,
    FAIL_STORAGE,
)
from src.services.deploy.store import INTERRUPTED

ClassKey = Annotated[str, Field(min_length=1, max_length=MAX_CLASS_KEY)]


class DeployRequest(CamelModel):
    """`commitSha` names the version the owner acted on: the saved version they reviewed, or
    the approved commit while its copy is on offer (`approvedRetryCommit`), which republishes
    the copy. Any other commit is refused.

    `answers` are the owner's Yes/No answers keyed by class key. They count only for scored
    classes, only while owners may change the reviewer's answers, and a class left out keeps
    the reviewer's answer; a key that is no active class is refused. There is no review field:
    the gate reads the stored review, and unknown body keys are dropped. `note` is required
    whenever the send goes to an administrator."""

    commit_sha: str = Field(min_length=1, max_length=64)
    answers: dict[ClassKey, bool] = Field(default_factory=dict)
    # Bounded at the boundary the way admin's `RejectRequest.note` is — an over-long note is
    # rejected, never silently truncated into a record that misrepresents what was said.
    note: str | None = Field(default=None, max_length=1000)


class DeployStartedResponse(CamelModel):
    """The 202 body. Carries the id to poll — the deploy itself has barely begun.

    `outcome` is the discriminator against `DeployRoutedResponse` (one POST, two
    success shapes): a client switches on it rather than sniffing which keys
    happen to be present. Additive for existing clients, which read `status`."""

    outcome: Literal["started"] = "started"
    deployment_id: str
    app_id: str
    status: str


class DeployRoutedResponse(CamelModel):
    """The 200 body when the publish gate ROUTES the app to an administrator instead
    of deploying (a hard block answered Yes, an unfinished review, a standing rejection, or
    a score over the threshold).
    An OUTCOME, not a failure — this renders as an informational state (the app is
    waiting in the queue, pinned to `commit_sha`) and must never paint the red failure
    badge over it: the platform did exactly what it said it would. Wire shape
    (camelCase): `{"outcome": "routed_for_review", "appId", "submissionId",
    "commitSha", "submittedAt", "message"}`."""

    outcome: Literal["routed_for_review"] = "routed_for_review"
    app_id: str
    submission_id: str
    commit_sha: str
    submitted_at: datetime
    # The citizen-facing sentence, so both publish surfaces render the same words
    # without owning copy of their own.
    message: str


class RestartStartedResponse(CamelModel):
    """The 202 body of an owner's restart. Carries the id to poll and nothing else it could
    be wrong about: recycling a revision runs for minutes, so the state the citizen watches
    comes back through `GET /v1/projects/{id}/deployment` like every other deploy state —
    one source for "what is it doing now", never a second channel."""

    deployment_id: str
    app_id: str
    status: str


class TakedownResponse(CamelModel):
    """An owner's take-down: which deployment left production, and when.

    Take-down is NOT delete and NOT the administrator's kill-switch — the container goes and
    every artefact stays — so `message` carries the sentence that says so, the way
    `DeployRoutedResponse` does, rather than leaving each surface to write its own. It also
    says when a version is still sitting in the review queue, because a take-down withdraws
    nothing and an owner should not have to discover that."""

    app_id: str
    deployment_id: str
    unpublished_at: datetime
    message: str


class UnpublishResponse(CamelModel):
    """The admin kill-switch's response — the deployment that was taken down (or
    already was, on an idempotent repeat) and when."""

    app_id: str
    deployment_id: str
    unpublished_at: datetime


class ApprovalState(CamelModel):
    """The app's APPROVAL lifecycle, carried on the deploy status response.
    RIDES HERE, NOT A SECOND CALL: the toolbar publish surface has a project id and no
    app id, so an app-scoped read isn't addressable there. Both surfaces poll this
    response through one hook, so hanging approval off it means inheriting that hook's
    staleness/refresh handling, not growing a second, fetch-once-and-rot lifetime.
    `submitted_sha`/`submitted_at` describe what's in the QUEUE; `approved_commit_sha` is the
    version the administrator approved."""

    status: AppStatus
    # NULL is a real state, not a gap: a never-approved app has no pin.
    approved_commit_sha: str | None = None
    # WHEN the administrator approved, beside WHICH commit they approved. The pin alone
    # cannot be rendered to a citizen — the status chip names the date first and mutes
    # the build code beside it, because a date is the thing a person recognises. Costs
    # nothing: `approved_at` is a column on the registry row this response already
    # selects in full, so surfacing it adds no query and no I/O. NULL means never
    # approved, exactly as `approved_commit_sha` does — the two are written together in
    # one place (`admin/router.py`'s `approve`) and are never apart.
    approved_at: datetime | None = None
    rejection_note: str | None = None
    submitted_sha: str | None = None
    submitted_at: datetime | None = None

    @classmethod
    def of(cls, row: AppRegistry) -> ApprovalState:
        return cls(
            status=row.status,
            approved_commit_sha=row.approved_commit_sha,
            approved_at=row.approved_at,
            rejection_note=row.rejection_note,
            submitted_sha=row.source_commit_sha,
            submitted_at=row.submitted_at,
        )


class PublishState(StrEnum):
    """THE single publish state the status chip renders — eleven values, authored here
    and nowhere else, so no client recombines `status` + `unpublished_at` + `failure_code`
    + the pin to guess at a state the server already knows. An
    **API** StrEnum, like `PreviewLifeState`: nothing persists it, the wire value equals
    the member's own string, and the chip's narrowing throws on anything it doesn't
    recognise — so this is the one place a new member gets added.
    UNKNOWN IS NEVER "UP TO DATE" — `LIVE_DRIFT_UNKNOWN` covers a storage HEAD that could
    not answer, the same tri-state discipline `SaveState.dirty` uses (`null` != clean)."""

    # No app row for the project at all — the only member with no approval block.
    NOTHING_BUILT = "nothing_built"
    # An app row exists; nothing has ever been submitted or deployed.
    DRAFT = "draft"
    # Submitted and awaiting an administrator, OR an older deployment row settled FAILED
    # with the routed code.
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    STARTING_UP = "starting_up"
    # Serving, and the saved snapshot's head matches the commit that went live.
    LIVE_CURRENT = "live_current"
    # Serving, and whether newer work exists could not be determined — a storage HEAD
    # that would not answer, or a bundle saved before the metadata stamp existed.
    LIVE_DRIFT_UNKNOWN = "live_drift_unknown"
    # Serving, and the saved snapshot (or the last submitted commit) differs from what
    # is live.
    LIVE_NEWER_WORK = "live_newer_work"
    # A SECOND AXIS layered onto a deployment row, not a status of the row itself
    # (`Deployment`'s own module docstring) — an administrator took a live app down.
    TAKEN_OFFLINE = "taken_offline"
    # `AppStatus.DISABLED` — a different remedy from `TAKEN_OFFLINE`, and both durable.
    SWITCHED_OFF = "switched_off"
    # The newest deployment failed with a code that is NOT one of the routed ones, or an
    # approved copy has not been attempted since its approval.
    DID_NOT_START = "did_not_start"


class SavedState(StrEnum):
    """WHY `saved_head`/`saved_at` are absent, which the two nulls cannot say themselves.

    THE SENTINEL WAS ONE VALUE FOR THREE FACTS. `_saved_version_for_publish_state`
    answered `head=None, saved_at=None` when the store was not configured, when its HEAD
    raised, AND when there was simply no bundle — three situations that are the same
    answer to "is there newer work" (`LIVE_DRIFT_UNKNOWN`, and rightly so: unknown is
    never spelled "up to date") and three DIFFERENT answers to "has this citizen ever
    saved". The rail could only render the union of them, so it told a citizen who had
    never saved that their last save could not be found — on the panel they open
    precisely when they are unsure their work is safe.

    So the drift question keeps reading one value and this axis is reported separately.
    Only `NEVER_SAVED` is a claim about the citizen's work; the other two are claims
    about the platform's own reach, and a client must not present them as the same thing.

    An **API** StrEnum like `PublishState` above: nothing persists it, and the wire value
    equals the member's own string."""

    # A bundle exists at the citizen's save key. Either half of the pair may still be
    # null — an unstamped bundle knows WHEN without knowing WHICH — and that is a saved
    # app whose version is unknown, never an unsaved one.
    SAVED = "saved"
    # The store answered, and there is no bundle: nothing has ever been saved. The one
    # member on which the rail omits its row entirely.
    NEVER_SAVED = "never_saved"
    # No object store is bound to this deployment — a supported dev/test posture this
    # whole route already accommodates. The platform cannot see the citizen's saves at
    # all; it must not report that as their absence.
    STORE_UNCONFIGURED = "store_unconfigured"
    # The store was asked and would not answer. Retrying can help, which is exactly what
    # distinguishes it from the two above.
    STORAGE_ERROR = "storage_error"


# Older rows only: nothing writes the routed code now, and those rows still read as in review.
_ROUTED_FAILURE_CODES: frozenset[str] = frozenset({FAIL_ROUTED_FOR_REVIEW})

# THE CODES A FAILED RESTART SETTLES UNDER, and the reason they need naming here: a restart
# claims a row of its own, so one that fails leaves a `failed` row newer than the attempt that
# published the container still serving. Read as a production fact it says the app did not
# start, while `liveness.live_app_ids` — which reads the last attempt that actually published —
# goes on showing the same app live. Two readers, one app, opposite answers.
#
# Same shape as the routed codes above, for the same reason: an attempt's ending is not always
# a statement about production. `service.py` owns the strings; this is a reader's copy, exactly
# as the routed set is.
_RESTART_FAILURE_CODES: frozenset[str] = frozenset({FAIL_RESTART, FAIL_RESTART_NOT_READY})


def _live_state(app: AppRegistry, deployment: Deployment, saved_head: str | None) -> PublishState:
    """WHICH of the three live readings applies — the drift comparison, and nothing else.

    AGAINST THE COMMIT THAT ACTUALLY WENT LIVE, never `approved_commit_sha`: that pin is NULL
    for every app published unattended, with no administrator, so comparing against it would read
    every one of those apps as unknown. `saved_head` is the primary signal. When it cannot be
    read, `source_commit_sha` (the last SUBMITTED commit, moved only by submit/withdraw — never by
    a Save, nor by a version the gate publishes without an administrator) reads as newer work
    only when it was submitted after this deployment was created. An older submission differs
    from what is live because it is older, and says nothing about newer work."""
    if saved_head is not None:
        return (
            PublishState.LIVE_CURRENT
            if saved_head == deployment.head_sha
            else PublishState.LIVE_NEWER_WORK
        )
    if (
        app.source_commit_sha is not None
        and app.source_commit_sha != deployment.head_sha
        and app.submitted_at is not None
        and app.submitted_at > deployment.created_at
    ):
        return PublishState.LIVE_NEWER_WORK
    return PublishState.LIVE_DRIFT_UNKNOWN


@dataclass(frozen=True)
class ApprovedCopy:
    """The submission copy an administrator approved, which the one button republishes."""

    submission_id: uuid.UUID
    commit_sha: str


def approved_copy(app: AppRegistry) -> ApprovedCopy | None:
    """The approved copy on record, or None. An app approved before copies were kept has
    nothing to republish and is a draft again."""
    if (
        app.status is not AppStatus.APPROVED
        or app.approved_submission_id is None
        or app.approved_commit_sha is None
    ):
        return None
    return ApprovedCopy(
        submission_id=app.approved_submission_id, commit_sha=app.approved_commit_sha
    )


def _attempted_since_approval(app: AppRegistry, deployment: Deployment | None) -> bool:
    """Whether `deployment` was claimed in or after the approving transaction. `approve`
    writes `approved_at` as the transaction's own `now()`, the instant its claim stamps on
    the row it starts, so that attempt counts and one still running from before does not."""
    return (
        deployment is not None
        and app.approved_at is not None
        and deployment.created_at >= app.approved_at
    )


def compute_publish_state(
    app: AppRegistry, deployment: Deployment | None, saved_head: str | None
) -> PublishState:
    """THE pure mapping: three plain values in, one `PublishState` out — no I/O, so it
    can't acquire a hidden input later; the one metadata HEAD it depends on is read by the
    CALLER, which turns a storage failure into `saved_head=None` before this ever sees it.
    ORDER IS THE POLICY: `DISABLED`/`PENDING` win outright over the deployment row —
    an admin's lockout or a pending submission is the most current fact, and must not be
    masked by an OLDER row in the append-only `deployments` table. An approved copy nobody
    has attempted since its approval reads `did_not_start`, because its one button publishes
    that copy; once attempted, the attempt's own row speaks for it."""
    if app.status is AppStatus.DISABLED:
        return PublishState.SWITCHED_OFF
    if app.status is AppStatus.PENDING:
        return PublishState.IN_REVIEW
    if app.status is AppStatus.REJECTED:
        return PublishState.CHANGES_REQUESTED
    if approved_copy(app) is not None and not _attempted_since_approval(app, deployment):
        return PublishState.DID_NOT_START
    if deployment is None:
        return PublishState.DRAFT
    if deployment.unpublished_at is not None:
        # AN OWNER TOOK IT DOWN, and that is a fact about PRODUCTION — not about the attempt the
        # stamp happened to land on. It sits above all three status arms because a take-down can
        # land on any of them: a restart that was failing at the time left a FAILED row, and
        # reading the attempt first told the owner "Could not restart" about an application they
        # had just removed from production themselves, with neither Publish nor Take down offered
        # beside it. The same distinction the restart arm below draws, made once and earlier.
        return PublishState.TAKEN_OFFLINE
    if deployment.status is DeploymentStatus.RUNNING:
        return PublishState.STARTING_UP
    if deployment.status is DeploymentStatus.FAILED:
        # A FAILED RESTART IS NOT A FAILED PUBLISH. A restart claims a row of its own and runs
        # only on an app that was already serving, so a failure here leaves the PREVIOUS
        # revision standing — `head_sha` was copied onto this row from the version that
        # published it, which is what lets the drift comparison below still answer. Reading it
        # as `did_not_start` told an owner their app had not started while the lists beside it
        # showed the same app live, and withheld Take down from the one surface that offers it.
        #
        # The failure is not swallowed: the row still carries its code and its citizen sentence,
        # and the production surface states them beside a status that is true.
        if deployment.failure_code in _RESTART_FAILURE_CODES and deployment.head_sha is not None:
            return _live_state(app, deployment, saved_head)
        if deployment.failure_code in _ROUTED_FAILURE_CODES:
            return PublishState.IN_REVIEW
        return PublishState.DID_NOT_START
    # `DeploymentStatus.SUCCEEDED` — the only member left, and un-stamped, so it is serving.
    return _live_state(app, deployment, saved_head)


class RegistryStatus(StrEnum):
    """The App Registry's status column. An **API** StrEnum like `PublishState`: nothing
    persists it, and the wire value equals the member's own string."""

    DRAFT = "draft"
    WAITING_FOR_REVIEW = "waiting_for_review"
    REJECTED = "rejected"
    NOT_PUBLISHED = "not_published"
    PUBLISHING = "publishing"
    LIVE = "live"
    PUBLISH_FAILED = "publish_failed"
    TAKEN_OFFLINE = "taken_offline"
    DISABLED = "disabled"


# No `NOTHING_BUILT`: that state is a project with no app row, and this reads an app row.
_REGISTRY_STATUS_OF: dict[PublishState, RegistryStatus] = {
    PublishState.DRAFT: RegistryStatus.DRAFT,
    PublishState.IN_REVIEW: RegistryStatus.WAITING_FOR_REVIEW,
    PublishState.CHANGES_REQUESTED: RegistryStatus.REJECTED,
    PublishState.STARTING_UP: RegistryStatus.PUBLISHING,
    PublishState.LIVE_CURRENT: RegistryStatus.LIVE,
    PublishState.LIVE_DRIFT_UNKNOWN: RegistryStatus.LIVE,
    PublishState.LIVE_NEWER_WORK: RegistryStatus.LIVE,
    PublishState.TAKEN_OFFLINE: RegistryStatus.TAKEN_OFFLINE,
    PublishState.SWITCHED_OFF: RegistryStatus.DISABLED,
    PublishState.DID_NOT_START: RegistryStatus.PUBLISH_FAILED,
}


def compute_registry_status(app: AppRegistry, deployment: Deployment | None) -> RegistryStatus:
    """The owner's publish state, read with no saved head, since every live reading is Live
    here. The owner sees one did-not-start state where an administrator sees two: an approved
    copy nobody has tried to publish yet, and a publish that failed."""
    if approved_copy(app) is not None and not _attempted_since_approval(app, deployment):
        return RegistryStatus.NOT_PUBLISHED
    return _REGISTRY_STATUS_OF[compute_publish_state(app, deployment, saved_head=None)]


# WHICH FAILED PUBLISHES OF THE APPROVED COPY ARE OFFERED AGAIN. Where the platform failed, the
# copy is offered again. Where the copy failed in itself, it fails the same way on every retry, so
# the one button acts on the saved version instead, which is where the owner can send a fix.
# Restarts and older routed rows are neither: they are not failed publishes.
_RETRYABLE_FAILURE_CODES: frozenset[str] = frozenset(
    {
        INTERRUPTED,
        FAIL_INTERNAL,
        FAIL_PROVISION,
        FAIL_STORAGE,
        FAIL_SNAPSHOT_UNREADABLE,
        FAIL_BUILD_UNAVAILABLE,
        FAIL_NOT_READY,
    }
)
_NON_RETRYABLE_FAILURE_CODES: frozenset[str] = frozenset(
    {
        FAIL_NO_SNAPSHOT,
        FAIL_SNAPSHOT_CORRUPT,
        FAIL_SNAPSHOT_MOVED,
        FAIL_CONTEXT_TOO_LARGE,
        FAIL_BUILD,
        FAIL_NOT_HEALTHY,
    }
)
# Every code a failed publish settles under.
PUBLISH_FAILURE_CODES: frozenset[str] = _RETRYABLE_FAILURE_CODES | _NON_RETRYABLE_FAILURE_CODES

# Failed publishes of the approved copy after which it is no longer offered: a copy's own fault
# misread as the platform's would otherwise be offered for ever. Past the cap the one button acts
# on the saved version, as after the copy's own fault.
MAX_APPROVED_COPY_ATTEMPTS: Final = 3


def _retryable_failure(deployment: Deployment) -> bool:
    """A publish that failed for a reason a retry can fix."""
    return (
        deployment.status is DeploymentStatus.FAILED
        and deployment.failure_code in _RETRYABLE_FAILURE_CODES
    )


def retry_needs_copy_failures(app: AppRegistry, deployment: Deployment | None) -> bool:
    """Whether `approved_retry_commit` has to know how many publishes of the approved copy have
    failed since approval: the newest attempt since then failed for a reason a retry can fix."""
    return (
        approved_copy(app) is not None
        and deployment is not None
        and _attempted_since_approval(app, deployment)
        and deployment.unpublished_at is None
        and _retryable_failure(deployment)
    )


def retry_needs_last_publish(app: AppRegistry, deployment: Deployment | None) -> bool:
    """Whether `approved_retry_commit` has to know if the approved copy went live: the newest
    attempt since approval failed, for a reason a retry can fix, before it could name the commit
    it was shipping, so it may have been the approved copy, or a later version sent after the
    copy went live."""
    return (
        retry_needs_copy_failures(app, deployment)
        and deployment is not None
        and deployment.head_sha is None
    )


def published_since_approval(app: AppRegistry, published: Deployment | None) -> bool:
    """Whether `published` — the newest attempt that put something live — came at or after
    the approval, which is when the approved copy is what it put there."""
    return _attempted_since_approval(app, published)


def approved_retry_commit(
    app: AppRegistry,
    deployment: Deployment | None,
    *,
    approved_went_live: bool,
    copy_failures: int,
) -> str | None:
    """The approved commit when the one button republishes the approved copy, else None.

    That is while the copy has not gone live since approval — nothing attempted yet, or a failure
    a retry can fix while fewer than `MAX_APPROVED_COPY_ATTEMPTS` publishes of it have failed
    (`copy_failures`) — and when the version taken offline is the approved one and did not fail in
    itself. A failure that named another commit goes through the gate; one that named none is an
    attempt at the copy only if nothing went live since approval (`approved_went_live`). Rule 3
    republishes exactly when this offers the commit it was sent."""
    copy = approved_copy(app)
    if copy is None:
        return None
    approved = copy.commit_sha
    if deployment is None or not _attempted_since_approval(app, deployment):
        return approved
    if deployment.unpublished_at is not None:
        # A takedown stamps the newest attempt whatever its ending, including a copy that failed
        # in itself, which is not offered back.
        failed_in_itself = deployment.failure_code in _NON_RETRYABLE_FAILURE_CODES
        return approved if deployment.head_sha == approved and not failed_in_itself else None
    if not _retryable_failure(deployment) or copy_failures >= MAX_APPROVED_COPY_ATTEMPTS:
        return None
    if deployment.head_sha == approved:
        return approved
    if deployment.head_sha is None and not approved_went_live:
        return approved
    return None


class DeploymentResponse(CamelModel):
    """The latest deploy attempt, or an empty envelope when there has never been one.

    Empty rather than a 404: "this app has never been deployed" is a normal state a client
    renders as a Deploy button, not an error.
    `publish_state` is the one field a client should actually branch on — see
    `PublishState`. Every other field here still rides along for the status chip's own
    rendering (the URL, the timestamps, the raw approval block), but none of them needs to be
    recombined to name a state; that work is already done."""

    deployment_id: str | None = None
    app_id: str | None = None
    status: str | None = None
    step: str | None = None
    url: str | None = None
    head_sha: str | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # WHETHER IT IS STILL LIVE — the second axis, and the only thing that separates "this
    # deploy succeeded and is serving traffic" from "an administrator took it down".
    # `status` cannot express the difference: an unpublished deployment stays `succeeded`,
    # because that is still how the attempt ended. A client rendering a live-app link MUST
    # test this as well, or it shows a citizen a URL that 404s with nothing to explain why.
    unpublished_at: datetime | None = None
    # The app's approval lifecycle. NULL has ONE defined meaning: this project has
    # no app row yet, so there is no lifecycle to report — never "we didn't look".
    approval: ApprovalState | None = None
    # THE one computed publish state. No default: every construction site names it
    # explicitly, the same fail-first posture `Settings` takes on a required field —
    # forgetting it should be a type error, not a value that quietly means nothing.
    publish_state: PublishState
    # The commit the one button posts when it republishes the approved copy — with no save,
    # no review and no dialog. Null whenever the button acts on the saved version instead.
    approved_retry_commit: str | None
    # THE CITIZEN'S OWN LAST SAVE, which `publish_state` until now only ever consumed
    # and threw away. The rail draws a "YOUR LATEST <date> <short id>" row, and both halves
    # of it come from the ONE metadata HEAD `latest_deployment` already takes — no second
    # call, and no container: a project whose workspace is stopped still answers, which is
    # the entire reason this rides here rather than on `save-state` (that read attaches to a
    # container first, so it is null in exactly the reclaimed case the row exists for).
    #
    # HONESTLY NULLABLE, AND THE TWO HALVES ARE INDEPENDENT. `saved_head` is NULL when the
    # bundle predates the metadata stamp, when the store would not answer, or when nothing
    # has ever been saved — the same "no claim" `head_sha_from_metadata` documents, which a
    # client renders as "cannot tell" and NEVER as a version. `saved_at` is the store's own
    # last-modified on that same object, so a stamp-less bundle can still say WHEN while
    # declining to say WHICH. Neither is ever invented: there is no placeholder that would
    # make a missing save look present.
    #
    # NO COUNT RIDES BESIDE THEM, and none can: `snapshot_key` is
    # overwrite-latest with one bundle per app and there is no version-history table, so
    # "4 newer saves" has no source anywhere in this process. The chip says newer work
    # exists; it does not count it. A count waits for save history.
    #
    # No defaults, for the reason `publish_state` above has none: three construction sites,
    # all in `latest_deployment`, and one that forgot would silently wire "nothing saved"
    # onto a project that has saved — which is the exact defect the nullability exists to
    # avoid, arriving through the back door.
    saved_head: str | None
    saved_at: datetime | None
    # WHY THE PAIR ABOVE IS ABSENT — see `SavedState`. The two nulls above are
    # reached four ways and only one of them means "this citizen has never saved"; without
    # this field a client rendering their absence has to speak all four with one sentence,
    # and the sentence it chose ("we could not tell") is false in the frightening direction
    # on the one case that matters most. No default, for the same reason `publish_state`
    # and the pair above have none.
    saved_state: SavedState

    @classmethod
    def of(
        cls,
        row: Deployment,
        *,
        approval: ApprovalState | None = None,
        publish_state: PublishState,
        approved_retry_commit: str | None,
        saved_head: str | None,
        saved_at: datetime | None,
        saved_state: SavedState,
    ) -> DeploymentResponse:
        # `image_digest`, `acr_run_id` and `revision_name` are deliberately NOT surfaced:
        # they are operator facts with no meaning to a citizen, and the digest in particular
        # is the reconciler's proof of ownership rather than something a client should be
        # able to read back and reason about.
        return cls(
            deployment_id=str(row.id),
            app_id=str(row.app_id),
            status=row.status.value,
            step=row.step,
            # Still the URL this deployment published, even once it is unpublished — it is a
            # fact about the attempt, not a promise the container is up. Nulling it here
            # would erase the record of where the app used to live; `unpublished_at` is what
            # tells a client not to link it.
            url=row.url,
            head_sha=row.head_sha,
            failure_code=row.failure_code,
            failure_detail=row.failure_detail,
            started_at=row.created_at,
            finished_at=row.finished_at,
            unpublished_at=row.unpublished_at,
            approval=approval,
            publish_state=publish_state,
            approved_retry_commit=approved_retry_commit,
            saved_head=saved_head,
            saved_at=saved_at,
            saved_state=saved_state,
        )
