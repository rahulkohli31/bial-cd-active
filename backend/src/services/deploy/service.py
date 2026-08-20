"""The deploy pipeline: saved code in, running app out.

Two halves with a hard line between them.

The ROUTE half is synchronous and must finish in well under a second — the edge gateway
times out at twenty. It resolves the app, refuses if a build session is live, claims the
one in-flight slot, and hands back a deployment id.

The PIPELINE half is a detached task that runs for minutes: extract the snapshot, pack a
build context, build an image, provision the container app, wait for the revision, record
the result. It outlives its request, so it never borrows the request's database session —
it opens short ones of its own, exactly as the turn engine does.

TWO THINGS THE PIPELINE NOW DECIDES, BOTH ADDED IN U10, AND NEITHER IS A STYLE CHOICE.

THE EXPECTED COMMIT. The gate makes its decision about a commit it read off the snapshot
blob's metadata stamp; the pipeline then extracts the mutable snapshot, and a save landing
in between would ship a tree nobody examined. So the commit travels with the claim and the
extracted head must equal it — `snapshot_moved`, failed closed, otherwise. That assertion
is what turns "what was approved is what is running" from an assumption into a property.

THE DRIFT RE-CHECK (R13). On save-and-publish the request necessarily returned before any
review of the version it just minted could exist, so the pipeline runs that review itself,
as its FIRST step, before packing — and then stands in for the ladder's rules 4-7: no
usable review for this version routes it (R20), a weighted Yes the submitted answer set
did not already carry routes it (R9's differential, so a version whose findings did not
change is not sent back to the queue that already approved them), and anything else
publishes. Routing here is a REAL queue entry, pinned to the commit just asserted, and the
deployment settles FAILED with `routed_for_review` — the existing terminal state with its
own code, never a fourth status (ASM20). The review is handed the tree the pipeline
already extracted: it uses a caller-owned root and never deletes one, so packing still has
its files afterwards.

THE PIPELINE NEVER TOUCHES A SANDBOX. Not the lock, not the registry, not `provision_new`
or `restore_from_snapshot`. That is a correctness boundary, not tidiness: `restore` tears a
container down BEFORE it pulls the bundle, and a confirmed-absent snapshot falls through to
a blank golden template — which would build cleanly, deploy successfully, and replace the
citizen's app with the starter, with a green checkmark. Deploy reads the bundle from object
storage and leaves the sandbox alone.

Every failure lands in two places: the deployment row (structured, for the API) and the
conversation (prose, for the citizen). The second is the one that matters — a build failure
the citizen cannot see is a build failure they cannot ask the agent to fix.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import AppApiError
from src.core.redaction import redact_secrets
from src.db.models.app_registry import AppRegistry, ApprovalRoute
from src.db.models.classification_review import ClassificationReviewStatus
from src.db.models.deployment import Deployment
from src.db.models.user import User
from src.services.approvals.submit import submit_app_for_review
from src.services.classification.constants import REVIEW_WALL_CLOCK_CEILING_S
from src.services.classification.merge import merge_questions
from src.services.classification.service import (
    ReviewReadout,
    get_classification_review_service,
)
from src.services.classification.store import ReviewRecord
from src.services.deploy import store
from src.services.deploy.aca_publish import PublishedAppProvisioner
from src.services.deploy.classification import DATA_CLASSIFICATION_QUESTIONS
from src.services.deploy.config import DeployConfig
from src.services.deploy.context import ContextTooLargeError, build_context_async
from src.services.deploy.env import PublishedStorageError, build_published_env
from src.services.deploy.gate import (
    DriftFacts,
    append_gate_audit,
    declaration_document,
    merge_inputs,
    review_at_head,
)
from src.services.deploy.images import ImageBuilder, ImageBuildError
from src.services.deploy.names import image_reference, revision_name
from src.services.deploy.outcome import write_deploy_outcome
from src.services.orchestrator.errors import from_next_build
from src.services.sandbox.aca import AcaError
from src.services.storage import StorageError, get_storage
from src.services.storage.snapshot_read import (
    ExtractedSnapshot,
    NoAppYet,
    SnapshotExtractionError,
    extract_snapshot,
)

_log = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Phase labels. Display only — never branched on, which is why `step` is a plain String.
# `checking` runs BEFORE packing and only on the drift path; it exists as a phase of its
# own so the progress control can name the re-check ("Checking your app") instead of
# falling through to its generic label while the citizen waits on a model.
STEP_CHECKING: Final = "checking"
STEP_PACKING: Final = "packing"
STEP_BUILDING: Final = "building"
STEP_PROVISIONING: Final = "provisioning"
STEP_STARTING: Final = "starting"

# Failure codes. Stable and greppable: an operator alerting on `acr_unauthorized` must not
# have to match on prose that a copy edit can change.
FAIL_NO_SNAPSHOT: Final = "no_saved_build"
FAIL_SNAPSHOT_UNREADABLE: Final = "snapshot_unreadable"
FAIL_CONTEXT_TOO_LARGE: Final = "context_too_large"
FAIL_BUILD: Final = "build_failed"
FAIL_STORAGE: Final = "storage_unavailable"
FAIL_PROVISION: Final = "provision_failed"
FAIL_NOT_HEALTHY: Final = "revision_unhealthy"
FAIL_INTERNAL: Final = "internal_error"

FAIL_SNAPSHOT_MOVED: Final = "snapshot_moved"
"""The extracted tree was not the commit the gate decided about — a save landed between
the claim and the extraction. Deliberately the SAME string the route's mid-request race
answers with (`deploy/router.py`'s 409), because it is the same event seen from a
different side of the 202, and a citizen reading both should not have to learn two words
for it. Fails closed: publishing an unexamined tree is the one outcome this feature
exists to prevent."""

FAIL_ROUTED_FOR_REVIEW: Final = "routed_for_review"
"""NOT A FAILURE OF THE PLATFORM, AND NOT RED (ASM20). The drift re-check found something
the submitted answers did not cover, so this version went into the admin queue instead of
going live. Modelled as the existing FAILED terminal state with its own code rather than a
fourth `DeploymentStatus` — that enum change would move what `uq_deployments_one_in_flight`
covers, a real schema decision — and named to match the route's own 200 `outcome`
discriminator, since the citizen ends up in exactly the same place either way. U12 renders
it as an informational state; anything painting `status == failed` red must special-case
this code."""

FAIL_ROUTE_REFUSED: Final = "route_refused"
"""The re-check decided to route and the QUEUE would not take it — a build session went
live, storage stopped answering, the app changed status underneath. Distinct from
`routed_for_review` on purpose: nothing was published AND nothing is waiting for an
administrator, so this one really is red, and the citizen is told which guard refused."""

# How often the running pipeline renews its liveness stamp. Comfortably inside the
# staleness window so a slow ARM call never looks like a crash.
_HEARTBEAT_S: Final = store.HEARTBEAT_CADENCE_S

# How long to wait for the new revision to report healthy, and how often to ask.
_REVISION_POLL_S: Final = 3.0

# How often the drift re-check asks whether the review has landed. The review service is a
# two-verb contract (start, read) whose run is detached, so waiting on it means reading the
# row — there is no task to await from here, and reaching for one would couple the pipeline
# to the runner's internals. Coarse on purpose: the run takes tens of seconds.
_REVIEW_POLL_S: Final = 2.0

# The belt-and-braces bound on that poll. The review's own wall-clock ceiling settles a
# stuck row as aged-out, which is what actually ends the loop; this is the answer to "and
# what if it doesn't" — a re-check that never resolves must not hold a deploy open forever.
_REVIEW_WAIT_MARGIN_S: Final = 60.0

# A failure detail that reaches the citizen. Bounded and redacted before it is stored: a
# build log is attacker-influenced text from a workspace the citizen's AI drove.
_DETAIL_MAX_CHARS: Final = 4_000


class DeployNotPossibleError(Exception):
    """The route cannot start a deploy, with a reason the citizen can act on."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StartedDeploy:
    deployment_id: uuid.UUID
    app_id: uuid.UUID


@dataclass(frozen=True)
class VersionRecheck:
    """THE DRIFT PATH'S ORDER TO THE PIPELINE (U10, R13): review the version you are about
    to ship, then decide, because the request could not.

    Set only by the gate's rule 3a — this request performed the save, so the stored review
    is stamped the commit before it. Everything the post-review decision needs travels
    here, because a detached task cannot ask the request anything afterwards:

    * `answered_about` — the commit the citizen's answers and explanation were written
      about (the stamp on the review that pre-filled their form). Carried so a queue item
      can say whose question the explanation actually answers.
    * `declaration` — the gate's own record of what was submitted, in `deploy/gate`'s
      documented shape. Its `citizen.answers` are re-merged against the NEW review, and
      its `merged.answers` are the BASELINE: a weighted Yes already in there is not news,
      and sending it back to the queue that just approved it is exactly the loop ladder
      rule 3 exists to break.
    """

    answered_about: str | None
    declaration: dict[str, Any]


class VersionReviewer(Protocol):
    """The review runner's two-verb contract, as the pipeline needs it — start a review
    for this version, read the result for this version.

    A Protocol rather than the concrete service so this module depends on the contract the
    review's own docstring advertises, and so a test can drive the drift path with a
    scripted reviewer instead of a model. `ClassificationReviewService` satisfies it
    structurally; nothing here knows about detached tasks, budgets or Foundry."""

    async def start(
        self,
        db: AsyncSession,
        *,
        app_id: uuid.UUID,
        user_id: uuid.UUID,
        head_sha: str,
        extracted: ExtractedSnapshot | None = None,
    ) -> ReviewRecord: ...

    async def read(self, db: AsyncSession, *, app_id: uuid.UUID) -> ReviewReadout | None: ...


class DeployService:
    """Owns the in-flight pipeline tasks. One process-wide instance."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        image_builder: ImageBuilder,
        published_apps: PublishedAppProvisioner,
        reviewer: VersionReviewer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._images = image_builder
        self._aca = published_apps
        # The drift re-check's reviewer. `None` means "the process-wide one", resolved at
        # use rather than at construction: this service is built by its own accessor, and
        # building the review singleton here would tie a deploy service's existence to a
        # review service's — including in the tests that build one with no model at all.
        self._reviewer = reviewer
        # Strong references: a task the loop can garbage-collect mid-flight would abandon a
        # half-provisioned container app with nothing left to reconcile against.
        self._tasks: set[asyncio.Task[None]] = set()

    # --- the route half ---------------------------------------------------------

    async def start(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        classification: dict[str, Any] | None = None,
        classification_score: int | None = None,
        expected_commit_sha: str | None = None,
        recheck: VersionRecheck | None = None,
    ) -> StartedDeploy:
        """Claim the slot and detach the pipeline. Fast — the caller is holding an HTTP
        request open and the edge gives it twenty seconds.

        `classification` is what the citizen declared their app handles and
        `classification_score` is the total that cleared the deploy gate. Both are recorded,
        neither is re-checked: the gate is the route's job, enforced before anything with a
        side effect runs. Scoring again here would put the same policy in two places and let
        them disagree about a deploy already in flight.

        `expected_commit_sha` IS THE ONE EXCEPTION TO THAT (U10), and it is not a second
        gate: it re-checks no policy and reads no answers. It says only "the tree you
        extract must be the tree the gate decided about", which nothing upstream can
        guarantee because the snapshot is mutable and the extraction happens minutes later.
        `None` — a saved bundle predating the metadata stamp — asserts nothing.

        `recheck` turns the pipeline into the second half of the ladder for the drift path
        alone; see `VersionRecheck` and the module docstring."""
        deployment_id = await store.claim(
            db,
            app_id=app_id,
            user_id=user_id,
            classification=classification,
            classification_score=classification_score,
        )
        if deployment_id is None:
            raise DeployNotPossibleError(
                "This app is already being deployed. Wait for it to finish, then try again.",
                code="deploy_in_flight",
            )

        task = asyncio.create_task(
            self._run(
                deployment_id=deployment_id,
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                conversation_id=conversation_id,
                expected_commit_sha=expected_commit_sha,
                recheck=recheck,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return StartedDeploy(deployment_id=deployment_id, app_id=app_id)

    # --- the pipeline half ------------------------------------------------------

    async def _run(
        self,
        *,
        deployment_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        expected_commit_sha: str | None = None,
        recheck: VersionRecheck | None = None,
    ) -> None:
        """The detached pipeline. NEVER raises: an escaping exception would leave the row
        `running` until the stale-claim window expires, and the citizen staring at a Deploy
        button that 409s for half an hour.

        THE ROUTED OUTCOME LEAVES THROUGH THE FAILURE FUNNEL, deliberately and without a
        branch of its own: it is a terminal state with a code and a sentence for the
        citizen, which is precisely what `_DeployFailedError` carries and what `_fail`
        settles. Giving it a third arm here would duplicate the guarded terminal write and
        the chat notice for a case whose only difference is which colour a client paints
        it."""
        async with self._beating(deployment_id):
            try:
                url = await self._deploy(
                    deployment_id=deployment_id,
                    app_id=app_id,
                    project_id=project_id,
                    user_id=user_id,
                    expected_commit_sha=expected_commit_sha,
                    recheck=recheck,
                )
            except _DeployFailedError as failure:
                await self._fail(
                    deployment_id,
                    app_id=app_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    code=failure.code,
                    detail=failure.detail,
                    citizen_message=failure.citizen_message,
                )
            except asyncio.CancelledError:
                # Shutdown. Leave the row alone — the reconciler resolves it against ARM,
                # which is the only source that knows whether the app actually came up.
                raise
            except Exception as exc:
                _log.exception("deploy_pipeline_crashed", deployment_id=str(deployment_id))
                await self._fail(
                    deployment_id,
                    app_id=app_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    code=FAIL_INTERNAL,
                    detail=type(exc).__name__,
                    citizen_message=(
                        "Something went wrong on the platform while deploying your app. "
                        "Nothing was changed — please try again."
                    ),
                )
            else:
                await self._succeed(
                    deployment_id,
                    app_id=app_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    url=url,
                )

    async def _deploy(
        self,
        *,
        deployment_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        expected_commit_sha: str | None = None,
        recheck: VersionRecheck | None = None,
    ) -> str:
        """The happy path. Every failure leaves by raising `_DeployFailedError` — including
        the routed outcome, which is a terminal state with a code, not an exception to the
        rule."""
        # 1 — the saved code. `NoAppYet` is a NORMAL outcome (nobody has built yet), not an
        # error; an unreadable bundle is the opposite and must never read the same.
        try:
            extracted = await extract_snapshot(app_id)
        except SnapshotExtractionError as exc:
            raise _DeployFailedError(
                FAIL_SNAPSHOT_UNREADABLE,
                detail=str(exc),
                citizen_message=(
                    "Your saved app could not be read. This is a platform problem — "
                    "please tell an administrator."
                ),
            ) from exc
        if isinstance(extracted, NoAppYet):
            raise _DeployFailedError(
                FAIL_NO_SNAPSHOT,
                detail=None,
                citizen_message=(
                    "There is no saved version of this app yet. Build something and save it, "
                    "then deploy."
                ),
            )

        # 1a — THE PIN (U10). The gate decided about a commit it read off the snapshot
        # blob's metadata stamp; this is the tree that stamp was supposed to name. A save
        # landing in the gap between them is not a race to tolerate — publishing it would
        # put unexamined code behind a decision made about something else — so it fails
        # closed, and the citizen re-publishes the version that now exists.
        if expected_commit_sha is not None and extracted.head_sha != expected_commit_sha:
            raise _DeployFailedError(
                FAIL_SNAPSHOT_MOVED,
                detail=f"expected {expected_commit_sha} but extracted {extracted.head_sha}",
                citizen_message=(
                    "Your app was saved again while this deploy was starting, so nothing "
                    "was published — the version that was checked is not the version that "
                    "would have gone live. Press Deploy again to publish what is saved now."
                ),
            )

        # 1b — THE DRIFT RE-CHECK (R13), before anything is packed or built: on this path
        # the citizen's answers describe the version BEFORE the save this request made, so
        # the platform reviews the one actually leaving and decides on the far side of the
        # 202 the route already sent. Returns to continue publishing, or raises with the
        # queue entry already made.
        if recheck is not None:
            await self._recheck(
                deployment_id=deployment_id,
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                extracted=extracted,
                recheck=recheck,
            )

        await self._advance(deployment_id, STEP_PACKING, head_sha=extracted.head_sha)

        # 2 — the build context, with the platform's own Dockerfile overlaid.
        try:
            context = await build_context_async(extracted.root)
        except ContextTooLargeError as exc:
            raise _DeployFailedError(
                FAIL_CONTEXT_TOO_LARGE,
                detail=str(exc),
                citizen_message=(
                    "Your app is too large to deploy. This usually means build output or "
                    "dependencies were saved with it — please tell an administrator."
                ),
            ) from exc

        # 3 — the image. This is also the BUILD GATE: `next build` runs here, and it is the
        # only check that sees the whole production-build failure class `tsc --noEmit` is
        # blind to.
        await self._advance(deployment_id, STEP_BUILDING)
        try:
            built = await self._images.build(
                app_id=app_id, deployment_id=deployment_id, context=context
            )
        except ImageBuildError as exc:
            raise _DeployFailedError.from_build(exc) from exc

        await self._advance(
            deployment_id, STEP_PROVISIONING, image_digest=built.digest, acr_run_id=built.run_id
        )

        # 4 — the runtime environment. Same database, same object-store container as the
        # sandbox; a LONG-LIVED Blob credential instead of the seven-day session one.
        async with self._session_factory() as db:
            try:
                env, container_url = await build_published_env(
                    db, app_id=app_id, project_id=project_id
                )
            except PublishedStorageError as exc:
                raise _DeployFailedError(
                    FAIL_STORAGE,
                    detail=str(exc),
                    citizen_message=(
                        "Your app could not be given access to its file storage, so it was "
                        "not deployed. Please tell an administrator."
                    ),
                ) from exc

        # 5 — the container app.
        image = image_reference(
            acr_server=self._aca_config.acr_server,
            repository_prefix=self._aca_config.image_repository_prefix,
            app_id=app_id,
            digest=built.digest,
        )
        try:
            fqdn = await self._aca.create_or_update(
                app_id=app_id,
                deployment_id=deployment_id,
                image=image,
                env=env,
                container_url=container_url,
            )
        except AcaError as exc:
            raise _DeployFailedError(
                FAIL_PROVISION,
                detail=str(exc),
                citizen_message=(
                    "Your app was built, but the platform could not start it. Your previous "
                    "version is still running. Please try again."
                ),
            ) from exc

        await self._advance(
            deployment_id,
            STEP_STARTING,
            container_app_name=self._aca_name(app_id),
            revision_name=revision_name(app_id, deployment_id),
        )

        # 6 — the revision. `create_or_update` returning an FQDN proves the APP exists, not
        # that the new REVISION is healthy; in single-revision mode ARM settles the app
        # while a revision can still fail to activate.
        await self._await_revision(app_id=app_id, deployment_id=deployment_id)
        return f"https://{fqdn}/"

    async def _await_revision(self, *, app_id: uuid.UUID, deployment_id: uuid.UUID) -> None:
        deadline = asyncio.get_running_loop().time() + self._aca_config.ready_timeout_s
        while True:
            state = await self._aca.get_revision(app_id=app_id, deployment_id=deployment_id)
            if state.healthy:
                return
            if state.failed:
                raise _DeployFailedError(
                    FAIL_NOT_HEALTHY,
                    detail=f"revision provisioning state: {state.provisioning_state}",
                    citizen_message=(
                        "Your app was built but did not start. Your previous version is "
                        "still running. This is usually a problem in the app itself — ask "
                        "the assistant to check it."
                    ),
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise _DeployFailedError(
                    FAIL_NOT_HEALTHY,
                    detail="the revision did not become healthy in time",
                    citizen_message=(
                        "Your app was built but did not start in time. Your previous version "
                        "is still running. Please try again."
                    ),
                )
            await asyncio.sleep(_REVISION_POLL_S)

    # --- the drift re-check (U10, R13) ------------------------------------------

    async def _recheck(
        self,
        *,
        deployment_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        extracted: ExtractedSnapshot,
        recheck: VersionRecheck,
    ) -> None:
        """Review the version actually leaving, then stand in for the ladder's rules 4-7.

        Returns when publishing may continue. Raises `_DeployFailedError` when it may not:
        `routed_for_review` with the queue entry already made, or `route_refused` when the
        queue would not take it.

        THE THREE OUTCOMES, in the ladder's own order:

        * no genuinely-complete review for this commit -> ROUTE (rule 4 / R20). A re-check
          that failed, aged out or came back partial is exactly the "unavailable" state the
          gate routes on; letting it publish because a failed review names no categories
          would make failure the cheapest way through the gate.
        * a weighted Yes the SUBMITTED answers did not already carry -> ROUTE (rule 6 /
          R9). Differential, not absolute, and that is rule 3's reasoning applied one
          version later: the same review returns the same Yes for the same data, so
          re-routing an app over findings its submission already carried would send it back
          to the queue that just dealt with them, forever.
        * anything else -> PUBLISH (rule 7 / R14).
        """
        await self._advance(deployment_id, STEP_CHECKING, head_sha=extracted.head_sha)

        readout = await self._review_of(app_id=app_id, user_id=user_id, extracted=extracted)
        review = review_at_head(readout, extracted.head_sha)

        # The citizen's own answers are re-merged against the NEW verdicts — they are still
        # the citizen's answers, and stricter-of still applies. What changes is the review
        # half of the merge, which is the entire point of re-checking.
        citizen = _answers_in(recheck.declaration, "citizen")
        submitted = _answers_in(recheck.declaration, "merged")
        merged = merge_questions(merge_inputs(citizen, review))
        newly_raised = tuple(
            question.key
            for question in merged.questions
            if question.weighted_yes and not submitted.get(question.key)
        )

        if review.complete and not newly_raised:
            _log.info(
                "deploy_recheck_agreed",
                deployment_id=str(deployment_id),
                app_id=str(app_id),
                head_sha=extracted.head_sha,
                answered_about=recheck.answered_about,
            )
            return

        # ROUTING. The declaration is rebuilt around the NEW review — it is what the
        # administrator will read — and carries the drift block that says whose question
        # the citizen's explanation actually answered (U13 leads with that distinction).
        declaration = declaration_document(
            head_sha=extracted.head_sha,
            citizen=citizen,
            explanation=_explanation_in(recheck.declaration),
            review=review,
            merged=merged,
            drift=DriftFacts(answered_about=recheck.answered_about, newly_raised=newly_raised),
        )
        rule = "recheck_new_yes" if review.complete else "recheck_review_not_current"
        submission_id = await self._route_to_queue(
            deployment_id=deployment_id,
            app_id=app_id,
            project_id=project_id,
            user_id=user_id,
            head_sha=extracted.head_sha,
            declaration=declaration,
            rule=rule,
        )
        _log.info(
            "deploy_routed_after_recheck",
            deployment_id=str(deployment_id),
            app_id=str(app_id),
            rule=rule,
            head_sha=extracted.head_sha,
            newly_raised=list(newly_raised),
        )
        if review.complete:
            changed = ", ".join(_labels_for(newly_raised))
            citizen_message = (
                "You saved changes, so the platform checked the new version before "
                f"publishing it — and it found something your answers had not covered: "
                f"{changed}. Your app was not published; this exact version was sent to "
                "an administrator for review, and you can publish it once it is approved."
            )
        else:
            citizen_message = (
                "You saved changes, so the platform tried to check the new version before "
                "publishing it, and that check could not be completed. Your app was not "
                "published; this exact version was sent to an administrator for review "
                "instead, and you can publish it once it is approved."
            )
        raise _DeployFailedError(
            FAIL_ROUTED_FOR_REVIEW,
            detail=(
                f"submitted for review as {submission_id} at {extracted.head_sha}"
                + (f"; newly raised: {', '.join(newly_raised)}" if newly_raised else "")
            ),
            citizen_message=citizen_message,
        )

    async def _review_of(
        self, *, app_id: uuid.UUID, user_id: uuid.UUID, extracted: ExtractedSnapshot
    ) -> ReviewReadout | None:
        """Start a review of the extracted version and wait for it to settle.

        THE TREE IS HANDED OVER, NOT RE-DOWNLOADED. The review's contract is explicit that
        a caller-owned root is used and never deleted, which is what makes this safe both
        ways: it does not clone a second copy of the same commit in the same minute, and
        packing still has its files when the review is done.

        Waiting is a POLL because the runner is a two-verb contract whose run is detached —
        there is no task to await from out here, and grabbing one would couple this pipeline
        to the runner's internals. The loop ends when the row is no longer RUNNING or has
        aged out past the review's own wall-clock ceiling; the deadline below is the answer
        to "and if neither ever happens", not the expected exit."""
        reviewer = (
            self._reviewer if self._reviewer is not None else get_classification_review_service()
        )
        async with self._session_factory() as db:
            await reviewer.start(
                db,
                app_id=app_id,
                user_id=user_id,
                head_sha=extracted.head_sha,
                extracted=extracted,
            )
        deadline = (
            asyncio.get_running_loop().time() + REVIEW_WALL_CLOCK_CEILING_S + _REVIEW_WAIT_MARGIN_S
        )
        while True:
            async with self._session_factory() as db:
                readout = await reviewer.read(db, app_id=app_id)
            if (
                readout is None
                or readout.aged_out
                or readout.review.status is not ClassificationReviewStatus.RUNNING
            ):
                return readout
            if asyncio.get_running_loop().time() >= deadline:
                # Neither settled nor aged out inside its own ceiling plus a margin. Hand
                # back the RUNNING row: `review_at_head` reads it as not-complete, which
                # routes — the fail-safe direction, and the same answer rule 4 gives.
                _log.warning(
                    "deploy_recheck_review_never_settled",
                    app_id=str(app_id),
                    head_sha=extracted.head_sha,
                )
                return readout
            await asyncio.sleep(_REVIEW_POLL_S)

    async def _route_to_queue(
        self,
        *,
        deployment_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        head_sha: str,
        declaration: dict[str, Any],
        rule: str,
    ) -> uuid.UUID:
        """Submit this exact version into the admin queue — R15a's one route in, reached
        from the pipeline this time. Returns the submission id.

        THE COPY IS PINNED TO THE COMMIT THE PIPELINE ASSERTED, not to whatever the mutable
        snapshot holds at the moment the submit reads it. The submit forks the bundle that
        is in the store now, so a save landing in this last gap would queue a version
        nobody examined — the same lie the expected-commit assertion exists to prevent, one
        step further down. The whole submit runs inside a SAVEPOINT precisely so that
        refusing it here leaves no half-submitted row: the copied blob is the accepted
        orphan class the submit service documents (D3), logged, referenced by nothing.

        EVERY GUARD FAILURE BECOMES THE DEPLOYMENT'S FAILURE DETAIL. A build session that
        went live, a store that stopped answering, an app an administrator disabled while
        the review ran — the submit refuses with a sentence written for the citizen, and it
        is the only explanation they will get, because this pipeline never raises out and
        no request is left to receive a 409."""
        try:
            storage = get_storage()
        except StorageError as exc:
            raise _DeployFailedError(
                FAIL_ROUTE_REFUSED,
                detail=str(exc),
                citizen_message=(
                    "Your app was not published: the platform could not reach your saved "
                    "app to send this version for review. Please try again in a moment."
                ),
            ) from exc

        async with self._session_factory() as db:
            # Owner-scoped read (ADR-0004) even though the pipeline resolved this app from
            # the citizen's own request: the submit service re-checks ownership fail-closed
            # for the same reason, and a service that trusts its caller with the predicate
            # is one refactor away from a cross-user write.
            app_row = (
                await db.execute(
                    sa.select(AppRegistry).where(
                        AppRegistry.id == app_id, AppRegistry.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            if app_row is None:
                raise _DeployFailedError(
                    FAIL_ROUTE_REFUSED,
                    detail="the app row was gone by the time the review finished",
                    citizen_message=(
                        "Your app was not published, and it could not be sent for review "
                        "either — it no longer exists."
                    ),
                )

            savepoint = await db.begin_nested()
            try:
                receipt = await submit_app_for_review(
                    db,
                    storage,
                    user_id=user_id,
                    app=app_row,
                    declaration=declaration,
                    route=ApprovalRoute.SELF_PUBLISH,
                )
            except AppApiError as exc:
                await savepoint.rollback()
                raise _DeployFailedError(
                    FAIL_ROUTE_REFUSED,
                    detail=exc.message,
                    citizen_message=(
                        "Your app was not published, and this version could not be sent "
                        f"for review either: {exc.message}"
                    ),
                ) from exc
            if receipt.commit_sha != head_sha:
                await savepoint.rollback()
                _log.warning(
                    "deploy_recheck_snapshot_moved_mid_route",
                    app_id=str(app_id),
                    examined=head_sha,
                    copied=receipt.commit_sha,
                )
                raise _DeployFailedError(
                    FAIL_ROUTE_REFUSED,
                    detail=f"examined {head_sha} but the queue copy was {receipt.commit_sha}",
                    citizen_message=(
                        "Your app was saved again while it was being checked, so nothing "
                        "was published and nothing was sent for review. Press Deploy again "
                        "to publish the version that is saved now."
                    ),
                )
            await savepoint.commit()

            # R22's record of THIS decision, written by the pipeline that made it — same
            # action, same shape, same actor as the ladder's own rows, so "what did the
            # gate decide for this app" stays one query. The email is denormalised because
            # the actor reference nulls when a user is removed.
            email = await db.scalar(sa.select(User.email).where(User.id == user_id))
            await append_gate_audit(
                db,
                actor_id=user_id,
                email=email,
                app_id=app_id,
                project_id=project_id,
                decision="routed",
                rule=rule,
                declaration=declaration,
                extra={
                    "deploymentId": str(deployment_id),
                    "submissionId": str(receipt.submission_id),
                    "commitSha": receipt.commit_sha,
                },
            )
            await db.commit()
        return receipt.submission_id

    # --- terminals --------------------------------------------------------------

    async def _succeed(
        self,
        deployment_id: uuid.UUID,
        *,
        app_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        url: str,
    ) -> None:
        async with self._session_factory() as db:
            settled = await store.succeed(db, deployment_id, url=url)
        if not settled:
            # Someone else settled this row — it was taken over, or the reconciler
            # promoted it. A late pipeline must not contradict what is on record.
            _log.warning("deploy_already_settled", deployment_id=str(deployment_id))
            return
        _log.info("deploy_succeeded", deployment_id=str(deployment_id), app_id=str(app_id))
        await self._tell_the_citizen(
            user_id=user_id,
            conversation_id=conversation_id,
            deployment_id=deployment_id,
            app_id=app_id,
            succeeded=True,
            message=f"Your app is live at {url}",
            url=url,
        )

    async def _fail(
        self,
        deployment_id: uuid.UUID,
        *,
        app_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        code: str,
        detail: str | None,
        citizen_message: str,
    ) -> None:
        safe = _safe_detail(detail)
        async with self._session_factory() as db:
            settled = await store.fail(db, deployment_id, code=code, detail=safe)
        if not settled:
            _log.warning("deploy_already_settled", deployment_id=str(deployment_id))
            return
        _log.warning("deploy_failed", deployment_id=str(deployment_id), code=code)
        await self._tell_the_citizen(
            user_id=user_id,
            conversation_id=conversation_id,
            deployment_id=deployment_id,
            app_id=app_id,
            succeeded=False,
            message=citizen_message,
            detail=safe,
        )

    async def _tell_the_citizen(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        deployment_id: uuid.UUID,
        app_id: uuid.UUID,
        succeeded: bool,
        message: str,
        url: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Write the outcome into the chat. Best-effort by design: the deployment row is the
        record of truth, and a chat write that fails must not undo a deploy that worked."""
        if conversation_id is None:
            return
        try:
            async with self._session_factory() as db:
                await write_deploy_outcome(
                    db,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    deployment_id=deployment_id,
                    app_id=app_id,
                    succeeded=succeeded,
                    message=message,
                    url=url,
                    detail=detail,
                )
        except Exception:
            _log.warning(
                "deploy_outcome_not_written", deployment_id=str(deployment_id), exc_info=True
            )

    # --- plumbing ---------------------------------------------------------------

    @property
    def _aca_config(self) -> DeployConfig:
        return self._aca.config

    def _aca_name(self, app_id: uuid.UUID) -> str:
        from src.services.deploy.names import published_app_name

        return published_app_name(app_id)

    async def _advance(self, deployment_id: uuid.UUID, step: str, **fields: object) -> None:
        async with self._session_factory() as db:
            await store.advance(db, deployment_id, step=step, **fields)

    def _beating(self, deployment_id: uuid.UUID) -> AbstractAsyncContextManager[None]:
        return _Heartbeat(self._session_factory, deployment_id)

    async def drain(self) -> None:
        """Await every in-flight pipeline. Used by tests; the lifespan lets them be
        cancelled instead, because the reconciler resolves whatever was in flight."""
        for task in list(self._tasks):
            with suppress(Exception):
                await task


class _DeployFailedError(Exception):
    """A pipeline failure with everything both audiences need: a stable code for the row and
    an operator's alert, and prose for the citizen."""

    def __init__(self, code: str, *, detail: str | None, citizen_message: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.citizen_message = citizen_message

    @classmethod
    def from_build(cls, exc: ImageBuildError) -> _DeployFailedError:
        """A build failure is the one the citizen can actually act on, so it carries the
        registry's own log through the same de-noiser the self-heal loop uses — ANSI
        stripped, paths relativized, secrets redacted, and titled on the line that names the
        fault rather than the Next.js banner."""
        log = exc.log_tail
        if not log:
            return cls(
                FAIL_BUILD,
                detail=str(exc),
                citizen_message=(
                    f"Your app did not build, so it was not deployed ({exc}). Your previous "
                    "version is still running."
                ),
            )
        error = from_next_build(log)
        return cls(
            FAIL_BUILD,
            detail=error.cleaned_stack,
            citizen_message=(
                f"Your app did not build, so it was not deployed:\n\n{error.title}\n\n"
                "Your previous version is still running. Ask me to fix it and try again."
            ),
        )


class _Heartbeat:
    """Renews the deployment's liveness stamp for as long as the pipeline runs.

    Without it a build that legitimately takes longer than the staleness window would be
    taken over by the citizen's next Deploy click, and two pipelines would provision the
    same container app."""

    def __init__(self, session_factory: SessionFactory, deployment_id: uuid.UUID) -> None:
        self._session_factory = session_factory
        self._deployment_id = deployment_id
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> None:
        self._task = asyncio.create_task(self._beat())

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_S)
            try:
                async with self._session_factory() as db:
                    await store.heartbeat(db, self._deployment_id)
            except Exception:
                # A blip must not kill the beat and silently hand the row to the next
                # claimant.
                _log.warning("deploy_heartbeat_failed", exc_info=True)


def _safe_detail(detail: str | None) -> str | None:
    """Redact then cap. The order matters: capping first can slice a credential in half and
    leave the recognizable prefix behind."""
    if not detail:
        return None
    return redact_secrets(detail)[:_DETAIL_MAX_CHARS]


def _answers_in(declaration: Mapping[str, Any], section: str) -> dict[str, bool]:
    """One answer block out of a declaration document (`deploy/gate` owns the shape).

    Read defensively — missing key, wrong type, a section that predates a question — and
    the missing answer is False, which is the policy table's own reading of an omitted key
    (`classification.total_weight`). Every direction of that leniency ADDS routing rather
    than removing it: an unreadable submitted baseline makes every Yes look new."""
    block = declaration.get(section)
    answers = block.get("answers") if isinstance(block, dict) else None
    if not isinstance(answers, dict):
        return {}
    return {str(key): bool(value) for key, value in answers.items()}


def _explanation_in(declaration: Mapping[str, Any]) -> str | None:
    """The citizen's R10 explanation, already redacted by the gate that stored it. On the
    drift path it was written about the EARLIER version — carried forward unchanged rather
    than dropped, with `drift.answeredAbout` naming the version it answers."""
    citizen = declaration.get("citizen")
    explanation = citizen.get("explanation") if isinstance(citizen, dict) else None
    return explanation if isinstance(explanation, str) else None


def _labels_for(keys: tuple[str, ...]) -> list[str]:
    """Questionnaire keys -> the labels the citizen saw on the form. The policy table is
    the only place that pairing lives, so a reworded question cannot leave this sentence
    naming something that is no longer on screen."""
    labels = {key: label for key, label, _weight in DATA_CLASSIFICATION_QUESTIONS}
    return [labels.get(key, key) for key in keys]


# --- the process-wide singleton ---------------------------------------------------

_service: DeployService | None = None


def get_deploy_service() -> DeployService:
    global _service
    if _service is None:
        from src.db.base import async_session_factory
        from src.services.deploy.aca_publish import get_published_apps
        from src.services.deploy.images import get_image_builder

        _service = DeployService(
            session_factory=async_session_factory,
            image_builder=get_image_builder(),
            published_apps=get_published_apps(),
        )
    return _service


def set_deploy_service_for_tests(service: DeployService | None) -> None:
    global _service
    _service = service


async def deployment_for_app(db: AsyncSession, *, app_id: uuid.UUID) -> Deployment | None:
    """The latest deploy attempt — the read behind the status endpoint."""
    return await store.latest_for_app(db, app_id=app_id)
