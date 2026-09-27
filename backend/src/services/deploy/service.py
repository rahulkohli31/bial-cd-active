"""The deploy pipeline: saved code in, running app out.

Two halves with a hard line between them: a synchronous ROUTE that must finish in well
under a second — the edge gateway times out at twenty — resolving the app, refusing a
live build session, claiming the one in-flight slot, and handing back a deployment id;
and a detached PIPELINE that runs for minutes — extract, pack, build, provision, wait for
the revision, record the result — opening its own short database sessions rather than
borrowing the request's, exactly as the turn engine does.

The pipeline pins the commit it ships to the one the gate decided about. It reads the saved
snapshot, or — for an approved publish — the immutable submission copy the administrator
reviewed.

WHY THIS EXISTS: THE PIPELINE NEVER TOUCHES A SANDBOX — not the lock, the registry,
`provision_new`, or `restore_from_snapshot`. `restore` tears a container down BEFORE
pulling the bundle, and a confirmed-absent snapshot falls through to a blank golden
template that builds and deploys cleanly — replacing the citizen's app with the starter,
green checkmark and all. Deploy reads the bundle from storage and leaves the sandbox
alone.

Every failure lands in the deployment row (for the API) and the conversation (for the
citizen) — a build failure the citizen cannot see is one they cannot ask the agent to fix.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.core.redaction import redact_and_cap
from src.db.models.deployment import Deployment
from src.services.deploy import store
from src.services.deploy.aca_publish import PublishedAppProvisioner
from src.services.deploy.config import DeployConfig
from src.services.deploy.context import ContextTooLargeError, build_context_async
from src.services.deploy.env import PublishedStorageError, build_published_env
from src.services.deploy.images import ImageBuilder, ImageBuildError, ImageBuildTransientError
from src.services.deploy.names import image_reference, revision_name
from src.services.deploy.outcome import write_deploy_outcome
from src.services.orchestrator.errors import from_next_build, is_dependency_failure
from src.services.sandbox.aca import AcaError
from src.services.storage.bundle import BundleValidationError
from src.services.storage.snapshot_read import (
    NoAppYet,
    SnapshotExtractionError,
    extract_snapshot,
)
from src.services.turns.copy import DEPENDENCY_DRIFT_TEXT

_log = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Phase labels. Display only — never branched on, which is why `step` is a plain String.
# `restarting` is how a recycled revision is told apart from a fresh publish, since both are
# a `running` row.
STEP_PACKING: Final = "packing"
STEP_BUILDING: Final = "building"
STEP_PROVISIONING: Final = "provisioning"
STEP_STARTING: Final = "starting"
STEP_RESTARTING: Final = "restarting"

# Failure codes. Stable and greppable: an operator alerting on `acr_unauthorized` must not
# have to match on prose that a copy edit can change.
FAIL_NO_SNAPSHOT: Final = "no_saved_build"
FAIL_SNAPSHOT_UNREADABLE: Final = "snapshot_unreadable"
FAIL_SNAPSHOT_CORRUPT: Final = "snapshot_corrupt"
"""The stored bundle itself is malformed. Unlike `snapshot_unreadable`, reading it again cannot
help: the same bytes fail the same way."""
FAIL_CONTEXT_TOO_LARGE: Final = "context_too_large"
FAIL_BUILD: Final = "build_failed"
FAIL_BUILD_UNAVAILABLE: Final = "build_unavailable"
"""The platform could not produce the image: the registry was unreachable, the wait for the run
expired, or a run that reported success left no image. Not the app's own build failing."""
FAIL_STORAGE: Final = "storage_unavailable"
FAIL_PROVISION: Final = "provision_failed"
FAIL_NOT_HEALTHY: Final = "revision_unhealthy"
FAIL_NOT_READY: Final = "revision_not_ready"
"""The readiness budget expired with no verdict either way, kept apart from `revision_unhealthy`
for the reason `restart_not_ready` is kept apart from `restart_failed`: a slow app is not a
broken one."""
FAIL_INTERNAL: Final = "internal_error"

FAIL_RESTART: Final = "restart_failed"
"""The recycled revision did not come up, and something SAID SO — ARM refused the spec, or
reported the revision itself failed. The application that was already serving is untouched."""

FAIL_RESTART_NOT_READY: Final = "restart_not_ready"
"""The readiness budget expired with no verdict either way. NOT a death certificate: a slow
application is the commonest way to reach this, and the container that was already serving is
still serving. Kept apart from `restart_failed` so nothing downstream can read an unknown as a
terminal state and reach for a remedy that destroys work — the sandbox lost a citizen's unsaved
files to exactly that collapse."""

FAIL_SNAPSHOT_MOVED: Final = "snapshot_moved"
"""The extracted tree was not the commit the gate decided about — a save landed between
the claim and the extraction. Deliberately the SAME string the route's mid-request race
answers with (`deploy/router.py`'s 409), because it is the same event seen from a
different side of the 202, and a citizen reading both should not have to learn two words
for it. Fails closed: publishing an unexamined tree is the one outcome this feature
exists to prevent."""

FAIL_ROUTED_FOR_REVIEW: Final = "routed_for_review"
"""A code older deployment rows carry: that attempt sent its version to an administrator
instead of publishing it. Nothing writes it now; readers still present it as in review,
never as a red failure."""

_ROW_SPEAKS_TO_THE_CITIZEN: Final = frozenset({FAIL_RESTART, FAIL_RESTART_NOT_READY})
"""Settlements whose stored `detail` is the citizen's sentence rather than the operator's,
because nothing else will say it. A restart writes no chat card — it changes no code, so
there is nothing to tell the conversation — which leaves the deployment row as the only
surface a person reads a failed restart from. The operator's text still reaches the log."""

# How often the running pipeline renews its liveness stamp. Comfortably inside the
# staleness window so a slow ARM call never looks like a crash.
_HEARTBEAT_S: Final = store.HEARTBEAT_CADENCE_S

# How long to wait for the new revision to report healthy, and how often to ask.
_REVISION_POLL_S: Final = 3.0

# A failure detail that reaches the citizen. Bounded and redacted before it is stored: a
# build log is attacker-influenced text from a workspace the citizen's AI drove.
_DETAIL_MAX_CHARS: Final = 4_000

_UNREADABLE_SNAPSHOT: Final = (
    "Your saved app could not be read. This is a platform problem — please tell an administrator."
)


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
class LiveRevision:
    """The version an application is ALREADY SERVING — the only one a restart may run.

    Resolved by the caller from the deployment row that published it. `image_digest` is what
    the container is running, pinned by digest so it cannot silently resolve to newer bits;
    `head_sha` is the commit inside that image, carried onto the restart's own row so the
    record of what is live never names a version nobody published."""

    image_digest: str
    head_sha: str | None


class DeployService:
    """Owns the in-flight pipeline tasks. One process-wide instance."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        image_builder: ImageBuilder,
        published_apps: PublishedAppProvisioner,
    ) -> None:
        self._session_factory = session_factory
        self._images = image_builder
        self._aca = published_apps
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
        expected_commit_sha: str | None = None,
        bundle_key: str | None = None,
    ) -> StartedDeploy:
        """Claim the slot and detach the pipeline. Fast — the caller holds an HTTP request
        open and the edge gives it twenty seconds. The gate decided before any side effect;
        `expected_commit_sha` is an assertion, not a gate, that the tree extracted minutes
        later is the tree the gate decided about. `bundle_key` names the bundle to ship when
        it is not the saved snapshot — an approved submission copy."""
        deployment_id = await store.claim(db, app_id=app_id, user_id=user_id)
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
                bundle_key=bundle_key,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return StartedDeploy(deployment_id=deployment_id, app_id=app_id)

    async def restart(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        live: LiveRevision,
    ) -> StartedDeploy:
        """Claim the slot and detach the recycle. Fast, for the same reason `start` is:
        recycling a revision is still an ARM long-running operation and the edge gives the
        request twenty seconds.

        `live` is the version already serving, and it is the only version this may run —
        composing an image from anything else would put a commit the publish gate never
        examined into production. Claiming through the SAME one-in-flight slot a deploy
        claims is the whole of the concurrency story: a second press is refused, never a
        second operation against the same container."""
        deployment_id = await store.claim(db, app_id=app_id, user_id=user_id)
        if deployment_id is None:
            raise DeployNotPossibleError(
                "This app is already being deployed or restarted. Wait for that to finish, "
                "then try again.",
                code="deploy_in_flight",
            )

        task = asyncio.create_task(
            self._run(
                deployment_id=deployment_id,
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                # NO CONVERSATION, deliberately: a restart changes no code, so there is
                # nothing to tell the chat and nothing for the agent to be asked to repair.
                # The deployment row is the surface, and it carries the citizen's sentence.
                conversation_id=None,
                live=live,
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
        bundle_key: str | None = None,
        live: LiveRevision | None = None,
    ) -> None:
        """The detached pipeline. NEVER raises: an escaping exception leaves the row
        `running` until the stale-claim window expires, and the citizen staring at a
        Deploy button that 409s for half an hour."""
        async with self._beating(deployment_id):
            # THE PROMISE IN THE DOCSTRING, MADE TRUE RATHER THAN INTENDED. The arms below
            # settle the row by WRITING to it, and a write is exactly what fails when the
            # database is the thing that has gone — so the success arm sat outside the `except`
            # that was meant to catch it, and a `_fail` that raised escaped its own handler.
            # Either way the exception leaves this task, the row stays `running`, and an owner
            # watches a Deploy button 409 until the stale-claim window expires. The reconciler
            # settles it against ARM, which is the only thing that knows what really happened;
            # what this guarantees is that it gets the chance to.
            try:
                try:
                    # THE ONE FORK, AND IT IS A PRODUCT RULE RATHER THAN A CONVENIENCE. A restart
                    # recycles the revision already running and must never fall through to the
                    # publish pipeline, which ships the newest SAVED commit — that would make the
                    # button a way past the review the publish gate exists to route work through.
                    if live is not None:
                        url = await self._restart(
                            deployment_id=deployment_id,
                            app_id=app_id,
                            project_id=project_id,
                            user_id=user_id,
                            live=live,
                        )
                    else:
                        url = await self._deploy(
                            deployment_id=deployment_id,
                            app_id=app_id,
                            project_id=project_id,
                            user_id=user_id,
                            expected_commit_sha=expected_commit_sha,
                            bundle_key=bundle_key,
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
                        model_detail=failure.model_detail,
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
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("deploy_settle_crashed", deployment_id=str(deployment_id))

    async def _deploy(
        self,
        *,
        deployment_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        expected_commit_sha: str | None = None,
        bundle_key: str | None = None,
    ) -> str:
        """The happy path. Every failure leaves by raising `_DeployFailedError`."""
        # 1 — the saved code. `NoAppYet` is a NORMAL outcome (nobody has built yet), not an
        # error; an unreadable bundle is the opposite and must never read the same.
        try:
            extracted = await extract_snapshot(app_id, bundle_key=bundle_key)
        except SnapshotExtractionError as exc:
            raise _DeployFailedError(
                FAIL_SNAPSHOT_UNREADABLE, detail=str(exc), citizen_message=_UNREADABLE_SNAPSHOT
            ) from exc
        except BundleValidationError as exc:
            raise _DeployFailedError(
                FAIL_SNAPSHOT_CORRUPT, detail=str(exc), citizen_message=_UNREADABLE_SNAPSHOT
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

        # 1a — THE PIN. The gate decided about a commit it read off the snapshot
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
        except ImageBuildTransientError as exc:
            log = exc.log_tail
            raise _DeployFailedError(
                FAIL_BUILD_UNAVAILABLE,
                # The registry's log, through the same de-noiser a build failure uses, is what an
                # operator diagnoses the platform fault from.
                detail=f"{exc}\n\n{from_next_build(log).cleaned_stack}" if log else str(exc),
                citizen_message=(
                    "Your app could not be built because of a platform problem, so it was not "
                    "deployed. Your previous version is still running. Please try again."
                ),
            ) from exc
        except ImageBuildError as exc:
            raise _DeployFailedError.from_build(exc) from exc

        await self._advance(
            deployment_id, STEP_PROVISIONING, image_digest=built.digest, acr_run_id=built.run_id
        )

        # 4 — the runtime environment. Same database, same object-store container as the
        # sandbox.
        async with self._session_factory() as db:
            try:
                env, container_url = await build_published_env(
                    db, app_id=app_id, project_id=project_id, user_id=user_id
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
            # The returned FQDN is deliberately NOT bound. It used to become the app's recorded
            # address; that address is now the router's, composed from the container name. What
            # this call still provides is the SIDE EFFECT and the failure — the app exists, or it
            # raises — which is exactly what the `except` below is for.
            await self._aca.create_or_update(
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
        await self._await_healthy_revision(
            app_id=app_id,
            deployment_id=deployment_id,
            failed_code=FAIL_NOT_HEALTHY,
            failed_message=(
                "Your app was built but did not start. Your previous version is still "
                "running. This is usually a problem in the app itself — ask the assistant "
                "to check it."
            ),
            timeout_code=FAIL_NOT_READY,
            timeout_detail="the revision did not become healthy in time",
            timeout_message=(
                "Your app was built but did not start in time. Your previous version is "
                "still running. Please try again."
            ),
        )
        # THE ADDRESS A PERSON IS GIVEN, not the container's own. `fqdn` is still what proves the
        # app exists, and it is still where the platform reaches it — but BIAL's Container Apps
        # environment is internal and publishes no public DNS, so a colleague who is sent that
        # name cannot resolve it. This value is recorded on the deployment row, rendered as a
        # link in the outcome card, and shared outside the platform; it has to be the address
        # the router actually serves.
        # Composed from the container app's own name — `pub-` plus 28 hex of the app id,
        # derived rather than looked up — so the address is STABLE across redeploys. That
        # stability is the point: a link a colleague already holds keeps working after the
        # next publish.
        return settings.app_url(self._aca_name(app_id))

    async def _await_healthy_revision(
        self,
        *,
        app_id: uuid.UUID,
        deployment_id: uuid.UUID,
        failed_code: str,
        failed_message: str,
        timeout_code: str,
        timeout_detail: str,
        timeout_message: str,
    ) -> None:
        """Poll the revision until it is healthy, or settle this attempt with the caller's own
        verdict. Both the publish and the restart wait exactly this way and differ only in what
        they call the two endings, which is why the loop is written once and the words are not.

        A DEADLINE THAT PASSES IS NOT A VERDICT. Only ARM reporting the revision failed says
        anything about the application; an expired budget says how fast it answers. The caller's
        `timeout_message` is what has to carry that distinction to the citizen."""
        deadline = asyncio.get_running_loop().time() + self._aca_config.ready_timeout_s
        while True:
            state = await self._aca.get_revision(app_id=app_id, deployment_id=deployment_id)
            if state.healthy:
                return
            if state.failed:
                raise _DeployFailedError(
                    failed_code,
                    detail=f"revision provisioning state: {state.provisioning_state}",
                    citizen_message=failed_message,
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise _DeployFailedError(
                    timeout_code, detail=timeout_detail, citizen_message=timeout_message
                )
            await asyncio.sleep(_REVISION_POLL_S)

    # --- the restart -------------------------------------------------------------

    async def _restart(
        self,
        *,
        deployment_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        live: LiveRevision,
    ) -> str:
        """Re-issue the container spec for the version already running, then wait for the new
        revision. Returns the app's address, which never moves.

        NOTHING HERE READS THE SAVED SNAPSHOT AND NOTHING HERE BUILDS AN IMAGE: the image is
        the digest the live deployment recorded, so the version that comes back is the version
        that went down. Digest-pinning is the backstop under that rule, not a substitute for
        it — a tag would let ARM resolve newer bits at revision time."""
        # The phase is written before anything slow, so a client polling mid-restart is never
        # left reading the generic claimed step. The digest rides the same statement: it is
        # what lets the crash reconciler prove this row's revision is the one ARM is serving,
        # rather than failing a restart that actually landed.
        await self._advance(
            deployment_id,
            STEP_RESTARTING,
            head_sha=live.head_sha,
            image_digest=live.image_digest,
        )

        # Rebuilt, not reused: create-or-update REPLACES the whole spec, so leaving the
        # environment out would strip the app's database and storage credentials on the way
        # back up. Same builder the publish path uses, so the two cannot describe one app
        # differently.
        async with self._session_factory() as db:
            try:
                env, container_url = await build_published_env(
                    db, app_id=app_id, project_id=project_id, user_id=user_id
                )
            except PublishedStorageError as exc:
                raise _DeployFailedError(
                    FAIL_RESTART,
                    detail=str(exc),
                    citizen_message=(
                        "Your app could not be given access to its file storage, so it was "
                        "not restarted. Please tell an administrator."
                    ),
                ) from exc

        image = image_reference(
            acr_server=self._aca_config.acr_server,
            repository_prefix=self._aca_config.image_repository_prefix,
            app_id=app_id,
            digest=live.image_digest,
        )
        try:
            await self._aca.create_or_update(
                app_id=app_id,
                deployment_id=deployment_id,
                image=image,
                env=env,
                container_url=container_url,
            )
        except AcaError as exc:
            raise _DeployFailedError(
                FAIL_RESTART,
                detail=str(exc),
                citizen_message=(
                    "Your app could not be restarted. The version that was already running "
                    "is still running — please try again."
                ),
            ) from exc

        # STILL `restarting`, not `starting`: a restart is ONE phase to the person watching,
        # and the publish pipeline already owns `starting` for its own readiness wait — so
        # moving to it here would make a recycled revision indistinguishable from a fresh
        # publish for the whole window the client is actually polling. The names are what
        # this statement is really for.
        await self._advance(
            deployment_id,
            STEP_RESTARTING,
            container_app_name=self._aca_name(app_id),
            revision_name=revision_name(app_id, deployment_id),
        )
        # A FAILED REVISION AND AN EXPIRED BUDGET ARE NOT THE SAME EVENT, and only the first
        # is a statement about the application: a slow-but-healthy app is the commonest way to
        # reach the second, and the container that was already serving is still serving in
        # both. Both settle this attempt and stop, so neither can reach a remedy that removes
        # the container or puts a saved bundle back over work the citizen has not saved.
        await self._await_healthy_revision(
            app_id=app_id,
            deployment_id=deployment_id,
            failed_code=FAIL_RESTART,
            failed_message=(
                "Your app did not come back up. A restart runs the same version again, so if "
                "it keeps failing the fault is in the app itself — ask the assistant to check "
                "it."
            ),
            timeout_code=FAIL_RESTART_NOT_READY,
            timeout_detail="the recycled revision did not report healthy in time",
            timeout_message=(
                "Your app is taking longer than expected to come back. Nothing was changed, "
                "and the version that was already running is still running — please try again "
                "in a moment."
            ),
        )
        return settings.app_url(self._aca_name(app_id))

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
        # None where the citizen sentence names the fault itself, which is every class but one.
        model_detail: str | None = None,
    ) -> None:
        safe = redact_and_cap(detail, _DETAIL_MAX_CHARS)
        # The same redactor and the same ceiling as the operator detail: this string now reaches
        # a model holding a shell, which is a harder egress than the deployment row.
        safe_for_the_model = redact_and_cap(model_detail, _DETAIL_MAX_CHARS)
        stored = citizen_message if code in _ROW_SPEAKS_TO_THE_CITIZEN else safe
        async with self._session_factory() as db:
            settled = await store.fail(db, deployment_id, code=code, detail=stored)
        if not settled:
            _log.warning("deploy_already_settled", deployment_id=str(deployment_id))
            return
        _log.warning("deploy_failed", deployment_id=str(deployment_id), code=code, detail=safe)
        await self._tell_the_citizen(
            user_id=user_id,
            conversation_id=conversation_id,
            deployment_id=deployment_id,
            app_id=app_id,
            succeeded=False,
            message=citizen_message,
            detail=safe,
            model_detail=safe_for_the_model,
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
        model_detail: str | None = None,
    ) -> None:
        """Write the outcome into the chat. Best-effort by design: the deployment row is the
        record of truth, and a chat write that fails must not undo a deploy that worked.

        `model_detail` rides a hidden row of its own — see `outcome.py`."""
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
                    model_detail=model_detail,
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
    """A pipeline failure with everything all three audiences need: a stable code for the row
    and an operator's alert, prose for the citizen, and — only where that prose deliberately
    names no fault — the fault itself for the agent that will be asked to repair it.

    `model_detail` None means the citizen sentence already carries the fault, so nothing extra
    is owed the model; sending it anyway would put the same diagnostic in history twice."""

    def __init__(
        self,
        code: str,
        *,
        detail: str | None,
        citizen_message: str,
        model_detail: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.citizen_message = citizen_message
        self.model_detail = model_detail

    @classmethod
    def from_build(cls, exc: ImageBuildError) -> _DeployFailedError:
        """A build failure is the one the citizen can actually act on, so it carries the
        registry's own log through the same de-noiser the self-heal loop uses — ANSI
        stripped, paths relativized, secrets redacted, and titled on the line that names the
        fault rather than the Next.js banner.

        THE TITLE IS NOT ALWAYS SAYABLE TO A CITIZEN. A failed dependency install titles on a
        package name and two version numbers, so that class reads a written sentence and the
        fault travels on `model_detail` and the operator detail instead."""
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
        hides_the_fault = is_dependency_failure(error)
        return cls(
            FAIL_BUILD,
            # The title LEADS the detail rather than being left to be found inside it: both
            # this field and `cleaned_stack` are capped from the FRONT, and a registry log can
            # open with more preamble than either cap allows before the fault is named.
            detail=f"{error.title}\n\n{error.cleaned_stack}",
            citizen_message=(
                DEPENDENCY_DRIFT_TEXT
                if hides_the_fault
                else (
                    f"Your app did not build, so it was not deployed:\n\n{error.title}\n\n"
                    "Your previous version is still running. Ask me to fix it and try again."
                )
            ),
            model_detail=error.title if hides_the_fault else None,
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


async def deployment_for_app(db: AsyncSession, *, app_id: uuid.UUID) -> Deployment | None:
    """The latest deploy attempt — the read behind the status endpoint."""
    return await store.latest_for_app(db, app_id=app_id)
