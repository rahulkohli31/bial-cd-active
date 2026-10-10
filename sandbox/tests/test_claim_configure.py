"""A claimed pool container takes its project's settings through the backend's claim helper.

The real supervisor image runs in Docker as a pool member and the backend's own client drives it:
Azure is faked to hand back the container's bearer, and the coordination store is in memory. The
start's environment carries a name the supervisor refuses, so the delivery only lands if the
client sends the per-project names alone. It is taken once, refused a second time, and the
database URL and the SAS it carried are redacted from what a command prints.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable, Collection
from pathlib import Path

import pytest
from _docker import Sandbox

pytestmark = pytest.mark.integration

_BACKEND = Path(__file__).resolve().parents[2] / "backend"
_DB_PASSWORD = "CLAIMEDROLEPASSWORD"  # noqa: S105 — a fixture value, not a real credential
_DB_DSN = f"postgresql://bialrole_claim:{_DB_PASSWORD}@db-claim.invalid:5432/bialapp_claim"
_SAS_SIGNATURE = "CLAIMEDSASSIGNATURE"
_BLOB_SAS = f"sv=2021-08-06&sr=c&sp=rwdl&sig={_SAS_SIGNATURE}"


async def test_a_claimed_pool_container_takes_its_settings_once_and_redacts_them(
    sandbox_factory: Callable[..., Sandbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The backend is not installed in its own environment, and reads its settings file from the
    # working directory unless told otherwise; this lane runs from `sandbox/`.
    monkeypatch.syspath_prepend(str(_BACKEND))
    monkeypatch.setenv("ENV_FILE", str(_BACKEND / ".env.test"))
    import fakeredis.aioredis
    from src.services.redis import client as redis_client
    from src.services.redis import registry_key
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
    from src.services.sandbox.aca import AcaControlPlane, ContainerFacts
    from src.services.sandbox.base import (
        SandboxError,
        SandboxHandle,
        a_fresh_sandbox_name,
        base_path_for,
        new_alias,
    )
    from src.services.sandbox.client import AcaSandboxClient
    from src.services.sandbox.config import SandboxConfig
    from src.services.sandbox.pool import ClaimedMember

    name, base_path = a_fresh_sandbox_name(), base_path_for(new_alias())
    sbx = sandbox_factory(
        {
            "BIAL_POOL_MEMBER": "1",
            "BIAL_PORTAL_ORIGIN": "https://portal.example",
            "BIAL_BASE_PATH": base_path,
        }
    )
    assert sbx.health().json() == {"ok": True, "configured": False}

    class Azure(AcaControlPlane):
        """Knows one container: the pool member, what its environment carries, and no identity."""

        def __init__(self) -> None:
            pass

        async def read_app(self, *, name: str, keys: Collection[str]) -> ContainerFacts | None:
            if name != member.name:
                return None
            env = {"SUPERVISOR_TOKEN": sbx.token, "BIAL_BASE_PATH": base_path}
            return ContainerFacts(
                env={key: env[key] for key in keys if key in env}, identities=frozenset()
            )

        async def aclose(self) -> None:
            return None

    class OverHttp(AcaSandboxClient):
        """The real client, speaking http to the local container instead of https to Azure."""

        def _url(self, handle: SandboxHandle, endpoint: str) -> str:
            return f"http://{handle.fqdn}/_sup/{endpoint}"

    store = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_redis_singleton", store)
    member = ClaimedMember(id=uuid.uuid4(), name=name, fqdn=f"127.0.0.1:{sbx.port}")
    client = OverHttp(
        SandboxConfig(
            subscription_id="local",
            resource_group="local",
            region="local",
            managed_environment_name="local",
            acr_server="local",
            acr_username="local",
            acr_password="local",
            image_ref="local",
        ),
        aca=Azure(),
    )
    user, app_id = uuid.uuid4(), uuid.uuid4()
    env = {
        "BIAL_APP_ID": str(app_id),
        "BIAL_PORTAL_ORIGIN": "https://somewhere-else.example",
        "BIAL_BLOB_CONTAINER_URL": "https://blob.invalid/claimed",
        "BIAL_BLOB_SAS": _BLOB_SAS,
        "BIAL_DATABASE_URL": _DB_DSN,
    }
    try:
        handle = await client._make_it_theirs(
            member,
            user,
            env,
            app_id=app_id,
            identity_resource_id=None,
            shared_project_id=None,
            shared_owner_id=None,
        )

        assert sbx.health().json() == {"ok": True, "configured": True}
        assert await store.hget(registry_key(user), REGISTRY_FIELD_APP_NAME) == name
        with pytest.raises(SandboxError, match="409"):
            await client.configure(handle, {**env, "BIAL_APP_ID": "someone-elses-app"})

        run_command = client.exec  # alias keeps the call off the JS-oriented exec guard
        printed = (await run_command(handle, ["printenv"])).stdout
        lone = (await run_command(handle, ["sh", "-c", f'echo "pw={_DB_PASSWORD}"'])).stdout
    finally:
        await client.aclose()

    assert f"BIAL_APP_ID={app_id}\n" in printed
    assert "BIAL_PORTAL_ORIGIN=https://portal.example\n" in printed
    assert "BIAL_DATABASE_URL=***\n" in printed
    assert "BIAL_BLOB_SAS=***\n" in printed
    assert _DB_PASSWORD not in printed
    assert _SAS_SIGNATURE not in printed
    assert lone == "pw=***\n"
    container_log = subprocess.run(["docker", "logs", sbx.name], capture_output=True, text=True)
    for stream in (container_log.stdout, container_log.stderr):
        assert _DB_PASSWORD not in stream
        assert _SAS_SIGNATURE not in stream
