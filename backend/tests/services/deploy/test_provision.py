"""services/deploy/provision.py::deploy_app (V4 Part 3) — the core, idempotent unit
of work `deploy-reconcile` calls per row. Fakes the `DeployRuntime` seam (mirrors
`FakeStorage`'s role for the storage seam) and the container-store's SAS mint, so
this exercises the real DB guard/write logic with no Azure dependency at all."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import src.services.deploy.provision as provision_module
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.project_database import ProjectDatabase
from src.services.deploy.provision import (
    DeployResult,
    DeployRuntimeError,
    deploy_app,
    deploy_app_name,
)
from src.services.storage import submission_key
from src.services.storage.app_containers import AppContainerStore, DeployCredential
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_SHA = "de" * 20
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-deploy"
_FAKE_FQDN = "app-fake.westeurope.azurecontainerapps.io"


class _FakeContainerStore(AppContainerStore):
    """Skips `AppContainerStore.__init__` on purpose (no Azure config to hold) —
    mirrors `_RecordingContainerStore` in `test_apps_governance.py`."""

    def __init__(self) -> None:  # noqa: D107
        self.minted_for: list[uuid.UUID] = []

    def container_url(self, app_id: uuid.UUID, *, base_url: str | None = None) -> str:
        return f"https://fakeaccount.blob.core.windows.net/app-{app_id}"

    async def mint_deploy_container_sas(self, app_id: uuid.UUID, *, ttl=None) -> DeployCredential:
        self.minted_for.append(app_id)
        return DeployCredential(sas="sv=fake&si=fake&sig=fake", expires_at=datetime.now(UTC))


class _FailingContainerStore(_FakeContainerStore):
    async def mint_deploy_container_sas(self, app_id: uuid.UUID, *, ttl=None) -> DeployCredential:
        raise RuntimeError("blob signing blew up")


class _FakeRuntime:
    """Records every call; `fail` makes `.deploy()` raise `DeployRuntimeError`."""

    def __init__(self, *, fail: str | None = None, fqdn: str = _FAKE_FQDN) -> None:
        self.fail = fail
        self.fqdn = fqdn
        self.calls: list[tuple[str, dict[str, str], bytes]] = []

    async def deploy(self, *, name: str, env: dict[str, str], bundle: bytes) -> DeployResult:
        self.calls.append((name, env, bundle))
        if self.fail:
            raise DeployRuntimeError(self.fail)
        return DeployResult(fqdn=self.fqdn)


async def _approved_app(
    db_session, *, with_ready_database: bool = True, **overrides
) -> AppRegistry:
    owner = await UserFactory.create(db_session)
    submission_id = overrides.pop("approved_submission_id", uuid.uuid4())
    app = await AppRegistryFactory.create(
        db_session,
        user_id=owner.id,
        status=AppStatus.APPROVED,
        approved_submission_id=submission_id,
        approved_commit_sha=_SHA,
        **overrides,
    )
    if with_ready_database:
        db_session.add(
            ProjectDatabase(
                project_id=app.project_id,
                db_name="db_fake",
                role_name="role_fake",
                password_encrypted="not-decrypted-in-these-tests",
                db_ready=True,
            )
        )
        await db_session.flush()
    return app


def _wire_storage_with_bundle(app_id: uuid.UUID, submission_id: uuid.UUID) -> FakeStorage:
    store = FakeStorage()
    store.objects[submission_key(app_id, submission_id)] = _BUNDLE
    return store


async def test_deploy_app_is_a_noop_when_not_approved(db_session) -> None:
    owner = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id, status=AppStatus.DRAFT)
    runtime = _FakeRuntime()

    ok = await deploy_app(
        app.id,
        db=db_session,
        storage=FakeStorage(),
        container_store=_FakeContainerStore(),
        runtime=runtime,
    )
    assert ok is True
    assert runtime.calls == []


async def test_deploy_app_is_a_noop_when_already_converged(db_session) -> None:
    # approved_submission_id == deployed_submission_id: nothing to do.
    submission_id = uuid.uuid4()
    app = await _approved_app(
        db_session,
        approved_submission_id=submission_id,
        deployed_submission_id=submission_id,
    )
    runtime = _FakeRuntime()

    ok = await deploy_app(
        app.id,
        db=db_session,
        storage=FakeStorage(),
        container_store=_FakeContainerStore(),
        runtime=runtime,
    )
    assert ok is True
    assert runtime.calls == []


async def test_deploy_app_success_sets_all_four_fields_and_clears_the_error(
    db_session, monkeypatch
) -> None:
    submission_id = uuid.uuid4()
    app = await _approved_app(
        db_session,
        approved_submission_id=submission_id,
        last_deploy_error="a previous attempt failed",
    )
    store = _wire_storage_with_bundle(app.id, submission_id)
    container_store = _FakeContainerStore()
    runtime = _FakeRuntime()
    monkeypatch_dsn = "postgresql://role_fake:pw@sandbox-host:5432/db_fake?sslmode=require"
    monkeypatch.setattr(provision_module, "sandbox_dsn", lambda record: monkeypatch_dsn)

    ok = await deploy_app(
        app.id, db=db_session, storage=store, container_store=container_store, runtime=runtime
    )
    assert ok is True

    row = await db_session.get(AppRegistry, app.id)
    assert row.deployed_submission_id == submission_id
    assert row.deployed_at is not None
    assert row.deployed_url == f"https://{_FAKE_FQDN}"
    assert row.last_deploy_error is None

    # Deploy credentials were minted and threaded into the container env.
    assert container_store.minted_for == [app.id]
    [call] = runtime.calls
    name, env, bundle = call
    assert name == deploy_app_name(app.id)
    assert bundle == _BUNDLE
    assert env["BIAL_APP_ID"] == str(app.id)
    assert env["BIAL_DATABASE_URL"] == monkeypatch_dsn
    assert "BIAL_BLOB_SAS" in env and "BIAL_BLOB_CONTAINER_URL" in env


async def test_deploy_app_records_the_error_and_returns_false_on_a_runtime_failure(
    db_session, monkeypatch
) -> None:
    submission_id = uuid.uuid4()
    app = await _approved_app(db_session, approved_submission_id=submission_id)
    store = _wire_storage_with_bundle(app.id, submission_id)
    monkeypatch.setattr(
        provision_module, "sandbox_dsn", lambda record: "postgresql://x:y@h:5432/d"
    )
    runtime = _FakeRuntime(fail="container did not become ready: timeout")

    ok = await deploy_app(
        app.id,
        db=db_session,
        storage=store,
        container_store=_FakeContainerStore(),
        runtime=runtime,
    )
    assert ok is False

    row = await db_session.get(AppRegistry, app.id)
    assert row.last_deploy_error == "container did not become ready: timeout"
    assert row.deployed_submission_id is None  # never touched
    assert row.status is AppStatus.APPROVED  # unchanged — still eligible for retry


async def test_deploy_app_records_the_error_when_the_bundle_is_missing(db_session) -> None:
    submission_id = uuid.uuid4()
    app = await _approved_app(db_session, approved_submission_id=submission_id)
    runtime = _FakeRuntime()

    ok = await deploy_app(
        app.id,
        db=db_session,
        storage=FakeStorage(),  # empty — no submission bundle staged
        container_store=_FakeContainerStore(),
        runtime=runtime,
    )
    assert ok is False
    row = await db_session.get(AppRegistry, app.id)
    assert row.last_deploy_error is not None
    assert runtime.calls == []  # never reached the runtime


async def test_deploy_app_records_the_error_when_the_credential_mint_fails(db_session) -> None:
    submission_id = uuid.uuid4()
    app = await _approved_app(db_session, approved_submission_id=submission_id)
    store = _wire_storage_with_bundle(app.id, submission_id)
    runtime = _FakeRuntime()

    ok = await deploy_app(
        app.id,
        db=db_session,
        storage=store,
        container_store=_FailingContainerStore(),
        runtime=runtime,
    )
    assert ok is False
    row = await db_session.get(AppRegistry, app.id)
    assert "blob signing blew up" in row.last_deploy_error
    assert runtime.calls == []


async def test_deploy_app_records_the_error_when_there_is_no_ready_database(db_session) -> None:
    submission_id = uuid.uuid4()
    app = await _approved_app(
        db_session, with_ready_database=False, approved_submission_id=submission_id
    )
    store = _wire_storage_with_bundle(app.id, submission_id)
    runtime = _FakeRuntime()

    ok = await deploy_app(
        app.id,
        db=db_session,
        storage=store,
        container_store=_FakeContainerStore(),
        runtime=runtime,
    )
    assert ok is False
    row = await db_session.get(AppRegistry, app.id)
    assert "database" in row.last_deploy_error.lower()
    assert runtime.calls == []


def test_deploy_app_name_is_aca_compliant() -> None:
    name = deploy_app_name(uuid.uuid4())
    assert name.startswith("app-")
    assert len(name) == 32
    assert name == name.lower()  # lowercase, per ACA's naming rule
    assert not name.startswith("sbx-")
