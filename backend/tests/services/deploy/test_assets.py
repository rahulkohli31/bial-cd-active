"""The platform-owned build assets.

The line-ending test is the reason this file exists: these assets ship inside the
Windows-built backend image and are copied verbatim into every generated app's build
context, so a CRLF checkout bakes `\\r` into the app Dockerfile and migrator and breaks
the build for every citizen at once — invisible on a Mac, and has taken this platform
down twice before. The root `.gitattributes` pins the directory to LF; this asserts the
pin held.

The content assertions are narrow on purpose: they pin the properties that are security
or correctness decisions rather than style, so removing one is a deliberate act with a
failing test attached.
"""

from __future__ import annotations

import re
from importlib import resources

import pytest

from src.services.orchestrator.errors import DRIFT_RECOVERED_NOTICE

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


def _instructions(name: str) -> str:
    """The asset with comment and blank lines removed. Needed because these files EXPLAIN
    their own hardening in prose, so a naive substring search matches the comment that says
    "not `npm run build`" and reports the opposite of the truth."""
    return "\n".join(
        line
        for line in _read(name).decode().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _shell_lines(name: str) -> list[str]:
    """The instructions with the shell's line continuations joined, so an instruction spread
    over several physical lines reads as the single command the shell actually runs."""
    return _instructions(name).replace("\\\n", " ").splitlines()


def _dependency_install() -> str:
    """The deps stage's install, as that one logical line."""
    return next(line for line in _shell_lines("Dockerfile") if line.startswith("RUN npm"))


_QUOTED = re.compile(r'"[^"]*"')


def _npm_installs(name: str) -> list[str]:
    """Every npm install the asset runs, in order, with quoted text blanked first. The
    dependency stage logs the arm it is about to take, so the words `npm install` also appear
    inside an echo argument — read raw, that sentence is indistinguishable from a command and
    is credited with whichever flags happen to follow it."""
    blanked = _QUOTED.sub('""', "\n".join(_shell_lines(name)))
    return [
        match.group().strip() for match in re.finditer(r"npm (?:ci|install)[^&|()\n]*", blanked)
    ]


def test_install_scripts_are_disabled_on_every_install_arm() -> None:
    """`package.json` is agent-editable, so a `postinstall` hook is arbitrary code execution
    inside the build agent. The fallback arm resolves straight from that file rather than from
    the vetted lock, which makes it the arm that can least afford to lose the flag."""
    installs = _npm_installs("Dockerfile")

    # Pins the extraction as well as the flag: a loop over an empty list asserts nothing.
    assert [install.split()[1] for install in installs] == ["ci", "install"]
    for install in installs:
        assert "--ignore-scripts" in install, f"install runs scripts: {install}"


def test_a_drifted_lockfile_falls_back_instead_of_failing_the_build() -> None:
    """An agent that edits a version by hand leaves the lock behind, and `npm ci` refuses a
    lock it cannot satisfy — with no fallback that app cannot be published at all. The fallback
    stays CONDITIONAL: `npm ci` honours the lock in every build where the lock is honourable."""
    first_arm, separator, fallback = _dependency_install().partition("||")

    assert first_arm.strip().startswith("RUN npm ci ")
    assert separator == "||", "an unconditional install would never honour the lockfile"
    assert "npm install" in fallback


def test_the_fallback_says_so_in_the_build_log_before_it_runs() -> None:
    """Both arms end in a successful install, from different versions, so the transcript is
    otherwise identical. Reading the log is the whole diagnosis for a dependency problem, so
    the log has to name the arm that ran."""
    fallback = _dependency_install().partition("||")[2]
    announcement, separator, command = fallback.partition("&& npm install")

    assert "echo " in announcement
    assert "npm install" in announcement, "the logged line does not name the arm it takes"
    assert separator == "&& npm install", "the announcement must precede the install"
    assert command.strip().startswith("--ignore-scripts")


def test_the_classifier_filters_the_exact_sentence_the_fallback_prints() -> None:
    """Two files, one string. The recovery notice is the first line a container build prints, so
    the classifier has to drop it or it titles every later failure of that build. Matching on a
    copy of the sentence would let this file and the Dockerfile drift apart silently, and the
    symptom would be a citizen told to fix a lockfile that installed cleanly."""
    announcement = _dependency_install().partition("||")[2].partition("&& npm install")[0]

    assert DRIFT_RECOVERED_NOTICE in announcement, (
        "the Dockerfile no longer prints the sentence the classifier filters on: " + announcement
    )


def test_only_a_drifted_lockfile_takes_the_fallback() -> None:
    """`npm ci` also refuses when a tarball does not match the hash the lock vetted. Falling
    back there would answer a supply-chain signal by fetching the package again — the one check
    that would catch a swapped tarball, downgraded to a suggestion.

    So the fallback is gated on npm's drift codes, and the guard must run BEFORE the install."""
    fallback = _dependency_install().partition("||")[2]
    guard = fallback.partition("&& npm install")[0]

    assert "grep" in guard, f"the fallback is ungated: {fallback}"
    for drift_only in ("EUSAGE", "Invalid:"):
        assert drift_only in guard, f"the gate does not name {drift_only}: {guard}"


def test_a_failure_that_is_not_drift_keeps_npms_own_diagnosis() -> None:
    """The first arm's output is held back so a RECOVERED drift does not litter the log of a
    build that later fails for an unrelated reason. Held back is not discarded: a build that is
    going to fail has to carry the reason it failed."""
    line = _dependency_install()

    assert "2>/tmp/npm-ci.err" in line, "the first arm's diagnosis is streamed, not captured"
    last_arm = line.rpartition("||")[2]
    assert "cat /tmp/npm-ci.err" in last_arm, f"the captured diagnosis is never replayed: {line}"
    assert "exit 1" in last_arm, "a build with no usable dependencies must still fail"


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
    """The two base-image defaults must not drift apart unnoticed: `config.py`'s value SHIPS
    (sent as the NODE_IMAGE build arg on every platform build), while the Dockerfile's own
    default only applies to a hand-run `docker build`, the go-live runbook path. Drift means
    the operator-built artifact and the platform-built one differ in base image with nothing
    reporting it.

    Read off `model_fields` rather than an instance — `DeployConfig` has ten required fields,
    so constructing one here would mean duplicating a fixture just to read a default."""
    from src.services.deploy.config import DeployConfig

    arg_line = next(
        line
        for line in _read("Dockerfile").decode().splitlines()
        if line.startswith("ARG NODE_IMAGE=")
    )
    arg_default = arg_line.split("=", 1)[1].split("#")[0].strip()

    assert arg_default == DeployConfig.model_fields["node_base_image"].default

    # The shipped base is pinned by digest, not by a tag that moves under us. A bare tag
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
    """`outputFileTracingIncludes` copies the files it is given and does NOT follow their
    dependencies, so naming packages by hand promises a closure it cannot deliver —
    `copy-runtime-deps.mjs` walks the real installed tree instead.

    Asserts the absence rather than the presence: the failure mode is someone adding back
    the one package a build complained about, leaving the next lockfile resolution to find
    the next hole."""
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
