"""Fail-closed child-env scrub test for the sandbox supervisor (review #1).

`_child_env` is the security boundary between the root supervisor process (which holds the
per-session bearer token plus whatever Azure injects into the parent env) and the untrusted,
agent-generated app it spawns as an unprivileged UID. This pins the scrub as a FAIL-CLOSED
ALLOWLIST: parent-env secrets a suffix denylist would miss — `IDENTITY_HEADER`, a `*_DSN`, a
`*_PASSWORD` — must NOT reach the child, while the four C9 `BIAL_*` identity vars must.

Import note: `app.py` resolves `SUPERVISOR_TOKEN` and `pwd.getpwnam(APP_USER)` at MODULE import
(fail-fast config). We seed a throwaway token and point APP_USER at this process's own account
so the import succeeds offline; on the sandbox image APP_USER is the real `appuser`.
"""

from __future__ import annotations

import os
import pwd

# Seed the module-level fail-fast config BEFORE importing app.py.
os.environ.setdefault("SUPERVISOR_TOKEN", "test-token-not-a-real-secret")
os.environ.setdefault("APP_USER", pwd.getpwuid(os.getuid()).pw_name)

from app import _child_env  # noqa: E402  (must follow the env seeding above)


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
