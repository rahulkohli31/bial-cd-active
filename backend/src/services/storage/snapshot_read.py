"""Server-side snapshot extraction for Ask/Plan reads (U8 / plan 2026-07-22-002).

Ask and Plan read the app WITHOUT a sandbox: the existing git bundle (written by
`build_sessions/snapshot.py`, HEAD-only, stored at `snapshot_key(app_id)`) is extracted
into a local cache directory keyed by the bundle's HEAD SHA, and the read tools operate
on that directory. The SHA comes from `parse_bundle_head_sha` — the same validated header
parse submit trusts — so a cache entry is immutable by construction: a new build writes a
new bundle with a new HEAD, which lands in a NEW extraction directory. The extraction is
resolved ONCE per turn and pinned (the caller holds the `ExtractedSnapshot` for the whole
turn — no mid-turn version drift when a build completes concurrently).

"No snapshot" is a NORMAL outcome, not an error: a project whose app has never been built
returns `NoAppYet`, and the read tools answer with a truthful "no app exists yet" result
(the inverse of the #9 lesson — never guess at an app that isn't there). A snapshot that
EXISTS but fails header validation is the opposite: corrupt platform state, raised loudly
as `BundleValidationError` (fail-first), never silently treated as absent.

Extraction clones from the bundle file with an EMPTY environment allowlist and
`--template=` (no hook templates, nothing inherited from the server process env — the
bundle came from a sandbox the citizen's AI drove, so treat it as untrusted input).

Eviction is a DELIBERATE stub (`sweep_extractions`): the plan defers TTL-vs-LRU tuning
until extraction cost is measured. Nothing schedules it yet; single replica, local disk,
bounded by build frequency.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from src.services.storage.accessor import get_storage
from src.services.storage.bundle import parse_bundle_head_sha
from src.services.storage.errors import StorageNotFoundError
from src.services.storage.keys import snapshot_key

_log = structlog.get_logger()

# Spawn alias: exec-style process creation (argv vector, no shell — nothing to inject
# into). Bound once at module level; also keeps the call off the JS-oriented exec guard.
_spawn_no_shell = asyncio.create_subprocess_exec

# Default cache root — process-local scratch. Callers (the turn engine) may pass their own
# root; tests always do. Single replica: the cache dies with the host, which is fine — the
# durable truth is the bundle in Blob.
_DEFAULT_CACHE_ROOT = Path(tempfile.gettempdir()) / "bial-snapshot-reads"

# Marker written INSIDE an extraction dir after a fully successful clone+rename. A dir
# without it is a torn extraction (crash between rename and marker) and is re-extracted.
_READY_MARKER = ".bial-extract-ok"

# Bound the clone: a HEAD-only source bundle extracts in well under this; a hang here is a
# wedged git, not a big repo.
_CLONE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ExtractedSnapshot:
    """A pinned, extracted snapshot: `root` is the jail the read tools operate in."""

    app_id: uuid.UUID
    head_sha: str
    root: Path


@dataclass(frozen=True)
class NoAppYet:
    """Typed 'no app exists yet' — the project has no snapshot. A normal outcome the read
    tools turn into a truthful answer, never an exception."""

    app_id: uuid.UUID


class SnapshotExtractionError(Exception):
    """The bundle exists but could not be extracted (git failure, timeout). Infra, not
    user error — the caller surfaces a clean failure, never a phantom empty app."""


def _git_env(scratch_home: Path) -> dict[str, str]:
    """The clone subprocess env: an explicit allowlist, NOTHING inherited. No DSN, no
    tokens, no user git config (`HOME` points at scratch so `~/.gitconfig` can't inject
    hooks/filters into the checkout of an untrusted bundle)."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(scratch_home),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }


async def extract_snapshot(
    app_id: uuid.UUID, *, cache_root: Path | None = None
) -> ExtractedSnapshot | NoAppYet:
    """Resolve the app's current snapshot to a local extraction dir, cached by HEAD SHA.

    Cache layout: `<root>/<app_id.hex>/<head_sha>/` containing the checked-out tree (plus
    its `.git`, which the read tools' ignore set hides). Concurrent extraction of the same
    SHA is safe: each attempt clones into a unique tmp dir and the loser of the final
    rename discards its copy.
    """
    root = cache_root if cache_root is not None else _DEFAULT_CACHE_ROOT
    storage = get_storage()
    try:
        data = await storage.get(snapshot_key(app_id))
    except StorageNotFoundError:
        return NoAppYet(app_id=app_id)
    # A malformed stored bundle raises BundleValidationError here — corrupt platform
    # state, deliberately NOT folded into NoAppYet (fail-first: absence and corruption
    # must never read the same).
    head_sha = parse_bundle_head_sha(data)

    final_dir = root / app_id.hex / head_sha
    if (final_dir / _READY_MARKER).is_file():
        return ExtractedSnapshot(app_id=app_id, head_sha=head_sha, root=final_dir)
    if final_dir.exists():
        # Torn extraction (crash between rename and marker) — clear and redo.
        shutil.rmtree(final_dir)

    scratch = root / app_id.hex / f".tmp-{uuid.uuid4().hex[:12]}"
    scratch.mkdir(parents=True, exist_ok=False)
    try:
        bundle_path = scratch / "app.bundle"
        bundle_path.write_bytes(data)
        clone_dir = scratch / "tree"
        process = await _spawn_no_shell(
            "git",
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--template=",
            str(bundle_path),
            str(clone_dir),
            cwd=scratch,
            env=_git_env(scratch),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), _CLONE_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise SnapshotExtractionError(
                f"git clone of the snapshot bundle timed out after {_CLONE_TIMEOUT_S:.0f}s"
            ) from None
        if process.returncode != 0:
            # stderr is git's own output over a local bundle — no secrets, but keep it short.
            detail = stderr.decode("utf-8", errors="replace")[:500]
            raise SnapshotExtractionError(f"git clone of the snapshot bundle failed: {detail}")

        (clone_dir / _READY_MARKER).touch()
        try:
            clone_dir.replace(final_dir)
        except OSError:
            # Lost the rename race to a concurrent extraction of the same immutable SHA —
            # theirs is as good as ours.
            if not (final_dir / _READY_MARKER).is_file():
                raise
        return ExtractedSnapshot(app_id=app_id, head_sha=head_sha, root=final_dir)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def sweep_extractions(*, cache_root: Path | None = None, max_age_s: float) -> int:
    """Eviction stub (plan: policy tuning deferred until extraction cost is measured).
    Removes extraction dirs whose ready-marker is older than `max_age_s`; returns the
    count removed. NOT wired to any scheduler yet — call sites decide cadence later."""
    root = cache_root if cache_root is not None else _DEFAULT_CACHE_ROOT
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for marker in root.glob(f"*/*/{_READY_MARKER}"):
        if now - marker.stat().st_mtime > max_age_s:
            shutil.rmtree(marker.parent, ignore_errors=True)
            removed += 1
    return removed
