"""The model-free credential scan over an extracted snapshot (U6, R4a/P8).

Runs FIRST, before the model, and its hits go into the review's prompt as directed
evidence — a path, a pattern family and a line, structurally never a value
(`CredentialHit` has nowhere to carry one). It is fast and deterministic, which is why it
sits on the critical path by choice.

THE WALK MIRRORS THE READ TOOLS' JAIL, deliberately: the scan does not go through the
model's tools, so it applies `IGNORED_DIRS` and `IGNORED_FILES` itself — without the
file-level set the lockfile (the single largest file in a generated app) would sit in the
path of the one check that must not be starved (R22a/U1). Symlinks are never listed and
symlinked directories are never descended into, same as `ExtractedSnapshotWorkspace`:
the tree came from a bundle the citizen's AI drove, so a planted link must not lead the
scan out of the extraction.

TRUNCATION IS INCOMPLETENESS, NEVER CLEANLINESS. The detector's ceiling
(`SCAN_INPUT_MAX_CHARS`) is per file; a file whose text exceeds it is scanned only on its
prefix and reported `truncated`, and ONE truncated file marks the whole sweep
`incomplete`. The runner treats an incomplete sweep as leaving the credentials floor
unsatisfied — a truncated scan must never read as a clean no-hit, or the one
un-appealable answer silently degrades to an appeal nobody made. The read is bounded at
`SCAN_INPUT_MAX_CHARS * 4 + 4` bytes: UTF-8 spends at most four bytes per character, so
that many bytes always decode to strictly more characters than the ceiling when the file
holds more — the detector then flags the truncation itself, from the one place that owns
the ceiling.

Everything — the walk, the reads, the regex sweep — happens off the event loop (the
`snapshot_read.py` convention for filesystem work that is not "fast enough to inline").
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from src.core.redaction import (
    SCAN_INPUT_MAX_CHARS,
    detect_credentials_off_loop,
)
from src.services.agent.read_tools import IGNORED_DIRS, IGNORED_FILES
from src.services.classification.prompts import LocatedHit

# Enough bytes to always expose a text longer than the detector's character ceiling
# (4 bytes is UTF-8's widest encoding), while never slurping a pathological blob whole.
_READ_MAX_BYTES = SCAN_INPUT_MAX_CHARS * 4 + 4

# The extraction cache's own ready-marker — cache plumbing, not app truth. The same name
# the read tools' listing skips (kept in step by a test there, not an import: this module
# must not reach into `snapshot_read`'s privates).
_EXTRACT_READY_MARKER = ".bial-extract-ok"


@dataclass(frozen=True)
class CredentialSweep:
    """One whole-tree scan. `hits` are prompt-ready located findings in deterministic
    path order; `incomplete=True` means at least one file was truncated at the per-file
    ceiling — the sweep saw a prefix of the app, and the runner must not let the
    credentials floor rest on it."""

    hits: tuple[LocatedHit, ...]
    incomplete: bool


def _walkable_files(root: Path) -> list[str]:
    """Every scannable file, workspace-relative, sorted — the read-tools walk's twin:
    ignored/symlinked directories are pruned before descent, lockfiles and symlinks are
    skipped, and the extraction ready-marker is cache plumbing, never app truth."""
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in IGNORED_DIRS and not (here / name).is_symlink()
        )
        base = here.relative_to(root)
        for name in filenames:
            if name == _EXTRACT_READY_MARKER:
                continue
            if name in IGNORED_FILES:
                continue
            if (here / name).is_symlink():
                continue
            entries.append((base / name).as_posix())
    return sorted(entries)


def _read_bounded(path: Path) -> str:
    """One file's text, read no further than the bytes needed to prove the detector's
    per-file ceiling exceeded. Non-UTF-8 bytes are replaced, never fatal — a binary
    asset scans as noise rather than aborting the sweep."""
    with path.open("rb") as handle:
        raw = handle.read(_READ_MAX_BYTES)
    return raw.decode("utf-8", errors="replace")


async def scan_snapshot(root: Path) -> CredentialSweep:
    """Scan the extracted tree under `root` for credential-shaped content.

    Walk and reads ride `asyncio.to_thread`; each file's regex sweep goes through
    `detect_credentials_off_loop`. The hit order is deterministic (sorted paths, then
    the detector's own position order) so two runs over one tree build one prompt.
    """
    hits: list[LocatedHit] = []
    incomplete = False
    for rel_path in await asyncio.to_thread(_walkable_files, root):
        text = await asyncio.to_thread(_read_bounded, root / rel_path)
        scan = await detect_credentials_off_loop(text)
        incomplete = incomplete or scan.truncated
        hits.extend(LocatedHit(path=rel_path, hit=hit) for hit in scan.hits)
    return CredentialSweep(hits=tuple(hits), incomplete=incomplete)
