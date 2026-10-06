"""Restore must never drop the ownership record while the container may still run.

THE GHOST FACTORY: the Redis registry hash is the only record that a container belongs to
somebody. Drop it while the container may still be running and it becomes anonymous —
unreachable by the product, invisible to the sweep, billing at ~$0.108/hr forever. That is
exactly the population fleet reclamation exists to collect.

The rule: the record goes only once the resource is CONFIRMED gone — see `teardown()` a few
methods below for the same pattern, commented *"Keep the registry so the reaper retries this
teardown; don't orphan."*
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from redis.exceptions import RedisError
from structlog.testing import capture_logs

import src.db.base as db_base
from src.db.models.sandbox_pool import SandboxPoolMember, SandboxPoolState
from src.services.sandbox import client as client_module
from src.services.sandbox import pool
from src.services.sandbox.aca import AcaError
from src.services.sandbox.base import SandboxError
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from tests.fakes import a_sandbox_name

_USER = uuid.uuid4()
_APP_ID = uuid.uuid4()
_NEW_APP = a_sandbox_name("new")
_OLD_APP = a_sandbox_name("old")

pytestmark = pytest.mark.usefixtures("empty_sandbox_pool")


def _config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="sub",
        resource_group="rg",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="acr.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="acr.azurecr.io/sandbox:latest",
    )


class _Aca:
    """Minimal ACA control-plane stub. `delete_fails` is the whole point: ARM refusing a delete
    is the state in which dropping the record manufactures a ghost."""

    def __init__(self, *, delete_fails: bool, create_fails: bool) -> None:
        self.delete_fails = delete_fails
        self.create_fails = create_fails
        self.deleted: list[str] = []
        self.created: list[str] = []

    async def delete_app(self, *, name: str) -> None:
        self.deleted.append(name)
        if self.delete_fails:
            raise AcaError("ARM refused the delete")

    async def create_app(
        self,
        *,
        name: str,
        env: dict[str, str],
        tags: dict[str, str],
        identity_resource_id: str | None = None,
    ) -> str:
        # `identity_resource_id` is accepted so the double matches the port and ignored because
        # this file asserts teardown ORDERING; which identity a container was born with is
        # asserted in `test_aca.py`.
        self.created.append(name)
        if self.create_fails:
            raise AcaError("ARM failed the create after it began")
        return f"{name}.westeurope.azurecontainerapps.io"


class _Storage:
    async def get(self, key: str) -> bytes:
        return b"PACK-bundle-bytes"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis) -> Any:
    """A client whose registry and object store are recorded rather than real. The pool's ledger
    is the test database's."""

    def _make(
        *,
        delete_fails: bool,
        restore_fails: bool,
        existing_app: str | None,
        create_fails: bool = False,
        registry_write_fails: bool = False,
    ) -> Any:
        aca = _Aca(delete_fails=delete_fails, create_fails=create_fails)
        # A structural stub, not an `AcaControlPlane` subclass: the client only ever calls
        # `delete_app` / `create_app` on this path, and inheriting the real class would drag an
        # ARM credential chain into a unit test.
        client = AcaSandboxClient(
            _config(),
            # All THREE checkers see the structural stub, so all three need a directive.
            aca=aca,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]  # noqa: E501
        )

        calls: dict[str, list[Any]] = {"delete_registry": [], "write_registry": []}

        async def _read_registry(user_uuid: uuid.UUID) -> dict[str, str] | None:
            if existing_app is None:
                return None
            return {"app_name": existing_app, "fqdn": "old.fqdn", "token_ref": "ref"}

        async def _delete_registry(user_uuid: uuid.UUID, app_name: str) -> bool:
            # BOTH ARGUMENTS ARE RECORDED. The name is what makes the delete refuse a record a
            # switch has already replaced, so a stub that swallowed it would leave the guard
            # untestable from here.
            calls["delete_registry"].append((user_uuid, app_name))
            return True

        async def _write_registry(user_uuid: uuid.UUID, **kwargs: Any) -> None:
            if registry_write_fails:
                raise RedisError("the registry write did not land")
            calls["write_registry"].append(kwargs.get("app_name"))

        async def _restore_into(handle: Any, bundle: bytes) -> None:
            if restore_fails:
                raise RuntimeError("restore blew up mid-stream")

        monkeypatch.setattr(client, "_read_registry", _read_registry)
        monkeypatch.setattr(client, "_delete_registry", _delete_registry)
        monkeypatch.setattr(client, "_write_registry", _write_registry)
        monkeypatch.setattr(client, "_restore_snapshot_into", _restore_into)
        monkeypatch.setattr(client_module, "get_storage", lambda: _Storage())
        return client, aca, calls

    return _make


def _env() -> dict[str, str]:
    return {"BIAL_APP_ID": str(_APP_ID)}


async def _ledger() -> dict[str, SandboxPoolState]:
    async with db_base.async_session_factory() as db:
        rows = await db.execute(sa.select(SandboxPoolMember.name, SandboxPoolMember.state))
    return {name: state for name, state in rows}


# ------------------------------------------------------------------ the failure path


async def test_a_failed_teardown_does_not_drop_the_ownership_record(wired: Any) -> None:
    """THE regression. A restore that fails after ARM refused the teardown must leave the
    registry intact, so a later sweep retries the teardown instead of meeting an anonymous
    container.

    Mutation check: restore the unconditional `await self._delete_registry(user_uuid)` in
    `restore_from_snapshot`'s except-branch and this goes red.
    """
    client, aca, calls = wired(delete_fails=True, restore_fails=True, existing_app=None)

    with pytest.raises((SandboxError, RuntimeError)):
        await client.restore_from_snapshot(
            str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
        )

    assert aca.deleted, "the teardown was never attempted"
    assert calls["delete_registry"] == [], (
        "the ownership record was deleted while ARM had REFUSED the container delete — the "
        "container is probably still running and is now anonymous. That is the ghost this "
        "whole plan exists to collect."
    )


async def test_a_confirmed_teardown_does_drop_the_ownership_record(wired: Any) -> None:
    """The other half: when the delete is CONFIRMED, the record must go. Without this, the fix
    above would be indistinguishable from never cleaning up at all — and a stale record makes a
    returning builder collide with a container that no longer exists."""
    client, aca, calls = wired(delete_fails=False, restore_fails=True, existing_app=None)

    with pytest.raises((SandboxError, RuntimeError)):
        await client.restore_from_snapshot(
            str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
        )

    assert aca.deleted, "the teardown was never attempted"
    assert calls["delete_registry"] == [(_USER, _NEW_APP)], (
        "ARM confirmed the container is gone, so the record must be cleared — leaving it "
        "behind would 409 the builder's next start against a container that does not exist. "
        "It must be cleared BY NAME: a delete keyed only by user takes whatever record a "
        "concurrent switch has since written, orphaning that container."
    )


# ------------------------------------------------------------------ the record it would replace


async def test_a_restore_refuses_to_provision_over_a_container_the_registry_still_names(
    wired: Any,
) -> None:
    """★ The record is the only thing naming the container it describes. Writing the new
    container's record over it would leave that one running with nothing that finds it, so the
    caller has to hand it over first, and the client refuses when nobody has.

    Mutation check: drop the guard in `_provision_container` and the new container is created and
    recorded over the old one's record."""
    client, aca, calls = wired(delete_fails=False, restore_fails=False, existing_app=_OLD_APP)

    with pytest.raises(SandboxError):
        await client.restore_from_snapshot(
            str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
        )

    assert aca.created == []
    assert aca.deleted == [], "the client deletes nothing a caller has not handed over"
    assert calls["write_registry"] == []


async def test_a_fresh_provision_refuses_the_same_way(wired: Any) -> None:
    client, aca, calls = wired(delete_fails=False, restore_fails=False, existing_app=_OLD_APP)

    with pytest.raises(SandboxError):
        await client.provision_new(str(_USER), _NEW_APP, app_env=_env())

    assert aca.created == []
    assert calls["write_registry"] == []


async def test_an_empty_slot_is_provisioned_into(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create is on the pool's ledger until its record is written, and off it after.

    Mutation check: keep the row once the record is written and it outlives a create that needs
    nothing found."""
    client, aca, calls = wired(delete_fails=False, restore_fails=False, existing_app=None)
    on_the_ledger_as_azure_was_asked: list[dict[str, SandboxPoolState]] = []
    create_app = aca.create_app

    async def create_app_seeing_the_ledger(**kwargs: Any) -> str:
        on_the_ledger_as_azure_was_asked.append(await _ledger())
        return await create_app(**kwargs)

    monkeypatch.setattr(aca, "create_app", create_app_seeing_the_ledger)

    handle = await client.restore_from_snapshot(
        str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
    )

    assert on_the_ledger_as_azure_was_asked == [{_NEW_APP: SandboxPoolState.CLAIMED}]
    assert aca.deleted == []
    assert aca.created == [_NEW_APP]
    assert calls["write_registry"] == [_NEW_APP]
    assert handle.app_name == _NEW_APP
    assert await _ledger() == {}


async def test_a_ledger_that_does_not_answer_costs_the_create_nothing(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, aca, calls = wired(delete_fails=False, restore_fails=False, existing_app=None)

    async def unreachable(name: str, image_ref: str) -> uuid.UUID:
        raise OSError("the database went away")

    monkeypatch.setattr(pool, "hold_a_create", unreachable)

    with capture_logs() as logged:
        handle = await client.restore_from_snapshot(
            str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
        )

    assert handle.app_name == _NEW_APP
    assert (aca.created, calls["write_registry"]) == ([_NEW_APP], [_NEW_APP])
    assert [
        e["app_name"] for e in logged if e["event"] == "sandbox_create_not_held_on_the_ledger"
    ] == [_NEW_APP]


# ------------------------------------------------------------------ a create left standing


@pytest.mark.parametrize("failing_step", ["create", "registry-write"])
@pytest.mark.parametrize("self_clean_refused", [True, False], ids=["refused", "confirmed"])
async def test_a_failed_create_keeps_its_ledger_row_only_while_its_container_may_stand(
    wired: Any, failing_step: str, self_clean_refused: bool
) -> None:
    """★ A container whose self-clean was refused carries a name nothing will create again, so
    its ledger row is the only thing left that can find it. One confirmed gone takes its row
    with it.

    Mutation check: drop the row whatever the self-clean said and the refused cases go red; keep
    it after a confirmed one and the confirmed cases do."""
    client, aca, _ = wired(
        delete_fails=self_clean_refused,
        restore_fails=False,
        existing_app=None,
        create_fails=failing_step == "create",
        registry_write_fails=failing_step == "registry-write",
    )

    with pytest.raises((SandboxError, RedisError)):
        await client.restore_from_snapshot(
            str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
        )

    assert aca.deleted == [_NEW_APP], "the self-clean was never attempted"
    assert await _ledger() == ({_NEW_APP: SandboxPoolState.CLAIMED} if self_clean_refused else {})


async def test_a_restore_failing_before_teardown_leaves_everything_intact(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundle is fetched BEFORE anything is created or recorded, so a missing one leaves the
    slot exactly as it was."""
    client, aca, calls = wired(delete_fails=False, restore_fails=False, existing_app=_OLD_APP)

    class _MissingStorage:
        async def get(self, key: str) -> bytes:
            raise FileNotFoundError("no such bundle")

    monkeypatch.setattr(client_module, "get_storage", lambda: _MissingStorage())

    with pytest.raises(FileNotFoundError):
        await client.restore_from_snapshot(
            str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
        )

    assert aca.deleted == [], "the container was torn down before the bundle was even fetched"
    assert calls["delete_registry"] == []
    assert calls["write_registry"] == []
