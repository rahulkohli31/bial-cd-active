"""V4 Part 3 — `deploy_app`: turn an auto-approved submission into a live Container
App, with no human step, called ONLY by `deploy-reconcile` (never from `submit()` —
see the module docstring in `api/v1/apps/router.py` and the plan's "background job,
not inline" decision).

Reuses every existing primitive rather than re-deriving them: the two runbook
credentials (`AppContainerStore.mint_deploy_container_sas`, `sandbox_dsn`) that
`deploy-credential`/`database-credential` already mint for a human, the raw ACA
control-plane (`AcaControlPlane.create_app`), and the supervisor's low-level
exec/files HTTP layer (`AcaSandboxClient.exec`/`.files`/`.wait_ready` — NOT its
higher `provision_new`/`restore_from_snapshot`, which are coupled to the one-
sandbox-per-user Redis registry and the wrong (mutable-snapshot) blob key; a
permanent per-app deployed container needs neither).

The container-creation + restore step is behind the `DeployRuntime` protocol so
this module is testable without live Azure (mirrors `FakeStorage`'s role for the
storage seam) — `RealDeployRuntime` is the live implementation, live-validated
against real ACA the same way `sandbox/aca.py` itself documents it must be
(mock-driven seam here, live join elsewhere).
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.project_database import ProjectDatabase
from src.services.appdb.errors import AppDatabaseUnconfiguredError
from src.services.appdb.provision import sandbox_dsn
from src.services.audit.log import append_audit
from src.services.sandbox.aca import AcaControlPlane, AcaError, AcaTransientError
from src.services.sandbox.base import FileCreate, SandboxClient, SandboxError, SandboxHandle
from src.services.sandbox.client import (
    _BUNDLE_B64_NAME,  # noqa: PLC2701 - deliberate same-package reuse, see module docstring
    _RESTORE_SCRIPT,  # noqa: PLC2701
    _RESTORE_TIMEOUT_SECONDS,  # noqa: PLC2701
)
from src.services.storage import ObjectStorage, StorageError, StorageNotFoundError, submission_key
from src.services.storage.app_containers import AppContainerStore

_log = structlog.get_logger()

# Distinct from `SANDBOX_NAME_PREFIX` ("sbx-") so `AcaControlPlane.list_sandbox_app_names`'s
# fleet filter — and the reaper that reads it — never sees, reports on, or offers to delete
# a permanently deployed app. `app_id.hex[:28]` mirrors `build_sessions.manager.app_name_for`
# exactly (ACA names are 2-32 chars, lowercase alphanumeric/hyphen, letter-first): 4 + 28 = 32.
DEPLOY_NAME_PREFIX = "app-"

# Bounds `last_deploy_error` to the column width (String(1000)) — an Azure SDK exception's
# `str()` can be arbitrarily long (embeds request/response bodies); never truncate silently
# past what the column can hold, and never let a giant message become a second failure mode.
_MAX_ERROR_LEN = 1000


def deploy_app_name(app_id: uuid.UUID) -> str:
    return f"{DEPLOY_NAME_PREFIX}{app_id.hex[:28]}"


class DeployRuntimeError(Exception):
    """Wraps any failure from the container-creation/restore step. `deploy_app` never
    lets a vendor (Azure SDK) or sandbox (`SandboxError`) type escape past this
    module — the reconcile endpoint's per-row report deals in one exception shape."""


@dataclass(frozen=True)
class DeployResult:
    fqdn: str
    """Public ACA ingress host, no scheme — `deployed_url` is `https://{fqdn}`."""


class DeployRuntime(Protocol):
    """The seam: create a permanent container from the golden image and restore the
    given bundle onto it, non-interactively, once. Real implementation below;
    fakeable in tests without any Azure/HTTP dependency."""

    async def deploy(self, *, name: str, env: dict[str, str], bundle: bytes) -> DeployResult: ...


class RealDeployRuntime:
    """Live implementation: `AcaControlPlane.create_app` (the raw ACA primitive, not
    the registry-coupled `SandboxClient.provision_new`) + the supervisor's exec/files
    HTTP layer, reusing the EXACT restore script the interactive sandbox path runs
    (`client._RESTORE_SCRIPT`) so the two never drift — imported, not duplicated."""

    def __init__(self, aca: AcaControlPlane, exec_client: SandboxClient) -> None:
        self._aca = aca
        self._exec = exec_client

    async def deploy(self, *, name: str, env: dict[str, str], bundle: bytes) -> DeployResult:
        try:
            fqdn = await self._aca.create_app(name=name, env=env)
        except (AcaError, AcaTransientError) as exc:
            raise DeployRuntimeError(f"container provisioning failed: {exc}") from exc

        # No supervisor bearer token is injected here (unlike the interactive sandbox
        # path) — a permanently deployed app has no interactive `/exec`/`/files`
        # caller after this one-time restore, so there is no token to protect. The
        # restore itself still needs SOME auth story once the supervisor requires
        # one on every image build; tracked as an open item, not silently assumed
        # away — see the PR body.
        handle = SandboxHandle(
            fqdn=fqdn, token="", app_name=name, preview_url=f"https://{fqdn}/", ready=False
        )
        try:
            await self._exec.wait_ready(handle, timeout_s=120.0)
        except SandboxError as exc:
            raise DeployRuntimeError(f"container did not become ready: {exc}") from exc

        encoded = base64.b64encode(bundle).decode("ascii")
        try:
            await self._exec.files(handle, FileCreate(path=_BUNDLE_B64_NAME, file_text=encoded))
            result = await self._exec.exec(
                handle, ["sh", "-c", _RESTORE_SCRIPT], timeout_s=_RESTORE_TIMEOUT_SECONDS
            )
        except SandboxError as exc:
            raise DeployRuntimeError(f"bundle restore failed: {exc}") from exc
        if result.exit != 0:
            raise DeployRuntimeError(f"restore script exited {result.exit}: {result.stderr[:500]}")

        return DeployResult(fqdn=fqdn)


def _truncated(message: str) -> str:
    return message if len(message) <= _MAX_ERROR_LEN else message[: _MAX_ERROR_LEN - 1] + "…"


async def deploy_app(
    app_id: uuid.UUID,
    *,
    db: AsyncSession,
    storage: ObjectStorage,
    container_store: AppContainerStore,
    runtime: DeployRuntime,
) -> bool:
    """The core, idempotent, retriable unit of work `deploy-reconcile` calls per
    eligible row. Returns `True` on success, `False` on a handled failure (the
    caller aggregates a report; nothing here raises for an ordinary deploy failure —
    only a genuinely unexpected error, e.g. a DB write failure, escapes).

    NO-OP (returns `True` immediately) unless the row is `APPROVED` with
    `redeploy_needed` — safe to call repeatedly without double-provisioning, and
    the ONLY guard against acting on a row a concurrent auto-reject just moved out
    from under this pass (re-checked here, not just by the reconciler's selection
    query, which can go stale between selection and this call)."""
    app = await db.get(AppRegistry, app_id)
    if app is None:
        return True
    if app.status is not AppStatus.APPROVED or app.approved_submission_id is None:
        return True
    if app.approved_submission_id == app.deployed_submission_id:
        return True  # already converged — not `redeploy_needed`

    submission_id = app.approved_submission_id
    try:
        bundle = await storage.get(submission_key(app_id, submission_id))
    except (StorageNotFoundError, StorageError) as exc:
        await _record_failure(db, app, f"could not read the approved submission bundle: {exc}")
        return False

    try:
        blob_credential = await container_store.mint_deploy_container_sas(app_id)
    except Exception as exc:  # noqa: BLE001 - any mint failure is a deploy failure, not a crash
        await _record_failure(db, app, f"could not mint the Blob deploy credential: {exc}")
        return False

    db_dsn = await _reveal_database_dsn(db, app)
    if db_dsn is None:
        await _record_failure(db, app, "this project has no ready database to deploy against")
        return False

    env = {
        "BIAL_APP_ID": str(app_id),
        "BIAL_BLOB_CONTAINER_URL": container_store.container_url(app_id),
        "BIAL_BLOB_SAS": blob_credential.sas,
        "BIAL_DATABASE_URL": db_dsn,
    }
    try:
        result = await runtime.deploy(name=deploy_app_name(app_id), env=env, bundle=bundle)
    except DeployRuntimeError as exc:
        await _record_failure(db, app, str(exc))
        return False

    now = datetime.now(UTC)
    app.deployed_submission_id = submission_id
    app.deployed_at = now
    app.deployed_url = f"https://{result.fqdn}"
    app.last_deploy_error = None
    await append_audit(
        db,
        actor_id=None,  # no human — the reconciler did this
        action="auto_deploy",
        resource_type="app",
        resource_id=str(app_id),
        detail={"submissionId": str(submission_id), "deployedUrl": app.deployed_url},
    )
    await db.commit()
    return True


async def _reveal_database_dsn(db: AsyncSession, app: AppRegistry) -> str | None:
    record = (
        await db.execute(
            sa.select(ProjectDatabase).where(
                ProjectDatabase.project_id == app.project_id,
                ProjectDatabase.db_ready.is_(True),
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    try:
        return sandbox_dsn(record)
    except AppDatabaseUnconfiguredError:
        return None


async def _record_failure(db: AsyncSession, app: AppRegistry, message: str) -> None:
    app.last_deploy_error = _truncated(message)
    await append_audit(
        db,
        actor_id=None,
        action="auto_deploy_failed",
        resource_type="app",
        resource_id=str(app.id),
        detail={"error": app.last_deploy_error},
    )
    await db.commit()
    _log.warning("auto_deploy_failed", app_id=str(app.id), error=message)
