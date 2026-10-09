"""A container a start created and never saw recorded is deleted by the pool's pass once its create
could no longer be running.

Every container takes a name nothing will create again, so one left behind by a cancelled start
or a create whose self-clean was refused cannot be adopted by a later create of the same name, and
no registry record names it. The row its start wrote on the pool's ledger before the create does,
and the pass deletes the container of a row past its deadline that no registry record names.
Driven through the real client against a control-plane double, on the test database's ledger.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr

import src.db.base as db_base
from src.config import settings
from src.db.models.sandbox_pool import SandboxPoolMember, SandboxPoolState
from src.db.models.sandbox_start import SandboxProjectType
from src.services.build_sessions.appdata import build_app_env
from src.services.build_sessions.locks import read_registry
from src.services.build_sessions.pool_pass import ROW_DEADLINE, keep_the_pool
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
from src.services.sandbox import pool
from src.services.sandbox.aca import AcaError
from src.services.sandbox.base import SandboxError, SandboxHandle, a_fresh_sandbox_name
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.test_relaunch import RecordingAca
from tests.fakes import FakeStorage, a_git_bundle

pytestmark = pytest.mark.usefixtures("empty_sandbox_pool")


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


class _AzureOutlivesTheStart(RecordingAca):
    """The control plane as a failed start leaves it. A held create is accepted, so the container
    exists, and never answers the start that asked; a failed one exists too. A refused delete
    deletes nothing."""

    def __init__(self, *, first_create: str = "", refused_deletes: int = 0) -> None:
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
    store: FakeStorage, monkeypatch: pytest.MonkeyPatch, aca: _AzureOutlivesTheStart
) -> tuple[AcaSandboxClient, uuid.UUID, dict[str, str]]:
    """The real client over `aca`, for an app with a saved copy to restore."""
    app_id = uuid.uuid4()
    await store.put(snapshot_key(app_id), a_git_bundle())
    client = AcaSandboxClient(_config(), aca=aca)

    async def _pushed(handle: SandboxHandle, bundle: bytes) -> None:
        return None

    monkeypatch.setattr(client, "_restore_snapshot_into", _pushed)
    return client, uuid.uuid4(), build_app_env(app_id)


async def _ledger() -> dict[str, SandboxPoolState]:
    async with db_base.async_session_factory() as db:
        rows = await db.execute(sa.select(SandboxPoolMember.name, SandboxPoolMember.state))
    return {name: state for name, state in rows}


def _past_the_deadline() -> datetime:
    return datetime.now(UTC) + ROW_DEADLINE + timedelta(minutes=1)


async def test_a_container_a_cancelled_start_left_unrecorded_is_deleted_after_its_deadline(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ A Stop pressed, or a project switched, during the minute a create takes cancels the start
    but not the create: Azure finishes it after the start has gone, and nothing records it. Its
    row stays, and once no create could still be running the pass deletes the container.

    Mutation check: write no row before the create and the container is never found."""
    aca = _AzureOutlivesTheStart(first_create="held")
    client, user_id, env = await _wired(fake_storage, monkeypatch, aca)
    cancelled = asyncio.create_task(
        client.restore_from_snapshot(str(user_id), a_fresh_sandbox_name(), app_env=env)
    )
    await aca.accepted.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    [stranded] = aca.create_calls
    assert await read_registry(fake_redis, user_id) is None, "premise: nothing recorded it"

    inside = (await keep_the_pool(client, at=datetime.now(UTC)))[SandboxProjectType.PLAIN]
    assert (inside.deleted, aca.delete_calls) == (0, []), "its create may still be running"
    after = (await keep_the_pool(client, at=_past_the_deadline()))[SandboxProjectType.PLAIN]

    assert after.deleted == 1
    assert aca.delete_calls == [stranded]
    assert await _ledger() == {}


async def test_a_failed_create_whose_self_clean_was_refused_is_deleted_after_its_deadline(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ A create that failed for good after Azure began it is self-cleaned, and when that delete
    is refused the container may still be running under a name the next attempt will not reuse.
    Its row stays, so the pass deletes it rather than it billing until someone reads the orphan
    report.

    Mutation check: drop the row whatever the self-clean said and the container is never
    found."""
    aca = _AzureOutlivesTheStart(first_create="failed", refused_deletes=1)
    client, user_id, env = await _wired(fake_storage, monkeypatch, aca)

    with pytest.raises(SandboxError):
        await client.restore_from_snapshot(str(user_id), a_fresh_sandbox_name(), app_env=env)
    [stranded] = aca.create_calls
    assert await _ledger() == {stranded: SandboxPoolState.CLAIMED}

    await keep_the_pool(client, at=_past_the_deadline())

    assert aca.delete_calls == [stranded, stranded], "refused at the self-clean, then deleted"
    assert await _ledger() == {}


async def test_a_create_whose_record_was_written_loses_only_its_row(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row outlived the write that should have taken it, and the registry now says whose
    workspace the container is: the pass forgets the row and leaves the container running."""
    aca = _AzureOutlivesTheStart()
    client, user_id, env = await _wired(fake_storage, monkeypatch, aca)

    async def unreachable(member_id: uuid.UUID) -> None:
        raise OSError("the database went away")

    with monkeypatch.context() as patched:
        patched.setattr(pool, "forget", unreachable)
        handle = await client.restore_from_snapshot(
            str(user_id), a_fresh_sandbox_name(), app_env=env
        )
    reg = await read_registry(fake_redis, user_id)
    assert reg is not None and reg[REGISTRY_FIELD_APP_NAME] == handle.app_name
    assert await _ledger() == {handle.app_name: SandboxPoolState.CLAIMED}

    outcome = (await keep_the_pool(client, at=_past_the_deadline()))[SandboxProjectType.PLAIN]

    assert (outcome.deleted, aca.delete_calls) == (0, [])
    assert await _ledger() == {}
