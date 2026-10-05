"""A container a birth created and never saw recorded is found by the next birth and deleted.

Every container takes a name nothing will create again, so one left behind by a cancelled start
or a create whose self-clean was refused cannot be adopted by a later create of the same name, and
no registry record names it. Its birth marker does, and the next birth hands it to the shutdown
routine like any other holder. Driven through the real client against a control-plane double, since
the marker is the client's own record of what it is creating.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

import src.db.base as db_base
from src.config import settings
from src.db.models.pending_teardown import PendingTeardown, PendingTeardownKind
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.build_sessions.locks import read_birth_marker, read_registry
from src.services.build_sessions.manager import SessionManager
from src.services.build_sessions.shutdown import OwedTeardown, shut_it_down_in_the_background
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
from src.services.sandbox.aca import AcaError
from src.services.sandbox.base import SandboxHandle
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.test_relaunch import RecordingAca
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeStorage, a_git_bundle, a_manager_whose_ledger_is


def _config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="s",
        resource_group="r",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="acr.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="acr/img:latest",
    )


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox", _config())


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _now(seconds: float) -> None:
        return None

    monkeypatch.setattr(manager_module, "_asleep", _now)


class _HandedOver:
    """Every hand-over a birth makes, recorded and then run, so a test can read the debt as it was
    owed and still wait for its delete."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.owed: list[OwedTeardown] = []
        self.runs: list[asyncio.Task[None]] = []
        run = shut_it_down_in_the_background

        def _record_then_run(owed: OwedTeardown, **aimed_at: Any) -> asyncio.Task[None]:
            self.owed.append(owed)
            self.runs.append(run(owed, **aimed_at))
            return self.runs[-1]

        monkeypatch.setattr(manager_module, "shut_it_down_in_the_background", _record_then_run)

    def debts(self) -> list[tuple[str, PendingTeardownKind, bool]]:
        return [(owed.app_name, owed.kind, owed.write_back) for owed in self.owed]


@pytest.fixture
def handed_over(monkeypatch: pytest.MonkeyPatch) -> _HandedOver:
    return _HandedOver(monkeypatch)


class _AzureOutlivesTheStart(RecordingAca):
    """The control plane as a failed birth leaves it. A held create is accepted, so the container
    exists, and never answers the start that asked; a failed one exists too. A refused delete
    deletes nothing."""

    def __init__(self, *, first_create: str, refused_deletes: int = 0) -> None:
        super().__init__()
        self.first_create = first_create
        self.refused_deletes = refused_deletes
        self.accepted = asyncio.Event()

    async def create_app(
        self,
        *,
        name: str,
        env: dict[str, str],
        tags: dict[str, str],
        identity_resource_id: str | None = None,
    ) -> str:
        fqdn = await super().create_app(
            name=name, env=env, tags=tags, identity_resource_id=identity_resource_id
        )
        if len(self.create_calls) == 1:
            self.accepted.set()
            if self.first_create == "held":
                await asyncio.Event().wait()
            if self.first_create == "failed":
                raise AcaError("the create failed after Azure began it")
        return fqdn

    async def delete_app(self, *, name: str) -> None:
        if self.refused_deletes > 0:
            self.refused_deletes -= 1
            self.delete_calls.append(name)
            raise AcaError("ARM refused the delete")
        await super().delete_app(name=name)


async def _wired(
    db: AsyncSession,
    store: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    aca: _AzureOutlivesTheStart,
) -> tuple[SessionManager, AcaSandboxClient, uuid.UUID, uuid.UUID, dict[str, str]]:
    """A saved app, a ledger-bound manager, and the real client over `aca`. The background
    routine's own sessions are the test's too, so it can settle the debt it is handed."""

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    monkeypatch.setattr(db_base, "async_session_factory", lambda: _session())
    user = await UserFactory.create(db)
    project = await ProjectFactory.create(db, user.id)
    app_id = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    await store.put(snapshot_key(app_id), a_git_bundle())
    client = AcaSandboxClient(_config(), aca=aca)

    async def _pushed(handle: SandboxHandle, bundle: bytes) -> None:
        return None

    monkeypatch.setattr(client, "_restore_snapshot_into", _pushed)
    return a_manager_whose_ledger_is(db), client, user.id, app_id, build_app_env(app_id)


async def _still_owed(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        sa.select(PendingTeardown.app_name).where(PendingTeardown.user_id == user_id)
    )
    return list(rows.scalars().all())


async def test_a_container_a_cancelled_start_left_unrecorded_is_deleted_behind_the_next_birth(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    handed_over: _HandedOver,
) -> None:
    """★ A Stop pressed, or a project switched, during the minute a create takes cancels the start
    but not the create: Azure finishes it after the start has gone, and nothing records it. The
    next birth finds it through its marker, owes it without a write-back, and the delete runs
    behind that birth.

    Mutation check: write no marker before the create and the container is never found; hand
    nothing over from the marker and it is never deleted."""
    aca = _AzureOutlivesTheStart(first_create="held")
    manager, client, user_id, app_id, env = await _wired(
        db_session, fake_storage, monkeypatch, aca
    )
    cancelled = asyncio.create_task(manager._restore_or_bust(client, user_id, app_id, env))
    await aca.accepted.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    [stranded] = aca.create_calls
    assert await read_registry(fake_redis, user_id) is None, "premise: nothing recorded it"

    handle = await manager._restore_or_bust(client, user_id, app_id, env)
    await asyncio.gather(*handed_over.runs)

    assert handed_over.debts() == [(stranded, PendingTeardownKind.BUILD, False)]
    assert aca.delete_calls == [stranded]
    assert await _still_owed(db_session, user_id) == []
    assert await read_birth_marker(fake_redis, user_id) is None
    reg = await read_registry(fake_redis, user_id)
    assert reg is not None and reg[REGISTRY_FIELD_APP_NAME] == handle.app_name != stranded


async def test_a_failed_create_whose_self_clean_was_refused_is_deleted_behind_the_next_attempt(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    handed_over: _HandedOver,
) -> None:
    """★ A create that failed for good after Azure began it is self-cleaned, and when that delete
    is refused the container may still be running under a name the next attempt will not reuse.
    The next attempt finds it through its marker and owes it, so it is deleted rather than billed
    until someone reads the orphan report.

    Mutation check: drop the marker whatever the self-clean said and the container is never
    found."""
    aca = _AzureOutlivesTheStart(first_create="failed", refused_deletes=1)
    manager, client, user_id, app_id, env = await _wired(
        db_session, fake_storage, monkeypatch, aca
    )

    handle = await manager._restore_or_bust(client, user_id, app_id, env)
    await asyncio.gather(*handed_over.runs)

    stranded, replacement = aca.create_calls
    assert handle.app_name == replacement
    assert handed_over.debts() == [(stranded, PendingTeardownKind.BUILD, False)]
    assert aca.delete_calls == [stranded, stranded], "refused at the self-clean, then deleted"
    assert stranded not in aca.created
    assert await _still_owed(db_session, user_id) == []
    assert await read_birth_marker(fake_redis, user_id) is None
