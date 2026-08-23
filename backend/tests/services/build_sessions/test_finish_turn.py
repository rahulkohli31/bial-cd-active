"""U3 — the turn-end recovery write, and the guard that will not overwrite good work with bad.

R8, AE4. The old write was gated on `touched` alone — "a mutating tool ran", not "the tree
changed" — and the `put` was unconditional. A container that reverted midway through a turn had
its empty tree stamped in as the newest copy of the user's work, over a perfectly good bundle,
with nothing recorded anywhere. That is one half of 2026-08-18; the swallowed failure is the
reason nobody could prove it afterwards.

THE TEST THIS FILE EXISTS FOR is `test_a_dirty_tree_at_unchanged_head_still_writes_a_recovery_
copy`, and it is named exactly that on purpose. It is the standing contract across a plan
boundary: the companion plan deletes agent-side commits, at which point "HEAD unchanged + dirty
tree" becomes the normal shape of EVERY building turn. A skip-on-HEAD-unchanged implementation
would then silently discard every turn's recovery copy — data loss, plus (ASM24) containers
nothing would ever reclaim, both reading green to every health check. If this test ever goes red,
that regression has landed.
"""

from __future__ import annotations

import uuid

import pytest

from src.services.build_sessions import snapshot as snapshot_module
from src.services.build_sessions.alarms import RECOVERY_WRITE_DID_NOT_LAND_EVENT
from src.services.build_sessions.snapshot import (
    Destination,
    RecoveryOutcome,
    write_recovery_copy,
    write_snapshot,
)
from src.services.sandbox import SandboxError
from src.services.sandbox.base import ExecResult, SandboxHandle
from src.services.storage import divert_prefix, quarantine_key, recovery_key, snapshot_key
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP = uuid.UUID("0198f2c0-2222-7000-8000-0000000d1ff7")
RECORDED = "a" * 40
MOVED_ON = "b" * 40
UNRELATED = "c" * 40
TAKEN_AT = __import__("datetime").datetime(
    2026, 8, 18, 11, 30, 0, 123456, tzinfo=__import__("datetime").UTC
)

_HANDLE = SandboxHandle(
    fqdn="app-x.westeurope.azurecontainerapps.io",
    token="t",
    app_name="app-x",
    preview_url="https://app-x.westeurope.azurecontainerapps.io/",
    ready=True,
)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(snapshot_module, "get_storage", lambda: fake)
    return fake


def _container(
    *, bundles_to: str, head: str, ancestry: str = "0 0", dirty: str = ""
) -> FakeSandboxClient:
    """A container that commits, bundles to `bundles_to`, and answers the ancestry question.

    `bundles_to` and `head` are separate on purpose: `_COMMIT_SCRIPT` runs as step ONE inside the
    write, so a dirty tree becomes a NEW commit before anything is bundled. That gap between "what
    HEAD was" and "what got bundled" is the whole subject of this file."""
    client = FakeSandboxClient()
    bundle = __import__("base64").b64encode(a_git_bundle(bundles_to)).decode()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            answered = ancestry if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{head}@@{dirty}@@4@@{answered}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


async def _seed_recovery(store: FakeStorage, sha: str | None = RECORDED) -> None:
    await store.put(
        recovery_key(APP),
        a_git_bundle(sha or RECORDED),
        metadata={"head_sha": sha} if sha else {},
    )


async def _head_sha_in_slot(store: FakeStorage, key: str) -> str | None:
    meta = await store.head(key)
    return (meta.metadata or {}).get("head_sha") if meta else None


# =============================================================================
# The no-op skip, and the regression it must never become
# =============================================================================


async def test_a_dirty_tree_at_unchanged_head_still_writes_a_recovery_copy(
    store: FakeStorage,
) -> None:
    """★★ THE NAMED CONTRACT. Do not rename this test, and do not let it go red.

    HEAD is exactly where the recovery copy is, and the worktree is dirty. The commit step inside
    the write turns that dirt into a commit, so the tree that actually gets bundled is NOT the one
    on record — and the write must proceed.

    A "skip when HEAD has not moved" implementation reads the sha BEFORE the commit step and
    discards this turn's work. Today the agent's own commits mask the difference; once they go
    away, this is every building turn.

    Mutation check: decide the skip on the container's pre-commit HEAD instead of on the bundled
    sha, and this goes red."""
    await _seed_recovery(store)
    client = _container(bundles_to=MOVED_ON, head=RECORDED, dirty="M  app/page.tsx")

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.WRITTEN
    assert await _head_sha_in_slot(store, recovery_key(APP)) == MOVED_ON


async def test_a_clean_tree_at_the_same_head_writes_nothing_at_all(store: FakeStorage) -> None:
    """The normal read-only-ish turn. Nothing changed, so nothing is uploaded — and crucially,
    nothing is alarmed either: an alarm that fires on every quiet turn is an alarm nobody reads."""
    await _seed_recovery(store)
    client = _container(bundles_to=RECORDED, head=RECORDED)

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.SKIPPED
    assert [k for k in store.objects if k.startswith(divert_prefix(APP))] == []


async def test_a_truncated_porcelain_still_reaches_a_durable_copy(store: FakeStorage) -> None:
    """A tree too dirty to enumerate is unambiguous evidence of real work. It commits, so it
    bundles to a new sha, so it writes — no special case needed, which is the point."""
    await _seed_recovery(store)
    client = _container(bundles_to=MOVED_ON, head=RECORDED, dirty="M  " + "a" * 400)

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.WRITTEN


async def test_the_first_copy_for_an_app_just_proceeds(store: FakeStorage) -> None:
    """Nothing to protect yet, so there is nothing to refuse."""
    client = _container(bundles_to=MOVED_ON, head=MOVED_ON, ancestry="1 128")

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.WRITTEN
    assert await _head_sha_in_slot(store, recovery_key(APP)) == MOVED_ON


async def test_a_copy_with_no_head_sha_is_treated_as_no_copy_at_all(store: FakeStorage) -> None:
    """ "No claim" (`head_sha_from_metadata`) is not "a claim we must protect". A bundle written
    before the stamp existed cannot be compared against anything, and refusing forever on it would
    freeze that app's durability permanently."""
    await _seed_recovery(store, sha=None)
    client = _container(bundles_to=MOVED_ON, head=MOVED_ON)

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.WRITTEN


# =============================================================================
# The refusal
# =============================================================================


async def test_a_reverted_container_cannot_overwrite_the_copy_it_reverted_from(
    store: FakeStorage,
) -> None:
    """★ AE4. The container reverted midway through the turn, so the tree in hand is not built on
    the copy on record. The existing bundle must be byte-identical afterwards."""
    await _seed_recovery(store)
    before = await store.get(recovery_key(APP))
    client = _container(bundles_to=UNRELATED, head=UNRELATED, ancestry="0 1")

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.DIVERTED
    assert await store.get(recovery_key(APP)) == before
    assert await _head_sha_in_slot(store, recovery_key(APP)) == RECORDED


async def test_the_refused_tree_is_kept_not_dropped(store: FakeStorage) -> None:
    """In a FALSE refusal the diverted bundle is the newest copy of somebody's afternoon. Writing
    it somewhere an operator can reach is what keeps a conservative guard from being a destructive
    one."""
    await _seed_recovery(store)
    client = _container(bundles_to=UNRELATED, head=UNRELATED, ancestry="0 1")

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.diverted_to is not None
    assert await _head_sha_in_slot(store, written.diverted_to) == UNRELATED


async def test_a_second_refusal_does_not_destroy_the_first_ones_evidence(
    store: FakeStorage,
) -> None:
    """★ A shared overwrite-latest divert key would make the alarm point at a tree the NEXT
    failure had already replaced."""
    import datetime as _dt

    await _seed_recovery(store)
    first = await write_recovery_copy(
        _container(bundles_to=UNRELATED, head=UNRELATED, ancestry="0 1"),
        _HANDLE,
        APP,
        taken_at=TAKEN_AT,
    )
    second = await write_recovery_copy(
        _container(bundles_to=MOVED_ON, head=MOVED_ON, ancestry="0 1"),
        _HANDLE,
        APP,
        taken_at=TAKEN_AT + _dt.timedelta(seconds=1),
    )

    assert first.diverted_to != second.diverted_to
    assert await _head_sha_in_slot(store, str(first.diverted_to)) == UNRELATED
    assert await _head_sha_in_slot(store, str(second.diverted_to)) == MOVED_ON


@pytest.mark.parametrize(
    ("ancestry", "why"),
    [
        ("0 1", "the lineage moved"),
        ("1 128", "the recorded tree is not in this repository"),
        ("", "the container did not answer the ancestry question"),
        ("nonsense", "the container answered in a shape we do not understand"),
    ],
)
async def test_every_answer_but_descendant_refuses(
    store: FakeStorage, ancestry: str, why: str
) -> None:
    """Only a POSITIVE "this tree was built on that one" earns the promotion. Every other answer
    — including both ways of not knowing — leaves the existing copy alone."""
    await _seed_recovery(store)
    client = _container(bundles_to=UNRELATED, head=UNRELATED, ancestry=ancestry)

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.DIVERTED, why
    assert await _head_sha_in_slot(store, recovery_key(APP)) == RECORDED


async def test_a_recorded_sha_that_is_not_a_sha_never_reaches_the_shell(
    store: FakeStorage,
) -> None:
    """The recorded head comes back from blob metadata and the ancestry probe composes it into an
    `sh -c` string. Seven to forty lowercase hex characters, or the question is not asked."""
    await _seed_recovery(store, sha="a" * 39 + "; rm -rf /")
    seen: list[list[str]] = []
    client = _container(bundles_to=MOVED_ON, head=MOVED_ON)
    inner = client.exec_handler
    assert inner is not None

    def record(cmd: list[str]) -> ExecResult:
        seen.append(cmd)
        return inner(cmd)

    client.exec_handler = record

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.DIVERTED
    assert all("rm -rf" not in part for cmd in seen for part in cmd)


async def test_a_container_that_stops_answering_refuses_rather_than_guessing(
    store: FakeStorage,
) -> None:
    await _seed_recovery(store)
    client = _container(bundles_to=MOVED_ON, head=MOVED_ON)
    inner = client.exec_handler
    assert inner is not None

    def flaky(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "merge-base" in cmd[-1]:
            raise SandboxError("the supervisor stopped answering")
        return inner(cmd)

    client.exec_handler = flaky

    written = await write_recovery_copy(client, _HANDLE, APP, taken_at=TAKEN_AT)

    assert written.outcome is RecoveryOutcome.DIVERTED
    assert await _head_sha_in_slot(store, recovery_key(APP)) == RECORDED


# =============================================================================
# The alarm
# =============================================================================


async def test_a_refusal_alarms_and_a_quiet_turn_does_not(
    store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ The event exists to settle "did this turn's work reach a durable copy". It has to fire on
    the refusal and stay silent on the ordinary turn, or it answers nothing."""
    raised: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        snapshot_module._log,
        "error",
        lambda event, **kw: raised.append((event, kw)),
    )
    await _seed_recovery(store)

    await write_recovery_copy(
        _container(bundles_to=RECORDED, head=RECORDED), _HANDLE, APP, taken_at=TAKEN_AT
    )
    assert raised == [], "a quiet turn must not alarm"

    await write_recovery_copy(
        _container(bundles_to=UNRELATED, head=UNRELATED, ancestry="0 1"),
        _HANDLE,
        APP,
        taken_at=TAKEN_AT,
    )

    assert [event for event, _ in raised] == [RECOVERY_WRITE_DID_NOT_LAND_EVENT]
    fields = raised[0][1]
    assert fields["reason"] == "diverted"
    assert fields["recorded_head"] == RECORDED
    assert fields["bundled_head"] == UNRELATED


def test_the_alarm_name_is_spelled_once_in_the_whole_codebase() -> None:
    """★ An alert cannot be written against a string that exists in two spellings, and a second
    spelling is invisible until the day it is the only one firing.

    Read off DISK rather than out of git, so an uncommitted second spelling fails here rather
    than in review."""
    import pathlib

    # tests/services/build_sessions/ -> backend/
    root = pathlib.Path(__file__).resolve().parents[3]
    literal = f'"{RECOVERY_WRITE_DID_NOT_LAND_EVENT}"'
    spellings = [
        str(path.relative_to(root))
        for folder in ("src", "tests")
        for path in sorted((root / folder).rglob("*.py"))
        if literal in path.read_text()
    ]

    assert spellings == ["src/services/build_sessions/alarms.py"], spellings


# =============================================================================
# Destination — the four places a bundle can go
# =============================================================================


async def test_write_snapshot_defaults_to_the_users_saved_bundle(store: FakeStorage) -> None:
    """The one key a platform-initiated write must never touch is also the one every caller of
    this function means, so it is the default rather than a flag."""
    client = _container(bundles_to=MOVED_ON, head=MOVED_ON)

    await write_snapshot(client, _HANDLE, APP)

    assert await _head_sha_in_slot(store, snapshot_key(APP)) == MOVED_ON
    assert await store.head(recovery_key(APP)) is None


async def test_a_quarantine_write_lands_where_an_operator_can_find_it(
    store: FakeStorage,
) -> None:
    client = _container(bundles_to=MOVED_ON, head=MOVED_ON)

    await write_snapshot(client, _HANDLE, APP, destination=Destination.quarantine(APP, TAKEN_AT))

    assert await _head_sha_in_slot(store, quarantine_key(APP, TAKEN_AT)) == MOVED_ON


def test_the_stamped_keys_sort_chronologically() -> None:
    """An operator listing these wants them in the order they happened, and lexical order is the
    only order a blob listing offers."""
    import datetime as _dt

    earlier = quarantine_key(APP, TAKEN_AT)
    later = quarantine_key(APP, TAKEN_AT + _dt.timedelta(microseconds=1))

    assert earlier < later


def test_a_naive_datetime_is_refused_rather_than_read_as_utc() -> None:
    """Two objects stamped from different offsets would sort by wall clock rather than by when
    they happened — silently reordering an operator's evidence."""
    import datetime as _dt

    from src.services.storage.errors import StorageError

    with pytest.raises(StorageError):
        quarantine_key(APP, _dt.datetime(2026, 8, 18, 11, 30))  # noqa: DTZ001
