"""The Docker build context, packed from an extracted snapshot.

The tree under test is deliberately HOSTILE: it carries an agent-authored `Dockerfile`, a
`.env` full of credentials, a `.git`, a `node_modules`, and a `next.config.ts` that turns
off type checking. Every one of those is something a generated app has a plausible reason
to contain, and every one of them would compromise the build if it survived packing.

Determinism gets its own tests because it is silently load-bearing: it is what lets the
registry's layer cache turn a no-op redeploy into seconds, and a stray timestamp defeats it
without failing anything.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from src.services.deploy.context import (
    MAX_CONTEXT_BYTES,
    ContextTooLargeError,
    build_context,
)


def _unpack(packed: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(packed)), mode="r") as archive:
        out: dict[str, bytes] = {}
        for member in archive.getmembers():
            handle = archive.extractfile(member)
            out[member.name] = handle.read() if handle else b""
        return out


@pytest.fixture
def hostile_tree(tmp_path: Path) -> Path:
    """An extracted snapshot shaped like something the agent actually produces — including
    every file the platform must refuse to trust."""
    root = tmp_path / "tree"
    (root / "app").mkdir(parents=True)
    (root / "db").mkdir()
    (root / "drizzle").mkdir()
    (root / "scripts").mkdir()
    (root / "components" / "ui").mkdir(parents=True)

    (root / "package.json").write_text('{"name":"app","scripts":{"build":"next build"}}')
    (root / "package-lock.json").write_text('{"lockfileVersion":3}')
    (root / "app" / "page.tsx").write_text("export default function Page(){return null}")
    (root / "components" / "ui" / "button.tsx").write_text("export const Button = () => null")
    (root / "db" / "schema.ts").write_text("export const items = {}")
    (root / "drizzle" / "0000_init.sql").write_text("CREATE TABLE items ();")
    (root / "scripts" / "db-migrate.mjs").write_text("// the app's own lenient migrator")
    (root / "tsconfig.json").write_text("{}")

    # The app's config — with the exact key the platform must take back.
    (root / "next.config.ts").write_text(
        "export default { typescript: { ignoreBuildErrors: true }, images: { unoptimized: true } }"
    )

    # --- the hostile parts -------------------------------------------------------
    (root / "Dockerfile").write_text("FROM alpine\nRUN echo pwned\n")
    (root / "Dockerfile.prod").write_text("FROM alpine\n")
    (root / ".dockerignore").write_text("**\n")
    (root / ".env").write_text("BIAL_DATABASE_URL=postgresql://u:p@h/db\n")
    (root / ".env.local").write_text("SECRET=hunter2\n")
    (root / "app.bundle").write_bytes(b"# v2 git bundle\n")

    # Materialization artifacts that must never ship.
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".bial-extract-ok").write_text("")
    (root / "node_modules" / "next").mkdir(parents=True)
    (root / "node_modules" / "next" / "index.js").write_text("module.exports={}")
    (root / ".next" / "cache").mkdir(parents=True)
    (root / ".next" / "cache" / "big.bin").write_bytes(b"\0" * 1024)

    return root


# --- the four hardening layers ----------------------------------------------------


def test_the_agents_dockerfile_never_enters_the_context(hostile_tree: Path) -> None:
    """Layer 1: EXCLUSION, not overwrite. If this ever regressed to overwriting, a
    reordering refactor would silently hand the build to the agent."""
    files = _unpack(build_context(hostile_tree))

    assert b"pwned" not in files["Dockerfile"]
    assert b"npm ci --ignore-scripts" in files["Dockerfile"]
    # Not merely shadowed at the canonical path — gone entirely, at every spelling.
    assert "Dockerfile.prod" not in files


def test_the_platform_dockerfile_is_byte_identical_to_the_asset(hostile_tree: Path) -> None:
    from importlib import resources

    expected = (resources.files("src.services.deploy.assets") / "Dockerfile").read_bytes()
    assert _unpack(build_context(hostile_tree))["Dockerfile"] == expected


def test_secrets_and_vcs_and_dependencies_are_stripped(hostile_tree: Path) -> None:
    """A `.env` baked into an image layer survives every later deletion."""
    names = set(_unpack(build_context(hostile_tree)))

    assert not [n for n in names if n.startswith(".env")]
    assert not [n for n in names if n.startswith(".git")]
    assert not [n for n in names if n.startswith("node_modules/")]
    assert not [n for n in names if n.startswith(".next/")]
    assert "app.bundle" not in names
    assert ".bial-extract-ok" not in names


def test_the_citizens_source_survives_intact(hostile_tree: Path) -> None:
    """The exclusions must not be so eager that they eat the app."""
    files = _unpack(build_context(hostile_tree))

    assert files["app/page.tsx"] == b"export default function Page(){return null}"
    assert files["components/ui/button.tsx"] == b"export const Button = () => null"
    assert files["db/schema.ts"] == b"export const items = {}"
    # Generated migration SQL is a versioned artifact and MUST travel — the container
    # applies it at start.
    assert files["drizzle/0000_init.sql"] == b"CREATE TABLE items ();"
    assert "package.json" in files
    assert "package-lock.json" in files


# --- the Next config wrapper ------------------------------------------------------


def test_the_app_config_is_moved_aside_not_deleted(hostile_tree: Path) -> None:
    files = _unpack(build_context(hostile_tree))

    # The citizen's own settings survive under the aliased name the wrapper imports.
    assert b"unoptimized: true" in files["next.config.app.ts"]
    # And the platform now owns the entry point.
    assert b'output: "standalone"' in files["next.config.ts"]
    assert b"ignoreBuildErrors: false" in files["next.config.ts"]


def test_an_agent_disabling_type_checking_is_overridden(hostile_tree: Path) -> None:
    """`ignoreBuildErrors: true` is the escape hatch an agent WILL find when it cannot fix a
    type error. The wrapper spreads the app config first and then re-asserts the key, so the
    agent's value is inert."""
    files = _unpack(build_context(hostile_tree))
    wrapper = files["next.config.ts"].decode()

    assert wrapper.index("...appConfig") < wrapper.index("ignoreBuildErrors: false")


def test_an_app_with_no_config_still_builds(tmp_path: Path) -> None:
    """The wrapper imports `./next.config.app` unconditionally, so a stub has to exist or
    the build fails on a missing module."""
    root = tmp_path / "bare"
    root.mkdir()
    (root / "package.json").write_text("{}")

    files = _unpack(build_context(root))
    assert "next.config.app.ts" in files
    assert b"export default {}" in files["next.config.app.ts"]


def test_a_javascript_config_keeps_its_extension(tmp_path: Path) -> None:
    """`./next.config.app` resolves whichever extension is present — but only if the rename
    preserves it."""
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text("{}")
    (root / "next.config.mjs").write_text("export default { poweredByHeader: false }")

    files = _unpack(build_context(root))
    assert "next.config.app.mjs" in files
    assert b"poweredByHeader" in files["next.config.app.mjs"]


# --- the platform files the Dockerfile depends on ----------------------------------


def test_the_strict_migrator_replaces_the_apps_lenient_one(hostile_tree: Path) -> None:
    """The app's copy always exits 0. Shipping it to production would give a container that
    passes its probe while serving a half-migrated schema."""
    migrator = _unpack(build_context(hostile_tree))["scripts/db-migrate.mjs"]

    assert b"the app's own lenient migrator" not in migrator
    assert b"--strict" in migrator


def test_public_always_exists(hostile_tree: Path) -> None:
    """The golden template ships no `public/`, and the Dockerfile copies it
    unconditionally — without the placeholder EVERY build fails on a missing path."""
    assert "public/.gitkeep" in _unpack(build_context(hostile_tree))


def test_the_platform_supplies_its_own_dockerignore(hostile_tree: Path) -> None:
    dockerignore = _unpack(build_context(hostile_tree))[".dockerignore"]
    assert dockerignore != b"**\n"
    assert b".env" in dockerignore


# --- determinism ------------------------------------------------------------------


def test_the_same_tree_packs_byte_identically(hostile_tree: Path) -> None:
    """What makes the registry layer cache work — and what a stray mtime silently breaks."""
    assert build_context(hostile_tree) == build_context(hostile_tree)


def test_entries_are_sorted_and_timestamps_zeroed(hostile_tree: Path) -> None:
    raw = gzip.decompress(build_context(hostile_tree))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as archive:
        members = archive.getmembers()

    assert [m.name for m in members] == sorted(m.name for m in members)
    assert all(m.mtime == 0 for m in members)
    assert all(m.uid == 0 and m.gid == 0 for m in members)
    assert all(m.uname == "" and m.gname == "" for m in members)


def test_a_changed_file_changes_the_bytes(hostile_tree: Path) -> None:
    before = build_context(hostile_tree)
    (hostile_tree / "app" / "page.tsx").write_text("export default function Page(){return <div/>}")
    assert build_context(hostile_tree) != before


# --- limits -----------------------------------------------------------------------


def test_an_oversized_context_is_refused_not_uploaded(hostile_tree: Path, monkeypatch) -> None:
    """A context this big means dependencies reached the snapshot. Fail with a legible
    message rather than spending forty minutes uploading it."""
    monkeypatch.setattr("src.services.deploy.context.MAX_CONTEXT_BYTES", 16)
    with pytest.raises(ContextTooLargeError):
        build_context(hostile_tree)


def test_the_limit_is_sane() -> None:
    assert MAX_CONTEXT_BYTES == 50 * 1024 * 1024
