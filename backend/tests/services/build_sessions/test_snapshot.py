"""The snapshot write (no DB / Redis; fake storage + a scripted fake client)."""

from __future__ import annotations

import asyncio
import base64
import uuid

import pytest
from structlog.testing import capture_logs

from src.services.build_sessions.snapshot import (
    SNAPSHOT_STEP_TIMINGS_EVENT,
    WorkspaceHasNoRepositoryError,
    write_snapshot,
)
from src.services.sandbox.base import ExecResult, SandboxError, SandboxHandle
from src.services.storage import snapshot_key
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP_ID = uuid.uuid4()

#: Spelled out rather than imported from the module under test, so a changed constant moves the
#: script and the assertion apart instead of moving them together.
NO_REPO_EXIT = 64


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
    assert fake_storage.objects[snapshot_key(APP_ID)] == a_git_bundle()
    # Asserted on the script text — the dict-backed fake cannot run real git. The commit script
    # PROBES for a repository and never creates one: a root commit written at the end of a turn
    # holds the finished app, which makes the starter-page check compare the app against itself.
    assert "git init" not in scripts[0]
    assert scripts[0].startswith(f"[ -e .git ] || exit {NO_REPO_EXIT}; ")
    assert "git diff --cached --quiet || git commit" in scripts[0]


async def test_a_workspace_with_no_repository_raises_the_named_error(
    fake_storage: FakeStorage,
) -> None:
    """The probe's exit 64, end to end: a named error and nothing written to the store."""
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["sh", "-c"]:
            return ExecResult(stdout="", stderr="", exit=NO_REPO_EXIT)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(WorkspaceHasNoRepositoryError):
        await write_snapshot(client, _handle(), APP_ID)
    assert snapshot_key(APP_ID) not in fake_storage.objects


async def test_write_snapshot_raises_on_commit_failure(fake_storage: FakeStorage) -> None:
    """A commit that failed for ANY other reason — a full disk, a locked index — stays the
    generic snapshot failure. The named error is the discriminator's alone, so a caller that
    branches on it cannot be handed a disk-full container to quarantine and restore."""
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["sh", "-c"]:
            return ExecResult(stdout="", stderr="fatal: unable to write new index file", exit=128)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError, match="commit failed") as caught:
        await write_snapshot(client, _handle(), APP_ID)
    assert not isinstance(caught.value, WorkspaceHasNoRepositoryError)
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
    assert snapshot_key(APP_ID) not in fake_storage.objects


# --- the container-state parse --------------------------------------------------------


def test_porcelain_paths_survive_the_stripped_first_line() -> None:
    """`git status --porcelain` is `XY path`, but the block is stripped before it is split, so
    only the FIRST line loses the leading status space that later lines keep — a fixed
    `line[3:]` slice therefore eats one character of the first filename. Splitting on
    whitespace instead handles both the ragged first line and paths that contain spaces."""
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
    be True and the "unambiguous evidence of real work" its comment describes did not exist.

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
    """The bundle path each call read from. Derived from the READ rather than the write so the
    assertion holds against any bundle-creation spelling — what matters is which file each call
    believed was its own."""
    return [cmd[1].removeprefix("if=") for cmd in commands if cmd[:1] == ["dd"]]


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

    # Serialized, not interleaved: unserialized, both `git add -A && git commit` runs race on
    # `.git/index.lock` and the loser exits non-zero, which the user sees as a failed Save.
    # Asserted on ordering, not a concurrency counter: the second commit must come AFTER the
    # first call's `rm`, not beside it.
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


# --- per-step timing -------------------------------------------------------------------


async def test_a_save_that_dies_midway_still_reports_the_steps_that_ran(
    fake_storage: FakeStorage,
) -> None:
    """The whole reason the accumulator is passed IN and mutated rather than returned: once an
    exception has unwound past `_bundle_the_tree`, a return value is gone, and the steps that
    did run are exactly what says WHERE the save died. A slow save that then fails is the case
    this instrument exists for, so it cannot be the case it goes blind on."""
    client = FakeSandboxClient()

    def dies_at_the_bundle(cmd: list[str]) -> ExecResult:
        if cmd[:2] == ["git", "bundle"]:
            return ExecResult(stdout="", stderr="no space left on device", exit=1)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = dies_at_the_bundle
    with capture_logs() as logs:
        with pytest.raises(SandboxError):
            await write_snapshot(client, _handle(), APP_ID)

    timings = [log for log in logs if log["event"] == SNAPSHOT_STEP_TIMINGS_EVENT]
    assert len(timings) == 1, "a failed save reported no timings at all"
    timing = timings[0]
    # The steps that RAN carry a number — the lock was waited for and the commit did happen.
    assert isinstance(timing["lock_wait_ms"], int)
    assert isinstance(timing["commit_ms"], int)
    # The steps that never ran are absent rather than zero: a zero would read as instantaneous.
    assert timing["base64_ms"] is None
    assert timing["store_ms"] is None


async def test_write_snapshot_times_every_step_of_a_save(fake_storage: FakeStorage) -> None:
    """Save is synchronous in-request with no client-side timeout, so this event is the only
    record of which of the three candidates — the four execs, the per-app lock queue, or the blob
    write — a slow save actually lost its time to. One field per step, not one duration
    for the whole call, and not for just the execs `_bundle_the_tree` can see."""
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["base64"]:
            return ExecResult(stdout=base64.b64encode(a_git_bundle()).decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with capture_logs() as logs:
        await write_snapshot(client, _handle(), APP_ID)

    timings = [log for log in logs if log["event"] == SNAPSHOT_STEP_TIMINGS_EVENT]
    assert len(timings) == 1
    timing = timings[0]
    assert timing["app_id"] == str(APP_ID)
    for field in (
        "lock_wait_ms",
        "commit_ms",
        "bundle_ms",
        "base64_ms",
        "cleanup_ms",
        "store_ms",
    ):
        assert isinstance(timing[field], int)
        assert timing[field] >= 0


def _a_container_holding(bundle: bytes) -> tuple[FakeSandboxClient, list[list[str]]]:
    """A fake whose `dd` really cuts the byte range it names and whose `base64` reads back the
    last cut, so a multi-chunk read is reassembled from the pieces rather than handed whole."""
    client = FakeSandboxClient()
    commands: list[list[str]] = []
    cut: dict[str, bytes] = {}

    def handler(cmd: list[str]) -> ExecResult:
        commands.append(cmd)
        if cmd[:1] == ["dd"]:
            args = dict(arg.split("=", 1) for arg in cmd[1:])
            start = int(args["skip"]) * 1024 * 1024
            cut["piece"] = bundle[start : start + int(args["count"]) * 1024 * 1024]
            return ExecResult(stdout="", stderr="", exit=0)
        if cmd[:1] == ["base64"]:
            return ExecResult(stdout=base64.b64encode(cut["piece"]).decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client, commands


@pytest.mark.parametrize("extra", [0, 1, 5 * 1024 * 1024])
async def test_a_large_bundle_leaves_the_container_in_chunks_and_arrives_whole(
    fake_storage: FakeStorage, extra: int
) -> None:
    # Two full chunks plus a remainder of 0, 1 and 5 MiB: the exact multiple is the case where
    # the last full chunk is followed by an empty one.
    bundle = a_git_bundle()
    bundle += b"x" * (2 * 8 * 1024 * 1024 + extra - len(bundle))
    client, commands = _a_container_holding(bundle)

    await write_snapshot(client, _handle(), APP_ID)

    stored = await fake_storage.get(snapshot_key(APP_ID))
    assert stored == bundle
    reads = [cmd for cmd in commands if cmd[:1] == ["base64"]]
    assert len(reads) == 3, "two full chunks, then the short one that ends the read"


async def test_a_failed_cut_aborts_the_save_instead_of_storing_a_short_bundle(
    fake_storage: FakeStorage,
) -> None:
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["dd"]:
            return ExecResult(stdout="", stderr="no space", exit=1)
        if cmd[:1] == ["base64"]:
            return ExecResult(stdout=base64.b64encode(a_git_bundle()).decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    with pytest.raises(SandboxError, match="cut failed"):
        await write_snapshot(client, _handle(), APP_ID)
    assert snapshot_key(APP_ID) not in fake_storage.objects
