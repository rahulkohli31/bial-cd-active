"""U4 — app-row resolution + the base provision-env builder (`:5432` test DB)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.sandbox import SandboxNotConfiguredError
from src.services.sandbox.config import SandboxConfig
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory


def _sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="s",
        resource_group="r",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="bialgenaicr01.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
    )


async def test_first_build_mints_row_and_builds_env(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())
    user = await UserFactory.create(db_session, email="c1@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)

    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    app = await db_session.get(AppRegistry, app_id)
    assert app is not None
    assert app.user_id == user.id and app.project_id == project.id  # owner + project scoped
    # The upsert still mints the publishable key on insert (`GET /apps/{id}/status` returns
    # it) — it is simply no longer returned here, and no longer injected into the sandbox.
    assert app.app_key.startswith("bial_")  # a bial_ key, not a UUID

    env = build_app_env(app_id)
    assert env["BIAL_APP_ID"] == str(app_id)
    # The retired shared data plane's two vars are GONE — injecting them again would hand a
    # generated app a credential to a plane that no longer exists (U6).
    assert "BIAL_APP_CREDENTIAL" not in env
    assert "BIAL_DATA_BASE_URL" not in env


async def test_repeat_build_reuses_row_and_key(db_session: AsyncSession) -> None:
    user = await UserFactory.create(db_session, email="c2@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    a1 = await resolve_app_for_project(db_session, user.id, project.id)
    row1 = await db_session.get(AppRegistry, a1)
    assert row1 is not None
    k1 = row1.app_key
    a2 = await resolve_app_for_project(db_session, user.id, project.id)
    row2 = await db_session.get(AppRegistry, a2)
    assert row2 is not None
    assert a1 == a2 and k1 == row2.app_key  # reused, not re-minted (continuity)


async def test_cross_user_project_is_404(db_session: AsyncSession) -> None:
    owner = await UserFactory.create(db_session, email="a@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="b@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, owner.id)
    with pytest.raises(AppApiError) as exc:
        await resolve_app_for_project(db_session, intruder.id, project.id)
    assert exc.value.status_code == 404  # non-leaking (ADR-0004)


async def test_foreign_owned_app_is_409(db_session: AsyncSession) -> None:
    owner = await UserFactory.create(db_session, email="a2@rvaiglobal.com")
    other = await UserFactory.create(db_session, email="b2@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, owner.id)
    # An ownership-invariant violation: the project is the owner's, but its app is another
    # user's — the owner-guarded upsert WHERE matches nothing, so it fails closed with 409.
    await AppRegistryFactory.create(db_session, user_id=other.id, project_id=project.id)
    with pytest.raises(AppApiError) as exc:
        await resolve_app_for_project(db_session, owner.id, project.id)
    assert exc.value.status_code == 409


async def test_project_deleted_mid_upsert_maps_to_404(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The project passes the owner check, then is deleted before the INSERT lands -> the FK
    # violation on `app_registry_project_id_fkey` is the loser of that race, mapped to a
    # non-leaking 404 (never a 500). owned_project_or_404 uses db.get, so patching db.execute
    # only fails the upsert.
    user = await UserFactory.create(db_session, email="frace@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)

    async def boom_fkey(*_a: object, **_k: object) -> object:
        raise IntegrityError(
            "INSERT INTO app_registry ...",
            {},
            Exception(
                'insert or update violates foreign key constraint "app_registry_project_id_fkey"'
            ),
        )

    monkeypatch.setattr(db_session, "execute", boom_fkey)
    with pytest.raises(AppApiError) as exc:
        await resolve_app_for_project(db_session, user.id, project.id)
    assert exc.value.status_code == 404


async def test_unrelated_integrity_error_propagates(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A DIFFERENT integrity violation is NOT the project-deleted race -> it must propagate,
    # never be swallowed into a misleading 404.
    user = await UserFactory.create(db_session, email="frace2@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)

    async def boom_other(*_a: object, **_k: object) -> object:
        raise IntegrityError(
            "INSERT INTO app_registry ...",
            {},
            Exception('duplicate key value violates unique constraint "uq_app_registry_project"'),
        )

    monkeypatch.setattr(db_session, "execute", boom_other)
    with pytest.raises(IntegrityError):
        await resolve_app_for_project(db_session, user.id, project.id)


def test_build_app_env_normalizes_portal_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://portal.example.com/app/")
    env = build_app_env(uuid.uuid4())
    # A FRONTEND_URL with a path / trailing slash is normalized to a bare origin (C8 §1).
    assert env["BIAL_PORTAL_ORIGIN"] == "https://portal.example.com"
    # No name ends in a scrub-triggering suffix (they survive the C1 scrub).
    for name in env:
        assert not name.endswith(("_TOKEN", "_SECRET", "_KEY"))


def test_build_app_env_requires_configured_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox", None)
    with pytest.raises(SandboxNotConfiguredError):
        build_app_env(uuid.uuid4())
