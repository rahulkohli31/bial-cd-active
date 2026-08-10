"""A1 — `BIAL_LOGIN_REQUIRED` reaches the sandbox on every BIRTH arm, reflecting
`app_registry.login_required`, and `SessionManager.live_handle_for_app` resolves the
sandbox login broker's bare-`app_id` lookup.

Mirrors `test_build_env_dsn.py`'s structure: the env-dict builder on its own, then the
manager's birth arms end-to-end off `FakeSandboxClient.provision_env` / `.restore_env`.
Unlike `BIAL_DATABASE_URL`, `BIAL_LOGIN_REQUIRED` is deliberately absent from the
supervisor's child-allowlist (`_INJECTED_ENV`) — it is platform-internal, consumed only
by the supervisor's own root process — so there is no equivalent "reaches `next dev`"
assertion here; that boundary is pinned on the sandbox side instead
(`sandbox/supervisor/test_app.py`).
"""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.user import User
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.applogin_env import provision_app_login_gate
from src.services.build_sessions.manager import SessionManager, app_name_for
from src.services.sandbox.config import SandboxConfig
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain, FakeSandboxClient, FakeStorage, _fake_handle


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


async def _set_login_required(db: AsyncSession, app_id: uuid.UUID, *, value: bool) -> None:
    await db.execute(
        sa.update(AppRegistry).where(AppRegistry.id == app_id).values(login_required=value)
    )
    await db.commit()


# --- the env-dict builder on its own ------------------------------------------------------


async def test_defaults_to_false_for_a_freshly_minted_app(db_session: AsyncSession) -> None:
    # `login_required` is `server_default=false` — a just-minted app must read as "off"
    # with no admin action taken.
    user, project_id = await _mk(db_session, "loginenv1@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()

    assert await provision_app_login_gate(db_session, app_id) == {"BIAL_LOGIN_REQUIRED": "false"}


async def test_reflects_true_once_the_admin_flag_is_set(db_session: AsyncSession) -> None:
    user, project_id = await _mk(db_session, "loginenv2@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    await _set_login_required(db_session, app_id, value=True)

    assert await provision_app_login_gate(db_session, app_id) == {"BIAL_LOGIN_REQUIRED": "true"}


# --- the manager's birth arms --------------------------------------------------------------


async def test_the_fresh_provision_arm_injects_the_default_off_flag(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "loginstart@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.start(
        db_session, user, project_id, "build it", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

    assert client.provision_env is not None
    assert client.provision_env["BIAL_LOGIN_REQUIRED"] == "false"


async def test_the_restore_arm_reflects_a_flag_toggled_after_first_build(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # Mirrors `test_the_restore_arm_reinjects_the_dsn`: a second `start()` on the SAME
    # project, with a fresh `FakeSandboxClient`, exercises `_restore_or_provision`'s restore
    # arm (the registry says "live" from the first client; this client has never seen it).
    user, project_id = await _mk(db_session, "loginrestore@rvaiglobal.com")
    manager = SessionManager()
    first = await manager.start(
        db_session,
        user,
        project_id,
        "build it",
        run_build=FakeBrain(),
        sandbox_client=FakeSandboxClient(),
    )
    assert first.task is not None
    await first.task

    # The admin flips the gate ON after the app already exists (PATCH /admin/apps/{id}).
    await _set_login_required(db_session, first.app_id, value=True)

    second_client = FakeSandboxClient()
    second = await manager.start(
        db_session,
        user,
        project_id,
        "refine it",
        run_build=FakeBrain(),
        sandbox_client=second_client,
    )
    assert second.task is not None
    await second.task

    assert second_client.restored == [app_name_for(second.app_id)]  # restored, not re-provisioned
    assert second_client.restore_env is not None
    assert second_client.restore_env["BIAL_LOGIN_REQUIRED"] == "true"


async def test_relaunch_preview_reflects_a_toggled_flag(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The relaunch merge is written SEPARATELY from the start merge on purpose
    # (`relaunch_preview` must not reuse `_restore_or_provision`) — a var added to only one
    # of the two sites is a silent half-fix, exactly the failure mode this test catches.
    user, project_id = await _mk(db_session, "loginrelaunch@rvaiglobal.com")
    manager = SessionManager()
    built = await manager.start(
        db_session,
        user,
        project_id,
        "build it",
        run_build=FakeBrain(),
        sandbox_client=FakeSandboxClient(),
    )
    assert built.task is not None
    await built.task
    await _set_login_required(db_session, built.app_id, value=True)

    client = FakeSandboxClient()
    await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.restored == [app_name_for(built.app_id)]
    assert client.restore_env is not None
    assert client.restore_env["BIAL_LOGIN_REQUIRED"] == "true"


# --- SessionManager.live_handle_for_app (the sandbox login broker's lookup) ---------------


async def test_live_handle_for_app_resolves_via_attach(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "loginhandle@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.start(
        db_session, user, project_id, "build it", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

    # Force resolution through the registry-backed attach path regardless of whatever the
    # in-process session bookkeeping currently considers "active" — the broker's callback is
    # a DIFFERENT router/request than the one that built the app, so it can never rely on an
    # in-process session handle either.
    client.attach_handle = _fake_handle(app_name_for(session.app_id))

    handle = await manager.live_handle_for_app(db_session, session.app_id, client)

    assert handle is not None
    assert handle.app_name == app_name_for(session.app_id)


async def test_live_handle_for_app_returns_none_for_an_unknown_app(
    db_session: AsyncSession,
) -> None:
    manager = SessionManager()
    client = FakeSandboxClient()

    assert await manager.live_handle_for_app(db_session, uuid.uuid4(), client) is None
