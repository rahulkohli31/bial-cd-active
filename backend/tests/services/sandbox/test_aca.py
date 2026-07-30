"""U2 — the C2 client's ACA lifecycle: provision / attach / restore / teardown, the
C5 registry hash, the token_ref map, and the C4 restore pull.

The ACA control plane is faked (a `FakeAca` recording created/deleted apps + their
injected env); Redis is `fake_redis`; Blob is `fake_storage`; the `/_sup/*` layer is
an `httpx.MockTransport`. Real Azure is Track SANDBOX's live-validation join.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr

from src.services.build_sessions.locks import stay_of_execution_is_current
from src.services.redis import REGISTRY_STATE_ENDING, REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from src.services.sandbox import client as client_module
from src.services.sandbox.aca import AcaControlPlane, AcaTransientError
from src.services.sandbox.base import SandboxError, SandboxGoneError, SandboxNotReadyError
from src.services.sandbox.client import _RESTORE_TIMEOUT_SECONDS, AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from src.services.storage.errors import StorageNotFoundError
from tests.fakes import FakeStorage

Handler = Callable[[httpx.Request], httpx.Response]

USER = uuid.uuid4()
APP_ID = uuid.uuid4()
APP_NAME = "sbx-abc123"


class FakeAca(AcaControlPlane):
    """A control plane that records calls instead of touching Azure. Overrides
    `__init__` so it never builds a credential / mgmt client."""

    def __init__(self) -> None:
        self.created: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []
        self.create_calls = 0
        self.transient_before_success = 0
        self.get_returns_none = False
        self.fqdn = "app-xyz.westeurope.azurecontainerapps.io"

    async def create_app(self, *, name: str, env: dict[str, str]) -> str:
        self.create_calls += 1
        if self.transient_before_success > 0:
            self.transient_before_success -= 1
            raise AcaTransientError("simulated transient ACA error")
        self.created[name] = env
        return self.fqdn

    async def delete_app(self, *, name: str) -> None:
        self.deleted.append(name)
        self.created.pop(name, None)

    async def get_app_fqdn(self, *, name: str) -> str | None:
        if self.get_returns_none or name not in self.created:
            return None
        return self.fqdn

    async def aclose(self) -> None:
        return None


def _config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="sub",
        resource_group="rg",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="bialgenaicr01.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
    )


def _app_env() -> dict[str, str]:
    return {
        "BIAL_APP_ID": str(APP_ID),
        "BIAL_PORTAL_ORIGIN": "https://portal.example",
        "BIAL_DATABASE_URL": "postgresql://bialrole_x:pw@db.example:5432/bialapp_x",
    }


def _client(aca: FakeAca, handler: Handler | None = None) -> AcaSandboxClient:
    transport = httpx.MockTransport(handler) if handler is not None else None
    return AcaSandboxClient(_config(), transport=transport, aca=aca)


_BIAL_KEYS = ("BIAL_APP_ID", "BIAL_PORTAL_ORIGIN", "BIAL_DATABASE_URL")


async def test_provision_new_writes_registry_and_injects_env(fake_redis: aioredis.Redis) -> None:
    aca = FakeAca()
    client = _client(aca)
    handle = await client.provision_new(str(USER), APP_NAME, app_env=_app_env())

    assert handle.ready is False
    assert handle.app_name == APP_NAME
    env = aca.created[APP_NAME]
    for key in _BIAL_KEYS:
        assert key in env  # the four C9 identity vars are injected at provision
    assert env["SUPERVISOR_TOKEN"] == handle.token  # bearer lives in the container env

    reg = await fake_redis.hgetall(registry_key(USER))
    assert reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_READY
    token_ref = reg[REGISTRY_FIELD_TOKEN_REF]
    assert isinstance(token_ref, str)  # decode_responses=True — Redis hands back str
    assert token_ref and token_ref != handle.token  # a REFERENCE, never the raw token
    assert handle.token not in reg.values()  # the raw token is absent from Redis
    assert client._token_refs[token_ref] == handle.token  # resolvable in-process only
    await client.aclose()


async def test_a_fresh_registry_never_inherits_a_previous_occupants_preview_stay(
    fake_redis: aioredis.Redis,
) -> None:
    # `_write_registry` is an `hset(mapping=…)` MERGE, so a field it does not name SURVIVES
    # a re-registration. `preview_stay_until` surviving is a leak with teeth (#43):
    #
    #   relaunch grants a 30-min stay -> the user starts a build -> reconcile's reap hits a
    #   transient ACA error, whose arm deliberately KEEPS the registry -> a preview holds no
    #   lock, so the build's acquire still succeeds -> the build re-registers over the
    #   surviving hash and INHERITS the lease -> if that build's process later dies, the
    #   sweep spares its ORPHANED container for the rest of the half hour.
    #
    # That inverts the protection into exactly the leak it exists to prevent, so a freshly
    # written registry is authoritative about the lease: it carries NO stay, always.
    await fake_redis.hset(
        registry_key(USER),
        mapping={
            REGISTRY_FIELD_APP_NAME: "sbx-preview-that-was-never-reaped",
            REGISTRY_FIELD_PREVIEW_STAY_UNTIL: (
                datetime.now(UTC) + timedelta(minutes=25)
            ).isoformat(),
        },
    )
    aca = FakeAca()
    client = _client(aca)
    handle = await client.provision_new(str(USER), APP_NAME, app_env=_app_env())

    reg = await fake_redis.hgetall(registry_key(USER))
    assert REGISTRY_FIELD_PREVIEW_STAY_UNTIL not in reg  # the stale lease is disowned
    assert reg[REGISTRY_FIELD_APP_NAME] == APP_NAME  # ...and this IS the new build's hash
    assert reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_READY
    # The build's container is therefore reapable the moment its own liveness lapses.
    assert await stay_of_execution_is_current(fake_redis, USER) is False
    assert handle.app_name == APP_NAME
    await client.aclose()


async def test_attach_existing_reconnects_without_a_new_create(fake_redis: aioredis.Redis) -> None:
    aca = FakeAca()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dev/status"):
            return httpx.Response(200, json={"running": True, "ready": True, "port": 3000})
        return httpx.Response(200, json={"ok": True})  # /_sup/health probe

    client = _client(aca, handler)
    provisioned = await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    creates_before = aca.create_calls
    handle = await client.attach_existing(str(USER))
    assert aca.create_calls == creates_before  # idempotent — no re-provision
    assert handle.app_name == APP_NAME
    assert handle.token == provisioned.token
    assert handle.ready is True  # the ACTUAL dev-server state, never a hardcoded False
    await client.aclose()


async def test_attach_existing_dev_status_blip_falls_back_to_not_ready(
    fake_redis: aioredis.Redis,
) -> None:
    # The post-probe readiness query is best-effort: a transient dev/status failure must not
    # fail an attach that just probed healthy — the handle degrades to ready=False.
    aca = FakeAca()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dev/status"):
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    client = _client(aca, handler)
    await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    handle = await client.attach_existing(str(USER))
    assert handle.ready is False
    await client.aclose()


async def test_attach_unreachable_and_confirmed_gone_raises_gone(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_PROBE_START_SECONDS", 0.001)
    monkeypatch.setattr(client_module, "_PROBE_MAX_SECONDS", 0.002)
    aca = FakeAca()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("supervisor down")

    client = _client(aca, handler)
    await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    aca.get_returns_none = True  # ACA confirms the revision is gone
    with pytest.raises(SandboxGoneError):
        await client.attach_existing(str(USER))
    await client.aclose()


async def test_attach_unreachable_but_exists_raises_not_ready(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single blip must NOT map to Gone (that would double-allocate + orphan the original).
    monkeypatch.setattr(client_module, "_PROBE_START_SECONDS", 0.001)
    monkeypatch.setattr(client_module, "_PROBE_MAX_SECONDS", 0.002)
    aca = FakeAca()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("transient blip")

    client = _client(aca, handler)
    await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    # get_returns_none stays False -> the container still exists -> retryable, not gone.
    with pytest.raises(SandboxNotReadyError):
        await client.attach_existing(str(USER))
    await client.aclose()


async def test_attach_ending_state_raises_gone_without_probing(fake_redis: aioredis.Redis) -> None:
    aca = FakeAca()
    probes = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        probes["n"] += 1
        return httpx.Response(200, json={"ok": True})

    client = _client(aca, handler)
    await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    # The provision itself legitimately reaches the container once (the git-repo init). This
    # test is about the ATTACH path, so count from zero after the setup rather than widening
    # the assertion — the number that must be 0 is "touches while ENDING".
    probes["n"] = 0
    await fake_redis.hset(registry_key(USER), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)
    with pytest.raises(SandboxGoneError):
        await client.attach_existing(str(USER))
    assert probes["n"] == 0  # honors the C5 mark-ending guard — never touches a dying box
    await client.aclose()


async def test_restore_pulls_bundle_and_reinjects_env(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    aca = FakeAca()
    calls = {"files": 0, "run": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_sup/files":
            calls["files"] += 1
            return httpx.Response(200, json={"ok": True, "created": "app.bundle.b64"})
        if request.url.path == "/_sup/exec":
            calls["run"] += 1
            return httpx.Response(200, json={"stdout": "", "stderr": "", "exit": 0})
        return httpx.Response(404)

    client = _client(aca, handler)
    await fake_storage.put(snapshot_key(APP_ID), b"BUNDLE-BYTES")
    handle = await client.restore_from_snapshot(str(USER), APP_NAME, app_env=_app_env())
    assert handle.ready is False
    assert calls["files"] == 1 and calls["run"] == 1
    env = aca.created[APP_NAME]
    for key in _BIAL_KEYS:
        assert key in env  # env re-injected onto the fresh container (C4/C9)
    await client.aclose()


async def test_restore_missing_snapshot_self_cleans(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    aca = FakeAca()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client(aca, handler)
    # No snapshot seeded -> the Blob get raises, surfaced cleanly after self-clean.
    with pytest.raises(StorageNotFoundError):
        await client.restore_from_snapshot(str(USER), APP_NAME, app_env=_app_env())
    assert APP_NAME in aca.deleted  # the just-created container was torn down
    assert await fake_redis.hgetall(registry_key(USER)) == {}
    await client.aclose()


async def test_restore_reconciles_deps_from_the_lockfile(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The restore script re-derives dynamic deps with `npm install` AFTER the git checkout, inside
    # the SAME single exec (one round trip), bounded by the sandbox-layer restore timeout (U6/R16).
    aca = FakeAca()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_sup/files":
            return httpx.Response(200, json={"ok": True, "created": "app.bundle.b64"})
        if request.url.path == "/_sup/exec":
            body = json.loads(request.content)
            captured["cmd"] = body["cmd"]
            captured["timeout"] = body["timeout"]
            return httpx.Response(200, json={"stdout": "", "stderr": "", "exit": 0})
        return httpx.Response(404)

    client = _client(aca, handler)
    await fake_storage.put(snapshot_key(APP_ID), b"BUNDLE-BYTES")
    await client.restore_from_snapshot(str(USER), APP_NAME, app_env=_app_env())
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    script = cmd[-1]  # ["sh", "-c", <script>]
    assert isinstance(script, str)
    assert "npm install" in script
    # The reconcile runs AFTER the checkout (deps overlay the restored tree), before cleanup.
    assert script.index("git checkout") < script.index("npm install")
    assert captured["timeout"] == _RESTORE_TIMEOUT_SECONDS

    # #11/R4 — the reconcile is CONDITIONAL on lockfile drift. The baked fingerprint is
    # taken BEFORE the fetch overwrites the workspace, the snapshot's AFTER the checkout,
    # and the install sits inside the comparison — an unchanged lockfile restores with
    # zero npm work.
    assert script.index("baked_lock=") < script.index("git fetch")
    assert script.index("git checkout") < script.index("snap_lock=")
    assert '[ "$baked_lock" = "$snap_lock" ]' in script
    assert script.index('[ "$baked_lock" = "$snap_lock" ]') < script.index("npm install")
    # Fail-safe toward INSTALLING: the two missing-file fallbacks are DIFFERENT sentinels,
    # so an absent lockfile on either side can never fake a match and skip the reconcile.
    assert "|| echo baked-lock-missing" in script
    assert "|| echo snap-lock-missing" in script
    await client.aclose()


async def test_restore_reconcile_failure_is_a_hard_error_and_self_cleans(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A non-zero restore script (e.g. `npm install` on an unresolvable lockfile version) aborts
    # restore with a hard SandboxError; the just-created container is torn down (R17 / AE3).
    aca = FakeAca()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_sup/files":
            return httpx.Response(200, json={"ok": True, "created": "app.bundle.b64"})
        if request.url.path == "/_sup/exec":
            fail = {"stdout": "", "stderr": "npm ERR! ETARGET", "exit": 1}
            return httpx.Response(200, json=fail)
        return httpx.Response(404)

    client = _client(aca, handler)
    await fake_storage.put(snapshot_key(APP_ID), b"BUNDLE-BYTES")
    with pytest.raises(SandboxError):
        await client.restore_from_snapshot(str(USER), APP_NAME, app_env=_app_env())
    assert APP_NAME in aca.deleted  # the just-created container was torn down (R17)
    assert await fake_redis.hgetall(registry_key(USER)) == {}
    await client.aclose()


async def test_teardown_is_idempotent_and_clears_registry(fake_redis: aioredis.Redis) -> None:
    aca = FakeAca()
    client = _client(aca)
    handle = await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    await client.teardown(handle)
    assert APP_NAME in aca.deleted
    assert await fake_redis.hgetall(registry_key(USER)) == {}
    # A second teardown on the already-deleted container is a clean no-op.
    await client.teardown(handle)
    await client.aclose()


async def test_provision_retries_transient_then_succeeds(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_ACA_RETRY_START_SECONDS", 0.001)
    monkeypatch.setattr(client_module, "_ACA_RETRY_MAX_SECONDS", 0.002)
    aca = FakeAca()
    aca.transient_before_success = 2
    client = _client(aca)
    handle = await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    assert handle.app_name == APP_NAME
    assert aca.create_calls == 3  # two transient failures + one success
    await client.aclose()


async def test_provision_persistent_transient_raises_and_self_cleans(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_ACA_RETRY_START_SECONDS", 0.001)
    monkeypatch.setattr(client_module, "_ACA_RETRY_MAX_SECONDS", 0.002)
    aca = FakeAca()
    aca.transient_before_success = 99  # never succeeds
    client = _client(aca)
    with pytest.raises(SandboxError):
        await client.provision_new(str(USER), APP_NAME, app_env=_app_env())
    assert APP_NAME in aca.deleted  # self-clean attempted, no orphan
    assert await fake_redis.hgetall(registry_key(USER)) == {}
    await client.aclose()


def test_snapshot_key_is_byte_stable() -> None:
    aid = uuid.UUID("00000000-0000-0000-0000-0000000000ab")
    assert snapshot_key(aid) == "snapshots/00000000-0000-0000-0000-0000000000ab/app.bundle"


async def test_attach_transient_during_confirm_gone_maps_to_not_ready(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient ARM error while confirming gone-vs-unreachable must NOT surface as Gone
    # (that would restore + double-allocate the live original) — it stays retryable.
    monkeypatch.setattr(client_module, "_PROBE_START_SECONDS", 0.001)
    monkeypatch.setattr(client_module, "_PROBE_MAX_SECONDS", 0.002)
    aca = FakeAca()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("supervisor unreachable")

    client = _client(aca, handler)
    await client.provision_new(str(USER), APP_NAME, app_env=_app_env())

    async def boom(*, name: str) -> str | None:
        raise AcaTransientError("transient ARM blip while confirming liveness")

    monkeypatch.setattr(aca, "get_app_fqdn", boom)
    with pytest.raises(SandboxNotReadyError):
        await client.attach_existing(str(USER))
    await client.aclose()
