"""The deploy pipeline: saved code in, running app out.

Two halves with a hard line between them.

The ROUTE half is synchronous and must finish in well under a second — the edge gateway
times out at twenty. It resolves the app, refuses if a build session is live, claims the
one in-flight slot, and hands back a deployment id.

The PIPELINE half is a detached task that runs for minutes: extract the snapshot, pack a
build context, build an image, provision the container app, wait for the revision, record
the result. It outlives its request, so it never borrows the request's database session —
it opens short ones of its own, exactly as the turn engine does.

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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.redaction import redact_secrets
from src.db.models.deployment import Deployment
from src.services.deploy import store
from src.services.deploy.aca_publish import PublishedAppProvisioner
from src.services.deploy.config import DeployConfig
from src.services.deploy.context import ContextTooLargeError, build_context_async
from src.services.deploy.env import PublishedStorageError, build_published_env
from src.services.deploy.images import ImageBuilder, ImageBuildError
from src.services.deploy.names import image_reference, revision_name
from src.services.deploy.outcome import write_deploy_outcome
from src.services.orchestrator.errors import from_next_build
from src.services.sandbox.aca import AcaError
from src.services.storage.snapshot_read import (
    NoAppYet,
    SnapshotExtractionError,
    extract_snapshot,
)

_log = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Phase labels. Display only — never branched on, which is why `step` is a plain String.
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

# How often the running pipeline renews its liveness stamp. Comfortably inside the
# staleness window so a slow ARM call never looks like a crash.
_HEARTBEAT_S: Final = store.HEARTBEAT_CADENCE_S

# How long to wait for the new revision to report healthy, and how often to ask.
_REVISION_POLL_S: Final = 3.0

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
    ) -> StartedDeploy:
        """Claim the slot and detach the pipeline. Fast — the caller is holding an HTTP
        request open and the edge gives it twenty seconds."""
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
    ) -> None:
        """The detached pipeline. NEVER raises: an escaping exception would leave the row
        `running` until the stale-claim window expires, and the citizen staring at a Deploy
        button that 409s for half an hour."""
        async with self._beating(deployment_id):
            try:
                url = await self._deploy(
                    deployment_id=deployment_id, app_id=app_id, project_id=project_id
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
        self, *, deployment_id: uuid.UUID, app_id: uuid.UUID, project_id: uuid.UUID
    ) -> str:
        """The happy path. Every failure leaves by raising `_DeployFailedError`."""
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
