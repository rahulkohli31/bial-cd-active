"""A pool member's project settings arrive once, after its claim — against the real image.

The offline suite cannot spawn a child, so this is where the whole path is proven end to end: a
container created with the pool flag and none of its project's settings refuses to start the app,
refuses a bad `configure` without repeating or logging it, takes one good one, refuses a second,
hands the delivered names to its children with the secrets redacted, and then starts the app.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

import httpx
import pytest
from _docker import Sandbox

pytestmark = pytest.mark.integration

_APP_ID = "app-pool-member"
_DB_PASSWORD = "POOLMEMBERROLEPASSWORD"  # noqa: S105 — a fixture value, not a real credential
_DB_DSN = f"postgresql://bialrole_pool:{_DB_PASSWORD}@db-pool.invalid:5432/bialapp_pool"
_SAS_SIGNATURE = "POOLMEMBERSASSIGNATURE"
_BLOB_SAS = f"sv=2021-08-06&sr=c&sp=rwdl&sig={_SAS_SIGNATURE}"


def _configure(sbx: Sandbox, env: dict[str, str]) -> httpx.Response:
    return httpx.post(
        f"{sbx.sup}/configure",
        json={"env": env},
        headers={"Authorization": f"Bearer {sbx.token}"},
        timeout=10.0,
    )


def test_a_pool_member_takes_its_settings_once_and_then_starts_the_app(
    sandbox_factory: Callable[..., Sandbox],
) -> None:
    sbx = sandbox_factory({"BIAL_POOL_MEMBER": "1"})
    assert sbx.health().json() == {"ok": True, "configured": False}
    refused_start = sbx.dev_start()
    assert refused_start.status_code == 412
    assert refused_start.json() == {"detail": "not configured yet"}

    refused = _configure(sbx, {"BIAL_DATABASE_URL": _DB_DSN, "PATH": "/tmp"})
    assert refused.status_code == 422
    assert refused.json() == {
        "detail": 'configure takes {"env": {...}} naming only per-project settings, '
        "each a non-empty string"
    }
    assert sbx.health().json() == {"ok": True, "configured": False}

    first = _configure(
        sbx, {"BIAL_APP_ID": _APP_ID, "BIAL_DATABASE_URL": _DB_DSN, "BIAL_BLOB_SAS": _BLOB_SAS}
    )
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert sbx.health().json() == {"ok": True, "configured": True}

    second = _configure(sbx, {"BIAL_APP_ID": "app-someone-else"})
    assert second.status_code == 409
    assert second.json() == {"detail": "already configured"}

    printed = sbx.exec_cmd(["printenv"]).json()["stdout"]
    assert f"BIAL_APP_ID={_APP_ID}\n" in printed
    assert "BIAL_DATABASE_URL=***\n" in printed
    assert "BIAL_BLOB_SAS=***\n" in printed
    assert "BIAL_POOL_MEMBER" not in printed
    # The whole-value registration cannot cover a line carrying only the password.
    lone = sbx.exec_cmd(["sh", "-c", f'echo "pw={_DB_PASSWORD}"']).json()["stdout"]
    assert lone == "pw=***\n"

    # The app's own dev command is wrapped so the dev server's environment shows in /dev/logs.
    started = sbx.dev_start(
        ["sh", "-c", "printenv BIAL_APP_ID BIAL_DATABASE_URL BIAL_BLOB_SAS; exec npm run dev"]
    )
    assert started.status_code == 200
    assert isinstance(started.json()["pid"], int)
    lines: list[str] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(lines) < 3:
        lines = sbx.dev_logs(0).json()["lines"]
        time.sleep(0.5)
    assert lines[:3] == [_APP_ID, "***", "***"]
    logged = "\n".join(lines)
    assert _DB_PASSWORD not in logged
    assert _SAS_SIGNATURE not in logged

    container_log = subprocess.run(["docker", "logs", sbx.name], capture_output=True, text=True)
    for stream in (container_log.stdout, container_log.stderr):
        assert _DB_PASSWORD not in stream
        assert _SAS_SIGNATURE not in stream
