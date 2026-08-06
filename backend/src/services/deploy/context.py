"""Turn an extracted snapshot into the Docker build context the registry builds from.

The workspace this reads was written by an AI the citizen drove, so the tree is UNTRUSTED
input and the build configuration must not come from it. Four independent layers enforce
that, and they are listed here because each one closes a different hole:

1. **Exclusion, not overwrite.** An agent-authored `Dockerfile` never enters the archive at
   all. Overwriting instead would make correctness depend on ordering — one refactor that
   copies assets before walking the tree and the agent's file wins silently.
2. **Assets come from the backend image**, loaded through `importlib.resources` from a path
   no sandbox can reach. They are not read from the context and cannot be influenced by it.
3. **The Dockerfile is named explicitly** in the build request, so a file smuggled at some
   other path is never selected.
4. **The Dockerfile itself** runs `npm ci --ignore-scripts` and `npx next build` rather than
   `npm run build`, because `package.json` is agent-editable and is otherwise an
   arbitrary-code-execution vector inside the build agent.

The archive is DETERMINISTIC — sorted entries, zeroed mtimes, normalized ownership and
modes, and a gzip header with no timestamp. Two consequences: an unchanged tree produces
byte-identical bytes, so the registry's layer cache turns a no-op redeploy into seconds
instead of minutes; and the context can be hashed to answer "is this the same code we
already built?" without a second source of truth.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import tarfile
from importlib import resources
from pathlib import Path
from typing import Final

import structlog

_log = structlog.get_logger()

# --- what never enters the archive -------------------------------------------------

# Directory names pruned wherever they appear. `.git` and the extraction marker are
# artifacts of how the tree was materialized; the rest are build output and dependencies
# that the image builds for itself (and that would dwarf the upload).
_EXCLUDED_DIRS: Final = frozenset({".git", "node_modules", ".next", "out", "build"})

# Exact filenames excluded at any depth. The Dockerfile entries are the load-bearing ones:
# this is layer 1 of the four above.
_EXCLUDED_FILES: Final = frozenset(
    {
        ".bial-extract-ok",
        ".dockerignore",
        "app.bundle",
        "app.bundle.b64",
        ".bial-restore.b64",
    }
)

# Prefix/suffix rules for the rest. `.env*` matters most: a values file committed by the
# agent would otherwise be baked into an image layer, where deleting it later does not
# remove it.
_EXCLUDED_PREFIXES: Final = ("Dockerfile", ".env")
_EXCLUDED_SUFFIXES: Final = (".b64",)

# A context this large means `node_modules` or `.next` reached the snapshot — a platform
# bug, not a user error. Fail with a legible message instead of a 40-minute upload.
MAX_CONTEXT_BYTES: Final = 50 * 1024 * 1024

# --- the platform's own files ------------------------------------------------------

_ASSET_PACKAGE: Final = "src.services.deploy.assets"

# asset filename -> path inside the build context. The `gitkeep` mapping is not cosmetic:
# the golden template ships NO `public/` directory, and the Dockerfile copies it
# unconditionally, so without this every build fails on a missing path.
_ASSET_TARGETS: Final = {
    "Dockerfile": "Dockerfile",
    "dockerignore": ".dockerignore",
    "db-migrate.mjs": "scripts/db-migrate.mjs",
    "gitkeep": "public/.gitkeep",
}

# The app's own Next config is renamed to this stem and re-exported by the platform
# wrapper, so the citizen's settings survive while the platform owns the keys that decide
# whether the artifact is deployable.
_APP_CONFIG_STEM: Final = "next.config.app"
_CONFIG_CANDIDATES: Final = (
    "next.config.ts",
    "next.config.mts",
    "next.config.mjs",
    "next.config.js",
)
_WRAPPER_TARGET: Final = "next.config.ts"

# Used when the app has no Next config at all — the wrapper still has to import something.
_EMPTY_APP_CONFIG: Final = (
    "// The app shipped no Next config; the platform wrapper still needs a base to spread.\n"
    "export default {};\n"
)


class ContextTooLargeError(Exception):
    """The packed context exceeded `MAX_CONTEXT_BYTES` — dependencies or build output
    reached the snapshot. A platform problem, surfaced rather than uploaded."""


def _asset_bytes(name: str) -> bytes:
    return (resources.files(_ASSET_PACKAGE) / name).read_bytes()


def _is_excluded_file(name: str) -> bool:
    return (
        name in _EXCLUDED_FILES
        or name.startswith(_EXCLUDED_PREFIXES)
        or name.endswith(_EXCLUDED_SUFFIXES)
    )


def _collect(root: Path) -> dict[str, bytes]:
    """Walk the extracted tree into `{archive path: contents}`, applying the exclusions.

    Symlinks are skipped outright. `extract_snapshot` already clones with
    `core.symlinks=false`, so a committed symlink arrives as an inert regular file holding
    its target as text — but this stays defensive rather than relying on a property set two
    modules away."""
    collected: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if _is_excluded_file(path.name):
            continue
        collected[path.relative_to(root).as_posix()] = path.read_bytes()
    return collected


def _apply_platform_overlay(files: dict[str, bytes]) -> None:
    """Rename the app's Next config aside, then write every platform-owned file.

    Mutates in place. Runs AFTER `_collect`, so a platform target always wins over whatever
    the tree happened to contain at that path — while the agent's Dockerfile, which is what
    would actually be dangerous, was already dropped during collection."""
    app_config = next((name for name in _CONFIG_CANDIDATES if name in files), None)
    if app_config is not None:
        suffix = Path(app_config).suffix
        files[f"{_APP_CONFIG_STEM}{suffix}"] = files.pop(app_config)
    else:
        files[f"{_APP_CONFIG_STEM}.ts"] = _EMPTY_APP_CONFIG.encode()

    files[_WRAPPER_TARGET] = _asset_bytes("next.config.ts")
    for asset, target in _ASSET_TARGETS.items():
        files[target] = _asset_bytes(asset)


def _pack(files: dict[str, bytes]) -> bytes:
    """Deterministic `.tar.gz`. Every field that would otherwise vary run-to-run is pinned:
    entry order, mtime, uid/gid, owner names, and the gzip header's own timestamp."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            payload = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(payload))

    compressed = io.BytesIO()
    # `mtime=0`: gzip stamps the current time into its header by default, which would make
    # every archive of the same tree differ and defeat the layer cache.
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return compressed.getvalue()


def build_context(root: Path) -> bytes:
    """Pack an extracted snapshot into a deterministic build context. Synchronous — call
    through `build_context_async` from request paths."""
    files = _collect(root)
    _apply_platform_overlay(files)
    packed = _pack(files)
    if len(packed) > MAX_CONTEXT_BYTES:
        raise ContextTooLargeError(
            f"the build context is {len(packed)} bytes, over the {MAX_CONTEXT_BYTES} limit — "
            "dependencies or build output reached the snapshot"
        )
    _log.info("deploy_context_packed", files=len(files), bytes=len(packed))
    return packed


async def build_context_async(root: Path) -> bytes:
    """Off the event loop: this reads and compresses a whole source tree, and doing that
    inline stalls every other request in the process — including the keepalives that tell
    clients the server is still alive."""
    return await asyncio.to_thread(build_context, root)
