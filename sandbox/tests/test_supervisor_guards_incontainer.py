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

# The per-app Blob vars injected into the guarded container (C9 §6). The SAS is a SECRET (redacted
# from observable output); the container URL is NOT (logs freely). A distinctive sig token so a
# redaction test can assert the value is gone.
_BLOB_SAS = "sv=2021-08-06&sr=c&sp=rwdl&sig=REDACTMESIGNATUREVALUE"
_BLOB_URL = "http://127.0.0.1:10000/devstoreaccount1/app-guard"


@pytest.fixture(scope="module")
def guarded(sandbox_image: str) -> Iterator[Sandbox]:
    """A container whose ROOT env carries the C9 BIAL_* + the two per-app Blob vars AND secrets the
    child-env scrub must drop: `IDENTITY_HEADER` (matches no suffix), a `*_DSN`, a `*_PASSWORD`,
    `SUPERVISOR_TOKEN`."""
    sbx = run_sandbox(
        {
            "BIAL_APP_ID": "app-guard",
            "BIAL_APP_CREDENTIAL": "bial_guard",
            "BIAL_DATA_BASE_URL": "http://127.0.0.1:9/v1",
            "BIAL_PORTAL_ORIGIN": "http://127.0.0.1:1",
            "BIAL_BLOB_CONTAINER_URL": _BLOB_URL,
            "BIAL_BLOB_SAS": _BLOB_SAS,
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


# --- U8: per-app Blob env — both vars reach the child; the SAS is redacted from output ---------
def test_exec_redacts_the_blob_sas_but_logs_the_container_url(guarded: Sandbox) -> None:
    out = _ok(guarded, ["printenv"])
    # Both blob vars reach the child (allowlisted by name), so their NAMES appear...
    assert "BIAL_BLOB_CONTAINER_URL" in out["stdout"]
    assert "BIAL_BLOB_SAS" in out["stdout"]
    # ...the non-secret container URL VALUE logs freely...
    assert "devstoreaccount1" in out["stdout"]
    # ...but the SECRET SAS value is REDACTED from the /exec output (C9 §6.4 / KTD-8).
    assert "REDACTMESIGNATUREVALUE" not in out["stdout"]
    assert "***" in out["stdout"]


def test_files_view_redacts_a_stored_blob_sas(guarded: Sandbox) -> None:
    # A file that happens to embed the SAS value comes back REDACTED through `/files view`.
    create = guarded.files(
        {"action": "create", "path": "leak.txt", "file_text": f"conn = {_BLOB_SAS}\n"}
    )
    assert create.status_code == 200
    view = guarded.files({"action": "view", "path": "leak.txt"})
    assert view.status_code == 200
    content = view.json()["content"]
    assert "REDACTMESIGNATUREVALUE" not in content
    assert "***" in content


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


# --- U13: runtime `npm install` as appuser succeeds on the built image -------------------------
def test_appuser_owns_node_modules_and_npm_cache(guarded: Sandbox) -> None:
    # The open-sandbox runtime `npm install` (and the restore reconcile) run as appuser and must
    # write node_modules + the npm cache without EACCES — the chown-after-bake + appuser-owned
    # cache invariants (U13). A pure write-probe, so it holds even with NO registry egress.
    script = 'touch node_modules/.bial-probe && mkdir -p "$npm_config_cache/_p" && echo OK'
    probe = _ok(guarded, ["sh", "-c", script])
    assert probe["exit"] == 0
    assert "OK" in probe["stdout"]


def test_appuser_npm_install_writes_node_modules(guarded: Sandbox) -> None:
    # The end-to-end open-sandbox install path: a real on-demand `npm install` as appuser writes
    # node_modules with exit 0, no EACCES (U13 / R8). Needs registry egress in the integration
    # lane; this is the load-bearing proof — a local same-arch build is NOT verification.
    out = guarded.exec_cmd(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error", "left-pad@1.3.0"],
        timeout=300,
    )
    assert out.status_code == 200
    body = out.json()
    assert body["exit"] == 0, body.get("stderr", "")[:400]
    listing = _ok(guarded, ["ls", "node_modules/left-pad"])
    assert listing["exit"] == 0


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
