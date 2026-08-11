"""Restore must never drop the ownership record while the container may still run (U18).

THIS IS THE GHOST FACTORY. The Redis registry hash is the only record that a container belongs
to somebody. Delete it while the container is still running and the container becomes anonymous:
unreachable by the product, invisible to the Redis-enumerating sweep, and billing at ~$0.108/hr
forever. That is exactly the population ADR-0029 exists to collect — and this code path
*manufactures* it.

Two paths in `restore_from_snapshot` had the defect, and both come from `_safe_teardown`
swallowing `AcaError`:

1. **The failure path.** `_safe_teardown` swallowed, then `_delete_registry` ran
   UNCONDITIONALLY — so a restore that failed after a failed teardown dropped the record of a
   container that was probably still running.
2. **The success path.** The defensive teardown of the OLD container also swallowed, and the code
   then provisioned a new container and OVERWROTE the registry with the new app name. Same
   outcome by a different route: the old container is left with nothing pointing at it.

The fix has a template three methods below it in the same file: `teardown()` raises on a failed
ACA delete and keeps the registry, commented *"Keep the registry so the reaper retries this
teardown; don't orphan."* The rule is the same here — the record goes only once the resource is
CONFIRMED gone.

WHY THIS UNIT LANDS IN PHASE 1 RATHER THAN PHASE 5: every ghost minted before identity stamping
ships is UNTAGGED, and an untagged container is permanently escalate-only under AE2. Each one is
manual work forever, not something the collector can ever clean up on its own.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import SecretStr

from src.services.sandbox import client as client_module
from src.services.sandbox.aca import AcaError
from src.services.sandbox.base import SandboxError
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig

_USER = uuid.uuid4()
_APP_ID = uuid.uuid4()
_NEW_APP = "sbx-new"
_OLD_APP = "sbx-old"


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

    def __init__(self, *, delete_fails: bool) -> None:
        self.delete_fails = delete_fails
        self.deleted: list[str] = []
        self.created: list[str] = []

    async def delete_app(self, *, name: str) -> None:
        self.deleted.append(name)
        if self.delete_fails:
            raise AcaError("ARM refused the delete")

    async def create_app(self, *, name: str, env: dict[str, str]) -> str:
        self.created.append(name)
        return f"{name}.westeurope.azurecontainerapps.io"


class _Storage:
    async def get(self, key: str) -> bytes:
        return b"PACK-bundle-bytes"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A client whose Redis and object store are recorded rather than real."""

    def _make(*, delete_fails: bool, restore_fails: bool, existing_app: str | None) -> Any:
        aca = _Aca(delete_fails=delete_fails)
        # A structural stub, not an `AcaControlPlane` subclass: the client only ever calls
        # `delete_app` / `create_app` on this path, and inheriting the real class would drag an
        # ARM credential chain into a unit test.
        client = AcaSandboxClient(
            _config(),
            # All THREE checkers see the structural stub, so all three want a directive; the
            # mypy one was missing and left `uv run mypy src tests` red at HEAD.
            aca=aca,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]  # noqa: E501
        )

        calls: dict[str, list[Any]] = {"delete_registry": [], "write_registry": []}

        async def _read_registry(user_uuid: uuid.UUID) -> dict[str, str] | None:
            if existing_app is None:
                return None
            return {"app_name": existing_app, "fqdn": "old.fqdn", "token_ref": "ref"}

        async def _delete_registry(user_uuid: uuid.UUID) -> None:
            calls["delete_registry"].append(user_uuid)

        async def _write_registry(user_uuid: uuid.UUID, **kwargs: Any) -> None:
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
    assert calls["delete_registry"] == [_USER], (
        "ARM confirmed the container is gone, so the record must be cleared — leaving it "
        "behind would 409 the builder's next start against a container that does not exist"
    )


# ------------------------------------------------------------------ the success path


async def test_a_failed_defensive_teardown_does_not_orphan_the_old_container(
    wired: Any,
) -> None:
    """The SECOND ghost factory, on the success path rather than the failure path.

    Before provisioning the replacement, restore tears down any container the registry still
    names. That teardown swallowed its error too — and the code then provisioned and overwrote
    the registry with the NEW app name, leaving the old container running with nothing pointing
    at it. Recovery must not manufacture the thing it is recovering from.
    """
    client, aca, calls = wired(delete_fails=True, restore_fails=False, existing_app=_OLD_APP)

    with pytest.raises(SandboxError):
        await client.restore_from_snapshot(
            str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
        )

    assert _OLD_APP in aca.deleted, "the old container's teardown was never attempted"
    assert calls["write_registry"] == [], (
        "the registry was overwritten with the new app name while the OLD container's delete "
        "had failed — the old container is now unrecorded and bills forever"
    )
    assert _NEW_APP not in aca.created, (
        "a replacement container was provisioned even though the old one could not be torn "
        "down, so the deployment now holds two containers and a record for one"
    )


async def test_a_confirmed_defensive_teardown_proceeds_normally(wired: Any) -> None:
    """The happy path must still work: old container confirmed gone, new one provisioned, and
    the registry now names the new app."""
    client, aca, calls = wired(delete_fails=False, restore_fails=False, existing_app=_OLD_APP)

    handle = await client.restore_from_snapshot(
        str(_USER), _NEW_APP, app_env=_env(), source_key="snap/key"
    )

    assert _OLD_APP in aca.deleted
    assert _NEW_APP in aca.created
    assert calls["write_registry"] == [_NEW_APP]
    assert handle.app_name == _NEW_APP


async def test_a_restore_failing_before_teardown_leaves_everything_intact(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetch-and-validate happens BEFORE anything is destroyed, so a missing bundle must leave
    the original container running and attachable. This pins the ordering the file already got
    right, so a later refactor cannot quietly undo it."""
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
