"""U8 — server-side snapshot extraction for Ask/Plan reads.

Round-trips a REAL git bundle (built in-test, the same `git bundle create <f> HEAD` shape
`build_sessions/snapshot.py` writes) through `extract_snapshot`: storage fetch → header
SHA parse → jailed `git clone` → immutable per-SHA cache dir. Absence is a typed
`NoAppYet`; corruption raises `BundleValidationError` (never folded into absence).
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.services.storage import snapshot_read
from src.services.storage.bundle import BundleValidationError
from src.services.storage.keys import snapshot_key
from src.services.storage.snapshot_read import (
    ExtractedSnapshot,
    NoAppYet,
    SnapshotExtractionError,
    extract_snapshot,
    sweep_extractions,
)
from tests.fakes import FakeStorage

_PAGE_CONTENT = "export default function VisitorLog() { return <main>visitors</main> }\n"


def _make_bundle(tmp_path: Path) -> tuple[bytes, str]:
    """A real HEAD-only bundle + its head SHA, mirroring snapshot.py's commit+bundle."""
    repo = tmp_path / "seed-repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "page.tsx").write_text(_PAGE_CONTENT)
    (repo / "package.json").write_text('{"name": "visitor-log"}\n')

    def _git(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    _git("init", "-q")
    _git("add", "-A")
    _git("commit", "-q", "-m", "bial-snapshot")
    head_sha = _git("rev-parse", "HEAD")
    _git("bundle", "create", str(repo / "app.bundle"), "HEAD")
    return (repo / "app.bundle").read_bytes(), head_sha


def _make_bundle_with_symlink(tmp_path: Path, target: str) -> bytes:
    """A HEAD-only bundle whose tree commits a symlink `app/loot` → `target` — the untrusted
    shape a citizen's AI could plant to escape the read jail."""
    repo = tmp_path / "evil-repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "page.tsx").write_text(_PAGE_CONTENT)
    (repo / "app" / "loot").symlink_to(target)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            capture_output=True,
            text=True,
            check=True,
        )

    _git("init", "-q")
    _git("add", "-A")
    _git("commit", "-q", "-m", "evil")
    _git("bundle", "create", str(repo / "app.bundle"), "HEAD")
    return (repo / "app.bundle").read_bytes()


@pytest.fixture
def app_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(snapshot_read, "get_storage", lambda: fake)
    return fake


async def test_extracts_the_bundle_to_a_sha_keyed_dir(
    tmp_path: Path, app_id: uuid.UUID, storage: FakeStorage
) -> None:
    data, head_sha = _make_bundle(tmp_path)
    storage.objects[snapshot_key(app_id)] = data

    extracted = await extract_snapshot(app_id, cache_root=tmp_path / "cache")

    assert isinstance(extracted, ExtractedSnapshot)
    assert extracted.head_sha == head_sha
    assert extracted.root == tmp_path / "cache" / app_id.hex / head_sha
    assert (extracted.root / "app" / "page.tsx").read_text() == _PAGE_CONTENT
    assert (extracted.root / "package.json").is_file()


async def test_committed_symlink_extracts_as_an_inert_file_not_a_link(
    tmp_path: Path, app_id: uuid.UUID, storage: FakeStorage
) -> None:
    # Layer 1 of the P0 jail-escape fix: `core.symlinks=false` at clone time means a symlink
    # committed into the untrusted bundle checks out as a REGULAR file holding its target text,
    # so no read command can follow it out of the extraction dir.
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("TOP SECRET")
    storage.objects[snapshot_key(app_id)] = _make_bundle_with_symlink(tmp_path, str(secret))

    extracted = await extract_snapshot(app_id, cache_root=tmp_path / "cache")
    assert isinstance(extracted, ExtractedSnapshot)
    loot = extracted.root / "app" / "loot"
    assert not loot.is_symlink()  # materialized inert, not a followable link
    assert loot.read_text() == str(secret)  # holds the TARGET PATH as text, never the secret
    assert "TOP SECRET" not in loot.read_text()


async def test_second_call_hits_the_cache_without_cloning(
    tmp_path: Path,
    app_id: uuid.UUID,
    storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, _ = _make_bundle(tmp_path)
    storage.objects[snapshot_key(app_id)] = data
    first = await extract_snapshot(app_id, cache_root=tmp_path / "cache")
    assert isinstance(first, ExtractedSnapshot)

    clones = {"n": 0}
    real_spawn = snapshot_read._spawn_no_shell

    async def counting_spawn(*args: Any, **kwargs: Any) -> Any:
        clones["n"] += 1
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(snapshot_read, "_spawn_no_shell", counting_spawn)
    second = await extract_snapshot(app_id, cache_root=tmp_path / "cache")

    assert isinstance(second, ExtractedSnapshot)
    assert second.root == first.root
    assert clones["n"] == 0  # the immutable SHA dir served the read


async def test_no_snapshot_is_a_typed_no_app_yet(app_id: uuid.UUID, storage: FakeStorage) -> None:
    outcome = await extract_snapshot(app_id, cache_root=Path("/nonexistent-root-never-touched"))
    assert isinstance(outcome, NoAppYet)
    assert outcome.app_id == app_id


async def test_a_corrupt_stored_bundle_raises_never_reads_as_absent(
    tmp_path: Path, app_id: uuid.UUID, storage: FakeStorage
) -> None:
    storage.objects[snapshot_key(app_id)] = b"definitely not a git bundle"
    with pytest.raises(BundleValidationError):
        await extract_snapshot(app_id, cache_root=tmp_path / "cache")


async def test_a_missing_git_binary_raises_the_error_the_callers_actually_catch(
    tmp_path: Path, app_id: uuid.UUID, storage: FakeStorage
) -> None:
    """A git-less image must fail as `SnapshotExtractionError`, not as `FileNotFoundError`.

    This is the shape of a REAL production defect, not a hypothetical: `backend/Dockerfile`
    builds on `python:3.14-slim`, which ships no git at all (no `/usr/bin/git`, no
    `/usr/lib/git-core`) — while `_git_env` pins the subprocess PATH to
    `/usr/local/bin:/usr/bin:/bin`.

    The failure mode is nastier than a non-zero exit. `create_subprocess_exec` resolves the
    binary against the PASSED env's PATH and raises `FileNotFoundError` BEFORE any process
    exists, so it never reaches the `returncode != 0` branch that raises
    `SnapshotExtractionError` — which means it sails straight past
    `deploy/service.py:229`'s `except SnapshotExtractionError`, the one handler written to
    turn this into a clean citizen-facing message. Publish dies on an unhandled exception
    instead.

    So this test pins the CONTRACT rather than the symptom: however git goes missing, the
    callers' own error type is what comes out.
    """
    data, _ = _make_bundle(tmp_path)
    storage.objects[snapshot_key(app_id)] = data
    # An empty PATH dir reproduces the git-less image exactly — same resolution failure,
    # same exception, without needing a container.
    empty_bin = tmp_path / "no-git-here"
    empty_bin.mkdir()
    monkey_env = {
        "PATH": str(empty_bin),
        "HOME": str(tmp_path),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snapshot_read, "_git_env", lambda _scratch: monkey_env)
        with pytest.raises(SnapshotExtractionError) as caught:
            await extract_snapshot(app_id, cache_root=tmp_path / "cache")

    # The message has to name the missing binary. An operator reading a 500 needs to land on
    # "the image has no git", not on a bare ENOENT that reads like a corrupt bundle.
    assert "git" in str(caught.value).lower()


async def test_a_torn_extraction_is_cleared_and_redone(
    tmp_path: Path, app_id: uuid.UUID, storage: FakeStorage
) -> None:
    data, head_sha = _make_bundle(tmp_path)
    storage.objects[snapshot_key(app_id)] = data
    torn = tmp_path / "cache" / app_id.hex / head_sha
    torn.mkdir(parents=True)
    (torn / "half-written.txt").write_text("crashed mid-extract")  # no ready-marker

    extracted = await extract_snapshot(app_id, cache_root=tmp_path / "cache")

    assert isinstance(extracted, ExtractedSnapshot)
    assert (extracted.root / "app" / "page.tsx").is_file()
    assert not (extracted.root / "half-written.txt").exists()


async def test_sweep_removes_only_aged_extractions(
    tmp_path: Path, app_id: uuid.UUID, storage: FakeStorage
) -> None:
    import os

    data, head_sha = _make_bundle(tmp_path)
    storage.objects[snapshot_key(app_id)] = data
    extracted = await extract_snapshot(app_id, cache_root=tmp_path / "cache")
    assert isinstance(extracted, ExtractedSnapshot)

    assert sweep_extractions(cache_root=tmp_path / "cache", max_age_s=3600) == 0
    marker = extracted.root / ".bial-extract-ok"
    os.utime(marker, (1, 1))  # age the marker far past any TTL
    assert sweep_extractions(cache_root=tmp_path / "cache", max_age_s=3600) == 1
    assert not extracted.root.exists()
