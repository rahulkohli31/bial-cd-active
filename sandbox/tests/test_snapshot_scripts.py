"""Offline unit tests for the snapshot/restore SCRIPTS — no container, no Azurite.

Runs the LITERAL `snapshot.sh` / `restore.sh` on temp dirs to pin the git-bundle round-trip
mechanics: the empty-diff guard (no lock-wedge), the refusal to snapshot a workspace with no
repository, git-bundle-verify (raw bundle, not base64 text), the .env / node_modules exclusion
(secrets never persist to Blob), and the overlay-onto-baked restore (node_modules survives).

The scripts are BAKED INTO the sandbox image — `Dockerfile.sandbox` installs them under
`/usr/local/bin` — and run inside a container via `/exec`; the control plane's own save path uses
the mirrored script in `backend/src/services/build_sessions/snapshot.py`. Here they run on the host
under `sh` + `git` + `base64` (GNU-compatible on Linux/CI and current macOS — the stdin form avoids
the BSD positional-arg incompatibility).

A hermetic git identity and default branch are supplied by the helpers below, so the tests do not
depend on host git config.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SNAPSHOT_SH = SCRIPTS / "snapshot.sh"
RESTORE_SH = SCRIPTS / "restore.sh"

#: The exit code `snapshot.sh` reserves for "this workspace has no repository", and nothing else.
#: `backend/src/services/build_sessions/snapshot.py` carries the same literal on the mirrored path.
NO_REPO_EXIT = 64

#: The subject `sandbox/client._INIT_REPO_SCRIPT` seeds the template's root commit under. Spelled
#: here because this harness cannot import the backend's constant.
BASELINE_SUBJECT = "bial: golden template baseline"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "BIAL Snapshot Bot",
    "GIT_AUTHOR_EMAIL": "snapshot-bot@bial.local",
    "GIT_COMMITTER_NAME": "BIAL Snapshot Bot",
    "GIT_COMMITTER_EMAIL": "snapshot-bot@bial.local",
}

# Skip cleanly where the toolchain the Linux image provides is not reproducible on the host.
_missing = [t for t in ("sh", "git", "base64") if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(bool(_missing), reason=f"missing host tools: {_missing}")


def _env() -> dict[str, str]:
    return {**os.environ, **GIT_ENV}


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *args], capture_output=True, text=True, env=_env(), timeout=60
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=_env(), check=True
    )


def _seed_repo(workspace: Path) -> None:
    """Mirror the provision-time seed (`sandbox/client._INIT_REPO_SCRIPT`).

    A container arrives as a repository carrying one baseline commit for the baked template, and
    `snapshot.sh` only ever adds to it — so every test below starts from that shape. `-b main`
    because `restore.sh` checks out `main` explicitly and the image bakes `init.defaultBranch`;
    `--allow-empty` because a template with nothing tracked yet still gets its birth certificate.
    """
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "--allow-empty", "-m", BASELINE_SUBJECT)


def snapshot(workspace: Path) -> bytes:
    """Run snapshot.sh; return the RAW git bundle (base64-decoded, as the client does)."""
    r = _run(SNAPSHOT_SH, str(workspace))
    assert r.returncode == 0, f"snapshot.sh failed: {r.stderr}"
    return base64.b64decode(r.stdout)


def restore(workspace: Path, raw_bundle: bytes) -> None:
    """Write the base64 (as the client's /files create would) and run restore.sh."""
    b64_path = workspace / ".bial-restore.b64"
    b64_path.write_text(base64.b64encode(raw_bundle).decode("ascii"), encoding="utf-8")
    r = _run(RESTORE_SH, str(workspace), str(b64_path))
    assert r.returncode == 0, f"restore.sh failed: {r.stderr}"


def _tracked(repo: Path) -> list[str]:
    r = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, env=_env()
    )
    return r.stdout.split()


def _commits(repo: Path) -> int:
    r = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True, env=_env()
    )
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


# --- happy round-trip: tree + history reproduced ----------------------------------------------
def test_snapshot_restore_round_trips_tree_and_history(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gitignore").write_text("node_modules\n.next\n.env\n.env*.local\n", encoding="utf-8")
    _seed_repo(src)
    (src / "feature.ts").write_text("export const x = 1\n", encoding="utf-8")
    raw = snapshot(src)

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "feature.ts").write_text("OLD BAKED VALUE\n", encoding="utf-8")  # baked; snapshot wins
    restore(dst, raw)

    assert (dst / "feature.ts").read_text(encoding="utf-8") == "export const x = 1\n"
    # The seeded baseline AND the snapshot commit on top of it survived the bundle round-trip.
    assert _commits(dst) == 2


# --- the empty-diff guard: a no-op re-snapshot must SUCCEED, or the lock wedges ----------
def test_empty_diff_re_snapshot_succeeds_and_stays_restorable(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _seed_repo(src)
    (src / "a.txt").write_text("hi\n", encoding="utf-8")
    snapshot(src)  # first snapshot commits
    raw2 = snapshot(src)  # NOTHING changed — must not error (empty-diff guard), still bundles HEAD

    dst = tmp_path / "dst"
    dst.mkdir()
    restore(dst, raw2)
    assert (dst / "a.txt").read_text(encoding="utf-8") == "hi\n"


# --- a workspace with no repository is REFUSED, never given a forged root ----------------------
def test_a_workspace_with_no_repository_is_refused(tmp_path: Path) -> None:
    """Exit 64 — the discriminator that says "no repository" and nothing else — with no repo
    created and no bundle emitted. A root commit minted here would hold the finished app, and the
    starter-page check would then compare the app against itself forever."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi\n", encoding="utf-8")

    r = _run(SNAPSHOT_SH, str(src))
    assert r.returncode == NO_REPO_EXIT, f"expected the no-repository exit: {r.stderr}"
    assert r.stdout == "", "a refused snapshot must emit no bundle"
    assert not (src / ".git").exists(), "the snapshot forged a repository"


def test_a_container_that_lost_its_repository_is_refused_not_re_rooted(tmp_path: Path) -> None:
    """The forged-root chain, end to end: a healthy workspace loses `.git`, and the next snapshot
    must terminate at the named failure rather than mint a new root holding the built app."""
    src = tmp_path / "src"
    src.mkdir()
    _seed_repo(src)
    (src / "app.ts").write_text("the built app\n", encoding="utf-8")
    snapshot(src)

    shutil.rmtree(src / ".git")
    r = _run(SNAPSHOT_SH, str(src))
    assert r.returncode == NO_REPO_EXIT, f"expected the no-repository exit: {r.stderr}"
    assert not (src / ".git").exists(), "the snapshot re-rooted a workspace holding the built app"


# --- consecutive snapshots against the seeded repo keep committing -----------------------------
def test_a_second_snapshot_commits_the_next_change(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _seed_repo(src)
    (src / "a.txt").write_text("hi\n", encoding="utf-8")
    snapshot(src)
    (src / "b.txt").write_text("more\n", encoding="utf-8")
    raw = snapshot(src)
    assert raw, "second snapshot produced no bundle"

    # Prove the second snapshot actually COMMITTED b.txt (not merely that it emitted a bundle):
    # restore into a fresh tree and check both the new file and the history round-tripped.
    dst = tmp_path / "dst"
    dst.mkdir()
    restore(dst, raw)
    assert (dst / "b.txt").read_text(encoding="utf-8") == "more\n"
    assert _commits(dst) == 3  # the seeded baseline, plus one commit per snapshot


# --- the stored object is a RAW git bundle (guards a raw-vs-base64 divergence) -----------------
def test_stored_object_is_a_raw_git_bundle(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _seed_repo(src)
    (src / "a.txt").write_text("hi\n", encoding="utf-8")
    raw = snapshot(src)

    bundle = tmp_path / "app.bundle"
    bundle.write_bytes(raw)
    verify = subprocess.run(
        ["git", "bundle", "verify", str(bundle)], capture_output=True, text=True, env=_env()
    )
    assert verify.returncode == 0, f"stored bytes are not a valid git bundle: {verify.stderr}"
    assert raw.startswith(b"# v"), "a git bundle starts with the `# vN git bundle` signature"


# --- secrets + baked deps never enter the persisted bundle ------------------------------------
def test_secrets_and_node_modules_excluded_from_bundle(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gitignore").write_text("node_modules\n.next\n.env\n.env*.local\n", encoding="utf-8")
    _seed_repo(src)
    (src / "app.ts").write_text("ok\n", encoding="utf-8")
    (src / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    (src / ".env.local").write_text("LOCAL=1\n", encoding="utf-8")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "dep.js").write_text("baked\n", encoding="utf-8")
    (src / ".next").mkdir()
    (src / ".next" / "trace").write_text("cache\n", encoding="utf-8")
    raw = snapshot(src)

    dst = tmp_path / "dst"
    dst.mkdir()
    restore(dst, raw)
    tracked = _tracked(dst)
    assert ".env" not in tracked and ".env.local" not in tracked, "a secret leaked into the bundle"
    assert not any(t.startswith("node_modules") for t in tracked)
    assert not any(t.startswith(".next") for t in tracked)
    assert "app.ts" in tracked


# --- restore OVERLAYS onto the baked tree: node_modules + baked-only files survive -------------
def test_restore_overlays_and_keeps_baked_node_modules(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    _seed_repo(src)
    (src / "src.ts").write_text("v2\n", encoding="utf-8")
    raw = snapshot(src)

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "node_modules").mkdir()
    (dst / "node_modules" / "dep.js").write_text("BAKED-KEEP-ME\n", encoding="utf-8")
    (dst / "only-in-baked.ts").write_text("keep\n", encoding="utf-8")
    (dst / "src.ts").write_text("stale-baked\n", encoding="utf-8")
    restore(dst, raw)

    assert (dst / "src.ts").read_text(encoding="utf-8") == "v2\n", "snapshot must win here"
    assert (dst / "node_modules" / "dep.js").read_text(encoding="utf-8") == "BAKED-KEEP-ME\n"
    assert (dst / "only-in-baked.ts").exists(), "overlay must keep baked-only untracked files"


# --- the transient base64 is cleaned up after restore -----------------------------------------
def test_restore_cleans_up_the_transient_base64(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _seed_repo(src)
    (src / "a.txt").write_text("hi\n", encoding="utf-8")
    raw = snapshot(src)

    dst = tmp_path / "dst"
    dst.mkdir()
    restore(dst, raw)
    assert not (dst / ".bial-restore.b64").exists(), "the /files-written base64 was not cleaned up"
