"""The admin unpublish kill-switch (#113): `POST /v1/apps/{app_id}/unpublish`.

No real Azure anywhere — `PublishedAppRemover` is a `Protocol`
(`services/deploy/aca_publish.py`) exactly so a fake can stand in for it, the same
"no Azure, no network" philosophy `test_aca_publish.py` uses for anything below the ARM
SDK boundary. What's under test here is the route's own state machine (order of checks,
what gets written when, what doesn't), not whether Azure can delete a container app.

Every `Deployment` row is inserted directly rather than driven through a real deploy
pipeline — the pipeline itself is exercised in `test_deploy_routes.py`; this file only
needs rows already in a known terminal shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.auth.session_jwt import mint_session_jwt
from src.services.deploy.aca_publish import PublishedAppRemover
from src.services.deploy.names import published_app_name
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_UNPUBLISH = "/v1/apps/{app_id}/unpublish"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com -> super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _deployment(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    unpublished_at: datetime | None = None,
) -> Deployment:
    row = Deployment(
        app_id=app_id,
        user_id=user_id,
        status=status,
        image_digest="sha256:" + "ab" * 32,
        container_app_name=published_app_name(app_id),
        url=f"https://{published_app_name(app_id)}.example/",
        unpublished_at=unpublished_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


class FakeRemover:
    """Records every `delete_app` call; can be told to raise once, matching a real
    transient ARM failure that a retry then clears."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[uuid.UUID] = []
        self._fail_times = fail_times

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        self.calls.append(app_id)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("simulated ACA delete failure")


def _wire(app, remover: PublishedAppRemover) -> None:
    from src.api.v1.deploy.deps import published_app_remover_or_none

    app.dependency_overrides[published_app_remover_or_none] = lambda: remover


async def _owned_app(db: AsyncSession):
    owner = await UserFactory.create(db, email="builder@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db, user_id=owner.id)
    return owner, app_row


async def test_happy_path_unpublishes_and_audits(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_row.id)
    assert body["deploymentId"] == str(deployment.id)
    assert body["unpublishedAt"]
    # Pins router.py: `swept = await sweep_published_apps([app_id], client=remover)` — revert
    # that call (e.g. skip straight to `store.unpublish`) and this goes to 0.
    assert remover.calls == [app_row.id]

    row = await db_session.get(Deployment, deployment.id)
    assert row is not None
    assert row.unpublished_at is not None

    audit = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "unpublish"))
    ).scalar_one()
    assert audit.resource_type == "app"
    assert audit.resource_id == str(app_row.id)
    assert audit.detail["deploymentId"] == str(deployment.id)
    assert audit.detail["projectId"] == str(app_row.project_id)
    assert audit.detail["containerAppName"] == deployment.container_app_name


async def test_idempotent_repeat_does_not_call_azure_again(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover()
    _wire(app, remover)

    first = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)
    second = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["unpublishedAt"] == second.json()["unpublishedAt"]
    # Pins router.py: `if row.unpublished_at is not None: return ... ` (the skip branch) —
    # remove that early return and this becomes 2.
    assert len(remover.calls) == 1


async def test_aca_delete_failure_leaves_unpublished_at_unset_and_retry_succeeds(
    app, client, db_session
) -> None:
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover(fail_times=1)
    _wire(app, remover)

    failed = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert failed.status_code == 503
    row = await db_session.get(Deployment, deployment.id)
    assert row is not None
    # Pins router.py: `if swept == 0: raise AppApiError(503, ...)` running BEFORE
    # `store.unpublish` — reorder them and this becomes non-None on a failed call.
    assert row.unpublished_at is None
    no_audit = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "unpublish"))
    ).scalar_one_or_none()
    assert no_audit is None

    # Retry: the fake no longer raises (fail_times exhausted), same as a transient ARM
    # error clearing. Proves the design's retry-safety claim, not just the 503 itself.
    retried = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)
    assert retried.status_code == 200
    assert len(remover.calls) == 2


async def test_blocked_while_a_deploy_is_in_flight(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    succeeded = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    await _deployment(
        db_session, app_id=app_row.id, user_id=owner.id, status=DeploymentStatus.RUNNING
    )
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "deploy_in_flight"
    # Pins router.py: `if await store.in_flight(db, app_id=app_id) is not None: raise ...`
    # running BEFORE the teardown call — remove that check and `remover.calls` becomes
    # non-empty, and the line below fails.
    assert remover.calls == []
    row = await db_session.get(Deployment, succeeded.id)
    assert row is not None
    assert row.unpublished_at is None


async def test_never_published_is_a_409(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    _owner, app_row = await _owned_app(db_session)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "never_published"
    assert remover.calls == []


async def test_non_admin_is_forbidden(app, client, db_session) -> None:
    citizen_headers = await _citizen(db_session)
    owner, app_row = await _owned_app(db_session)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=citizen_headers)

    assert resp.status_code == 403
    assert remover.calls == []
    row = await db_session.get(Deployment, deployment.id)
    assert row is not None
    assert row.unpublished_at is None


async def test_republish_restores_the_app_at_the_same_url(db_session: AsyncSession) -> None:
    """Structural proof, at the store layer: `unpublished_at` lives on the ROW, so a later
    successful deploy — a NEW row — is what `last_successful` returns, unpublished or not.
    Pins `store.last_successful`'s `id DESC` ordering: if it ever returned the OLDEST
    succeeded row instead, this would return the unpublished one."""
    from src.services.deploy import store

    owner = await UserFactory.create(db_session, email="repub@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db_session, user_id=owner.id)

    first = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)
    await store.unpublish(db_session, first.id, at=datetime.now(UTC))

    second = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)

    current = await store.last_successful(db_session, app_id=app_row.id)
    assert current is not None
    assert current.id == second.id
    assert current.unpublished_at is None
    # Same URL: the container name is a pure function of the immutable app id, unaffected
    # by which deployment row is "current".
    assert current.container_app_name == first.container_app_name


@pytest.mark.parametrize("status", [DeploymentStatus.FAILED])
async def test_a_failed_only_deploy_history_is_also_never_published(
    app, client, db_session, status: DeploymentStatus
) -> None:
    """An app whose only deploy attempt FAILED has no succeeded row either — same 409 as
    never having tried, not a different error shape."""
    admin_headers = await _admin(db_session)
    owner, app_row = await _owned_app(db_session)
    await _deployment(db_session, app_id=app_row.id, user_id=owner.id, status=status)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=app_row.id), headers=admin_headers)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "never_published"


async def test_app_not_found_is_404(app, client, db_session) -> None:
    admin_headers = await _admin(db_session)
    remover = FakeRemover()
    _wire(app, remover)

    resp = await client.post(_UNPUBLISH.format(app_id=uuid.uuid4()), headers=admin_headers)

    assert resp.status_code == 404


async def test_unpublish_store_write_does_not_commit_on_its_own(db_session) -> None:
    """Review finding on #120: `store.unpublish` used to `db.commit()` on its own, landing
    the state change in a transaction separate from the `append_audit` write that follows
    it in `deploy/router.py` — a crash or failure between the two could leave the app
    durably unpublished with no audit record of who did it. Fixed by dropping that
    internal commit so both writes share the router's single trailing `db.commit()`
    (router.py, right after `append_audit`), mirroring `admin/router.py`'s `_transition`
    helper, which is equally commit-less for the same reason.

    Testing this end to end through the HTTP layer doesn't work in this suite: `db_session`
    (conftest.py) binds the whole test to ONE already-open connection-level transaction, so
    a mid-test `session.rollback()` unwinds back to the test's own start — including fixture
    setup — not just the request's writes, making a commit/then-fail/then-rollback dance
    indistinguishable from a plain reset. Spying on `db.commit` directly is what actually
    isolates the claim: `store.unpublish` itself must never call it, full stop — the router
    (already covered by `test_happy_path_unpublishes_and_audits`, which needs BOTH the row
    and the audit entry to appear) is the only place a commit is allowed to happen.

    Mutation receipt: restoring the deleted `await db.commit()` in `store.unpublish`
    (services/deploy/store.py) turns this red — `commits` stops being empty."""
    from src.services.deploy import store

    owner = await UserFactory.create(db_session, email="spy-owner@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    deployment = await _deployment(db_session, app_id=app_row.id, user_id=owner.id)

    commits = 0
    real_commit = db_session.commit

    async def spy_commit() -> None:
        nonlocal commits
        commits += 1
        await real_commit()

    db_session.commit = spy_commit
    try:
        settled = await store.unpublish(db_session, deployment.id, at=datetime.now(UTC))
    finally:
        db_session.commit = real_commit

    assert settled is True
    assert commits == 0
