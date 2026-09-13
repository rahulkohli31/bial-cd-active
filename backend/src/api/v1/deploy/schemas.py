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
    All six are required booleans — the portal only builds this once every question is
    answered, so a default would let a caller under-declare by omission.
    NO REVIEW FIELD, BY CONSTRUCTION: the platform's own review is read from the store
    inside the publish request, and `CamelModel`'s `extra="ignore"` drops any unknown key
    — no request body can put words in the review's mouth. The notes gate lives at ladder
    rule 6 (`deploy/router.py`), which reads the MERGED answers this schema cannot see."""

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
    """`saveFirst` is the citizen's explicit "save and deploy". Default False, the safe
    default: a deploy ships the last SAVED version, so deploying over unsaved work without
    being asked would publish something they never chose, with no way to notice.
    `answers` is REQUIRED — no shape of this request deploys without a declaration, so no
    caller reaches the pipeline by skipping the modal. Re-answered every deploy, never
    remembered on the app, since the agent edits it between deploys and an old declaration
    is not evidence about what's shipping now; a redeploy client may prefill the form, but
    it still arrives here as a fresh declaration."""

    save_first: bool = False
    answers: DataClassificationAnswers


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
    of deploying (ladder rules 4-6: no current review, a standing rejection, or a
    weighted Yes on the merged answers).
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
    `submitted_sha`/`submitted_at` describe what's in the QUEUE; `approved_commit_sha` +
    `approval_route` are what ladder rule 3 consumes — a `runbook` approval never
    self-publishes, so a client rendering "you may publish" must read the lineage too."""

    status: AppStatus
    # NULL is a real state, not a gap: a never-approved app has no pin, and a
    # never-submitted draft has no lineage (see `ApprovalRoute`'s NULL semantics).
    approved_commit_sha: str | None = None
    # WHEN the administrator approved, beside WHICH commit they approved. The pin alone
    # cannot be rendered to a citizen — the status chip names the date first and mutes
    # the build code beside it, because a date is the thing a person recognises. Costs
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
    """THE single publish state the status chip renders — thirteen values, authored here
    and nowhere else, so no client recombines `status` + `unpublished_at` + `failure_code`
    + the approval route + the pin to guess at a state the server already knows. An
    **API** StrEnum, like `PreviewLifeState`: nothing persists it, the wire value equals
    the member's own string, and the chip's narrowing throws on anything it doesn't
    recognise — so this is the one place a new member gets added.
    UNKNOWN IS NEVER "UP TO DATE" — `LIVE_DRIFT_UNKNOWN` covers a storage HEAD that could
    not answer, the same tri-state discipline `SaveState.dirty` uses (`null` != clean)."""

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
    """THE pure mapping: three plain values in, one `PublishState` out — no I/O, so it
    can't acquire a hidden input later; the one metadata HEAD it depends on is read by the
    CALLER, which turns a storage failure into `saved_head=None` before this ever sees it.
    ADDS NO POLICY: the seven-rule publish ladder stands as-is, this only presents facts
    already written as one of the thirteen states.
    ORDER IS THE POLICY: `DISABLED`/`PENDING` win outright over the deployment row —
    an admin's lockout or a pending submission is the most current fact, and must not be
    masked by an OLDER row in the append-only `deployments` table.
    AN APPROVAL OUTRANKS THE ROUTED-FAILURE ARM AND NOTHING ELSE: the drift re-check
    settles a routed submission AS a failed row, so an approved app's newest row is that
    same failed one, and reading it answers in-review to a citizen an administrator
    already said yes to. Every other row is a container fact no approval can contradict
    and falls through — a serving app reports live or offline, a running one starting up,
    and a publish that genuinely broke still reports that it did not start."""
    if app.status is AppStatus.DISABLED:
        return PublishState.SWITCHED_OFF
    if app.status is AppStatus.PENDING:
        return PublishState.IN_REVIEW
    if app.status is AppStatus.REJECTED:
        return PublishState.CHANGES_REQUESTED
    if app.status is AppStatus.APPROVED and (
        deployment is None
        or (
            deployment.status is DeploymentStatus.FAILED
            and deployment.failure_code in _ROUTED_FAILURE_CODES
        )
    ):
        # Nothing of this approval was ever attempted: no row at all, or the one the
        # routing itself settled. "Ready" is the self-publish lineage with a pin that
        # still names what is saved — anything else (the runbook lineage, no pin, or a
        # pin a later Save has moved past) needs the citizen to publish through the gate
        # again rather than press one button.
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
        # start / Try again" — the exact defect reintroduced at the seam built to end
        # it.
        if deployment.failure_code in _ROUTED_FAILURE_CODES:
            return PublishState.IN_REVIEW
        return PublishState.DID_NOT_START
    # `DeploymentStatus.SUCCEEDED` — the only member left.
    if deployment.unpublished_at is not None:
        return PublishState.TAKEN_OFFLINE
    # THE DRIFT COMPARISON: against the commit that actually WENT LIVE, never
    # `approved_commit_sha` — that pin is NULL for every app published unattended under
    # ladder rule 7, so comparing against it would read every one of those apps as
    # unknown. `saved_head` is the primary signal; `source_commit_sha` (the last
    # SUBMITTED commit, moved only by submit/withdraw, never by a Save) is the
    # secondary one that still fires `live_newer_work` even when the saved head could
    # not be read at all (four saves and no new submission is exactly the case a
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
            saved_head=saved_head,
            saved_at=saved_at,
            saved_state=saved_state,
        )
