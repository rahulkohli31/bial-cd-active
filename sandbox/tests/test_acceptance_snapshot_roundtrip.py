"""Acceptance (b) — local-disk -> Blob snapshot/restore round-trip via the C2 shape (U16).

The second program risk, proven against REAL Azurite through a C2-ABC-conforming reference client:
provision -> write a distinctive file -> snapshot (decode -> put RAW bundle to Blob) -> teardown ->
provision a FRESH container -> restore (get -> encode -> files create -> restore.sh + C9 re-inject)
-> resume `next dev`. The C4 ordering exercised here uses a MOCK in-proc lock — SESSION-API
re-verifies the shipped Redis-lock/ACA ordering (R3). Integration-marked; skips when Docker/Azurite
are absent. Conformance is a pure-Python offline test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest
from fake_storage import ExplodingStorage, FakeStorage
from snapshot_ref_client import InProcLock, ReferenceSandboxClient, snapshot_key
from src.services.sandbox.base import (
    FileCreate,
    FileView,
    SandboxClient,
    SandboxGoneError,
)
from src.services.storage.base import ObjectStorage
from src.services.storage.errors import StorageError

APP_ENV = {
    "BIAL_APP_ID": "app-acceptance-b",
    "BIAL_PORTAL_ORIGIN": "http://127.0.0.1:14300",
    # ADR-0028: the per-project DSN rides the SAME birth-env path as the identity vars, so the
    # round-trip must prove restore re-injects it too (a restored container is a NEW
    # container — it gets its env at birth and never again, KTD-3).
    "BIAL_DATABASE_URL": (
        "postgresql://bialrole_acceptanceb:acceptancerolepassword@127.0.0.1:5432/bialapp_acceptanceb"
    ),
}


# --- conformance: pure Python, offline (no container / Azurite) --------------------------------
def test_reference_client_conforms_to_frozen_c2_abc() -> None:
    # A complete subclass instantiates — all 10 frozen abstract methods are implemented.
    c = ReferenceSandboxClient(image="unused", storage=FakeStorage(), lock=InProcLock())
    assert isinstance(c, SandboxClient)
    assert not ReferenceSandboxClient.__abstractmethods__

    # Omitting ONE abstract method -> TypeError at instantiation (genuine nominal conformance).
    class Incomplete(SandboxClient):
        async def provision_new(self, *a, **k): ...
        async def wait_ready(self, *a, **k): ...
        async def attach_existing(self, *a, **k): ...
        async def restore_from_snapshot(self, *a, **k): ...
        async def files(self, *a, **k): ...
        async def dev_start(self, *a, **k): ...
        async def dev_status(self, *a, **k): ...
        async def dev_logs(self, *a, **k): ...
        async def teardown(self, *a, **k): ...

        # deliberately omits the `exec` method

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


# --- integration lane helpers -----------------------------------------------------------------
@pytest.fixture
async def make_client(
    sandbox_image: str,
) -> AsyncIterator[Callable[[ObjectStorage], ReferenceSandboxClient]]:
    """Build reference clients bound to a given storage; tear them all (containers + http) down."""
    clients: list[ReferenceSandboxClient] = []

    def _make(storage: ObjectStorage) -> ReferenceSandboxClient:
        c = ReferenceSandboxClient(image=sandbox_image, storage=storage, lock=InProcLock())
        clients.append(c)
        return c

    try:
        yield _make
    finally:
        for c in clients:
            await c.aclose()


async def _view(client: ReferenceSandboxClient, handle: object, path: str) -> str:
    fr = await client.files(handle, FileView(path=path))
    return str(fr.detail.get("content", ""))


# --- the full round-trip via Blob (the second real risk) --------------------------------------
@pytest.mark.integration
async def test_snapshot_restore_round_trip_via_blob(azurite_storage, make_client) -> None:
    client = make_client(azurite_storage)
    app_id = APP_ENV["BIAL_APP_ID"]
    marker = "SNAPSHOT-PROOF-acceptance-b-42"

    # container #1: provision + a distinctive workspace change
    h1 = await client.provision_new("userb", "appb", app_env=APP_ENV)
    await client.files(h1, FileCreate(path="snapshot-marker.txt", file_text=marker + "\n"))

    # snapshot -> RAW bundle to Azurite at the frozen key
    await client.snapshot(h1, app_id=app_id)
    stored = await azurite_storage.get(snapshot_key(app_id))
    assert stored.startswith(b"# v"), "the stored Blob object must be a RAW git bundle (D3)"

    await client.teardown(h1)  # idempotent

    # container #2: FRESH (baked, non-repo) -> restore overlays the snapshot + re-injects C9
    h2 = await client.restore_from_snapshot("userb", "appb", app_env=APP_ENV)
    assert marker in await _view(client, h2, "snapshot-marker.txt")

    # git history survived the Blob round-trip
    ws = ReferenceSandboxClient.WORKSPACE
    log = await client.run_exec(h2, ["git", "-C", ws, "log", "--oneline"])
    assert log.exit == 0 and any(line.strip() for line in log.stdout.splitlines())

    # Identity + the DSN re-injected AND surviving the child-env scrub; the token never leaks
    env = await client.run_exec(h2, ["printenv"])
    injected = ("BIAL_APP_ID", "BIAL_PORTAL_ORIGIN", "BIAL_DATABASE_URL")
    assert all(k in env.stdout for k in injected)
    assert "SUPERVISOR_TOKEN" not in env.stdout
    # The DSN's NAME is there; its VALUE (and its password) are redacted out of the output.
    assert "acceptancerolepassword" not in env.stdout

    # `next dev` RESUMES on the restored workspace
    await client.dev_start(h2)
    ready = await client.wait_ready(h2, timeout_s=120.0)
    assert ready.ready


# --- a missing snapshot surfaces SandboxGoneError, never a silent empty restore ---------------
@pytest.mark.integration
async def test_restore_missing_snapshot_raises(azurite_storage, make_client) -> None:
    client = make_client(azurite_storage)
    with pytest.raises(SandboxGoneError):
        await client.restore_from_snapshot(
            "userx", "appx", app_env={**APP_ENV, "BIAL_APP_ID": "no-such-app-id"}
        )


# --- C4 ordering: snapshot -> teardown -> release, in that exact order -------------------------
@pytest.mark.integration
async def test_checkpoint_ordering_is_snapshot_teardown_release(make_client) -> None:
    storage = FakeStorage()
    client = make_client(storage)
    app_id = APP_ENV["BIAL_APP_ID"]
    client._lock.acquire("userc")

    h = await client.provision_new("userc", "appc", app_env=APP_ENV)
    await client.checkpoint_and_teardown(h, app_id=app_id, user_id="userc")

    assert client.trace == ["snapshot", "teardown", "release"]  # strict C4 order
    assert not client._lock.is_held("userc")  # released LAST — after the snapshot is durable
    assert snapshot_key(app_id) in storage.objects  # durable BEFORE the release
    assert h.fqdn not in client._procs  # the container was actually torn down


# --- C4 abort-before-teardown: a put failure must NOT teardown or release the lock ------------
@pytest.mark.integration
async def test_snapshot_put_failure_aborts_before_teardown(make_client) -> None:
    storage = ExplodingStorage()  # put() always raises
    client = make_client(storage)
    app_id = APP_ENV["BIAL_APP_ID"]
    client._lock.acquire("userd")

    h = await client.provision_new("userd", "appd", app_env=APP_ENV)
    with pytest.raises(StorageError):
        await client.checkpoint_and_teardown(h, app_id=app_id, user_id="userd")

    # The whole point of C4: the lock guards snapshot->teardown atomicity.
    assert client.trace == ["snapshot"]  # never reached teardown / release
    assert client._lock.is_held("userd")  # lock STAYS held — a second provision cannot race in
    assert h.fqdn in client._procs  # the container is still live (not torn down)
    st = await client.dev_status(h)  # and really reachable
    assert st.port == 3000
