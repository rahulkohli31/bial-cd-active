"""Live end-to-end harness for the crash-recovery feature.

Everything here is REAL except Azure Resource Manager itself:

* the sandbox container is the actual image built from `sandbox/Dockerfile.sandbox`, running
  under local Docker, with the real Caddy + supervisor inside it;
* every `/exec`, `/files` and `/dev/*` call is a real HTTP round trip to that supervisor;
* every git operation — `git init`, `add`, `commit`, `bundle create`, `fetch`, `checkout` —
  runs for real inside the container;
* blob storage is Azurite, which gives the SAME `Last-Modified` semantics as Azure (whole
  seconds), the resolution the recovery-vs-saved comparison depends on and which the unit
  suite's microsecond-stamped fake cannot exercise;
* Redis and PostgreSQL are real servers.

Only `AcaControlPlane` is substituted, for local Docker. That is the one seam that would
otherwise bill a subscription and create real cloud resources.

TO RUN:

    docker build -t bial-sandbox:e2e -f Dockerfile.sandbox ../sandbox
    docker compose -f docker-compose.test.yml up -d          # Azurite on :10000
    docker run -d --name bial-redis -p 6379:6379 redis:7-alpine
    uv run pytest tests/e2e -m integration

Every fixture skips cleanly when its dependency is absent, so the lane degrades to a clear
skip rather than a hang or a false pass. It is `-m integration`, i.e. OUT of the default lane:
each scenario boots a real container, so the suite is minutes, not seconds.

WHY THIS LANE EARNS ITS KEEP: it found a data-loss bug the unit suite structurally could not.
`FakeStorage` stamps `last_modified` in microseconds; Azure and Azurite stamp WHOLE SECONDS.
A Save and a turn-boundary write landing in the same second therefore compared EQUAL in
production and never in the fake — and the restore path resolved that tie toward the older
saved tree, silently discarding the newer work. See
`test_s13b_a_forced_same_second_tie_does_not_silently_restore_the_older_tree`.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr

from src.services.sandbox.aca import AcaControlPlane, AcaError
from src.services.sandbox.base import SandboxHandle
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import reset_storage_for_tests
from src.services.storage.azure_backend import AzureBlobStorage
from src.services.storage.config import AzureStorageConfig

IMAGE = "bial-sandbox:e2e"
AZURITE_ACCOUNT_URL = "http://127.0.0.1:10000/devstoreaccount1"
AZURITE_CONN = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AcaError(f"docker {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class LocalDockerAca(AcaControlPlane):
    """`AcaControlPlane` against local Docker instead of ARM.

    Deliberately mirrors the real control plane's OBSERVABLE contract rather than its
    implementation: `create_app` returns a reachable host only once the app is actually
    serving (ARM's `begin_create_or_update(...).result()` likewise does not return early),
    `delete_app` is idempotent and swallows an already-absent app, and `get_app_fqdn` returns
    None for an app that does not exist — the confirmed-absent signal `attach_existing` uses
    to tell a torn-down container from a transient blip.
    """

    def __init__(self) -> None:  # never build an Azure credential
        self._ports: dict[str, int] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []

    async def create_app(self, *, name: str, env: dict[str, str], tags: dict[str, str]) -> str:
        # `tags` is ARM envelope metadata with no docker equivalent — accepted so the signature
        # matches the port, ignored because a local container has no resource to carry them.
        port = _free_port()
        args = ["run", "-d", "--name", name, "-p", f"{port}:8080"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += [IMAGE]
        await asyncio.to_thread(_docker, *args)
        self._ports[name] = port
        self.created.append(name)
        fqdn = f"127.0.0.1:{port}"
        await self._await_supervisor(fqdn, name)
        return fqdn

    async def _await_supervisor(self, fqdn: str, name: str) -> None:
        import httpx

        deadline = 90
        async with httpx.AsyncClient(timeout=2.0) as http:
            for _ in range(deadline):
                try:
                    resp = await http.get(f"http://{fqdn}/_sup/health")
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        logs = await asyncio.to_thread(_docker, "logs", "--tail", "40", name, check=False)
        raise AcaError(f"sandbox {name} never became healthy; logs:\n{logs}")

    async def delete_app(self, *, name: str) -> None:
        await asyncio.to_thread(_docker, "rm", "-f", name, check=False)
        self._ports.pop(name, None)
        self.deleted.append(name)

    async def get_app_fqdn(self, *, name: str) -> str | None:
        out = await asyncio.to_thread(_docker, "ps", "-q", "-f", f"name=^{name}$", check=False)
        if not out:
            return None
        port = self._ports.get(name)
        return f"127.0.0.1:{port}" if port else None

    async def get_app_env_value(self, *, name: str, key: str) -> str | None:
        out = await asyncio.to_thread(
            _docker, "inspect", "-f", "{{json .Config.Env}}", name, check=False
        )
        if not out:
            return None
        for entry in json.loads(out):
            entry_key, _, entry_value = entry.partition("=")
            if entry_key == key:
                return entry_value
        return None

    async def aclose(self) -> None:
        return None


class LocalSandboxClient(AcaSandboxClient):
    """The REAL client, speaking http to a local container instead of https to ACA ingress.

    The scheme is the only difference. Every other byte of the path under test — the retry
    policy, the registry writes, the token map, the restore script, `write_snapshot`'s four
    execs — is production code."""

    def _url(self, handle: SandboxHandle, endpoint: str) -> str:
        return f"http://{handle.fqdn}/_sup/{endpoint}"


def _sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="local",
        resource_group="local",
        region="local",
        managed_environment_name="local",
        acr_server="local",
        acr_username="local",
        acr_password=SecretStr("local"),
        image_ref=IMAGE,
    )


@pytest.fixture(autouse=True)
def _require_stack() -> None:
    if not _port_open("127.0.0.1", 10000):
        pytest.skip("Azurite not reachable on :10000")
    if not _port_open("127.0.0.1", 6379):
        pytest.skip("Redis not reachable on :6379")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"sandbox image {IMAGE} not built")


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "sandbox", _sandbox_config())


@pytest.fixture
async def live_redis() -> AsyncIterator[aioredis.Redis]:
    """A REAL Redis, on its own database, flushed around each test."""
    from src.services.redis import client as redis_client

    conn = aioredis.Redis(host="127.0.0.1", port=6379, db=9, decode_responses=True)
    await conn.flushdb()
    redis_client._redis_singleton = conn
    yield conn
    await conn.flushdb()
    await conn.aclose()
    redis_client._redis_singleton = None


@pytest.fixture
async def live_storage() -> AsyncIterator[AzureBlobStorage]:
    """A REAL Azure Blob backend (Azurite) with a fresh container per test.

    Azurite is what makes the timestamp comparison honest: its `Last-Modified` is whole
    seconds, exactly like Azure, whereas the unit suite's fake stamps microseconds."""
    from azure.core.exceptions import ResourceExistsError

    from src.services.storage import accessor as storage_accessor

    await reset_storage_for_tests()
    container = f"e2e-{uuid.uuid4().hex[:16]}"
    backend = AzureBlobStorage.from_config(
        AzureStorageConfig(
            account_url=AZURITE_ACCOUNT_URL,
            container=container,
            connection_string=SecretStr(AZURITE_CONN),
        )
    )
    state = await backend._state()
    try:
        await state.service_client.create_container(container)
    except ResourceExistsError:
        pass
    storage_accessor._backend_singleton = backend
    yield backend
    storage_accessor._backend_singleton = None
    await backend.aclose()
    await reset_storage_for_tests()


@pytest.fixture
async def sandbox() -> AsyncIterator[LocalSandboxClient]:
    """The real client over a Docker-backed control plane. Every container it created is
    removed afterwards, whatever the test did."""
    aca = LocalDockerAca()
    client = LocalSandboxClient(_sandbox_config(), aca=aca)
    yield client
    await client.aclose()
    for name in aca.created:
        await asyncio.to_thread(_docker, "rm", "-f", name, check=False)
