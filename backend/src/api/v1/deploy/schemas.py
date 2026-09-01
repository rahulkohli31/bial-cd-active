"""Request/response bodies for one-click deploy.

The response deliberately carries BOTH the machine-readable failure code and the prose: a
client needs the code to decide what to offer next (retry, open the chat, tell an admin),
and the citizen needs the sentence. Reporting only one of the two has to be worked around
later by whichever consumer was left short.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.schemas import CamelModel
from src.services.deploy.classification import CLASSIFICATION_KEYS
from src.services.deploy.service import FAIL_ROUTED_FOR_REVIEW


class DataClassificationAnswers(CamelModel):
    """What the citizen declares their app handles, answered fresh at every deploy.

    All six categories are required booleans. A partly-answered set never reaches this
    schema — the portal only builds one once every question has a Yes or a No — so
    "unanswered" is a pre-submission state on the client and not something this boundary
    has to represent. Making them required rather than defaulting to False also means a
    caller cannot under-declare by omission, which a default would have made invisible.

    NO REVIEW FIELD, BY CONSTRUCTION (R12). The platform's own review of the saved code
    is read from the store inside the publish request, keyed by app and version; a
    browser-supplied copy is not a field this schema has, and `CamelModel`'s pydantic
    default (`extra="ignore"`) drops any unknown key a caller smuggles in — so no shape
    of request body can put words in the review's mouth.

    THE NOTES GATE MOVED OUT OF THIS SCHEMA in U9, deliberately. It used to be a
    `model_validator` here, keyed on the citizen's own answers — but the explanation is
    obliged exactly when the MERGED answer set routes (ASM22/R10), and the merge reads
    the stored review, which a request schema cannot see. Worse, keeping it here would
    422 the one publish that must succeed without it: an approved app (ladder rule 3)
    ships a weighted-Yes declaration and needs no fresh explanation — the one on file
    with the approved submission already answered it. The gate enforces the requirement
    at ladder rule 6, where the merged outcome exists (`api/v1/deploy/router.py`).
    """

    credentials_secrets: bool
    health_data: bool
    personal_information: bool
    financial_data: bool
    confidential_business_data: bool
    public_data: bool
    # Bounded at the boundary the way admin's `RejectRequest.note` is — an over-long
    # explanation is rejected, never silently truncated into a record that misrepresents
    # what was said.
    notes: str | None = Field(default=None, max_length=1000)

    def classification_flags(self) -> dict[str, bool]:
        """The six answers as the plain mapping the policy module scores.

        Built from `CLASSIFICATION_KEYS` rather than a literal dict so a question added to
        the questionnaire cannot be silently dropped here — it would fail loudly at the
        `getattr` instead of quietly scoring as No.
        """
        return {key: bool(getattr(self, key)) for key in CLASSIFICATION_KEYS}


class DeployRequest(CamelModel):
    """`saveFirst` is the citizen's explicit "save and deploy".

    Default False, and that is the safe default: a deploy ships the last SAVED version, so
    deploying over unsaved work without being asked publishes something they never chose and
    gives them no way to notice.

    `answers` is REQUIRED, which is what makes the questionnaire a gate rather than a
    prompt: there is no shape of this request that deploys without a declaration, so no
    caller can reach the pipeline by simply not asking for the modal. It is re-answered at
    every deploy rather than remembered on the app, because the agent edits the app between
    deploys — a declaration made three deploys ago is not evidence about what is shipping
    now. A client that wants one-click redeploys prefills the form from the previous
    answers; that is a client affordance and still arrives here as a fresh declaration.
    """

    save_first: bool = False
    answers: DataClassificationAnswers


class DeployStartedResponse(CamelModel):
    """The 202 body. Carries the id to poll — the deploy itself has barely begun.

    `outcome` is the discriminator against `DeployRoutedResponse` (one POST, two
    success shapes since U9): a client switches on it rather than sniffing which keys
    happen to be present. Additive for existing clients, which read `status`."""

    outcome: Literal["started"] = "started"
    deployment_id: str
    app_id: str
    status: str


class DeployRoutedResponse(CamelModel):
    """The 200 body when the publish gate ROUTES the app to an administrator instead
    of deploying (ladder rules 4-6: no current review, a standing rejection, or a
    weighted Yes on the merged answers).

    An OUTCOME, not a failure — U12 renders it as an informational state (the app is
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


class UnpublishResponse(CamelModel):
    """The admin kill-switch's response (#113) — the deployment that was taken down (or
    already was, on an idempotent repeat) and when."""

    app_id: str
    deployment_id: str
    unpublished_at: datetime


class ApprovalState(CamelModel):
    """The app's APPROVAL lifecycle, carried on the deploy status response (U12).

    WHY IT RIDES HERE AND NOT ON A SECOND CALL. The citizen has two publish surfaces —
    the project-page card and the builder toolbar button — and the toolbar one is
    mounted with a project id and NO app id, so an app-scoped approval read is not
    even addressable from there. Both surfaces already poll THIS response through one
    hook, so hanging the approval state off it is what lets them inherit that hook's
    generation guard and its visibility/focus refresh instead of growing a second,
    fetch-once-and-rot lifetime of their own. Two surfaces, one source, one staleness
    story.

    `submitted_sha`/`submitted_at` describe the submission currently in the QUEUE (the
    R15b waiting state); `approved_commit_sha` + `approval_route` are what rule 3 of
    the publish gate consumes — a `runbook` approval authorises the manual go-live
    runbook and never self-publishing (P5), so a client that renders "you may publish
    this" must read the lineage as well as the pin."""

    status: AppStatus
    # NULL is a real state, not a gap: a never-approved app has no pin, and a
    # never-submitted draft has no lineage (see `ApprovalRoute`'s NULL semantics).
    approved_commit_sha: str | None = None
    # WHEN the administrator approved, beside WHICH commit they approved. The pin alone
    # cannot be rendered to a citizen — Plan G's chip names the date first and mutes the
    # build code beside it, because a date is the thing a person recognises. Costs
    # nothing: `approved_at` is a column on the registry row this response already
    # selects in full, so surfacing it adds no query and no I/O. NULL means never
    # approved, exactly as `approved_commit_sha` does — the two are written together in
    # one place (`admin/router.py`'s `approve`) and are never apart.
    approved_at: datetime | None = None
    approval_route: ApprovalRoute | None = None
    rejection_note: str | None = None
    submitted_sha: str | None = None
    submitted_at: datetime | None = None

    @classmethod
    def of(cls, row: AppRegistry) -> ApprovalState:
        return cls(
            status=row.status,
            approved_commit_sha=row.approved_commit_sha,
            approved_at=row.approved_at,
            approval_route=row.approval_route,
            rejection_note=row.rejection_note,
            submitted_sha=row.source_commit_sha,
            submitted_at=row.submitted_at,
        )


class PublishState(StrEnum):
    """THE single publish state Plan G's chip renders (U15, R38/R39) — thirteen values,
    authored here and nowhere else, so no client recombines `status` + `unpublished_at`
    + `failure_code` + the approval route + the pin to guess at a state the server
    already knows. An **API** StrEnum like `PreviewLifeState`
    (`build_sessions/schemas.py`): nothing persists it, and the wire value equals the
    member's own string. G's narrowing throws on a value it does not recognise, so this
    list is the one place a thirteenth (or fourteenth) member gets added.

    UNKNOWN IS NEVER SPELLED "UP TO DATE" — `LIVE_DRIFT_UNKNOWN` exists for exactly the
    case where a storage HEAD could not answer or a bundle predates the metadata stamp,
    the same tri-state discipline `SaveState.dirty` already uses, where `null` is never
    read as clean (L12)."""

    # No app row for the project at all — the only member with no approval block.
    NOTHING_BUILT = "nothing_built"
    # An app row exists; nothing has ever been submitted or deployed.
    DRAFT = "draft"
    # Submitted and awaiting an administrator, OR a deployment row settled FAILED with a
    # routed code (the drift re-check's own way of landing in the same queue).
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    # Approved, self-publish lineage, the pin still names what is saved: publish is a
    # citizen's button press away. Approval starts no pipeline (`admin/router.py`'s
    # `approve` never calls `_start_pipeline`), so an app can sit here indefinitely.
    APPROVED_READY_TO_PUBLISH = "approved_ready_to_publish"
    # Approved, but either the runbook lineage (which never self-publishes) or a Save
    # since approval has moved the saved commit off the approved pin.
    APPROVED_NEEDS_REVIEW_AGAIN = "approved_needs_review_again"
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
    # The newest deployment failed with a code that is NOT one of the routed ones.
    DID_NOT_START = "did_not_start"


# Mirrors `deploy/service.py`'s own private `_ROUTED_CODES`, which in turn mirrors the
# portal's `ROUTED_FAILURE_CODES` (`deployApi.ts`) — a third copy of one string, for the
# same reason the other two stay apart: `service.py`'s set exists to steer its
# citizen-message/operator-detail split, a decision this module has no business
# reaching into. One member today; this grows exactly when the drift re-check gains a
# second reason to route rather than fail.
_ROUTED_FAILURE_CODES: frozenset[str] = frozenset({FAIL_ROUTED_FOR_REVIEW})


def compute_publish_state(
    app: AppRegistry, deployment: Deployment | None, saved_head: str | None
) -> PublishState:
    """THE pure mapping (U15's technical design): three plain values in, one
    `PublishState` out — no I/O, no storage handle, so it cannot acquire a hidden input
    later. The single object-store metadata HEAD this depends on is read by the CALLER
    (`latest_deployment`, exactly where the two shipped readers at
    `classification/router.py:159-175` and this module's own `_shipping_head` already
    read it), and a storage failure is turned into `saved_head=None` there — this
    function never learns why the value is absent, only that it is.

    ADDS NO POLICY AND REMOVES NONE: the seven-rule publish ladder (`deploy_project`)
    stands exactly as it is. This only PRESENTS facts the ladder and the pipeline
    already wrote — `app`'s lifecycle columns and `deployment`'s terminal state — as one
    of the thirteen `PublishState` values.

    ORDER IS THE POLICY HERE. `DISABLED` and `PENDING` win outright, "whatever its
    deployment row says" (AE23) — an administrator's lockout or a citizen's pending
    submission is the most current, most actionable fact about the app, and must not be
    masked by an OLDER deployment row still sitting in the append-only `deployments`
    table (a routed-then-withdrawn app, or one disabled while still technically live).
    Only once those are ruled out does a deployment row get to describe what is
    actually running."""
    if app.status is AppStatus.DISABLED:
        return PublishState.SWITCHED_OFF
    if app.status is AppStatus.PENDING:
        return PublishState.IN_REVIEW
    if app.status is AppStatus.REJECTED:
        return PublishState.CHANGES_REQUESTED
    if app.status is AppStatus.APPROVED and deployment is None:
        # Never published since approval (or never published at all): "ready" is the
        # self-publish lineage with a pin that still names what is saved — anything else
        # (the runbook lineage, no pin, or a pin a later Save has moved past) needs the
        # citizen to publish through the gate again rather than press one button.
        pin_matches = (
            app.approval_route is ApprovalRoute.SELF_PUBLISH
            and app.approved_commit_sha is not None
            and app.approved_commit_sha == saved_head
        )
        return (
            PublishState.APPROVED_READY_TO_PUBLISH
            if pin_matches
            else PublishState.APPROVED_NEEDS_REVIEW_AGAIN
        )
    if deployment is None:
        # DRAFT, or APPROVED-but-never-deployed already returned above: nothing else
        # reaches here with no deployment row.
        return PublishState.DRAFT
    if deployment.status is DeploymentStatus.RUNNING:
        return PublishState.STARTING_UP
    if deployment.status is DeploymentStatus.FAILED:
        # THE FAILURE_CODE BULLET: this check sits ABOVE the generic failure arm on
        # purpose. A drift-routed publish is modelled as a FAILED row with a distinct
        # code (`routed_for_review`) rather than a fourth `DeploymentStatus` — without
        # this, a citizen correctly routed to an administrator would read "Didn't
        # start / Try again", L12's exact defect reintroduced at the seam built to end
        # it.
        if deployment.failure_code in _ROUTED_FAILURE_CODES:
            return PublishState.IN_REVIEW
        return PublishState.DID_NOT_START
    # `DeploymentStatus.SUCCEEDED` — the only member left.
    if deployment.unpublished_at is not None:
        return PublishState.TAKEN_OFFLINE
    # THE DRIFT COMPARISON (R39): against the commit that actually WENT LIVE, never
    # `approved_commit_sha` — that pin is NULL for every app published unattended under
    # ladder rule 7, so comparing against it would read every one of those apps as
    # unknown. `saved_head` is the primary signal; `source_commit_sha` (the last
    # SUBMITTED commit, moved only by submit/withdraw, never by a Save) is the
    # secondary one that still fires `live_newer_work` even when the saved head could
    # not be read at all (AE24: four saves and no new submission is exactly the case a
    # submitted-commit check alone reads as unknown).
    if saved_head is not None:
        return (
            PublishState.LIVE_CURRENT
            if saved_head == deployment.head_sha
            else PublishState.LIVE_NEWER_WORK
        )
    if app.source_commit_sha is not None and app.source_commit_sha != deployment.head_sha:
        return PublishState.LIVE_NEWER_WORK
    return PublishState.LIVE_DRIFT_UNKNOWN


class DeploymentResponse(CamelModel):
    """The latest deploy attempt, or an empty envelope when there has never been one.

    Empty rather than a 404: "this app has never been deployed" is a normal state a client
    renders as a Deploy button, not an error.

    `publish_state` (U15) is the one field a client should actually branch on — see
    `PublishState`. Every other field here still rides along for Plan G's own rendering
    (the URL, the timestamps, the raw approval block), but none of them needs to be
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
    # deploy succeeded and is serving traffic" from "an administrator took it down" (#113).
    # `status` cannot express the difference: an unpublished deployment stays `succeeded`,
    # because that is still how the attempt ended. A client rendering a live-app link MUST
    # test this as well, or it shows a citizen a URL that 404s with nothing to explain why.
    unpublished_at: datetime | None = None
    # The app's approval lifecycle (U12). NULL has ONE defined meaning: this project has
    # no app row yet, so there is no lifecycle to report — never "we didn't look".
    approval: ApprovalState | None = None
    # THE one computed publish state (U15). No default: every construction site names it
    # explicitly, the same fail-first posture `Settings` takes on a required field —
    # forgetting it should be a type error, not a value that quietly means nothing.
    publish_state: PublishState

    @classmethod
    def of(
        cls, row: Deployment, *, approval: ApprovalState | None = None, publish_state: PublishState
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
        )
