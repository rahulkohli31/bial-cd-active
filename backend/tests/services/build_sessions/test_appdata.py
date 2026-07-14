"""U4 — C9 app-data credential mint + the provision-env builder (`:5432` test DB)."""

from __future__ import annotations

import uuid

import pytest
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
        image_ref="bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
        app_data_base_url="https://platform.example/v1",
    )


async def test_first_build_mints_key_and_builds_env(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())
    user = await UserFactory.create(db_session, email="c1@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)

    app_id, app_key = await resolve_app_for_project(db_session, user.id, project.id)
    app = await db_session.get(AppRegistry, app_id)
    assert app is not None
    assert app.user_id == user.id and app.project_id == project.id  # owner + project scoped
    assert app_key.startswith("bial_") and app_key == app.app_key  # a bial_ key, not a UUID

    env = build_app_env(app_id, app_key)
    assert env["BIAL_APP_ID"] == str(app_id)
    assert env["BIAL_APP_CREDENTIAL"] == app_key
    assert env["BIAL_DATA_BASE_URL"] == "https://platform.example/v1"


async def test_repeat_build_reuses_row_and_key(db_session: AsyncSession) -> None:
    user = await UserFactory.create(db_session, email="c2@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    a1, k1 = await resolve_app_for_project(db_session, user.id, project.id)
    a2, k2 = await resolve_app_for_project(db_session, user.id, project.id)
    assert a1 == a2 and k1 == k2  # reused, not re-minted (continuity)


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


def test_build_app_env_normalizes_portal_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://portal.example.com/app/")
    env = build_app_env(uuid.uuid4(), "bial_abc")
    # A FRONTEND_URL with a path / trailing slash is normalized to a bare origin (C8 §1).
    assert env["BIAL_PORTAL_ORIGIN"] == "https://portal.example.com"
    # None of the four names ends in a scrub-triggering suffix (they survive the C1 scrub).
    for name in env:
        assert not name.endswith(("_TOKEN", "_SECRET", "_KEY"))


def test_build_app_env_requires_configured_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox", None)
    with pytest.raises(SandboxNotConfiguredError):
        build_app_env(uuid.uuid4(), "bial_abc")
