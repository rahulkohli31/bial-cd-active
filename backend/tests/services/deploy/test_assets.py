"""The platform-owned build assets.

The line-ending test is the reason this file exists. These assets ship inside the backend
image, which is built on a Windows VM, and they are copied verbatim into every generated
app's build context. A CRLF checkout would bake `\\r` into the app Dockerfile and the
production migrator and break the build for every citizen at once — the exact failure mode
that has taken this platform's container down twice before, and one that is invisible on a
Mac. The root `.gitattributes` pins the directory to LF; this asserts the pin held.

The content assertions are narrow on purpose. They pin the handful of properties that are
security or correctness decisions rather than style, so that removing one is a deliberate
act with a failing test attached.
"""

from __future__ import annotations

from importlib import resources

import pytest

_ASSETS = "src.services.deploy.assets"
_ASSET_NAMES = ("Dockerfile", "dockerignore", "db-migrate.mjs", "next.config.ts", "gitkeep")


def _read(name: str) -> bytes:
    return (resources.files(_ASSETS) / name).read_bytes()


@pytest.mark.parametrize("name", _ASSET_NAMES)
def test_every_asset_is_lf_only(name: str) -> None:
    assert b"\r\n" not in _read(name), f"{name} has CRLF line endings"
    assert b"\r" not in _read(name), f"{name} has a stray carriage return"


@pytest.mark.parametrize("name", _ASSET_NAMES)
def test_every_asset_is_non_empty(name: str) -> None:
    # A resource that silently resolves to nothing would produce an empty Dockerfile and a
    # baffling build error rather than a missing-file one.
    assert _read(name).strip()


# --- the Dockerfile's security decisions ------------------------------------------


def test_install_scripts_are_disabled() -> None:
    """`package.json` is agent-editable, so a `postinstall` hook is arbitrary code
    execution inside the build agent."""
    assert b"npm ci --ignore-scripts" in _read("Dockerfile")


def _instructions(name: str) -> str:
    """The asset with comment and blank lines removed. Needed because these files EXPLAIN
    their own hardening in prose, so a naive substring search matches the comment that says
    "not `npm run build`" and reports the opposite of the truth."""
    return "\n".join(
        line
        for line in _read(name).decode().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_the_build_does_not_go_through_an_agent_editable_script() -> None:
    """`npm run build` would run whatever the agent put in the `build` script."""
    instructions = _instructions("Dockerfile")
    assert "npx --no-install next build" in instructions
    assert "npm run build" not in instructions


def test_the_container_does_not_run_as_root() -> None:
    assert b"USER node" in _read("Dockerfile")


def test_migrations_gate_the_server_start() -> None:
    """`&&`, never `;`. With `;` a failed migration still starts the app, which is the
    whole failure this design exists to prevent."""
    cmd = _read("Dockerfile").splitlines()[-1]
    assert b"db-migrate.mjs --strict &&" in cmd
    assert b"; exec node server.js" not in cmd


# --- the migrator's two modes ------------------------------------------------------


def test_the_migrator_can_fail_in_strict_mode() -> None:
    migrator = _read("db-migrate.mjs")
    assert b"--strict" in migrator
    # The lenient path's giveaway — an exit(0) on the timeout — must be conditional now.
    assert b"process.exit(STRICT ? 1 : 0)" in migrator


def test_the_strict_timeout_aborts_rather_than_continuing() -> None:
    """The non-strict timer calls exit(0) MID-MIGRATION. Repeating that against a live
    database is how you get a schema nobody can reason about."""
    migrator = _read("db-migrate.mjs").decode()
    strict_branch = migrator[migrator.index("const timer = setTimeout") :]
    assert "if (STRICT) {" in strict_branch
    assert "aborting" in strict_branch


# --- the Next config wrapper -------------------------------------------------------


def test_the_wrapper_forces_standalone_output() -> None:
    assert b'output: "standalone"' in _read("next.config.ts")


def test_the_wrapper_reclaims_type_checking_after_spreading_the_app_config() -> None:
    wrapper = _read("next.config.ts").decode()
    assert wrapper.index("...appConfig") < wrapper.index("ignoreBuildErrors: false")


def test_the_wrapper_traces_the_migrator_and_its_sql() -> None:
    """Neither is in the Next module graph, so tracing cannot find them. Without these the
    container starts, runs the migrator, and dies with MODULE_NOT_FOUND."""
    wrapper = _read("next.config.ts")
    assert b"outputFileTracingIncludes" in wrapper
    assert b"./drizzle/**" in wrapper
    assert b"./scripts/db-migrate.mjs" in wrapper
    assert b"./node_modules/drizzle-orm/**" in wrapper
