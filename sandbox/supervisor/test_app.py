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

from app import _BIAL_INJECTED_KEYS, APP_HOME, WORKSPACE, _child_env, _redact, app  # noqa: E402
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
# never framed. `ready` now ORs in a local HTTP probe of the dev port; `running` stays
# child-process truth. These are offline: the child is a fake and the probe is patched — the
# only real sockets are the probe-semantics tests, which bind an ephemeral local port.
import io  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

import app as sup  # noqa: E402


class _FakeProc:
    """poll()-only stand-in for the dev child; None = alive, an int = exited."""

    def __init__(self, returncode: int | None) -> None:
        self._returncode = returncode
        self.pid = 4242
        self.stdout = io.StringIO("")

    def poll(self) -> int | None:
        return self._returncode


def _serving(*args: object, **kwargs: object) -> bool:
    """Fail loudly if the probe is consulted when the marker path already answered."""
    raise AssertionError("the owned-and-ready path must never pay for a probe")


def test_dev_status_owned_ready_child_is_ready_and_never_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
    monkeypatch.setattr(sup._Dev, "ready", True)
    monkeypatch.setattr(sup, "_dev_port_serving", _serving)  # raises if called
    r = client.get("/dev/status", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"running": True, "ready": True, "port": 3000}


def test_dev_status_booting_child_is_not_ready_unless_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Child alive, no marker yet, nothing answering the port: binding the port early (Next
    # prints "Local:" before the first compile) must not frame the preview.
    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(None))
    monkeypatch.setattr(sup._Dev, "ready", False)
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: False)
    r = client.get("/dev/status", headers=AUTH)
    assert r.json() == {"running": True, "ready": False, "port": 3000}


def test_dev_status_dead_child_with_live_port_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE round-3 bug, as a mutation check: the agent pkill'd the supervisor's child and
    nohup-relaunched the server, the app served fine at the ingress, and `/dev/status` said
    not-ready forever — so the preview never framed. Remove the probe OR-arm in `dev_status`
    and this goes red."""
    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(137))  # pkill'd
    monkeypatch.setattr(sup._Dev, "ready", True)  # marker WAS seen before the kill
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: True)  # the nohup replacement
    r = client.get("/dev/status", headers=AUTH)
    assert r.json() == {"running": False, "ready": True, "port": 3000}


def test_dev_status_dead_child_and_dead_port_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sup._Dev, "proc", _FakeProc(137))
    monkeypatch.setattr(sup._Dev, "ready", True)
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: False)
    r = client.get("/dev/status", headers=AUTH)
    assert r.json() == {"running": False, "ready": False, "port": 3000}


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


def test_dev_start_refuses_while_something_serves_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The door KTD-1 closes: with an unowned server holding the port, Next 13.4+ does NOT
    fail to bind — it falls back to the next free port and still prints "Ready in", minting a
    sticky marker-`ready` child that Caddy never proxies. The start must refuse instead."""
    monkeypatch.setattr(sup._Dev, "proc", None)
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: True)

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
    monkeypatch.setattr(sup, "_dev_port_serving", lambda *a: False)
    spawned: list[list[str]] = []

    def fake_popen(cmd: list[str], **kwargs: object) -> _FakeProc:
        spawned.append(cmd)
        return _FakeProc(None)

    monkeypatch.setattr(sup.subprocess, "Popen", fake_popen)
    r = client.post("/dev/start", json={}, headers=AUTH)
    assert r.status_code == 200
    assert spawned == [["npm", "run", "dev"]]
