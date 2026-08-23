"""U5 — the C4 snapshot write (no DB / Redis; fake storage + a scripted fake client)."""

from __future__ import annotations

import asyncio
import base64
import uuid

import pytest

from src.services.build_sessions.snapshot import write_snapshot
from src.services.sandbox.base import ExecResult, SandboxError, SandboxHandle
from src.services.storage import snapshot_key
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP_ID = uuid.uuid4()


def _handle() -> SandboxHandle:
    return SandboxHandle(
        fqdn="sbx-x.example",
        token="tok",
        app_name="sbx-x",
        preview_url="https://sbx-x.example/",
        ready=False,
    )


async def test_write_snapshot_bundles_and_puts_to_blob(fake_storage: FakeStorage) -> None:
    client = FakeSandboxClient()
    scripts: list[str] = []

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["sh", "-c"]:
            scripts.append(cmd[2])
        if cmd[:1] == ["base64"]:
            return ExecResult(stdout=base64.b64encode(a_git_bundle()).decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    await write_snapshot(client, _handle(), APP_ID)
    # The base64'd bundle round-trips to Blob at the C4 key (byte-stable).
    assert fake_storage.objects[snapshot_key(APP_ID)] == a_git_bundle()
    # The commit script survives a GIT-LESS workspace (the baked image has no .git): it inits
    # idempotently and guards the nothing-to-commit case (mirrors sandbox/scripts/snapshot.sh).
    # Asserted on the script text — the dict-backed fake cannot run real git.
    assert scripts[0].startswith("git init -q")
    assert "git diff --cached --quiet || git commit" in scripts[0]


async def test_write_snapshot_raises_on_commit_failure(fake_storage: FakeStorage) -> None:
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["sh", "-c"] and "git init" in cmd[2]:
            return ExecResult(stdout="", stderr="fatal: not a work tree", exit=128)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError, match="commit failed"):
        await write_snapshot(client, _handle(), APP_ID)
    assert snapshot_key(APP_ID) not in fake_storage.objects


async def test_write_snapshot_bundle_failure_never_uploads_a_stale_bundle(
    fake_storage: FakeStorage,
) -> None:
    # The bundle step fails but a STALE app.bundle from an earlier snapshot is still on disk:
    # the exit-code check must abort before the base64 read ever ships it as "latest".
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:3] == ["git", "bundle", "create"]:
            return ExecResult(stdout="", stderr="fatal: refusing to create empty bundle", exit=128)
        if cmd[:1] == ["base64"]:  # the stale on-disk bundle would read back fine
            return ExecResult(stdout=base64.b64encode(b"STALE").decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError, match="bundle failed"):
        await write_snapshot(client, _handle(), APP_ID)
    assert snapshot_key(APP_ID) not in fake_storage.objects


async def test_write_snapshot_raises_on_bundle_read_failure(fake_storage: FakeStorage) -> None:
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["base64"]:
            return ExecResult(stdout="", stderr="bundle failed", exit=1)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError):
        await write_snapshot(client, _handle(), APP_ID)
    # A failed bundle read leaves no dangling blob.
    assert snapshot_key(APP_ID) not in fake_storage.objects


# --- the container-state parse (#83 follow-up) ---------------------------------------


def test_porcelain_paths_survive_the_stripped_first_line() -> None:
    """The parse that cost a live debugging session.

    `git status --porcelain` is `XY path` — two status columns, a space, then the path. The
    block gets stripped before it is split, so the FIRST line has already lost its leading
    status space while later lines keep theirs. Slicing a fixed `line[3:]` therefore ate one
    character of the first filename — `next-env.d.ts` became `ext-env.d.ts`, matched nothing,
    and a pristine workspace kept reading as "has unsaved changes".

    Splitting on whitespace once is right for the ragged first line AND for paths with spaces,
    which a fixed offset is not."""
    from src.services.build_sessions.integrity import ContainerState, parse_state

    st = parse_state(
        "abc123\n@@\nM next-env.d.ts\n M tsconfig.json\n?? notes my file.txt\n@@\n1\n"
    )
    assert isinstance(st, ContainerState)
    assert st.changed_paths == ("next-env.d.ts", "tsconfig.json", "notes my file.txt")
    assert st.commits == 1
    assert st.uncommitted is True


def test_a_rename_reports_the_destination_not_the_source() -> None:
    from src.services.build_sessions.integrity import parse_state

    st = parse_state("abc\n@@\nR  old/name.ts -> new/name.ts\n@@\n2\n")
    assert st.changed_paths == ("new/name.ts",)


def test_a_listing_at_the_cap_reads_as_truncated() -> None:
    """`porcelain_truncated` is the backstop for a tree too dirty to enumerate, and it shipped
    DEAD: the shell capped at `head -c 200` while the parse tested `>= 400`, so it could never
    be True and the "unambiguous evidence of real work" its comment describes did not exist
    (#83 review, finding 6).

    Both now derive from `PORCELAIN_CAP_BYTES`, so this fails if they ever drift apart again.
    `>=` rather than `>` is deliberate: output landing exactly on the cap is indistinguishable
    from output cut there, and "assume truncated" is the arm that REFUSES a reclaim."""
    from src.services.build_sessions.integrity import PORCELAIN_CAP_BYTES, parse_state

    at_cap = "M " + "a" * (PORCELAIN_CAP_BYTES - 2)
    assert len(at_cap) == PORCELAIN_CAP_BYTES
    assert parse_state(f"abc\n@@\n{at_cap}\n@@\n1\n").porcelain_truncated is True

    under = "M " + "a" * 10
    assert parse_state(f"abc\n@@\n{under}\n@@\n1\n").porcelain_truncated is False
    # A clean tree is not "truncated" either — the flag must not fire on emptiness.
    assert parse_state("abc\n@@\n\n@@\n1\n").porcelain_truncated is False


def test_truncation_is_measured_in_bytes_not_characters() -> None:
    # `head -c` counts bytes. A non-ASCII filename makes the character count read short, so a
    # character-based test would under-detect exactly the very dirty trees the flag exists for.
    from src.services.build_sessions.integrity import PORCELAIN_CAP_BYTES, parse_state

    multibyte = "M " + "é" * (PORCELAIN_CAP_BYTES // 2)  # 2 bytes each → lands on the cap
    assert len(multibyte) < PORCELAIN_CAP_BYTES  # ...but reads SHORT as characters
    assert len(multibyte.encode()) >= PORCELAIN_CAP_BYTES
    assert parse_state(f"abc\n@@\n{multibyte}\n@@\n1\n").porcelain_truncated is True


class _RealisticContainer(FakeSandboxClient):
    """A fake whose `exec` YIELDS, so two concurrent `write_snapshot`s actually interleave.

    The stock fake's `exec` has no await inside it, so awaiting it never reaches the event loop
    and `gather` would run one call fully before the other — hiding every concurrency bug these
    tests exist to catch. A real exec is an HTTP round trip to the supervisor; `sleep(0)` is the
    cheapest honest stand-in for that suspension point.
    """

    def __init__(self, *, read_fails: bool = False) -> None:
        super().__init__()
        self.commands: list[list[str]] = []
        self.read_fails = read_fails

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int = 900,
    ) -> ExecResult:
        await asyncio.sleep(0)
        self.commands.append(cmd)
        if cmd[:1] == ["base64"]:
            if self.read_fails:
                return ExecResult(stdout="", stderr="cannot read", exit=1)
            return ExecResult(stdout=base64.b64encode(a_git_bundle()).decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)


def _positions(commands: list[list[str]], head: list[str]) -> list[int]:
    return [i for i, cmd in enumerate(commands) if cmd[: len(head)] == head]


def _read_paths(commands: list[list[str]]) -> list[str]:
    """The bundle path each call handed to `base64`. Derived from the READ rather than the write
    so the assertion holds against any bundle-creation spelling — what matters is which file each
    call believed was its own."""
    return [cmd[1] for cmd in commands if cmd[:1] == ["base64"]]


async def test_concurrent_snapshots_of_one_app_never_share_a_bundle_path(
    fake_storage: FakeStorage,
) -> None:
    client = _RealisticContainer()

    await asyncio.gather(
        write_snapshot(client, _handle(), APP_ID),
        write_snapshot(client, _handle(), APP_ID),
    )

    # Two distinct on-disk paths. Sharing one meant the first call's `rm -f` deleted the file the
    # second had not read yet, and `base64` could read a path `git bundle create` was still
    # writing — either way a SHORT READ was uploaded over the only copy of the user's work.
    paths = _read_paths(client.commands)
    assert len(paths) == 2
    assert paths[0] != paths[1]
    # And each call cleaned up its OWN file, never the other's.
    removed = [cmd[2] for cmd in client.commands if cmd[:2] == ["rm", "-f"]]
    assert sorted(removed) == sorted(paths)


async def test_concurrent_snapshots_of_one_app_run_one_at_a_time(
    fake_storage: FakeStorage,
) -> None:
    client = _RealisticContainer()

    await asyncio.gather(
        write_snapshot(client, _handle(), APP_ID),
        write_snapshot(client, _handle(), APP_ID),
    )

    # Serialized, not interleaved: the first call finishes its whole sequence — including its
    # cleanup — before the second one commits. Unserialized, both `git add -A && git commit` runs
    # race on `.git/index.lock` and the loser exits non-zero, which the user sees as a failed
    # Save. Asserted on ordering rather than on a concurrency counter so it reads as the property
    # it protects: the second commit must come AFTER the first call's `rm`, not beside it.
    commits = [
        i
        for i, cmd in enumerate(client.commands)
        if cmd[:2] == ["sh", "-c"] and "git commit" in cmd[2]
    ]
    removals = _positions(client.commands, ["rm", "-f"])
    assert len(commits) == 2
    assert commits[1] > removals[0]


async def test_a_failed_snapshot_leaves_no_bundle_for_the_next_one_to_commit(
    fake_storage: FakeStorage,
) -> None:
    client = _RealisticContainer(read_fails=True)

    with pytest.raises(SandboxError):
        await write_snapshot(client, _handle(), APP_ID)

    # The cleanup runs on the FAILURE path too. A bundle left behind is multi-MB of binary in the
    # worktree that the NEXT snapshot's `git add -A` would commit into the user's own tree.
    paths = _read_paths(client.commands)
    removed = [cmd[2] for cmd in client.commands if cmd[:2] == ["rm", "-f"]]
    assert removed == paths
