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
