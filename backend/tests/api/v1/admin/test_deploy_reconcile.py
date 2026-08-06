"""POST /admin/apps/deploy-reconcile (V4 Part 3) — the operator-invoked auto-deploy
sweep. Mirrors `reconcile-storage`/`reconcile-sandboxes` in posture: superadmin-gated,
idempotent, one row's failure never aborts the pass.

`deploy_app` itself is unit-tested in `tests/services/deploy/test_provision.py` — these
tests monkeypatch it (and the ACA control-plane constructor) so the endpoint's OWN job
(the kill switch, the eligibility query, report aggregation) is exercised without any
real Azure dependency."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

import src.api.v1.admin.router as admin_router
from src.api.deps import container_store_dependency, storage_or_none_dependency
from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db_session: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db_session, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _approved_app(db_session: AsyncSession, *, redeploy_needed: bool = True, **overrides):
    owner = await UserFactory.create(db_session)
    approved = overrides.pop("approved_submission_id", uuid.uuid4())
    deployed = None if redeploy_needed else approved
    return await AppRegistryFactory.create(
        db_session,
        user_id=owner.id,
        status=AppStatus.APPROVED,
        approved_submission_id=approved,
        deployed_submission_id=deployed,
        **overrides,
    )


class _FakeAca:
    async def aclose(self) -> None:
        return None


def _sandbox_ready(monkeypatch, app) -> None:
    """Makes the endpoint's `sandbox is None or settings.sandbox is None` guard pass, and
    the ACA control-plane construction inert — no real Azure dependency anywhere."""
    monkeypatch.setattr(settings, "sandbox", object())
    monkeypatch.setattr(admin_router, "create_aca_control_plane", lambda config: _FakeAca())
    app.dependency_overrides[storage_or_none_dependency] = lambda: FakeStorage()
    app.dependency_overrides[container_store_dependency] = lambda: object()
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()


async def test_deploy_reconcile_409s_when_the_kill_switch_is_off(
    client, db_session, monkeypatch
) -> None:
    headers = await _admin(db_session)
    resp = await client.post("/v1/admin/apps/deploy-reconcile", headers=headers)
    assert resp.status_code == 409
    assert "disabled" in resp.json()["error"]["message"].lower()


async def test_deploy_reconcile_503s_when_the_sandbox_is_unconfigured(
    client, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings.deploy, "auto_deploy_enabled", True)
    monkeypatch.setattr(settings, "sandbox", None)
    headers = await _admin(db_session)
    resp = await client.post("/v1/admin/apps/deploy-reconcile", headers=headers)
    assert resp.status_code == 503


async def test_deploy_reconcile_selects_exactly_the_redeploy_needed_rows(
    client, app, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings.deploy, "auto_deploy_enabled", True)
    _sandbox_ready(monkeypatch, app)

    needs_deploy = await _approved_app(db_session, redeploy_needed=True)
    already_deployed = await _approved_app(db_session, redeploy_needed=False)
    draft_owner = await UserFactory.create(db_session)
    draft = await AppRegistryFactory.create(
        db_session, user_id=draft_owner.id, status=AppStatus.DRAFT
    )
    await db_session.commit()

    attempted_ids: list[uuid.UUID] = []

    async def _fake_deploy_app(app_id, **kwargs):
        attempted_ids.append(app_id)
        return True

    monkeypatch.setattr(admin_router, "deploy_app", _fake_deploy_app)

    headers = await _admin(db_session)
    resp = await client.post("/v1/admin/apps/deploy-reconcile", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["attempted"] == 1
    assert body["succeeded"] == 1
    assert body["failed"] == 0
    assert body["failures"] == []
    assert attempted_ids == [needs_deploy.id]
    assert already_deployed.id not in attempted_ids
    assert draft.id not in attempted_ids


async def test_deploy_reconcile_reports_a_per_row_failure_without_aborting_the_pass(
    client, app, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings.deploy, "auto_deploy_enabled", True)
    _sandbox_ready(monkeypatch, app)

    failing = await _approved_app(db_session, redeploy_needed=True)
    succeeding = await _approved_app(db_session, redeploy_needed=True)
    await db_session.commit()

    async def _fake_deploy_app(app_id, *, db: AsyncSession, **kwargs):
        if app_id == failing.id:
            row = await db.get(AppRegistry, app_id)
            row.last_deploy_error = "container did not become ready: timeout"
            await db.commit()
            return False
        return True

    monkeypatch.setattr(admin_router, "deploy_app", _fake_deploy_app)

    headers = await _admin(db_session)
    resp = await client.post("/v1/admin/apps/deploy-reconcile", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["attempted"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    [failure] = body["failures"]
    assert failure["appId"] == str(failing.id)
    assert "timeout" in failure["error"]
    assert succeeding.id  # sanity: the fixture built without error


async def test_deploy_reconcile_requires_superadmin(client, db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings.deploy, "auto_deploy_enabled", True)
    user = await UserFactory.create(db_session, email="nobody@rvaiglobal.com")
    headers = _cookie(mint_session_jwt(user.id, user.token_version, _TTL))
    resp = await client.post("/v1/admin/apps/deploy-reconcile", headers=headers)
    assert resp.status_code == 403
