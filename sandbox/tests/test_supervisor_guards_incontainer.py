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
# The per-project database DSN (ADR-0028). SECRET as a whole AND as its password sub-token — two
# distinct redaction registrations, so each gets its own distinctive token to assert on.
_DB_PASSWORD = "REDACTMEROLEPASSWORD"  # noqa: S105 — a fixture value, not a real credential
_DB_DSN = f"postgresql://bialrole_guard:{_DB_PASSWORD}@db-guard.invalid:5432/bialapp_guard"


@pytest.fixture(scope="module")
def guarded(sandbox_image: str) -> Iterator[Sandbox]:
    """A container whose ROOT env carries the injected BIAL_* identity vars + the two per-app
    Blob vars + the per-project database DSN, AND secrets the child-env scrub must drop:
    `IDENTITY_HEADER` (matches no suffix), a `*_DSN`, a `*_PASSWORD`, `SUPERVISOR_TOKEN`. Note
    the pairing:
    `APP_DB_DSN` is DENIED while `BIAL_DATABASE_URL` is admitted — the allowlist keys on the
    exact NAME, never on the suffix."""
    sbx = run_sandbox(
        {
            "BIAL_APP_ID": "app-guard",
            "BIAL_PORTAL_ORIGIN": "http://127.0.0.1:1",
            "BIAL_BLOB_CONTAINER_URL": _BLOB_URL,
            "BIAL_BLOB_SAS": _BLOB_SAS,
            "BIAL_DATABASE_URL": _DB_DSN,
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
    # ...while exactly the injected identity vars — and the ADR-0028 DSN — survive.
    for k in (
        "BIAL_APP_ID",
        "BIAL_PORTAL_ORIGIN",
        "BIAL_DATABASE_URL",
    ):
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


# --- ADR-0028: the DSN reaches the child but neither it NOR its password survives any surface ---
def test_exec_redacts_the_whole_dsn_and_its_password_sub_token(guarded: Sandbox) -> None:
    # `printenv` prints `BIAL_DATABASE_URL=<dsn>` — the NAME is there (it reached the child), the
    # VALUE is not. Then a line carrying ONLY the password: the whole-value registration cannot
    # cover it, so this is what proves the sub-token registration exists (D18).
    out = _ok(guarded, ["printenv"])
    assert "BIAL_DATABASE_URL" in out["stdout"]
    assert _DB_PASSWORD not in out["stdout"]
    assert "db-guard.invalid" not in out["stdout"]  # host + database name ride the whole value

    lone = _ok(guarded, ["sh", "-c", f'echo "password authentication failed: {_DB_PASSWORD}"'])
    assert _DB_PASSWORD not in lone["stdout"]
    assert "***" in lone["stdout"]


def test_dev_logs_redact_the_dsn_and_its_password(
    sandbox_factory: Callable[..., Sandbox],
) -> None:
    # /dev/logs is redacted per line from the same secret set — a migration tool printing the DSN
    # (or just the password) into the dev server's stdout must not ride the orchestrator's context.
    # Its OWN container: `dev/start` is one-shot per container (409 on a second call).
    sbx = sandbox_factory({"BIAL_DATABASE_URL": _DB_DSN})
    script = f'echo "connect {_DB_DSN}"; echo "pw={_DB_PASSWORD}"; sleep 30'
    assert sbx.dev_start(["sh", "-c", script]).status_code == 200

    lines: list[str] = []
    for _ in range(20):
        lines = sbx.dev_logs(0).json()["lines"]
        if len(lines) >= 2:
            break
        time.sleep(0.5)
    joined = "\n".join(lines)
    assert joined.count("***") >= 2  # both lines actually arrived and were scrubbed
    assert _DB_DSN not in joined
    assert _DB_PASSWORD not in joined


def test_files_view_redacts_a_stored_dsn(guarded: Sandbox) -> None:
    # An agent that inlines the DSN into a file gets it back REDACTED through `/files view` —
    # the accidental-leak guard for the "never inline the connection string" prompt rule.
    create = guarded.files(
        {"action": "create", "path": "leak-dsn.ts", "file_text": f'const url = "{_DB_DSN}";\n'}
    )
    assert create.status_code == 200
    view = guarded.files({"action": "view", "path": "leak-dsn.ts"})
    assert view.status_code == 200
    content = view.json()["content"]
    assert _DB_DSN not in content
    assert _DB_PASSWORD not in content
    assert "***" in content


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


def test_exec_closes_stdin_so_a_stdin_reader_eofs_instead_of_hanging(guarded: Sandbox) -> None:
    # F4: exec_cmd runs every command with stdin=DEVNULL. `cat` with no file argument reads stdin
    # until EOF — with an OPEN/TTY stdin it would block forever (and burn the full timeout); with a
    # CLOSED stdin it EOFs instantly and exits 0. A short timeout proves the fast-fail: if stdin
    # were still inherited from the supervisor, this would 504, not return promptly. This is the
    # in-container proof of what the offline lane asserts via subprocess.run wiring, and the class-
    # level guard behind the drizzle-kit rename-prompt hang (which aborts the same way on
    # no-TTY — verified out-of-band against drizzle-kit 0.31.10's TTY-guarded prompt renderer).
    out = guarded.exec_cmd(["cat"], timeout=10)
    assert out.status_code == 200, "a closed-stdin `cat` must EOF fast, not time out (504)"
    body = out.json()
    assert body["exit"] == 0, body.get("stderr", "")[:400]


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
