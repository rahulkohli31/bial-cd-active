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
_ASSET_NAMES = (
    "Dockerfile",
    "dockerignore",
    "db-migrate.mjs",
    "next.config.ts",
    "gitkeep",
    "copy-runtime-deps.mjs",
)


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


def test_the_config_default_and_the_dockerfile_arg_name_the_same_base() -> None:
    """The two base-image defaults must not drift apart unnoticed.

    They are two halves of one decision with different blast radii. `config.py`'s value is what
    SHIPS — `images.py` sends it as the NODE_IMAGE build arg on every platform build, so it
    always beats the ARG default below it. The Dockerfile's own default only applies to a
    hand-run `docker build`, which is how the go-live runbook path works. Changing one and not
    the other means the artifact an operator builds by hand and the artifact the platform builds
    differ in their base image, and nothing anywhere reports it.

    Read off `model_fields` rather than an instance: `DeployConfig` has ten required fields
    (registry credentials, subscription, resource group, …), so constructing one here would mean
    duplicating a ten-key fixture into this file just to read a default.
    """
    from src.services.deploy.config import DeployConfig

    arg_line = next(
        line
        for line in _read("Dockerfile").decode().splitlines()
        if line.startswith("ARG NODE_IMAGE=")
    )
    arg_default = arg_line.split("=", 1)[1].split("#")[0].strip()

    assert arg_default == DeployConfig.model_fields["node_base_image"].default

    # R5: the shipped base is pinned by digest, not by a tag that moves under us. A bare tag
    # would still pass the equality above while quietly reintroducing the drift the pin exists
    # to stop — the same failure that let the portal's base go 16 months stale.
    assert "@sha256:" in arg_default


def test_the_address_arguments_are_declared_where_the_build_can_see_them() -> None:
    """A pre-FROM `ARG` is in scope for `FROM` lines and nothing else.

    Declared beside `NODE_IMAGE` at the top, these two would be accepted by the registry, expand
    to the empty string inside the builder stage, and produce an image that serves at `/` — a
    green build, a green deploy, and a 404 for the first person who opens the app. So they are
    re-declared INSIDE the builder stage and promoted to `ENV` before `next build`, which is
    where the next.config wrapper reads them.
    """
    # Comments stripped: this Dockerfile explains its own ARG scoping in prose, so a raw
    # search finds the sentence about `next build` long before the instruction.
    instructions = _instructions("Dockerfile")
    builder = instructions[
        instructions.index("FROM ${NODE_IMAGE} AS builder") : instructions.index(
            "FROM ${NODE_IMAGE} AS runner"
        )
    ]

    for name in ("BIAL_BASE_PATH", "BIAL_APPS_HOSTNAME"):
        assert f"ARG {name}\n" in builder, f"{name} is not declared inside the builder stage"
        assert f"{name}=${{{name}}}" in builder, f"{name} is never promoted to the environment"
        assert builder.index(f"ARG {name}") < builder.index("next build")
        assert builder.index(f"{name}=${{{name}}}") < builder.index("next build")


def test_neither_address_argument_has_a_default() -> None:
    """The whole fail-loud path depends on absence being visible.

    Unlike `NODE_IMAGE`, whose default is a legitimate hand-build fallback, there is no value
    here that is right for more than one app. A default would satisfy the wrapper's guard with
    someone else's address instead of failing the build.
    """
    dockerfile = _read("Dockerfile").decode()
    assert "ARG BIAL_BASE_PATH=" not in dockerfile
    assert "ARG BIAL_APPS_HOSTNAME=" not in dockerfile


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


def test_the_wrapper_asserts_the_base_path_rather_than_merging_it() -> None:
    """The app's config is agent-written. An agent that sets `basePath` for its own reasons
    would otherwise move the app off the only path the router forwards to it — and that reads
    as a 404, not as a bad config. So this key follows `output`'s hard-override shape, with no
    spread of the app's value anywhere near it."""
    wrapper = _read("next.config.ts").decode()

    assert 'process.env.BIAL_BASE_PATH ?? ""' in wrapper
    assert "appConfig?.basePath" not in wrapper
    assert wrapper.index("...appConfig") < wrapper.index("\n  basePath,")


def test_the_wrapper_lets_server_actions_accept_a_post_from_the_apps_hostname() -> None:
    """Once traffic arrives through the router the browser's origin and the upstream host
    differ by construction, and Next aborts a Server Action on that mismatch. Without this key
    every form in the app fails its CSRF check and nothing else does.

    Spread at BOTH levels, unlike `basePath`: `experimental` is a bag of unrelated keys and the
    one the platform needs is two levels down, so a bare assignment at either level would
    silently delete whatever else the app had set there."""
    wrapper = _read("next.config.ts").decode()

    assert 'process.env.BIAL_APPS_HOSTNAME ?? ""' in wrapper
    assert "...(appConfig?.experimental ?? {})" in wrapper
    assert "...(appConfig?.experimental?.serverActions ?? {})" in wrapper
    assert "allowedOrigins: [appsHostname]" in wrapper


def test_the_wrapper_refuses_to_build_an_app_with_no_address() -> None:
    """The error path this exists for: a build that reached `next build` without either value.

    The alternative is an image that compiles clean, deploys clean, serves at `/` and answers
    404 to every request the router forwards — a failure discovered by whoever opens the app.
    The throw runs when `next build` evaluates the config, so the ACR run fails and the message
    lands in the build log the platform already fetches."""
    wrapper = _read("next.config.ts").decode()
    guard = wrapper[wrapper.index("if (!basePath)") : wrapper.index("export default")]

    assert guard.count("throw new Error") == 2
    assert "BIAL_BASE_PATH" in guard
    assert "BIAL_APPS_HOSTNAME" in guard


def test_the_wrapper_traces_the_migrator_and_its_sql() -> None:
    """Neither is in the Next module graph, so tracing cannot find them. Without these the
    container starts, runs the migrator, and dies with MODULE_NOT_FOUND."""
    wrapper = _read("next.config.ts")
    assert b"outputFileTracingIncludes" in wrapper
    assert b"./drizzle/**" in wrapper
    assert b"./scripts/db-migrate.mjs" in wrapper


def test_the_wrapper_never_lists_node_modules_by_hand() -> None:
    """The regression this exists to prevent shipped an image that died on start with
    `Cannot find module 'xtend/mutable'`.

    `outputFileTracingIncludes` copies the files it is given and does NOT follow their
    dependencies, so naming `pg` and friends by hand promises a closure it cannot deliver.
    The hand-written list was missing THREE packages (`xtend`, `pgpass`, `split2`) and
    looked complete. `copy-runtime-deps.mjs` walks the real installed tree instead.

    This asserts the absence rather than the presence, because the failure mode is someone
    'helpfully' adding the one package a build complained about — which fixes that build and
    leaves the next lockfile resolution to find the next hole."""
    wrapper = _read("next.config.ts").decode()
    includes = wrapper[wrapper.index("outputFileTracingIncludes") :]
    assert "./node_modules/" not in includes


def test_the_dependency_closure_is_computed_not_listed() -> None:
    """The script walks `dependencies` transitively from its roots, so a new driver or a
    version bump needs no edit anywhere. `optionalDependencies` are followed when installed
    and skipped when not — `pg` ships one (`pg-cloudflare`) that is absent on Linux."""
    script = _read("copy-runtime-deps.mjs").decode()
    assert "optionalDependencies" in script
    assert "'pg'" in script and "'drizzle-orm'" in script
    # The Dockerfile has to actually run it, in the builder stage where node_modules exists.
    dockerfile = _read("Dockerfile").decode()
    assert "node copy-runtime-deps.mjs" in dockerfile
    assert dockerfile.index("next build") < dockerfile.index("node copy-runtime-deps.mjs")
