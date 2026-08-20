"""Request/response bodies for one-click deploy.

The response deliberately carries BOTH the machine-readable failure code and the prose: a
client needs the code to decide what to offer next (retry, open the chat, tell an admin),
and the citizen needs the sentence. Reporting only one of the two has to be worked around
later by whichever consumer was left short.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.deployment import Deployment
from src.schemas import CamelModel
from src.services.deploy.classification import CLASSIFICATION_KEYS


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
    approval_route: ApprovalRoute | None = None
    rejection_note: str | None = None
    submitted_sha: str | None = None
    submitted_at: datetime | None = None

    @classmethod
    def of(cls, row: AppRegistry) -> ApprovalState:
        return cls(
            status=row.status,
            approved_commit_sha=row.approved_commit_sha,
            approval_route=row.approval_route,
            rejection_note=row.rejection_note,
            submitted_sha=row.source_commit_sha,
            submitted_at=row.submitted_at,
        )


class DeploymentResponse(CamelModel):
    """The latest deploy attempt, or an empty envelope when there has never been one.

    Empty rather than a 404: "this app has never been deployed" is a normal state a client
    renders as a Deploy button, not an error."""

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

    @classmethod
    def of(cls, row: Deployment, *, approval: ApprovalState | None = None) -> DeploymentResponse:
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
        )
