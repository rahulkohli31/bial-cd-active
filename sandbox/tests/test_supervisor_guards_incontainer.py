"""In-container C1 guard-regression + spawning-surface suite (U14, integration lane).

The scenarios the offline lane cannot cover, because every `/exec` + `/dev/*` path spawns through
`_DEMOTE` (`user=`/`group=`/`extra_groups=` -> `setgroups()`), which needs root: the three
supervisor guards (UID demotion to appuser uid 10001, token isolation, the workspace-escape guard)
and the spawning `/exec` / `/dev/*` surface, all against the REAL image where the supervisor is
root and can demote children. Requirement R4.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import pytest
from _docker import Sandbox, run_sandbox

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def guarded(sandbox_image: str) -> Iterator[Sandbox]:
    """A container whose ROOT env carries the four C9 BIAL_* AND secrets the child-env scrub must
    drop: `IDENTITY_HEADER` (matches no suffix), a `*_DSN`, a `*_PASSWORD`, `SUPERVISOR_TOKEN`."""
    sbx = run_sandbox(
        {
            "BIAL_APP_ID": "app-guard",
            "BIAL_APP_CREDENTIAL": "bial_guard",
            "BIAL_DATA_BASE_URL": "http://127.0.0.1:9/v1",
            "BIAL_PORTAL_ORIGIN": "http://127.0.0.1:1",
            "IDENTITY_HEADER": "azure-msi-secret",
            "APP_DB_DSN": "postgres://u:p@h/db",
            "SOME_PASSWORD": "hunter2",
        },
        image=sandbox_image,
    )
    try:
        yield sbx
    finally:
        sbx.stop()


def _ok(sbx: Sandbox, cmd: list[str], **kw: object) -> dict:
    r = sbx.exec_cmd(cmd, **kw)  # type: ignore[arg-type]
    assert r.status_code == 200, f"{cmd} -> {r.status_code} {r.text[:200]}"
    return r.json()


# --- guard 1: UID demotion — children run as appuser (uid 10001), root's groups dropped -------
def test_exec_runs_as_appuser_uid_10001(guarded: Sandbox) -> None:
    out = _ok(guarded, ["id", "-u"])
    assert out["stdout"].strip() == "10001" and out["exit"] == 0


def test_exec_drops_root_supplementary_groups(guarded: Sandbox) -> None:
    out = _ok(guarded, ["id"])
    assert "uid=10001(appuser)" in out["stdout"]
    # extra_groups=[APP_GID] drops root's supplementary groups a preexec setuid would have left.
    assert "0(root)" not in out["stdout"]


# --- guard 2: token isolation + the fail-closed scrub, proven in the REAL container -----------
def test_appuser_child_cannot_read_supervisor_token(guarded: Sandbox) -> None:
    out = _ok(guarded, ["printenv"])
    assert "SUPERVISOR_TOKEN" not in out["stdout"]
    # Arbitrary parent secrets are DENIED by the allowlist (a suffix denylist would miss these)...
    assert "IDENTITY_HEADER" not in out["stdout"]
    assert "APP_DB_DSN" not in out["stdout"]
    assert "SOME_PASSWORD" not in out["stdout"]
    # ...while exactly the four C9 identity vars survive.
    for k in ("BIAL_APP_ID", "BIAL_APP_CREDENTIAL", "BIAL_DATA_BASE_URL", "BIAL_PORTAL_ORIGIN"):
        assert k in out["stdout"]


def test_appuser_child_cannot_read_pid1_environ(guarded: Sandbox) -> None:
    # /proc/1/environ belongs to the ROOT supervisor; the unprivileged child cannot read it, so the
    # token never leaks through it.
    out = _ok(guarded, ["cat", "/proc/1/environ"])
    assert out["exit"] != 0  # permission denied for uid 10001
    assert "SUPERVISOR_TOKEN" not in out["stdout"]


# --- the spawning /exec surface: exit codes, timeout, workspace-escape ------------------------
def test_exec_nonzero_exit_is_http_200(guarded: Sandbox) -> None:
    # A failed command is a NORMAL 200 carrying exit != 0 — BRAIN's self-heal reads it off the 200.
    out = _ok(guarded, ["sh", "-c", "exit 3"])
    assert out["exit"] == 3


def test_exec_timeout_returns_504(guarded: Sandbox) -> None:
    r = guarded.exec_cmd(["sleep", "5"], timeout=1)
    assert r.status_code == 504


def test_exec_cwd_escape_returns_400(guarded: Sandbox) -> None:
    r = guarded.exec_cmd(["pwd"], cwd="../../../../etc")
    assert r.status_code == 400


# --- guard 3 (spawning): /dev lifecycle — 409 on double-start, monotonic clamped log cursor ---
def test_dev_lifecycle_and_log_cursor(
    sandbox_factory: Callable[..., Sandbox],
) -> None:
    sbx = sandbox_factory({"BIAL_PORTAL_ORIGIN": "http://127.0.0.1:1"})
    assert sbx.dev_start().status_code == 200
    assert sbx.dev_start().status_code == 409  # already running

    total = 0
    for _ in range(40):
        total = sbx.dev_logs(0).json()["next"]
        if total >= 1:
            break
        time.sleep(0.5)
    assert total >= 1, "no dev-server log lines were captured"

    # The cursor is an absolute, monotonic index (it never rewinds).
    assert sbx.dev_logs(total).json()["next"] >= total
    # A cursor far past the total CLAMPS: no lines, next stays the total (no negative slice).
    far = sbx.dev_logs(total + 1_000_000).json()
    assert far["lines"] == []
    assert far["next"] >= total
