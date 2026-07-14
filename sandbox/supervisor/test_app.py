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

import os
import pwd
import tempfile
from pathlib import Path

# Seed the module-level fail-fast config BEFORE importing app.py.
os.environ.setdefault("SUPERVISOR_TOKEN", "test-token-not-a-real-secret")
os.environ.setdefault("APP_USER", pwd.getpwuid(os.getuid()).pw_name)
os.environ["WORKSPACE"] = tempfile.mkdtemp(prefix="bial-sup-ws-")

from app import WORKSPACE, _child_env, app  # noqa: E402
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
    # suffix; a *_DSN sails through), the supervisor token, and the four C9 identity vars.
    seeded = {
        "FOO_PASSWORD": "hunter2",
        "IDENTITY_HEADER": "azure-msi-secret",
        "SOME_DSN": "postgres://u:p@h/db",
        "BIAL_APP_ID": "app-123",
        "BIAL_APP_CREDENTIAL": "cred-abc",
        "BIAL_DATA_BASE_URL": "https://platform.example/v1",
        "BIAL_PORTAL_ORIGIN": "https://portal.example",
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

    # The four C9 identity vars survive the scrub.
    assert env["BIAL_APP_ID"] == "app-123"
    assert env["BIAL_APP_CREDENTIAL"] == "cred-abc"
    assert env["BIAL_DATA_BASE_URL"] == "https://platform.example/v1"
    assert env["BIAL_PORTAL_ORIGIN"] == "https://portal.example"


def test_child_env_extra_layers_on_and_path_survives() -> None:
    env = _child_env({"PORT": "3000", "HOST": "0.0.0.0"})
    assert env["PORT"] == "3000"
    assert env["HOST"] == "0.0.0.0"
    # PATH is on the allowlist, so it comes through from the parent for the node runtime.
    assert "PATH" in env


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
