"""The superadmin deploy-credential mint (U2/R2): `POST /v1/admin/apps/{appId}/deploy-credential`.

The long-lived, container-scoped Blob credential a DEPLOYED app uses to reach its own storage
directly — the runbook's step-5 env pair, and the end of its KNOWN GAP. What these tests pin:
the gate (superadmin-only, owner-agnostic), the fail-closed 409 on a config that physically
cannot mint one, the 503-on-ambiguity twin of `approve`/`bundle-url`, and the security contract
— the SAS is returned to the admin ONCE and appears in neither the audit trail nor the logs.

The last test is the composition-root check (`mocks-mask-composition-seams`, 2026-07-15): it
mints through the REAL `AppContainerStore` against Azurite with NO `dependency_overrides` on the
container-store seam, so the wiring itself is under test and not stubbed past.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
import structlog
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import container_store_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import AppContainerStore, StorageError, reset_storage_for_tests
from src.services.storage.app_containers import DEPLOY_POLICY_PREFIX, DeployCredential
from src.services.storage.config import AzureStorageConfig
from src.services.storage.constants import DEPLOY_SAS_TTL
from src.services.storage.errors import StorageSignError
from tests.factories import AppRegistryFactory, UserFactory
from tests.services.storage.conftest import AZURITE_ACCOUNT_URL, AZURITE_CONN, _port_open

_TTL = settings.auth.access_ttl_seconds
_FAKE_SAS = "sv=2026-06-06&si=deploy&sr=c&sig=THISISTHEBEARERTOKEN"


class _FakeContainerStore(AppContainerStore):
    """A container-store double for the router tests. Skips `AppContainerStore.__init__` on
    purpose (no Azure config): the endpoint only calls `mint_deploy_container_sas` +
    `container_url`, both overridden here — mirrors `_RecordingContainerStore` in
    test_apps_governance."""

    def __init__(self, *, error: Exception | None = None) -> None:  # noqa: D107 — see docstring
        self.error = error
        self.minted: list[uuid.UUID] = []

    async def mint_deploy_container_sas(
        self, app_id: uuid.UUID, *, ttl: timedelta = DEPLOY_SAS_TTL
    ) -> DeployCredential:
        if self.error is not None:
            raise self.error
        self.minted.append(app_id)
        return DeployCredential(sas=_FAKE_SAS, expires_at=datetime.now(UTC) + ttl)

    def container_url(self, app_id: uuid.UUID, *, base_url: str | None = None) -> str:
        return f"https://acct.blob.core.windows.net/app-{app_id}"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _app_row(db: AsyncSession, **overrides) -> AppRegistry:
    owner = await UserFactory.create(db)
    return await AppRegistryFactory.create(
        db, user_id=owner.id, status=AppStatus.APPROVED, **overrides
    )


def _wire(app, store: _FakeContainerStore | None) -> None:
    app.dependency_overrides[container_store_dependency] = lambda: store


def _url(app_id: uuid.UUID) -> str:
    return f"/v1/admin/apps/{app_id}/deploy-credential"


async def _audit_rows(db: AsyncSession, app_id: uuid.UUID) -> list[AuditLog]:
    rows = await db.execute(
        sa.select(AuditLog).where(
            AuditLog.action == "deploy-credential:mint", AuditLog.resource_id == str(app_id)
        )
    )
    return list(rows.scalars())


async def test_mint_returns_the_credential_and_audits_the_event(app, client, db_session) -> None:
    row = await _app_row(db_session)
    store = _FakeContainerStore()
    _wire(app, store)

    resp = await client.post(_url(row.id), headers=await _admin(db_session))

    assert resp.status_code == 200
    body = resp.json()
    # camelCase wire parity with the rest of the admin surface.
    assert body["containerUrl"] == f"https://acct.blob.core.windows.net/app-{row.id}"
    assert body["sas"] == _FAKE_SAS
    # Long-lived by construction: ~365 days out, not the 7-day session ceiling.
    expires_at = datetime.fromisoformat(body["expiresAt"])
    assert expires_at > datetime.now(UTC) + timedelta(days=364)
    assert store.minted == [row.id]

    (audit,) = await _audit_rows(db_session, row.id)
    assert audit.resource_type == "app"
    assert audit.detail == {"expiresAt": expires_at.isoformat()}


async def test_mint_never_writes_the_sas_to_the_audit_trail_or_the_logs(
    app, client, db_session
) -> None:
    # security.md: never log a credential value. The audit row proves WHO minted WHAT and when it
    # dies — the token itself is handed to the admin once and is never persisted by us.
    row = await _app_row(db_session)
    _wire(app, _FakeContainerStore())

    with structlog.testing.capture_logs() as logs:
        resp = await client.post(_url(row.id), headers=await _admin(db_session))
    assert resp.status_code == 200

    (audit,) = await _audit_rows(db_session, row.id)
    assert _FAKE_SAS not in str(audit.detail)
    assert "sig=" not in str(audit.detail)
    assert "THISISTHEBEARERTOKEN" not in str(logs)


async def test_managed_identity_config_is_a_409_with_a_fix(app, client, db_session) -> None:
    # A user-delegation SAS cannot outlive 7 days, so this config physically cannot mint the
    # credential. Say so — and say what to change — rather than minting a credential that strands
    # the app in a week.
    row = await _app_row(db_session)
    _wire(app, _FakeContainerStore(error=StorageSignError("no account key", provider="azure")))

    resp = await client.post(_url(row.id), headers=await _admin(db_session))

    assert resp.status_code == 409
    detail = resp.json()["error"]["message"]
    assert "managed identity" in detail
    assert "account key" in detail
    assert not await _audit_rows(db_session, row.id)  # nothing minted → nothing to record


async def test_unconfigured_storage_is_a_409(app, client, db_session) -> None:
    # Object storage off (the dependency yields None) → no container exists to scope a
    # credential to. Fail closed with the reason, never a 500.
    row = await _app_row(db_session)
    _wire(app, None)

    resp = await client.post(_url(row.id), headers=await _admin(db_session))

    assert resp.status_code == 409
    assert "not configured" in resp.json()["error"]["message"]


async def test_transient_storage_error_is_a_503(app, client, db_session) -> None:
    # The fail-closed twin of approve/bundle-url: a storage blip is ambiguity, not a rejection.
    row = await _app_row(db_session)
    _wire(app, _FakeContainerStore(error=StorageError("blip", provider="azure")))

    resp = await client.post(_url(row.id), headers=await _admin(db_session))

    assert resp.status_code == 503
    assert not await _audit_rows(db_session, row.id)


async def test_citizen_cannot_mint_a_deploy_credential(app, client, db_session) -> None:
    row = await _app_row(db_session)
    store = _FakeContainerStore()
    _wire(app, store)

    resp = await client.post(_url(row.id), headers=await _citizen(db_session))

    assert resp.status_code == 403
    assert store.minted == []  # the gate runs BEFORE any minting


async def test_unknown_app_is_a_404(app, client, db_session) -> None:
    store = _FakeContainerStore()
    _wire(app, store)

    resp = await client.post(_url(uuid.uuid4()), headers=await _admin(db_session))

    assert resp.status_code == 404
    assert store.minted == []  # never provision a container for an app that doesn't exist


@pytest.mark.integration
async def test_composition_root_mints_through_the_real_store_against_azurite(
    app, client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `dependency_overrides` on the container-store seam: `container_store_dependency` →
    `get_app_container_store()` → a REAL `AppContainerStore` → Azurite. A double on this seam
    would prove only that our double works (the 2026-07-15 composition-root lesson); this proves
    the endpoint actually mints a credential a deployed app could use."""
    if not _port_open("127.0.0.1", 10000):
        pytest.skip(
            "Azurite not reachable on :10000 — `docker compose -f docker-compose.test.yml up -d`"
        )
    await reset_storage_for_tests()
    config = AzureStorageConfig(
        account_url=AZURITE_ACCOUNT_URL,
        container="platform",
        connection_string=SecretStr(AZURITE_CONN),
    )
    monkeypatch.setattr(settings, "object_store", config)
    assert container_store_dependency not in app.dependency_overrides  # the REAL dep runs
    row = await _app_row(db_session)

    try:
        resp = await client.post(_url(row.id), headers=await _admin(db_session))

        assert resp.status_code == 200
        body = resp.json()
        assert body["containerUrl"] == f"{AZURITE_ACCOUNT_URL}/app-{row.id}"
        # A real Azure-signed, policy-referencing SAS — the `si=` handle is the revocation lever.
        assert f"si={DEPLOY_POLICY_PREFIX}" in body["sas"]
        assert "sig=" in body["sas"]
        (audit,) = await _audit_rows(db_session, row.id)
        assert "sig=" not in str(audit.detail)
    finally:
        await AppContainerStore(config).delete_container(row.id)
        await reset_storage_for_tests()
