"""U2 — the pre-turn integrity gate: say so, quarantine, restore, then confirm.

R1/R2/R3/R5/R6. Until this unit the attach path re-attached on a supervisor `/health` 200 and
never looked at the tree. On 2026-08-18 that is exactly what happened: a container that had
factory-reset to its baked image answered every check the platform had, and the agent built on
the wiped workspace in front of a client.

THE ORDER OF THE ASSERTIONS IN THIS FILE IS THE ORDER OF THE RISK.

* `test_the_sentence_arrives_before_the_restore_runs` is the unit's shape. Putting an app back
  takes tens of seconds during which the screen would otherwise say nothing at all.
* `test_a_check_that_times_out_touches_nothing` is the one that must never regress. `REVERTED` is
  the only state that may destroy anything, and the whole safety argument collapses if an
  unanswerable check can reach a teardown.
* `test_a_seeded_bundle_alone_does_not_make_a_container_look_reverted` is the inertness guard. It
  is what proves the fakes' default is right — and, more usefully, that it STAYS right.
"""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.user import User
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions import snapshot as snapshot_module
from src.services.build_sessions.alarms import WORKSPACE_LOST_WHILE_IDLE_EVENT
from src.services.build_sessions.integrity import (
    WorkspaceState,
    reset_integrity_streaks_for_tests,
)
from src.services.build_sessions.manager import (
    RecoveryNews,
    SessionManager,
    WorkspaceUnreadableError,
    reset_idle_checks_for_tests,
)
from src.services.build_sessions.snapshot import reset_divert_streaks_for_tests
from src.services.sandbox import SandboxError
from src.services.sandbox.base import ExecResult, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.storage import quarantine_prefix, recovery_key, snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

RECORDED = "a" * 40


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


@pytest.fixture(autouse=True)
def _no_leaked_streaks() -> None:
    reset_integrity_streaks_for_tests()
    reset_divert_streaks_for_tests()
    reset_idle_checks_for_tests()


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


def _answers(head: str | None, *, ancestry: str = "0 0", porcelain: str = "", commits: int = 4):
    """A container whose workspace-state probe answers exactly this."""

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            answered = ancestry if "merge-base" in cmd[-1] else ""
            return ExecResult(
                stdout=f"{head or ''}@@{porcelain}@@{commits}@@{answered}", stderr="", exit=0
            )
        if cmd[0] == "base64":
            import base64 as _b64

            return ExecResult(
                stdout=_b64.b64encode(a_git_bundle("e" * 40)).decode(), exit=0, stderr=""
            )
        return ExecResult(stdout="", stderr="", exit=0)

    return handler


class _Heard:
    """Records what the gate announced, and WHEN — the ordering is half the unit."""

    def __init__(self) -> None:
        self.news: list[RecoveryNews] = []

    async def __call__(self, news: RecoveryNews) -> None:
        self.news.append(news)


async def _attached(
    db: AsyncSession, manager: SessionManager, user: User, project_id: uuid.UUID
) -> tuple[FakeSandboxClient, uuid.UUID]:
    """Get to the ATTACH arm — the only one where the tree is older than the request.

    The other two arms have just built the workspace from a bundle or a template, so there is
    nothing for them to have lost. Reaching this one takes a real provision first."""
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=False)
    client.attach_handle = session.handle
    return client, session.app_id


async def _seed_recovery(store: FakeStorage, app_id: uuid.UUID, sha: str = RECORDED) -> None:
    await store.put(recovery_key(app_id), a_git_bundle(sha), metadata={"head_sha": sha})


async def _seed_saved(store: FakeStorage, app_id: uuid.UUID, sha: str = RECORDED) -> None:
    await store.put(snapshot_key(app_id), a_git_bundle(sha), metadata={"head_sha": sha})


# =============================================================================
# The ordinary case, and the guard that keeps it ordinary
# =============================================================================


async def test_an_intact_workspace_is_attached_exactly_as_before(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """No message, no quarantine, no restore, and the same container."""
    user, project_id = await _mk(db_session, "u2a@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers("b" * 40)
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == []
    assert session.news is None
    assert session.restored is False
    assert client.restored == []
    assert [k for k in fake_storage.objects if k.startswith(quarantine_prefix(app_id))] == []


async def test_a_seeded_bundle_alone_does_not_make_a_container_look_reverted(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE INERTNESS GUARD, and it is worth more than it looks.

    `FakeSandboxClient.exec` used to answer every unrecognised command with an empty stdout at
    exit 0, which `parse_state` reads as `head=None` — and under U1 a repo-less container with a
    recovery bundle present is a CONFIRMED REVERSION. So without a default arm for the state
    probe, every pre-existing turn test that happened to seed a bundle would silently have
    exercised the quarantine-and-restore branch while asserting something else entirely.

    This test does NOT script `exec`. That is the whole point: it fails the day the default stops
    being the ordinary case.

    Mutation check: delete the `_STATE_MARKER` arm from `tests/fakes.py` and this goes red."""
    user, project_id = await _mk(db_session, "u2b@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == []
    assert session.restored is False


async def test_a_brand_new_project_attaches_with_nothing_to_say(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Never-built by U1's four conditions: one commit, a clean tree, no bundles anywhere."""
    user, project_id = await _mk(db_session, "u2c@rvaiglobal.com")
    manager = SessionManager()
    client, _ = await _attached(db_session, manager, user, project_id)
    client.exec_handler = _answers("seed", commits=1, ancestry="")
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == []
    assert session.restored is False


# =============================================================================
# Confirmed loss
# =============================================================================


async def test_the_sentence_arrives_before_the_restore_runs(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ AE1, and the ordering IS the unit.

    Putting an app back is a full bundle of the reverted tree plus a complete restore — tens of
    seconds during which the screen would otherwise say nothing at all, which is indistinguishable
    from the product having hung.

    Mutation check: move the announce below the restore and this goes red."""
    user, project_id = await _mk(db_session, "u2d@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")
    order: list[str] = []
    heard = _Heard()

    async def note(news: RecoveryNews) -> None:
        order.append(f"said:{news.value}")
        await heard(news)

    real_restore = client.restore_from_snapshot

    async def watched_restore(
        user_id: str, app_name: str, *, app_env: dict[str, str], source_key: str | None = None
    ) -> SandboxHandle:
        order.append("restored")
        return await real_restore(user_id, app_name, app_env=app_env, source_key=source_key)

    monkeypatch.setattr(client, "restore_from_snapshot", watched_restore)

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=note
    )

    assert order == ["said:restoring", "restored"]
    assert session.news is RecoveryNews.RESTORING
    assert session.restored is True


async def test_the_reverted_tree_is_parked_before_it_is_replaced(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ A container with NO REPOSITORY tells us nothing about its working directory: `git status
    --porcelain` returns empty whether the folder holds the bare template or somebody's finished
    app with `.git` deleted out from under it. So it is quarantined, because in that second case
    the files on disk are the only surviving copy of their work."""
    user, project_id = await _mk(db_session, "u2e@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")

    await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    parked = [k for k in fake_storage.objects if k.startswith(quarantine_prefix(app_id))]
    assert len(parked) == 1


async def test_a_tree_we_can_see_is_the_template_is_not_bundled_just_to_throw_it_away(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ The repository was re-seeded rather than deleted, so we can SEE the tree: one commit,
    clean, on a lineage the copy is not below. Bundling that would be a full `git bundle` + base64
    + upload on the slowest path in the system to preserve the starter template.

    Mutation check: drop the `provably_bare` guard and this goes red."""
    user, project_id = await _mk(db_session, "u2f@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers("reseeded", commits=1, ancestry="0 1")

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert [k for k in fake_storage.objects if k.startswith(quarantine_prefix(app_id))] == []
    assert session.restored is True  # ...and the restore still happened


async def test_a_quarantine_that_fails_stops_the_restore(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ NEVER DESTROY THE ONLY COPY TO MAKE A RECOVERY SUCCEED. If the tree could not be set
    aside, the container keeps whatever it has."""
    user, project_id = await _mk(db_session, "u2g@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    inner = _answers(None, commits=0, porcelain="M  app/page.tsx", ancestry="")

    def refuse_to_bundle(cmd: list[str]) -> ExecResult:
        if cmd[:1] == ["git"] and "bundle" in cmd:
            raise SandboxError("the container will not bundle")
        return inner(cmd)

    client.exec_handler = refuse_to_bundle
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert client.restored == []
    assert session.restored is False
    assert heard.news == [RecoveryNews.RESTORING, RecoveryNews.UNRECOVERABLE]


async def test_a_restore_that_fails_still_tells_the_citizen(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alternative is a preview that quietly shows a template beside a chat that says
    nothing."""
    user, project_id = await _mk(db_session, "u2h@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")

    async def refuse(
        user_id: str, app_name: str, *, app_env: dict[str, str], source_key: str | None = None
    ) -> SandboxHandle:
        raise SandboxError("the restore did not complete")

    monkeypatch.setattr(client, "restore_from_snapshot", refuse)
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == [RecoveryNews.RESTORING, RecoveryNews.UNRECOVERABLE]
    assert session.restored is False
    assert session.news is RecoveryNews.UNRECOVERABLE


async def test_confirmed_loss_with_nothing_to_restore_says_so_and_restores_nothing(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ AE3. Neither a recovery copy nor a saved bundle. The one thing that must not happen is
    presenting the empty template as their app."""
    user, project_id = await _mk(db_session, "u2i@rvaiglobal.com")
    manager = SessionManager()
    client, _ = await _attached(db_session, manager, user, project_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == [RecoveryNews.UNRECOVERABLE]
    assert client.restored == []
    assert session.restored is False


async def test_a_saved_bundle_is_restored_when_the_recovery_slot_is_empty(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A citizen who clicked Save but lost their autosave must not be told their app is
    unrecoverable while the saved bundle sits in Blob."""
    user, project_id = await _mk(db_session, "u2j@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == [RecoveryNews.RESTORING]
    assert session.restored is True
    assert client.restored_from == [None]  # `None` is the saved bundle


async def test_a_poisoned_recovery_slot_is_stepped_over(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE REFUSAL LOOP, BOUNDED. `recoverable_work` ranks the two bundles by `last_modified`,
    never by ancestry, so a recovery copy that was overwritten with a bad tree outranks a
    perfectly good saved one — and every restore afterwards hands back the poison. Two consecutive
    refusals by U3's guard is the signal that the slot rather than the turn is the problem.

    Mutation check: raise `_POISONED_SLOT_REFUSALS` and this goes red."""
    user, project_id = await _mk(db_session, "u2k@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
    await _seed_recovery(fake_storage, app_id, sha="f" * 40)  # newer, and poisoned
    snapshot_module._consecutive_diverts[app_id] = 2
    client.exec_handler = _answers(None, commits=0, ancestry="")

    await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert client.restored_from == [None], "the saved bundle, not the poisoned recovery slot"


# =============================================================================
# The two ways of not knowing, which fail in opposite directions
# =============================================================================


async def test_a_check_that_times_out_touches_nothing(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★★ AE2(a). `REVERTED` is the only state that may destroy anything, and the entire safety
    argument collapses if an unanswerable check can reach a teardown. The container stays running,
    attached and untouched; the turn fails as retryable."""
    user, project_id = await _mk(db_session, "u2l@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)

    def times_out(cmd: list[str]) -> ExecResult:
        raise SandboxError("the supervisor did not answer")

    client.exec_handler = times_out

    with pytest.raises(WorkspaceUnreadableError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )

    assert client.restored == []
    assert client.torn_down == []
    assert [k for k in fake_storage.objects if k.startswith(quarantine_prefix(app_id))] == []


async def test_a_structurally_unanswerable_check_lets_the_turn_through(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Retrying cannot help, so refusing would lock the citizen out of their own project for good.
    Proceed, say so once, restore nothing, destroy nothing."""
    user, project_id = await _mk(db_session, "u2m@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    # A lineage that moved over a tree that still holds content — `git reset --hard`'s shape.
    client.exec_handler = _answers("rewound", commits=12, ancestry="0 1")
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == [RecoveryNews.UNVERIFIED]
    assert session.restored is False
    assert client.restored == []
    assert client.torn_down == []


async def test_the_slot_is_freed_even_when_the_gate_refuses(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ A refusal that leaked the one-per-user build slot would turn one bad probe into a
    permanent lockout — the exact failure the retryable arm exists to avoid."""
    user, project_id = await _mk(db_session, "u2n@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)

    def times_out(cmd: list[str]) -> ExecResult:
        raise SandboxError("the supervisor did not answer")

    client.exec_handler = times_out
    with pytest.raises(WorkspaceUnreadableError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )

    # The retry can now attach, which is the whole promise of "retryable".
    client.exec_handler = _answers("b" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert session.restored is False


# =============================================================================
# U4 — the reversion that happens while nobody is sending messages
# =============================================================================


async def test_an_idle_reversion_is_caught_at_the_poll_and_alarmed(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ R4/R7 — THE TURN MAY NEVER COME. Every other check in this system runs at the start of a
    turn; a citizen who is reading, or in another tab, or at lunch gets none of them, and the
    completion claim above their preview goes on being displayed over a dead app for as long as
    the page stays open.

    Mutation check: return INTACT unconditionally from `project_workspace_check` and this goes
    red."""
    user, project_id = await _mk(db_session, "u4a@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")
    raised: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        manager_module._log, "error", lambda event, **kw: raised.append((event, kw))
    )

    state = await manager.project_workspace_check(
        db_session, user, project_id, sandbox_client=client
    )

    assert state is WorkspaceState.REVERTED
    assert [event for event, _ in raised] == [WORKSPACE_LOST_WHILE_IDLE_EVENT]
    payload = raised[0][1]
    assert payload["app_id"] == str(app_id)
    assert payload["recovery_copy_available"] is True
    assert payload["verdict"] == WorkspaceState.REVERTED.value


async def test_the_idle_check_restores_nothing_and_destroys_nothing(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ IT ONLY REPORTS. The restore belongs to the next turn, where the citizen is present, has
    been told, and can confirm — recovering somebody's app behind their back while they are
    looking at another tab is not a kindness, and it would run a full teardown-and-restore under
    a preview they are staring at."""
    user, project_id = await _mk(db_session, "u4b@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")

    await manager.project_workspace_check(db_session, user, project_id, sandbox_client=client)

    assert client.restored == []
    assert client.torn_down == []
    assert [k for k in fake_storage.objects if k.startswith(quarantine_prefix(app_id))] == []


async def test_repeated_polls_inside_the_window_make_one_container_call(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE RATE LIMIT IS NOT POLITENESS. A tab left open overnight polls every 45 seconds; with
    no window that is a container exec every 45 seconds, forever, for an answer that changes at
    most once.

    Mutation check: drop the memo and the second assertion goes red."""
    user, project_id = await _mk(db_session, "u4c@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    asked: list[list[str]] = []
    inner = _answers("b" * 40)

    def counting(cmd: list[str]) -> ExecResult:
        asked.append(cmd)
        return inner(cmd)

    client.exec_handler = counting

    first = await manager.project_workspace_check(
        db_session, user, project_id, sandbox_client=client
    )
    after_one = len(asked)
    second = await manager.project_workspace_check(
        db_session, user, project_id, sandbox_client=client
    )

    assert first is second is WorkspaceState.INTACT
    assert after_one > 0, "the first ask must actually reach the container"
    assert len(asked) == after_one, "the second must not"


@pytest.mark.parametrize(
    ("ancestry", "head", "commits", "expected"),
    [
        ("0 1", "rewound", 12, WorkspaceState.UNVERIFIABLE),
        ("", "b" * 40, 4, WorkspaceState.UNREADABLE),
    ],
)
async def test_an_unanswerable_check_does_not_retract_a_standing_claim(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    ancestry: str,
    head: str,
    commits: int,
    expected: WorkspaceState,
) -> None:
    """★ Neither way of not knowing may strike through a completion claim or alarm as a reversion.
    A claim retracted because a supervisor blipped is a new false statement, made by the code that
    exists to stop false statements."""
    user, project_id = await _mk(db_session, f"u4d-{expected.value}@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_recovery(fake_storage, app_id)
    client.exec_handler = _answers(head, commits=commits, ancestry=ancestry)
    raised: list[str] = []
    monkeypatch.setattr(manager_module._log, "error", lambda event, **kw: raised.append(event))

    state = await manager.project_workspace_check(
        db_session, user, project_id, sandbox_client=client
    )

    assert state is expected
    assert WORKSPACE_LOST_WHILE_IDLE_EVENT not in raised
