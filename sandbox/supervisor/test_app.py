"""Offline, NON-SPAWNING C1-conformance + guard-regression suite for the supervisor (U14).

The offline lane covers everything that does NOT spawn a child: the fail-closed child-env scrub
(a pure function), `/files` (all actions), auth, and Pydantic/action body-validation — including
the frozen 400-vs-422 split. SPAWNING scenarios (`/exec` exit/timeout, `/dev/*`), real UID
demotion, and token isolation need root to demote to `appuser` (`_DEMOTE`'s `setgroups()` raises
EPERM for a non-root process even demoting to itself), so they run IN-CONTAINER as root
(`tests/test_supervisor_guards_incontainer.py`). See the plan U14 lane split.

Import note: `app.py` resolves `SUPERVISOR_TOKEN`, `WORKSPACE`, and `pwd.getpwnam(APP_USER)` at
MODULE import (fail-fast config), so we seed a throwaway token, a temp workspace, and this
process's own account BEFORE importing `app`; on the image `APP_USER` is the real `appuser`.
"""

from __future__ import annotations

import atexit
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# Seed the module-level fail-fast config BEFORE importing app.py.
os.environ.setdefault("SUPERVISOR_TOKEN", "test-token-not-a-real-secret")
os.environ.setdefault("APP_USER", pwd.getpwuid(os.getuid()).pw_name)
_WS = tempfile.mkdtemp(prefix="bial-sup-ws-")
os.environ["WORKSPACE"] = _WS
atexit.register(shutil.rmtree, _WS, ignore_errors=True)  # don't leak the temp workspace per run

from urllib.parse import unquote  # noqa: E402

from app import (  # noqa: E402
    _BIAL_INJECTED_KEYS,
    APP_HOME,
    WORKSPACE,
    _child_env,
    _mint_token,
    _redact,
    app,
)
from fastapi.testclient import TestClient  # noqa: E402  (must follow the env seeding above)

TOKEN = os.environ["SUPERVISOR_TOKEN"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}
client = TestClient(app)


def _write(name: str, text: str) -> Path:
    p = WORKSPACE / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- the fail-closed child-env scrub (a pure function — the security boundary) ----------------
def test_child_env_is_a_fail_closed_allowlist() -> None:
    # The parent env carries secrets a suffix denylist would miss (IDENTITY_HEADER matches no
    # suffix; a *_DSN sails through), the supervisor token, and every injected BIAL_* var.
    seeded = {
        "FOO_PASSWORD": "hunter2",
        "IDENTITY_HEADER": "azure-msi-secret",
        "SOME_DSN": "postgres://u:p@h/db",
        "BIAL_APP_ID": "app-123",
        "BIAL_PORTAL_ORIGIN": "https://portal.example",
        "BIAL_BLOB_SAS": "sv=2021-08-06&sig=abc",
        "BIAL_DATABASE_URL": "postgresql://bialrole_x:pw@db.example:5432/bialapp_x",
    }
    os.environ.update(seeded)
    try:
        env = _child_env()
    finally:
        for k in seeded:
            os.environ.pop(k, None)

    # Denied by default — none of these match the allowlist.
    assert "FOO_PASSWORD" not in env
    assert "IDENTITY_HEADER" not in env
    assert "SOME_DSN" not in env
    assert "SUPERVISOR_TOKEN" not in env

    # Every injected BIAL_* var survives the scrub — including the two that end in `_URL`,
    # which a suffix denylist would wrongly drop.
    assert env["BIAL_APP_ID"] == "app-123"
    assert env["BIAL_PORTAL_ORIGIN"] == "https://portal.example"
    assert env["BIAL_BLOB_SAS"] == "sv=2021-08-06&sig=abc"
    assert env["BIAL_DATABASE_URL"] == "postgresql://bialrole_x:pw@db.example:5432/bialapp_x"


def test_child_env_extra_layers_on_and_path_survives() -> None:
    env = _child_env({"PORT": "3000", "HOST": "0.0.0.0"})
    assert env["PORT"] == "3000"
    assert env["HOST"] == "0.0.0.0"
    # PATH is on the allowlist, so it comes through from the parent for the node runtime.
    assert "PATH" in env


def test_child_env_carries_the_npm_and_node_runtime_names() -> None:
    # The open-sandbox `run_command` npm install (U1/U7) needs its runtime env to survive the
    # scrub: the `npm_`/`NODE_` prefixes are allowlisted so npm config + node options pass
    # through, and HOME is forced to the appuser-owned account so npm's default cache
    # ($HOME/.npm) is writable (R9/R13). This proves the EXISTING allowlist already covers a
    # runtime install — no new entry (and no wildcard) is needed for a public, no-proxy registry.
    seeded = {
        "npm_config_registry": "https://registry.npmjs.org/",
        "NODE_OPTIONS": "--max-old-space-size=512",
        "SUPERVISOR_TOKEN": "tok-should-not-leak",  # noqa: S105
    }
    os.environ.update(seeded)
    try:
        env = _child_env()
    finally:
        for k in seeded:
            os.environ.pop(k, None)
    assert env["npm_config_registry"] == "https://registry.npmjs.org/"
    assert env["NODE_OPTIONS"] == "--max-old-space-size=512"
    assert env["HOME"] == APP_HOME  # npm's default cache ($HOME/.npm) is appuser-writable
    assert "SUPERVISOR_TOKEN" not in env  # the allowlist did NOT widen to admit the token


def test_child_env_sets_ci_so_clis_refuse_to_prompt() -> None:
    # F4: no TTY reaches a demoted child, so an interactive CLI (drizzle-kit's rename-vs-create
    # disambiguation, npm/next confirmations) would block on stdin until the timeout burned — the
    # 600s hang the walkthrough QA hit. CI=1 makes well-behaved tools fail fast. It is set before
    # `extra`, so a step that genuinely needs CI unset can still override it.
    assert _child_env()["CI"] == "1"
    assert _child_env({"CI": "0"})["CI"] == "0"


def test_exec_closes_child_stdin_so_a_prompt_cannot_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    # F4: exec_cmd hands the child a CLOSED stdin (immediate EOF) so a CLI that probes
    # `process.stdin.isTTY` (drizzle-kit's prompt renderer does this) aborts fast, not waiting on
    # input that never comes. We intercept subprocess.run to inspect the wiring without a real
    # spawn — a real spawn would demote to APP_USER (needs root, the in-container lane's job).
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.subprocess.run", fake_run)
    resp = client.post("/exec", json={"cmd": ["true"]}, headers=AUTH)
    assert resp.status_code == 200
    assert captured["stdin"] == subprocess.DEVNULL


def test_a_manufactured_tty_is_refused_before_it_can_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4 — the escalation the trace actually recorded. Told to run a prompting
    `drizzle-kit generate`, the agent worked AROUND the closed stdin by manufacturing a
    terminal, and the command then sat at its prompt for 4m09s until the timeout fired. A real
    pty defeats every `isTTY` check, so this is the only layer that can refuse it.

    Refused as a normal exit-1 result with a correctable message, not an HTTP error: the caller
    is a model, and a 4xx reads as an opaque tool failure it cannot learn anything from.

    Mutation-check: delete the `_refuse_a_manufactured_tty` call in `exec_cmd` and the pty case
    reaches `subprocess.run` — in production, that is the four-minute hang.
    """
    spawned: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        spawned.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.subprocess.run", fake_run)

    for cmd in (
        ["python3", "-c", "import pty; pty.spawn(['npx', 'drizzle-kit', 'generate'])"],
        ["script", "-qec", "npx drizzle-kit generate", "/dev/null"],
        ["script", "-qc", "npm run migrate", "/dev/null"],
        ["expect", "-c", "spawn npx drizzle-kit generate"],
    ):
        resp = client.post("/exec", json={"cmd": cmd}, headers=AUTH)
        assert resp.status_code == 200, cmd
        body = resp.json()
        assert body["exit"] == 1, cmd
        # The message must name the way OUT, not just say no — a refusal the model cannot act
        # on just becomes another workaround attempt.
        assert "non-interactively" in body["stderr"], cmd
        assert "--name" in body["stderr"], cmd

    assert spawned == [], "a refused command must never reach subprocess.run"


def test_the_denylist_does_not_refuse_ordinary_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half, and the reason the match is scoped to inline program text. Write mode's
    `run_command` is deliberately unrestricted; a filter that refused any command MENTIONING a
    pty would block reading the very files that mention one."""
    spawned: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        spawned.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.subprocess.run", fake_run)

    allowed = [
        ["grep", "-rn", "pty.spawn", "src/"],  # searching FOR the pattern is not using it
        ["cat", "openpty.md"],
        ["npx", "drizzle-kit", "generate", "--name", "add_status"],
        ["npm", "run", "build"],
        ["node", "-e", "console.log('script')"],  # the WORD, not a pty call
    ]
    for cmd in allowed:
        assert client.post("/exec", json={"cmd": cmd}, headers=AUTH).status_code == 200, cmd
    assert spawned == allowed


# --- auth: /health is open; any bearer mismatch is 401 ----------------------------------------
def test_health_is_open_and_ok() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_missing_bearer_is_401() -> None:
    assert client.post("/files", json={"action": "view", "path": "x"}).status_code == 401


def test_wrong_bearer_is_401() -> None:
    r = client.post(
        "/files", json={"action": "view", "path": "x"}, headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401


def test_extra_whitespace_bearer_is_401() -> None:
    # Exact string compare: even a doubled space between scheme and token fails (C1).
    r = client.post(
        "/files",
        json={"action": "view", "path": "x"},
        headers={"Authorization": f"Bearer  {TOKEN}"},
    )
    assert r.status_code == 401


# --- /files: view -----------------------------------------------------------------------------
def test_files_view_is_1indexed_tab_separated() -> None:
    _write("v.txt", "alpha\nbeta\ngamma")
    r = client.post("/files", json={"action": "view", "path": "v.txt"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == "1\talpha\n2\tbeta\n3\tgamma"


def test_files_view_range_clamps_end_to_last_line() -> None:
    _write("v2.txt", "a\nb\nc")
    r = client.post(
        "/files", json={"action": "view", "path": "v2.txt", "view_range": [2, 99]}, headers=AUTH
    )
    assert r.json()["content"] == "2\tb\n3\tc"


def test_files_view_range_minus_one_means_end_of_file() -> None:
    # The C2/C7 read tool promises `end=-1` = end of file; a naive min(-1, len) computed an
    # EMPTY range. Regression-pin the full-file and from-line-2 spellings.
    _write("v3.txt", "a\nb\nc")
    full = client.post(
        "/files", json={"action": "view", "path": "v3.txt", "view_range": [1, -1]}, headers=AUTH
    )
    assert full.status_code == 200
    assert full.json()["content"] == "1\ta\n2\tb\n3\tc"

    tail = client.post(
        "/files", json={"action": "view", "path": "v3.txt", "view_range": [2, -1]}, headers=AUTH
    )
    assert tail.json()["content"] == "2\tb\n3\tc"


# --- /files: create (LF-normalizes + mkdir -p) ------------------------------------------------
def test_files_create_lf_normalizes_and_makes_parents() -> None:
    r = client.post(
        "/files",
        json={"action": "create", "path": "sub/dir/new.tsx", "file_text": "a\r\nb\r\n"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert (WORKSPACE / "sub/dir/new.tsx").read_text(encoding="utf-8") == "a\nb\n"


def test_files_create_missing_file_text_is_400() -> None:
    r = client.post("/files", json={"action": "create", "path": "x.txt"}, headers=AUTH)
    assert r.status_code == 400


# --- /files: str_replace (exactly-once semantics) ---------------------------------------------
def test_files_str_replace_single_match() -> None:
    _write("r.txt", "foo bar baz")
    r = client.post(
        "/files",
        json={"action": "str_replace", "path": "r.txt", "old_str": "bar", "new_str": "BAR"},
        headers=AUTH,
    )
    assert r.status_code == 200 and r.json()["replacements"] == 1
    assert (WORKSPACE / "r.txt").read_text(encoding="utf-8") == "foo BAR baz"


def test_files_str_replace_zero_matches_is_422() -> None:
    _write("r0.txt", "foo")
    r = client.post(
        "/files",
        json={"action": "str_replace", "path": "r0.txt", "old_str": "zzz", "new_str": "x"},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_files_str_replace_multi_match_is_422() -> None:
    _write("r2.txt", "x x x")
    r = client.post(
        "/files",
        json={"action": "str_replace", "path": "r2.txt", "old_str": "x", "new_str": "y"},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_files_str_replace_missing_subfield_is_400() -> None:
    _write("r3.txt", "foo")
    r = client.post(
        "/files",
        json={"action": "str_replace", "path": "r3.txt", "old_str": "foo"},  # no new_str
        headers=AUTH,
    )
    assert r.status_code == 400


# --- /files: insert (0-based list index) ------------------------------------------------------
def test_files_insert_at_0based_index() -> None:
    _write("i.txt", "line0\nline1\nline2")
    r = client.post(
        "/files",
        json={"action": "insert", "path": "i.txt", "insert_line": 1, "insert_text": "INS"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert (WORKSPACE / "i.txt").read_text(encoding="utf-8").split("\n")[1] == "INS"


def test_files_insert_missing_subfield_is_400() -> None:
    _write("i2.txt", "a\nb")
    r = client.post(
        "/files", json={"action": "insert", "path": "i2.txt", "insert_line": 0}, headers=AUTH
    )
    assert r.status_code == 400


# --- /files: unknown action + workspace-escape guard ------------------------------------------
def test_files_unknown_action_is_400() -> None:
    r = client.post("/files", json={"action": "bogus", "path": "x"}, headers=AUTH)
    assert r.status_code == 400


def test_files_path_escape_is_400() -> None:
    r = client.post(
        "/files",
        json={"action": "create", "path": "../../../../etc/evil", "file_text": "x"},
        headers=AUTH,
    )
    assert r.status_code == 400


# --- the 400-vs-422 split: top-level Pydantic (422) vs action sub-field (400) ------------------
def test_missing_top_level_path_is_422() -> None:
    assert client.post("/files", json={"action": "view"}, headers=AUTH).status_code == 422


def test_missing_top_level_action_is_422() -> None:
    assert client.post("/files", json={"path": "x"}, headers=AUTH).status_code == 422


def test_exec_missing_cmd_is_422() -> None:
    # Missing the top-level required `cmd` -> 422 (Pydantic), WITHOUT spawning (validation first).
    assert client.post("/exec", json={}, headers=AUTH).status_code == 422


def test_exec_cwd_escape_is_400_before_any_spawn() -> None:
    # The workspace-escape guard rejects an escaping `cwd` in `_resolve` BEFORE `subprocess.run`,
    # so this 400 is reachable offline (the actual command never spawns).
    r = client.post("/exec", json={"cmd": ["echo", "hi"], "cwd": "../../../../etc"}, headers=AUTH)
    assert r.status_code == 400


# --- U8: the two per-app Blob vars reach the child via the allowlist --------------------------
def test_child_env_admits_the_blob_vars() -> None:
    seeded = {
        "BIAL_BLOB_CONTAINER_URL": "http://azurite:10000/devstoreaccount1/app-x",
        "BIAL_BLOB_SAS": "sv=2021-08-06&sr=c&sig=abcdef",
        "SOME_DSN": "postgres://u:p@h/db",  # denied by default (a denylist would miss it)
    }
    os.environ.update(seeded)
    try:
        env = _child_env()
    finally:
        for k in seeded:
            os.environ.pop(k, None)
    assert env["BIAL_BLOB_CONTAINER_URL"] == seeded["BIAL_BLOB_CONTAINER_URL"]
    assert env["BIAL_BLOB_SAS"] == seeded["BIAL_BLOB_SAS"]
    assert "SOME_DSN" not in env
    assert "SUPERVISOR_TOKEN" not in env  # the real token is never carried into the child env


# --- A1: the two Entra reference vars reach the child via the allowlist -----------------------
def test_child_env_admits_the_entra_vars() -> None:
    seeded = {
        "BIAL_ENTRA_TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "BIAL_ENTRA_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    }
    os.environ.update(seeded)
    try:
        env = _child_env()
    finally:
        for k in seeded:
            os.environ.pop(k, None)
    assert env["BIAL_ENTRA_TENANT_ID"] == seeded["BIAL_ENTRA_TENANT_ID"]
    assert env["BIAL_ENTRA_CLIENT_ID"] == seeded["BIAL_ENTRA_CLIENT_ID"]


# --- U8: GET /env/manifest — names + descriptions, NEVER values -------------------------------
def test_env_manifest_requires_auth() -> None:
    assert client.get("/env/manifest").status_code == 401


def test_env_manifest_returns_names_with_descriptions_and_no_values() -> None:
    os.environ["BIAL_BLOB_SAS"] = "sv=2021&sr=c&sig=SUPERSECRETSIGVALUE"
    try:
        r = client.get("/env/manifest", headers=AUTH)
    finally:
        os.environ.pop("BIAL_BLOB_SAS", None)
    assert r.status_code == 200
    body = r.json()
    names = [v["name"] for v in body["vars"]]
    # The manifest is DERIVED from the same table as the child-env allowlist, so assert they match
    # EXACTLY — a var can never be advertised-but-not-injected (or injected-but-not-advertised).
    assert names == list(_BIAL_INJECTED_KEYS)
    assert {"BIAL_APP_ID", "BIAL_BLOB_CONTAINER_URL", "BIAL_BLOB_SAS"} <= set(names)
    # Every entry is exactly {name, description} — a description present, no value field anywhere.
    assert all(set(v.keys()) == {"name", "description"} and v["description"] for v in body["vars"])
    # The SAS VALUE never appears in the manifest response (names only).
    assert "SUPERSECRETSIGVALUE" not in r.text


# --- U8: the pure redactor — raw (already-encoded) AND URL-decoded forms, min-length guard -----
def test_redactor_strips_raw_and_url_decoded_secret_forms() -> None:
    # The SDK returns the SAS ALREADY percent-encoded; env holds that raw form.
    raw_sas = "sv=2021-08-06&sr=c&sp=rwdl&sig=abc%2Bdef%2Fghi%3D"
    decoded_sas = unquote(raw_sas)  # what a layer that parsed the query emits: sig=abc+def/ghi=
    assert decoded_sas != raw_sas  # the two forms genuinely differ
    os.environ["BIAL_BLOB_SAS"] = raw_sas
    try:
        buf = (
            f"GET https://acct/app-x?{raw_sas} 200\n"  # a logged URL carries the raw encoded form
            f"URL.searchParams -> {decoded_sas}\n"  # a parser emits the URL-decoded form
            f"keep this ordinary text"
        )
        red = _redact(buf)
    finally:
        os.environ.pop("BIAL_BLOB_SAS", None)
    assert raw_sas not in red  # raw (encoded) form redacted
    assert decoded_sas not in red  # URL-decoded form ALSO redacted (KTD-8)
    assert "keep this ordinary text" in red  # non-secret text untouched
    assert "***" in red


# --- A1: the login gate ----------------------------------------------------------------------
def test_auth_check_passes_when_login_not_required() -> None:
    os.environ.pop("BIAL_LOGIN_REQUIRED", None)
    assert client.get("/auth/check").status_code == 200


def test_auth_check_401_interstitial_when_required_and_no_cookie() -> None:
    os.environ["BIAL_LOGIN_REQUIRED"] = "true"
    os.environ["BIAL_PORTAL_ORIGIN"] = "https://portal.example"
    os.environ["BIAL_APP_ID"] = "app-123"
    try:
        r = client.get("/auth/check")
    finally:
        for k in ("BIAL_LOGIN_REQUIRED", "BIAL_PORTAL_ORIGIN", "BIAL_APP_ID"):
            os.environ.pop(k, None)
    assert r.status_code == 401
    assert "Sign in with Microsoft" in r.text
    assert 'href="https://portal.example/api/v1/auth/sandbox/login?app_id=app-123"' in r.text
    assert 'target="_top"' in r.text  # only a real, top-level, user-activated click may escape


def test_auth_check_passes_with_a_valid_session_cookie() -> None:
    os.environ["BIAL_LOGIN_REQUIRED"] = "true"
    try:
        session_token = _mint_token({"app_id": "app-123"}, TOKEN, 3600)
        r = client.get("/auth/check", cookies={"bial_sandbox_session": session_token})
    finally:
        os.environ.pop("BIAL_LOGIN_REQUIRED", None)
    assert r.status_code == 200


def test_auth_check_treats_a_forged_cookie_as_unauthenticated() -> None:
    os.environ["BIAL_LOGIN_REQUIRED"] = "true"
    os.environ["BIAL_PORTAL_ORIGIN"] = "https://portal.example"
    os.environ["BIAL_APP_ID"] = "app-123"
    try:
        forged = _mint_token({"app_id": "app-123"}, "wrong-key-not-this-sandboxs-token", 3600)
        r = client.get("/auth/check", cookies={"bial_sandbox_session": forged})
    finally:
        for k in ("BIAL_LOGIN_REQUIRED", "BIAL_PORTAL_ORIGIN", "BIAL_APP_ID"):
            os.environ.pop(k, None)
    assert r.status_code == 401


def test_auth_complete_sets_a_cookie_and_redirects_on_a_valid_handoff() -> None:
    os.environ["BIAL_APP_ID"] = "app-123"
    try:
        handoff = _mint_token({"app_id": "app-123"}, TOKEN, 30)
        r = client.get("/auth/complete", params={"token": handoff}, follow_redirects=False)
    finally:
        os.environ.pop("BIAL_APP_ID", None)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    set_cookie = r.headers.get("set-cookie", "")
    assert "bial_sandbox_session=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_auth_complete_rejects_an_invalid_token() -> None:
    r = client.get(
        "/auth/complete", params={"token": "not-a-real-token"}, follow_redirects=False
    )
    assert r.status_code == 401


def test_auth_complete_rejects_a_token_signed_for_a_different_sandbox() -> None:
    os.environ["BIAL_APP_ID"] = "app-123"
    try:
        forged = _mint_token({"app_id": "app-123"}, "not-this-containers-supervisor-token", 30)
        r = client.get("/auth/complete", params={"token": forged}, follow_redirects=False)
    finally:
        os.environ.pop("BIAL_APP_ID", None)
    assert r.status_code == 401


def test_auth_complete_rejects_a_token_minted_for_a_different_app() -> None:
    os.environ["BIAL_APP_ID"] = "app-123"
    try:
        handoff = _mint_token({"app_id": "some-other-app"}, TOKEN, 30)
        r = client.get("/auth/complete", params={"token": handoff}, follow_redirects=False)
    finally:
        os.environ.pop("BIAL_APP_ID", None)
    assert r.status_code == 401


# --- ADR-0028: the per-project database DSN — admitted by name, redacted both ways -----------
_DSN = "postgresql://bialrole_ab12:Sup3rSecretRolePassw0rd@db.example:5432/bialapp_ab12"


def test_child_env_admits_the_database_url() -> None:
    # The allowlist is fail-closed BY EXACT NAME: without its `_INJECTED_ENV` row the child
    # `next dev` (and every run_command child) would SILENTLY never see the DSN — no error, just
    # an app that cannot connect. The `*_URL` suffix earns nothing: `SOME_DSN` is still denied.
    seeded = {"BIAL_DATABASE_URL": _DSN, "SOME_DSN": "postgres://u:p@h/db"}
    os.environ.update(seeded)
    try:
        env = _child_env()
    finally:
        for k in seeded:
            os.environ.pop(k, None)
    assert env["BIAL_DATABASE_URL"] == _DSN
    assert "SOME_DSN" not in env
    assert "SUPERVISOR_TOKEN" not in env


def test_redactor_strips_the_dsn_and_its_password_sub_token() -> None:
    # `_redact` is a blind whole-VALUE substring replace, so the whole DSN and its password are two
    # DIFFERENT registrations covering two different leaks — assert both, on lines where only one
    # of the two forms appears (a line carrying both would pass with either registration alone).
    password = "Sup3rSecretRolePassw0rd"  # noqa: S105 — a test fixture, not a real credential
    os.environ["BIAL_DATABASE_URL"] = _DSN
    try:
        red = _redact(
            f"drizzle-kit: connecting to {_DSN}\n"
            f"error: password authentication failed (pw={password})\n"
            f"keep this ordinary text"
        )
    finally:
        os.environ.pop("BIAL_DATABASE_URL", None)
    assert _DSN not in red  # the whole value
    assert password not in red  # ...and the password ALONE, which the whole value cannot cover
    assert "db.example" not in red  # the host rides inside the whole-value redaction
    assert "keep this ordinary text" in red


def test_redactor_strips_the_url_decoded_password_form() -> None:
    # `render_as_string` percent-encodes the password into the DSN, but node-postgres reports the
    # DECODED form in its auth errors — so the decoded sub-token must be registered too.
    encoded = "p%40ss-w0rd%2Fwith%2Bpunctuation"
    decoded = unquote(encoded)
    assert decoded != encoded
    os.environ["BIAL_DATABASE_URL"] = (
        f"postgresql://bialrole_x:{encoded}@db.example:5432/bialapp_x"
    )
    try:
        red = _redact(f"pg: auth failed for password {decoded}")
    finally:
        os.environ.pop("BIAL_DATABASE_URL", None)
    assert decoded not in red
    assert "***" in red


def test_redactor_survives_an_unparseable_database_url() -> None:
    # The sub-token parse runs INSIDE the redactor: a malformed value (an unclosed IPv6 bracket
    # makes `urlsplit` itself raise ValueError) must never turn sanitizing a log line into a 500.
    malformed = "postgresql://u:unparseablepw@[::1/bialapp_x"
    os.environ["BIAL_DATABASE_URL"] = malformed
    try:
        red = _redact(f"{malformed} appears here plus ordinary text")
    finally:
        os.environ.pop("BIAL_DATABASE_URL", None)
    assert malformed not in red  # the whole value is still redacted
    assert "ordinary text" in red


def test_redactor_ignores_short_or_empty_secret() -> None:
    # A short/empty secret is skipped (len >= 8 guard) so it never blanks ordinary text.
    os.environ["BIAL_BLOB_SAS"] = "short"  # len 5 < 8
    os.environ["BIAL_DATABASE_URL"] = ""  # empty
    try:
        red = _redact("this short text and empty value stay fully intact")
    finally:
        os.environ.pop("BIAL_BLOB_SAS", None)
        os.environ.pop("BIAL_DATABASE_URL", None)
    assert red == "this short text and empty value stay fully intact"  # nothing redacted


# --- /dev/status is OBSERVED truth (the round-3 demo blocker) ---------------------------------
# The supervisor only ever tracked its OWN child, so a dev server the agent relaunched itself
# (`pkill` + `nohup`) stayed invisible forever: `ready=False` over a live app, and the preview
# never framed. `ready` is now a SERVED RESPONSE — a local HTTP probe of the dev port — and
# `running` stays child-process truth. These are offline: the child is a fake and the probe is
# either patched or pointed at an ephemeral local port bound by the test itself.
import contextlib  # noqa: E402
import io  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer  # noqa: E402

import app as sup  # noqa: E402


class _FakeProc:
    """poll()-only stand-in for the dev child; None = alive, an int = exited."""

    def __init__(self, returncode: int | None) -> None:
        self._returncode = returncode
        self.pid = 4242
        self.stdout = io.StringIO("")

    def poll(self) -> int | None:
        return self._returncode


@pytest.fixture(autouse=True)
def _fresh_readiness_cache() -> None:
    """`/dev/status` caches its affirmative in module state (that is the point — the watchers
    poll every second), so clear it between tests or one test's ready leaks into the next."""
    sup._forget_ready()


def _poll_dev_status(deadline_s: float) -> bool:
    """Poll `/dev/status` the way every consumer does — a 1s-ish cadence with a bounded budget
    (`are_we_there_yet`, `wait_ready`, both `_watch_preview`s) — and report whether it ever said
    ready. Readiness is allowed to arrive on a LATER poll: the probe is single-flight and
    bounded, so a slow first render lands in the cache and the next poll picks it up."""
    deadline = time.monotonic() + deadline_s
    while True:
        if client.get("/dev/status", headers=AUTH).json()["ready"]:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


@contextlib.contextmanager
def _bound_but_silent_port() -> Iterator[int]:
    """A port that ACCEPTS the connection and never answers — the compile window. The kernel
    completes the handshake from the listen backlog without an `accept()`, so a connect succeeds
    and the read blocks, exactly as `next dev` behaves while Turbopack compiles the route.

    The backlog is deliberately generous: nothing here ever calls `accept()`, so every probe a
    test makes permanently consumes a slot, and a queue of 1 turns the SECOND connect into a
    refusal — a false "nothing is bound" that has nothing to do with the code under test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    try:
        yield int(sock.getsockname()[1])
    finally:
        sock.close()


@contextlib.contextmanager
def _http_serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    """A real local HTTP server on an ephemeral port. Threading, so a slow handler cannot
    serialize the probes that overlap it."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_port
    finally:
        srv.shutdown()
        srv.server_close()


def test_dev_status_owned_ready_child_probes_once_then_serves_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewritten from `..._is_ready_and_never_probes`. That test asserted the owned-and-ready
    path never pays for a probe — and that short-circuit IS the defect: the marker prints before
    the first route compiles, so `ready` meant "Next announced itself", not "a request
    succeeded". The probe therefore runs on this path now. It must run exactly ONCE: the
    watchers poll `/dev/status` every second, and the cached affirmative is what keeps that
    cheap.

    Do NOT make this green again by restoring the short-circuit — that silently reverts the
    whole unit.
    """
    probes: list[tuple[object, ...]] = []

    def counting_probe(*args: object) -> bool:
        probes.append(args)
        return True

    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
    monkeypatch.setattr(sup._Dev, "ready", True)
    monkeypatch.setattr(sup, "_dev_port_serving", counting_probe)
    r = client.get("/dev/status", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"running": True, "ready": True, "port": 3000, "exit_code": None}
    assert len(probes) == 1, "a served response — not the stdout marker — must decide `ready`"

    for _ in range(3):
        assert client.get("/dev/status", headers=AUTH).json()["ready"] is True
    assert len(probes) == 1, "the cached affirmative must not re-probe on every 1s poll"


def test_dev_status_booting_child_is_not_ready_unless_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Child alive, no marker yet, nothing answering the port: binding the port early (Next
    # prints "Local:" before the first compile) must not frame the preview.
    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
    monkeypatch.setattr(sup._Dev, "ready", False)
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: False)
    r = client.get("/dev/status", headers=AUTH)
    assert r.json() == {"running": True, "ready": False, "port": 3000, "exit_code": None}


def test_dev_status_dead_child_with_live_port_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE round-3 bug, as a mutation check: the agent pkill'd the supervisor's child and
    nohup-relaunched the server, the app served fine at the ingress, and `/dev/status` said
    not-ready forever — so the preview never framed. Remove the probe OR-arm in `dev_status`
    and this goes red."""
    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(137))  # pkill'd
    monkeypatch.setattr(sup._Dev, "ready", True)  # marker WAS seen before the kill
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: True)  # the nohup replacement
    r = client.get("/dev/status", headers=AUTH)
    # The kill's post-mortem (137) rides along even when the replacement serves fine.
    assert r.json() == {"running": False, "ready": True, "port": 3000, "exit_code": 137}


def test_dev_status_dead_child_and_dead_port_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(137))
    monkeypatch.setattr(sup._Dev, "ready", True)
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: False)
    r = client.get("/dev/status", headers=AUTH)
    assert r.json() == {"running": False, "ready": False, "port": 3000, "exit_code": 137}


class _AlwaysErrorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self.send_response(500)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        pass  # silence per-request stderr noise


def test_the_probe_counts_any_http_response_as_serving() -> None:
    # A 500-ing dev server is still SERVING — its brokenness is the app's business, not the
    # supervisor's. urllib surfaces 4xx/5xx as HTTPError, which must still count.
    srv = HTTPServer(("127.0.0.1", 0), _AlwaysErrorHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert sup._dev_port_serving(port=srv.server_port) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_probe_counts_connection_refused_as_not_serving() -> None:
    # Bind-then-close guarantees the port is real but nobody is listening.
    srv = HTTPServer(("127.0.0.1", 0), _AlwaysErrorHandler)
    port = srv.server_port
    srv.server_close()
    assert sup._dev_port_serving(port=port) is False


@contextlib.contextmanager
def _a_peer_that_trickles_header_bytes() -> Iterator[int]:
    """A peer that accepts, then answers ONE byte at a time and never a newline — so the status
    line is never complete and every individual read succeeds. This is what defeats a per-recv
    timeout, and the response here comes from unreviewed, agent-authored code."""
    stop = threading.Event()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)

    def drip(conn: socket.socket) -> None:
        try:
            conn.recv(4096)
            while not stop.wait(0.05):
                conn.sendall(b"X")  # never a "\r\n": readline can never finish
        except OSError:
            pass
        finally:
            conn.close()

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=drip, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    try:
        yield int(srv.getsockname()[1])
    finally:
        stop.set()
        srv.close()


def test_the_probe_gives_up_on_a_peer_that_trickles_header_bytes() -> None:
    """★ THE PROBE HAS TO HAVE A TOTAL BOUND, not just per-operation ones. `settimeout` re-arms
    on every socket operation and `http.client` performs many of them parsing a status line, so a
    peer feeding one byte per 50ms satisfies each read forever and the probe never returns.

    The consequence is not a slow poll, it is a permanent one: the probe holds the single-flight
    slot, so every `/dev/status` caller waits on an answer that will never come and `ready` reads
    False over an app that may since have become perfectly healthy. Re-arming `settimeout` before
    `getresponse()` does NOT fix this — only a deadline does.

    Mutation check: delete the watchdog timer from `_dev_port_serving` and this hangs to the
    join timeout and then fails on `is_alive()`."""
    with _a_peer_that_trickles_header_bytes() as port:
        answers: list[bool] = []

        def probe() -> None:
            answers.append(sup._dev_port_serving(port=port, timeout=0.5, read_timeout=1.0))

        prober = threading.Thread(target=probe, daemon=True)
        started = time.monotonic()
        prober.start()
        prober.join(timeout=8.0)
        elapsed = time.monotonic() - started

    assert not prober.is_alive(), "the probe never returned — the single-flight slot is pinned"
    assert answers == [False], "a peer that never completes a status line is not serving"
    assert elapsed < 5.0, f"the probe took {elapsed:.1f}s against a 1.0s read budget"


def test_the_probe_counts_a_bound_but_silent_port_as_not_serving() -> None:
    # A connection ACCEPTED but never answered is the compile window, and excluding it is the
    # whole point of the unit. A two-tier "accepted counts as serving" rule would latch `ready`
    # True during exactly the window this exists to exclude: `next dev` binds and accepts the
    # instant it starts, then holds the request open while Turbopack compiles.
    with _bound_but_silent_port() as port:
        assert sup._dev_port_serving(port=port, timeout=0.5) is False


# --- `ready` means a request ACTUALLY SUCCEEDED (U6) ------------------------------------------
def test_dev_status_is_ready_when_the_root_route_500s(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fail-open guard, and the single most dangerous behaviour in this change to get wrong.

    A compile error makes the root route answer 500 — that IS a served response, so `ready` must
    stay True and let the app's own error reach the model through `/dev/logs`. A false negative
    here makes `are_we_there_yet` return False, which synthesizes `dev_not_ready_error()` and
    tells the model its app "hangs at startup or renders a blank page" — sending the agent to
    debug a bug that does not exist. The marker is deliberately UNSET: the response alone
    decides.
    """
    with _http_serving(_AlwaysErrorHandler) as port:
        monkeypatch.setattr(sup, "_DEV_PORT", port)
        monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
        monkeypatch.setattr(sup._Dev, "ready", False)
        r = client.get("/dev/status", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["ready"] is True


def test_dev_status_is_not_ready_while_the_route_is_still_compiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE defect being fixed. `next dev` prints its ready marker as soon as it is listening and
    then holds the first request open while the route compiles; `ready` used to latch on that
    marker alone, so the preview framed onto a blank page and every `/dev/status` consumer
    believed a still-compiling app was up. Marker printed, child alive, port bound, nothing
    answered yet -> NOT ready."""
    with _bound_but_silent_port() as port:
        monkeypatch.setattr(sup, "_DEV_PORT", port)
        monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
        monkeypatch.setattr(sup._Dev, "ready", True)  # the marker HAS printed
        body = client.get("/dev/status", headers=AUTH).json()
        assert body["ready"] is False
        assert body["running"] is True  # `running` stays child-process truth


def test_dev_status_answers_promptly_when_the_port_never_responds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe bounds the poll. A dev port that accepts and never answers must not hang the
    caller: the watchers poll every second and the control plane caps a `/dev/status` request at
    30s, so a generous READ budget must never become a generous RESPONSE time."""
    with _bound_but_silent_port() as port:
        monkeypatch.setattr(sup, "_DEV_PORT", port)
        monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
        monkeypatch.setattr(sup._Dev, "ready", True)
        started = time.monotonic()
        r = client.get("/dev/status", headers=AUTH)
        elapsed = time.monotonic() - started
        assert r.json()["ready"] is False
        assert elapsed < 5.0, f"/dev/status blocked for {elapsed:.1f}s on a silent port"


class _SlowRootHandler(BaseHTTPRequestHandler):
    """200, but only after a cold-server-render's worth of delay."""

    delay = 2.5

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        time.sleep(self.delay)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        pass


def test_dev_status_becomes_ready_when_a_slow_render_finally_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other mandatory case, and the false negative that would send the agent chasing a
    phantom bug. A generated BIAL app server-renders against its per-project Postgres, so a cold
    root route routinely outlives the opportunistic 1.0s the `/dev/start` guard uses. A ~2.5s
    response must still reach ready — within a budget far smaller than `are_we_there_yet`'s."""
    with _http_serving(_SlowRootHandler) as port:
        monkeypatch.setattr(sup, "_DEV_PORT", port)
        monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
        monkeypatch.setattr(sup._Dev, "ready", False)
        assert _poll_dev_status(deadline_s=10.0) is True


def test_a_restart_invalidates_the_cached_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching the affirmative is required (the watchers poll at 1s) but a STICKY cache is a
    correctness bug: `selfheal.verify`'s dead-child rescue calls `dev_start` mid-verify, and the
    agent can restart the server itself. A cache that survived the restart would report the
    PREVIOUS server's `ready` while the new one is still compiling — the exact defect this unit
    removes, in a stickier form that never self-corrects."""
    answers = {"serving": True}
    probes: list[tuple[object, ...]] = []

    def probe(*args: object) -> bool:
        probes.append(args)
        return answers["serving"]

    # A server IS serving the port (here an unowned one, so `/dev/start` is reachable without
    # the already-running guard firing first) and `ready` latches True.
    monkeypatch.setattr(sup._Dev, "proc", None)
    monkeypatch.setattr(sup._Dev, "ready", True)
    monkeypatch.setattr(sup, "_dev_port_serving", probe)
    # The start guard asks a DIFFERENT question (`_dev_port_bound` — occupied, not answering),
    # so it gets its own stub; here the server goes away entirely, releasing the port.
    monkeypatch.setattr(sup, "_dev_port_bound", lambda *a: answers["serving"])
    assert client.get("/dev/status", headers=AUTH).json()["ready"] is True
    assert len(probes) == 1

    # It goes away, so `/dev/start`'s double-spawn guard lets a replacement spawn — and the
    # same call that clears the marker must clear the cache.
    answers["serving"] = False
    monkeypatch.setattr(sup.subprocess, "Popen", lambda cmd, **kwargs: _FakeProc(None))
    assert client.post("/dev/start", json={}, headers=AUTH).status_code == 200
    assert client.get("/dev/status", headers=AUTH).json()["ready"] is False

    # ...and the NEW server's first served response re-latches it.
    answers["serving"] = True
    assert client.get("/dev/status", headers=AUTH).json()["ready"] is True


def test_the_childs_death_invalidates_the_cached_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other reset point. Whatever answered the last probe may have BEEN the child that has
    since died, so a cached affirmative cannot outlive it — otherwise `/dev/status` reports a
    dead app as ready forever and `selfheal.verify`'s dead-child rescue never fires."""
    proc = _FakeProc(None)
    answers = {"serving": True}
    monkeypatch.setattr(sup._Dev, "proc", proc)
    monkeypatch.setattr(sup._Dev, "ready", True)
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: answers["serving"])
    assert client.get("/dev/status", headers=AUTH).json()["ready"] is True

    proc._returncode = 137  # SIGKILL — and nothing took its place on the port
    answers["serving"] = False
    assert client.get("/dev/status", headers=AUTH).json() == {
        "running": False,
        "ready": False,
        "port": 3000,
        "exit_code": 137,
    }


def test_a_probe_thread_that_cannot_start_hands_the_single_flight_slot_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE PERMANENT-WEDGE BRANCH, which had no coverage at all. `_dev_is_serving` claims the
    single-flight slot BEFORE it spawns the probe thread, so a spawn that fails under memory
    pressure — the realistic case in a memory-capped ACA container — would leave the slot held by
    a probe that will never run and never clear it. Unlike a stale affirmative, which
    `_READY_CACHE_TTL` bounds, that state has no expiry: `ready` reads False forever, for the
    whole life of the container, and no consumer can tell why.

    Two things must be true, and the branch exists for both: the failure PROPAGATES (a supervisor
    that cannot spawn threads is not a supervisor that should quietly answer "not ready"), and
    the slot is free afterwards so the next caller can still start a fresh probe.

    Mutation check: delete the `except BaseException` recovery in `_dev_is_serving` and the
    `probing is None` assertion goes red while the `pytest.raises` still passes — which is
    exactly how this hid."""

    class _CannotStart(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(sup.threading, "Thread", _CannotStart)

    with pytest.raises(RuntimeError):
        sup._dev_is_serving()

    assert sup._Ready.probing is None, "the single-flight slot is held by a probe that never ran"

    # …and the proof that it is genuinely reusable rather than merely nulled: with threads back,
    # the very next call runs a probe and latches its answer.
    monkeypatch.undo()
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a, **k: True)
    assert sup._dev_is_serving() is True


def test_dev_start_refuses_while_something_serves_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The door KTD-1 closes: with an unowned server holding the port, Next 13.4+ does NOT
    fail to bind — it falls back to the next free port and still prints "Ready in", minting a
    sticky marker-`ready` child that Caddy never proxies. The start must refuse instead."""
    monkeypatch.setattr(sup._Dev, "proc", None)
    monkeypatch.setattr(sup, "_dev_port_bound", lambda *a: True)

    def refuse_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused dev_start must never spawn")

    monkeypatch.setattr(sup.subprocess, "Popen", refuse_spawn)
    r = client.post("/dev/start", json={}, headers=AUTH)
    assert r.status_code == 409
    assert "already serving" in r.json()["detail"]


def test_dev_start_with_a_free_port_still_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    # The guard must not over-refuse: port free, no child → the normal spawn happens.
    monkeypatch.setattr(sup._Dev, "proc", None)
    monkeypatch.setattr(sup._Dev, "ready", False)
    monkeypatch.setattr(sup, "_dev_port_bound", lambda *a: False)
    spawned: list[list[str]] = []

    def fake_popen(cmd: list[str], **kwargs: object) -> _FakeProc:
        spawned.append(cmd)
        return _FakeProc(None)

    monkeypatch.setattr(sup.subprocess, "Popen", fake_popen)
    r = client.post("/dev/start", json={}, headers=AUTH)
    assert r.status_code == 200
    assert spawned == [["npm", "run", "dev"]]


def test_dev_start_refuses_a_bound_but_silent_port_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE DOUBLE-SPAWN WINDOW U1/U2 made routine. The relaunch attach arm and the Write
    turn's boot-at-attach now call `/dev/start` against containers that are ALREADY running a
    dev server — and a server mid-recompile accepts the connection and answers nothing, which is
    exactly what `_bound_but_silent_port` reproduces. The old guard asked "did anything ANSWER
    within a second?", read that as an empty port, and spawned a second `next dev`; Next then
    port-falls-back to a port Caddy does not proxy, and two Turbopack processes share a
    memory-capped ACA container (the `exit_code 137` this codebase already handles).

    The port being OCCUPIED is what triggers the fallback, so occupancy is what the guard asks.
    Mutation check: point `dev_start` back at `_dev_port_serving` and the spawn below fires."""
    with _bound_but_silent_port() as port:
        monkeypatch.setattr(sup, "_DEV_PORT", port)
        monkeypatch.setattr(sup._Dev, "proc", None)  # no owned child — only the unowned server

        def refuse_spawn(*args: object, **kwargs: object) -> None:
            raise AssertionError("a still-compiling dev server must not be double-spawned onto")

        monkeypatch.setattr(sup.subprocess, "Popen", refuse_spawn)
        # Premise guard: the answer-based probe genuinely calls this port absent, so the two
        # questions really do disagree here — otherwise this test would pass either way.
        assert sup._dev_port_serving(port=port, timeout=0.5) is False
        assert sup._dev_port_bound(port=port) is True

        r = client.post("/dev/start", json={}, headers=AUTH)

    assert r.status_code == 409
    assert "already serving" in r.json()["detail"]


# --- the kill denylist steers the agent away from the dev server ------------------------------
def test_process_kill_commands_are_refused_with_the_steering_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3's opening move: `pkill` the supervisor's dev child, nohup a replacement, and the
    replacement dies unwatched after the turn. The probe makes that survivable; this makes it
    rare. Refused as a normal exit-1 with a correctable message (same shape as the TTY guard),
    and nothing is executed."""
    spawned: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        spawned.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.subprocess.run", fake_run)

    for cmd in (
        ["pkill", "-f", "npm run dev"],
        ["kill", "1234"],
        ["killall", "node"],
        ["fuser", "-k", "3000/tcp"],
        ["/usr/bin/pkill", "node"],  # basename match, not a literal-string match
    ):
        resp = client.post("/exec", json={"cmd": cmd}, headers=AUTH)
        assert resp.status_code == 200, cmd
        body = resp.json()
        assert body["exit"] == 1, cmd
        # The message must say what to do INSTEAD — the dev server is managed for the agent.
        assert "managed" in body["stderr"] or "handled for you" in body["stderr"], cmd

    assert spawned == [], "a refused kill must never reach subprocess.run"


def test_merely_mentioning_kill_words_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # The denylist matches argv[0]'s basename only — reading, grepping, or npm-running things
    # that mention the words is ordinary work.
    spawned: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        spawned.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("app.subprocess.run", fake_run)

    allowed = [
        ["cat", "killers.md"],
        ["grep", "-rn", "kill", "src/"],
        ["npm", "run", "dev"],
        ["node", "-e", "console.log('kill nothing')"],
    ]
    for cmd in allowed:
        assert client.post("/exec", json={"cmd": cmd}, headers=AUTH).status_code == 200, cmd
    assert spawned == allowed


def test_the_tty_and_kill_guards_fire_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two frozensets, two refusal messages, no interference.
    monkeypatch.setattr(
        "app.subprocess.run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    tty = client.post(
        "/exec", json={"cmd": ["script", "-qec", "npm run dev", "/dev/null"]}, headers=AUTH
    ).json()
    kill = client.post("/exec", json={"cmd": ["pkill", "node"]}, headers=AUTH).json()
    assert "non-interactively" in tty["stderr"] and "managed" not in tty["stderr"]
    assert "managed" in kill["stderr"] and "non-interactively" not in kill["stderr"]


def test_an_unowned_server_that_dies_stops_being_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ THE STICKY-AFFIRMATIVE GUARD. The dead-child-with-a-live-unowned-port state is exactly
    the one the probe OR-arm exists to serve — the agent `pkill`s our child and `nohup`s its own
    replacement. But the child's death is a ONE-SHOT reset (`_Ready.mourned` remembers the
    corpse), so once that unowned server latched the affirmative there was no second event left
    to clear it: when the unowned server itself died, `/dev/status` went on reporting `ready:
    true` over a dead app forever, and no consumer could tell. `_watch_preview` consults `ready`
    before `running`, so it would never even emit `preview_reconnecting`.

    The old `(_Dev.ready and running) or _dev_port_serving()` got this right by never caching at
    all. The affirmative has to EXPIRE, not merely be invalidated at every place we can think
    of — an enumeration is only as good as its completeness, and this file keeps growing ways
    for a dev server to appear and vanish."""
    proc = _FakeProc(137)  # our child is already dead and already mourned
    answers = {"serving": True}
    monkeypatch.setattr(sup._Dev, "proc", proc)
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a, **k: answers["serving"])

    first = client.get("/dev/status", headers=AUTH).json()
    assert first["ready"] is True and first["running"] is False  # the unowned server answers

    answers["serving"] = False  # …and now it dies too. No further death event will fire.
    time.sleep(sup._READY_CACHE_TTL + 0.05)

    assert client.get("/dev/status", headers=AUTH).json() == {
        "running": False,
        "ready": False,
        "port": 3000,
        "exit_code": 137,
    }


def test_a_healthy_affirmative_is_still_cached_between_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The companion bound: expiry must not turn the cache back into a probe-per-poll. Within
    the TTL a burst of polls — two watchers at 1s plus `are_we_there_yet` — pays for exactly one
    request, which is the whole reason the cache exists."""
    probes = {"n": 0}

    def _counting(*_a: object, **_k: object) -> bool:
        probes["n"] += 1
        return True

    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
    monkeypatch.setattr(sup, "_dev_port_serving", _counting)

    for _ in range(6):
        assert client.get("/dev/status", headers=AUTH).json()["ready"] is True

    assert probes["n"] == 1, "six polls inside the TTL must cost one probe, not six"


def test_concurrent_polls_share_one_probe_and_agree_on_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE FLAPPING GUARD. Two watchers poll `/dev/status` every second while a probe can be
    in flight for up to `_READY_READ_TIMEOUT`. An earlier cut let the callers who did NOT start
    the probe return straight away on the theory they took "the last known answer" — but
    negatives are deliberately never cached, so what they actually took was an unconditional
    False. Over a healthy-but-slow route most polls in that window reported not-ready, and
    paired with `running: false` (a dev server the agent started itself, which is precisely what
    the probe exists to see) `_watch_preview` reads that pair as a crash edge and re-mounts the
    citizen's iframe. A healthy app flapped between ready and reconnecting.

    Every caller now waits on the SAME probe, so they cannot disagree — and it is still one
    loopback request, which is the whole point of the single flight."""
    with _http_serving(_SlowRootHandler) as port:
        monkeypatch.setattr(sup, "_DEV_PORT", port)
        monkeypatch.setattr(sup._Dev, "proc", _FakeProc(137))  # dead child, unowned live server
        monkeypatch.setattr(sup, "_STATUS_PROBE_WAIT", _SlowRootHandler.delay + 2.0)
        answers: list[bool] = []

        def poll() -> None:
            answers.append(sup._dev_is_serving())

        threads = [threading.Thread(target=poll) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        assert answers == [True, True, True], (
            "three simultaneous pollers must agree — a lone False here is the flap"
        )
