"""U19 / R25 / ASM5 — what the Save indicator reports once the agent stops committing.

THE CHANGE THIS FILE GUARDS IS A CHANGE OF WEIGHT, NOT OF SHAPE, which is exactly why it needs
tests of its own. `_save_state_of` has always answered "uncommitted tree → dirty" before it
compared any commits. While the Write prompt told the agent to commit each coherent slice, that
arm was a backstop for a model that skipped one. U19 deleted the instruction — the platform
commits the tree itself, once, inside the turn-boundary bundle (`snapshot._COMMIT_SCRIPT`) — so
for the whole of every building turn, and forever afterwards if the turn died before its
finalizer ran, the user's new work exists ONLY as an uncommitted worktree at an unmoved HEAD.

That arm is now the entire answer for that shape. Delete it and the ladder falls through to
"HEAD == savedHead", the Save button disappears, and the citizen is told "all changes saved" over
a tree nothing has saved anywhere. Silent, and in the direction that loses work — the same
failure the turn-end recovery write has its own named contract for, one surface over.

Tri-state is pinned here too: `null` is "nobody could check", never "clean".
"""

from __future__ import annotations

import uuid

import pytest

from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.manager import SessionManager
from src.services.sandbox import SandboxHandle
from src.services.sandbox.base import ExecResult
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP = uuid.UUID("0198f2c0-3333-7000-8000-00000000d1a7")
BASELINE = "a" * 40
SAVED_AT = "a" * 40
MOVED_ON = "b" * 40

_HANDLE = SandboxHandle(
    fqdn="app-x.westeurope.azurecontainerapps.io",
    token="t",
    app_name="app-x",
    preview_url="https://app-x.westeurope.azurecontainerapps.io/",
    ready=True,
)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    """`_saved_head` and `_recovery_written_at` both reach the store through `manager`'s own
    `get_storage`, so binding it there covers the whole ladder."""
    fake = FakeStorage()
    monkeypatch.setattr(manager_module, "get_storage", lambda: fake)
    return fake


def _container(*, head: str | None, porcelain: str = "", commits: int = 4) -> FakeSandboxClient:
    """A container answering `integrity.state_script`'s four `@@`-separated fields.

    `porcelain` non-empty is the whole subject of this file: files written and not committed."""
    client = FakeSandboxClient()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            return ExecResult(stdout=f"{head or ''}@@{porcelain}@@{commits}@@", stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


async def _saved(store: FakeStorage, sha: str) -> None:
    from src.services.storage import snapshot_key

    await store.put(snapshot_key(APP), a_git_bundle(sha))


# =============================================================================
# The shape U19 makes normal: files written, nothing committed
# =============================================================================


async def test_a_turn_that_wrote_files_and_committed_nothing_reports_unsaved_work(
    store: FakeStorage,
) -> None:
    """★★ THE CONTRACT. HEAD is exactly where the user's last Save is, and the tree is dirty.

    Before U19 this shape barely occurred: the agent committed as it worked, so its files had
    become commits and the bottom of the ladder saw HEAD move. Now it is every building turn.

    Mutation check: drop the `state.uncommitted` arm from `_save_state_of` and this goes red with
    `dirty is False` — the platform telling the citizen their unsaved work is already saved."""
    await _saved(store, SAVED_AT)
    client = _container(head=SAVED_AT, porcelain=" M app/page.tsx\n?? app/new/page.tsx")

    state = await SessionManager()._save_state_of(client, _HANDLE, APP)

    assert state.dirty is True


async def test_unsaved_work_is_reported_on_top_of_the_saved_version_not_instead_of_it(
    store: FakeStorage,
) -> None:
    """The same shape, read for what it says ABOUT the save. "You have unsaved changes on top of
    the version you saved" and "nothing here is saved" are different sentences, and only the
    first one is true — a report that dropped `savedHead` would describe a bigger loss than the
    one that happened, on the one screen a worried user goes to."""
    await _saved(store, SAVED_AT)
    client = _container(head=SAVED_AT, porcelain=" M app/page.tsx")

    state = await SessionManager()._save_state_of(client, _HANDLE, APP)

    assert state.dirty is True
    assert state.saved_head == SAVED_AT  # the user's save is still on record
    assert state.container_head == SAVED_AT  # and so is where the container actually sits
    assert state.app_id == APP


async def test_the_platforms_own_turn_boundary_commit_settles_the_indicator(
    store: FakeStorage,
) -> None:
    """The other half of the new normal, and the reason the uncommitted arm is not the whole
    story. Once the turn-boundary bundle has committed, the tree is clean and HEAD has moved —
    so the ladder falls through to the commit comparison, which is still what answers "is what
    is in this container the thing I saved?"."""
    await _saved(store, SAVED_AT)
    clean_and_moved_on = _container(head=MOVED_ON, porcelain="")

    state = await SessionManager()._save_state_of(clean_and_moved_on, _HANDLE, APP)

    assert state.dirty is True
    assert state.container_head == MOVED_ON
    assert state.saved_head == SAVED_AT


async def test_a_clean_tree_at_the_saved_commit_is_genuinely_clean(store: FakeStorage) -> None:
    """The liveness assertion beside the three above: `dirty` must still be capable of being
    False, or "always dirty" would pass every test in this file and put a Save button on a
    project with nothing to save, permanently."""
    await _saved(store, SAVED_AT)
    client = _container(head=SAVED_AT, porcelain="")

    state = await SessionManager()._save_state_of(client, _HANDLE, APP)

    assert state.dirty is False


# =============================================================================
# Tri-state: null is "no claim", never "clean"
# =============================================================================


async def test_a_container_that_will_not_answer_is_unknown_not_clean(store: FakeStorage) -> None:
    """`dirty=None` survives U19 unchanged, and it is NOT False. A probe that could not run tells
    us nothing about the tree — and a UI that renders unknown as clean tells the user their work
    is safe when nobody checked."""
    await _saved(store, SAVED_AT)
    mute = FakeSandboxClient()
    mute.exec_handler = lambda cmd: ExecResult(stdout="", stderr="boom", exit=1)

    state = await SessionManager()._save_state_of(mute, _HANDLE, APP)

    assert state.dirty is None
    assert state.dirty is not False  # the distinction the whole tri-state exists for
    assert state.container_head is None


async def test_a_project_with_no_app_yet_is_unknown_rather_than_saved() -> None:
    """The other `null` producer, asserted through the public entry point's own no-app arm: there
    is no workspace to compare, so there is no claim to make."""
    from src.services.build_sessions.manager import SaveState

    nothing_to_compare = SaveState(app_id=None, dirty=None, container_head=None, saved_head=None)

    assert nothing_to_compare.dirty is None
    assert nothing_to_compare.dirty is not False


# =============================================================================
# A never-saved project still offers the Save button
# =============================================================================


async def test_work_no_one_has_ever_saved_is_dirty_not_unknown(store: FakeStorage) -> None:
    """Nothing in the store at all, and a container holding real commits. Reading that as unknown
    hid the Save button on exactly the projects that most need it (#83)."""
    client = _container(head=MOVED_ON, porcelain="")

    state = await SessionManager()._save_state_of(client, _HANDLE, APP)

    assert state.dirty is True
    assert state.saved_head is None
