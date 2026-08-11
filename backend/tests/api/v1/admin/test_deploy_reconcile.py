"""POST /v1/admin/apps/reconcile-deploys — the operator lever deploy reconciliation never had.

Three of the four reconciling sweeps already had one; this one was reachable only from the boot
path and a 300-second in-process timer, so an operator staring at an app whose Deploy button 409s
could either wait out thirty minutes of `DEPLOY_STALE_AFTER_S` or restart the control plane.

The reconciler's own four-answer logic is pinned in `tests/services/deploy/test_reconcile.py` and
its scheduled wrapper in `tests/workers/test_deploy_reconcile.py`. This file pins the ROUTE: who
may call it, what the wire body says, what reaches the audit trail, and which failures are
retryable — the same set its three siblings carry (`test_sandbox_reconcile.py`,
`test_database_reconcile.py`, `test_storage_reconcile.py`), because `.claude/rules/testing.md`
asks for RBAC to be tested rather than reasoned about.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.auth.session_jwt import mint_session_jwt
from src.services.deploy import reconcile as deploy_reconcile_service
from src.services.deploy import store
from src.services.sandbox.aca import AcaTransientError
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_RECONCILE = "/v1/admin/apps/reconcile-deploys"
_DIGEST = "sha256:" + "ef" * 32


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


class _Arm:
    """Answers as ARM does: `None` is a CONFIRMED absence, a blip RAISES. Not a subclass of the
    real publish client — the route only ever calls the two read methods, and inheriting the
    concrete class would drag a `DefaultAzureCredential` into the test."""

    def __init__(self, *, fqdn: str | None, image: str | None = None, blip: bool = False) -> None:
        self.fqdn = fqdn
        self.image = image
        self.blip = blip

    async def get_app_fqdn(self, *, app_id: uuid.UUID) -> str | None:
        if self.blip:
            raise AcaTransientError("ARM is throttling")
        return self.fqdn

    async def get_app_image(self, *, app_id: uuid.UUID) -> str | None:
        return self.image


def _wire(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession, arm: _Arm) -> None:
    """Point the route at the test's transaction and at a fake ARM.

    Patched on the ROUTER's own module attributes, not on the services they come from: the router
    binds both names at import, so `src.db.base.async_session_factory` and the publish accessor in
    their home modules are no longer what this route reads. Python resolves module globals at CALL
    time, which is what makes patching them here take effect.
    """

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        # Yields the fixture's session WITHOUT closing it. `async with AsyncSession(...)` closes on
        # exit and the reconciler opens one per row, so a naive factory would close the session out
        # from under the second row — and the test's rollback with it.
        yield db_session

    monkeypatch.setattr("src.api.v1.admin.router.async_session_factory", lambda: _session())
    monkeypatch.setattr("src.api.v1.admin.router.get_published_apps", lambda: arm)


async def _abandoned(db: AsyncSession) -> tuple[Any, uuid.UUID]:
    """An app plus a deployment row whose pipeline stopped beating."""
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    deployment_id = await store.claim(db, app_id=app.id, user_id=user.id)
    assert deployment_id is not None
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id)
        .values(
            image_digest=_DIGEST,
            heartbeat_at=datetime.now(UTC)
            - timedelta(seconds=deploy_reconcile_service.STALE_AFTER_S + 60),
        )
    )
    return app, deployment_id


# --- the gate ---------------------------------------------------------------------


async def test_citizen_is_forbidden(client, db_session) -> None:
    assert (await client.post(_RECONCILE, headers=await _citizen(db_session))).status_code == 403


async def test_unauthenticated_is_401(client) -> None:
    assert (await client.post(_RECONCILE)).status_code == 401


# --- the body ---------------------------------------------------------------------


async def test_a_stalled_deploy_is_settled_and_counted(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lever, end to end: the operator presses it and the wedged row settles, without waiting
    thirty minutes for the next claim to take it over."""
    admin = await _admin(db_session)
    _app, deployment_id = await _abandoned(db_session)
    _wire(monkeypatch, db_session, _Arm(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}"))

    response = await client.post(_RECONCILE, headers=admin)

    assert response.status_code == 200
    assert response.json() == {"resolved": 1}
    row = await db_session.get(Deployment, deployment_id)
    await db_session.refresh(row)
    assert row.status is DeploymentStatus.SUCCEEDED


async def test_nothing_to_settle_reports_zero(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await _admin(db_session)
    _wire(monkeypatch, db_session, _Arm(fqdn=None))

    assert (await client.post(_RECONCILE, headers=admin)).json() == {"resolved": 0}


async def test_a_row_arm_could_not_answer_for_is_not_reported_as_resolved(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ARM that cannot answer is not an ARM saying the app is gone. The row stays exactly as it
    was, so the next pass — or the next press of this button — asks again; counting it as resolved
    would mean nobody ever looks at it again."""
    admin = await _admin(db_session)
    _app, deployment_id = await _abandoned(db_session)
    _wire(monkeypatch, db_session, _Arm(fqdn="pub-x.example.io", blip=True))

    assert (await client.post(_RECONCILE, headers=admin)).json() == {"resolved": 0}
    row = await db_session.get(Deployment, deployment_id)
    await db_session.refresh(row)
    assert row.status is DeploymentStatus.RUNNING
    assert row.failure_code is None


# --- the audit trail --------------------------------------------------------------


async def test_the_audit_row_carries_counts_but_no_deployment_or_app(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counts only (`.claude/rules/security.md`), the same split every sibling report makes. A
    deployment id or an app name in the trail turns an accountability record into a durable
    inventory of who deployed what — and unlike the sandbox report, this one has no operator need
    for names at all, so nothing identifying travels in the response either."""
    admin = await _admin(db_session)
    app, deployment_id = await _abandoned(db_session)
    _wire(monkeypatch, db_session, _Arm(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}"))

    assert (await client.post(_RECONCILE, headers=admin)).status_code == 200

    row = await db_session.scalar(select(AuditLog).where(AuditLog.action == "deploy:reconcile"))
    assert row is not None
    assert row.resource_type == "deployment"
    assert row.resource_id is None
    assert row.detail == {"resolved": 1}
    trail = f"{row.resource_id}{row.detail}"
    assert str(deployment_id) not in trail
    assert str(app.id) not in trail


# --- the failure surface ----------------------------------------------------------


async def test_an_unconfigured_publish_plane_is_503_not_a_clean_report(client, db_session) -> None:
    """Deliberately unpatched: `.env.test` carries no `DEPLOY__*` block, so this is the real
    unconfigured path. It must not answer "resolved 0" — that is the opposite fact from "this
    deployment cannot reconcile deploys at all", and it is the one that gets a wedged row
    forgotten."""
    admin = await _admin(db_session)
    assert settings.deploy is None

    response = await client.post(_RECONCILE, headers=admin)

    assert response.status_code == 503
    assert "try again" in response.json()["error"]["message"].lower()


def test_openapi_documents_the_route() -> None:
    from src.main import create_app

    responses = set(create_app().openapi()["paths"][_RECONCILE]["post"]["responses"])
    assert {"200", "503", "401", "403"} <= responses
