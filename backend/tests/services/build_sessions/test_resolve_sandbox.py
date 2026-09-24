"""The pre-turn integrity gate: say so, quarantine, restore, then confirm.

Until this gate the attach path re-attached on a supervisor `/health` 200 and never looked at
the tree. THE ORDER OF THE ASSERTIONS IN THIS FILE IS THE ORDER OF THE RISK.

* `test_the_sentence_arrives_before_the_restore_runs` is the unit's shape: putting an app back
  takes tens of seconds during which the screen would otherwise say nothing at all.
* `test_a_check_that_times_out_touches_nothing` must never regress: `REVERTED` is the only state
  that may destroy anything, and an unanswerable check must never reach a teardown.
* `test_a_seeded_bundle_alone_does_not_make_a_container_look_reverted` proves the fakes'
  default is right — and, more usefully, that it STAYS right.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import Literal

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from src.api.v1.build_sessions.schemas import PreviewLifeState
from src.config import settings
from src.db.models.user import User
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions import pass_history
from src.services.build_sessions.alarms import (
    APP_FIRST_SERVED_EVENT,
    APP_STOPPED_WHILE_IDLE_EVENT,
    WORKSPACE_LOST_WHILE_IDLE_EVENT,
)
from src.services.build_sessions.integrity import (
    WorkspaceState,
    reset_integrity_streaks_for_tests,
)
from src.services.build_sessions.locks import (
    mark_registry_ending,
    mark_serving,
    read_registry,
    renew_liveness_lease,
    stamp_is_proven,
)
from src.services.build_sessions.manager import (
    RecoveryNews,
    SessionManager,
    WorkspaceUnreadableError,
    reset_idle_checks_for_tests,
)
from src.services.build_sessions.pass_history import CopyAttempt
from src.services.redis import REGISTRY_STATE_READY
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE
from src.services.sandbox import SandboxError
from src.services.sandbox.base import DevStatus, ExecResult, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.storage import quarantine_prefix, snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import DevServerDownUntilStarted, FakeSandboxClient, FakeStorage, a_git_bundle

RECORDED = "a" * 40

# What `/dev/status` answers, by name. `_STOPPED` carries 137, the SIGKILL a shell reports when the
# out-of-memory killer takes the process below the supervisor's child (a kill landing on the child
# itself reads -9). `_RESTARTED_BY_THE_AGENT` is a NORMAL state: the open sandbox lets the agent
# `pkill` the supervisor's child and `nohup` its own replacement, so the supervisor's child is gone
# while the app answers perfectly well.
_SERVING = DevStatus(running=True, ready=True, port=3000, root_status=None)
_STOPPED = DevStatus(running=False, ready=False, port=3000, exit_code=137, root_status=None)
_RESTARTED_BY_THE_AGENT = DevStatus(running=False, ready=True, port=3000, root_status=None)


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
    reset_idle_checks_for_tests()


@pytest.fixture(autouse=True)
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[CopyAttempt]:
    """Every copy-before-reclaim outcome a test here recorded, WITHOUT touching the database.

    AUTOUSE, AND NOT FOR CONVENIENCE: `record_durable_copy_attempt` opens its own session and
    COMMITS, so an unspied reap here leaves a permanent row in the SHARED test database that
    `test_reclamation_report_only.py` counts. The real writer is exercised, against a connection
    that rolls back, in `test_write_back_before_reclaim.py`."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


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
    db: AsyncSession,
    manager: SessionManager,
    user: User,
    project_id: uuid.UUID,
    *,
    client: FakeSandboxClient | None = None,
) -> tuple[FakeSandboxClient, uuid.UUID]:
    """Get to the ATTACH arm — the only one where the tree is older than the request.

    The other two arms have just built the workspace from a bundle or a template, so there is
    nothing for them to have lost. Reaching this one takes a real provision first."""
    client = client or FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=False)
    client.attach_handle = session.handle
    return client, session.app_id


async def _seed_saved(store: FakeStorage, app_id: uuid.UUID, sha: str = RECORDED) -> None:
    await store.put(snapshot_key(app_id), a_git_bundle(sha), metadata={"head_sha": sha})


# =============================================================================
# The ordinary case, and the guard that keeps it ordinary
# =============================================================================


async def test_an_intact_workspace_is_attached_exactly_as_before(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "u2a@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
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
    """★ THE INERTNESS GUARD, and it is worth more than it looks: an unrecognised exec command
    must default to a real answer, not `head=None`, or a repo-less container with a recovery
    bundle present reads as a CONFIRMED REVERSION and quarantines apps nobody touched. This test
    does NOT script `exec`, on purpose — it fails the day the default stops being the ordinary
    case.

    Mutation check: delete the `_STATE_MARKER` arm from `tests/fakes.py` and this goes red."""
    user, project_id = await _mk(db_session, "u2b@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
    heard = _Heard()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=heard
    )

    assert heard.news == []
    assert session.restored is False


async def test_a_brand_new_project_attaches_with_nothing_to_say(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
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
    """★ The ordering IS the unit.

    Putting an app back is a full bundle of the reverted tree plus a complete restore — tens of
    seconds during which the screen would otherwise say nothing at all, which is indistinguishable
    from the product having hung.

    Mutation check: move the announce below the restore and this goes red."""
    user, project_id = await _mk(db_session, "u2d@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")
    order: list[str] = []
    heard = _Heard()

    async def note(news: RecoveryNews) -> None:
        order.append(f"said:{news.value}")
        await heard(news)

    real_restore = client.restore_from_snapshot

    async def watched_restore(
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
        kind: Literal["build_sandbox", "shared_sandbox"] = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> SandboxHandle:
        order.append("restored")
        return await real_restore(
            user_id,
            app_name,
            app_env=app_env,
            source_key=source_key,
            kind=kind,
            shared_project_id=shared_project_id,
            shared_owner_id=shared_owner_id,
        )

    monkeypatch.setattr(client, "restore_from_snapshot", watched_restore)

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True, announce=note
    )

    assert order == ["said:restoring", "restored"]
    assert session.news is RecoveryNews.RESTORING
    assert session.restored is True


async def test_a_restore_inside_a_turn_starts_the_dev_server_and_stamps_its_first_page(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The restore brought this container up, so the restore is what must start its app.

    The turn it happens in is held and never reaches the engine's own start, so a restore that
    stops at the files leaves the preview waiting on a server nobody started.

    Mutation check: drop the `dev_start` call and the preview stays STARTING; drop the watcher and
    no first page is ever stamped."""
    monkeypatch.setattr(manager_module, "READINESS_POLL_S", 0)
    user, project_id = await _mk(db_session, "u2restart@rvaiglobal.com")
    manager = SessionManager()
    client = DevServerDownUntilStarted()
    _, app_id = await _attached(db_session, manager, user, project_id, client=client)
    await _seed_saved(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")

    with capture_logs() as logs:
        session = await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )
        for watcher in list(manager._tasks):
            with contextlib.suppress(Exception):
                await watcher

    assert session.restored is True
    assert client.dev_started == [session.handle.app_name]
    preview = await manager.project_preview_state(db_session, user, project_id)
    assert preview.state is PreviewLifeState.ALIVE
    served = [e for e in logs if e.get("event") == APP_FIRST_SERVED_EVENT]
    assert [e["observer"] for e in served] == ["restore_continuation"]


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
    await _seed_saved(fake_storage, app_id)
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
    await _seed_saved(fake_storage, app_id)
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
    await _seed_saved(fake_storage, app_id)
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
    await _seed_saved(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")

    async def refuse(
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
        kind: Literal["build_sandbox", "shared_sandbox"] = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
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
    """★ Neither a recovery copy nor a saved bundle. The one thing that must not happen is
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


# =============================================================================
# The two ways of not knowing, which fail in opposite directions
# =============================================================================


async def test_a_check_that_times_out_touches_nothing(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★★ `REVERTED` is the only state that may destroy anything, and the entire safety
    argument collapses if an unanswerable check can reach a teardown. The container stays running,
    attached and untouched; the turn fails as retryable."""
    user, project_id = await _mk(db_session, "u2l@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)

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
    """Retrying cannot help, so refusing would lock the citizen out of their own project for
    good."""
    user, project_id = await _mk(db_session, "u2m@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
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
    await _seed_saved(fake_storage, app_id)

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
# The reversion that happens while nobody is sending messages
# =============================================================================


async def test_an_idle_reversion_is_caught_at_the_poll_and_alarmed(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE TURN MAY NEVER COME — every other check in this system runs at the start of a turn,
    and this one runs at the preview poll. Why that is the only notice an idle reversion gets is
    on `WORKSPACE_LOST_WHILE_IDLE_EVENT`.

    Mutation check: return INTACT unconditionally from `project_workspace_check` and this goes
    red."""
    user, project_id = await _mk(db_session, "u4a@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
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


@pytest.mark.parametrize("dev", [_SERVING, _STOPPED], ids=["serving", "stopped"])
async def test_the_idle_check_restores_nothing_and_destroys_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    dev: DevStatus,
) -> None:
    """★ A REVERTED TREE IS ONLY REPORTED. The restore belongs to the next turn, where the citizen
    is present, has been told, and can confirm — recovering somebody's app behind their back while
    they are looking at another tab is not a kindness, and it would run a full teardown-and-restore
    under a preview they are staring at.

    AND A STOPPED DEV SERVER DOES NOT CHANGE THAT. The one thing this check puts away is an INTACT
    app that has stopped. A reverted container is not holding the citizen's app, so there is
    nothing of theirs in it to protect by a copy, and the next turn's restore is the path that
    knows how to bring the right tree back."""
    user, project_id = await _mk(db_session, "u4b@rvaiglobal.com")
    manager = SessionManager()
    client, app_id = await _attached(db_session, manager, user, project_id)
    await _seed_saved(fake_storage, app_id)
    client.exec_handler = _answers(None, commits=0, ancestry="")
    _script_dev(monkeypatch, client, dev)

    await manager.project_workspace_check(db_session, user, project_id, sandbox_client=client)

    assert client.restored == []
    assert client.torn_down == []
    assert [k for k in fake_storage.objects if k.startswith(quarantine_prefix(app_id))] == []


# =============================================================================
# The app that stopped while nobody was sending messages
# =============================================================================


def _script_dev(
    monkeypatch: pytest.MonkeyPatch, client: FakeSandboxClient, *readings: DevStatus
) -> None:
    """Script what `/dev/status` answers, one reading per ask; the last one repeats."""
    queue = list(readings)

    async def _read(handle: SandboxHandle) -> DevStatus:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(client, "dev_status", _read)


def _sandbox_name(client: FakeSandboxClient) -> str:
    assert client.attach_handle is not None
    return client.attach_handle.app_name


class _KilledUntilStarted(DevServerDownUntilStarted):
    """A dev server the out-of-memory killer took, which comes back once something starts it.

    Records whether the serving proof was still standing AT THE MOMENT of that start: the order is
    half the promise, and a test that only read the registry afterwards would pass just as happily
    against a restart that framed the dead server first."""

    def __init__(self, redis: aioredis.Redis, user_id: uuid.UUID) -> None:
        super().__init__()
        self._redis = redis
        self._user_id = user_id
        self.proof_at_start: bool | None = None

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        reg = await read_registry(self._redis, self._user_id)
        self.proof_at_start = reg is not None and stamp_is_proven(reg)
        return await super().dev_start(handle, cmd=cmd, cwd=cwd)

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        status = await super().dev_status(handle)
        return status if status.running else _STOPPED


async def _a_stopped_app(
    db: AsyncSession,
    redis: aioredis.Redis,
    store: FakeStorage,
    email: str,
    *,
    saved: bool = True,
) -> tuple[SessionManager, _KilledUntilStarted, User, uuid.UUID]:
    """An app that served, then lost its dev server with nothing else touching the container."""
    user, project_id = await _mk(db, email)
    manager = SessionManager()
    client = _KilledUntilStarted(redis, user.id)
    _, app_id = await _attached(db, manager, user, project_id, client=client)
    if saved:
        await _seed_saved(store, app_id)
    assert await mark_serving(
        redis, user.id, app_name=_sandbox_name(client), when=datetime.now(UTC)
    ), "premise: the app served before it stopped"
    return manager, client, user, project_id


async def _settle(manager: SessionManager) -> None:
    for watcher in list(manager._tasks):
        with contextlib.suppress(Exception):
            await watcher


@pytest.mark.parametrize("saved", [True, False], ids=["saved", "never-saved"])
async def test_an_intact_app_whose_dev_server_stopped_is_restarted_in_place(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    attempts: list[CopyAttempt],
    saved: bool,
) -> None:
    """★ THE WAIT THAT NEVER ENDED. `preview-state` cannot see a process (no container call, by
    contract) and the integrity verdict reads the git tree, which a dead process does not change,
    so this check is the only thing that notices. It starts the dev server again where it stopped:
    the container, its unsaved tree and the commit an approval pins all stay, and nothing is
    written back, whether or not the app was ever saved.

    The serving proof is retracted BEFORE the start, so the pane waits for the restarted app's
    first page rather than framing a server that is not there yet.

    Mutation check: tear the container down instead and `torn_down` fills; skip the retraction
    and `proof_at_start` is True."""
    monkeypatch.setattr(manager_module, "READINESS_POLL_S", 0)
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, f"u4-stopped-{saved}@rvaiglobal.com", saved=saved
    )
    name = _sandbox_name(client)
    on_record = dict(fake_storage.objects)

    with capture_logs() as logs:
        state = await manager.project_workspace_check(
            db_session, user, project_id, sandbox_client=client
        )
    await _settle(manager)

    assert state is WorkspaceState.INTACT, "the files are fine; this is not a reversion"
    assert client.dev_started == [name]
    assert client.proof_at_start is False, "the dead server's proof was retracted first"
    assert client.torn_down == []
    assert attempts == [], "nothing was written back"
    assert fake_storage.objects == on_record
    reg = await read_registry(fake_redis, user.id)
    assert reg is not None
    assert reg[REGISTRY_FIELD_APP_NAME] == name
    assert reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_READY
    stopped = [e for e in logs if e["event"] == APP_STOPPED_WHILE_IDLE_EVENT]
    assert [(e["log_level"], e["exit_code"], e["restarted"]) for e in stopped] == [
        ("error", 137, True)
    ]


async def test_a_restarted_app_is_stamped_serving_by_its_first_page(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart hands the wait to the same bounded watcher a restore uses, so the first page
    the restarted server paints is stamped and the pane's next reading is `alive`.

    Mutation check: drop the watcher and the preview stays STARTING."""
    monkeypatch.setattr(manager_module, "READINESS_POLL_S", 0)
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, "u4-stopped-served@rvaiglobal.com"
    )

    with capture_logs() as logs:
        await manager.project_workspace_check(db_session, user, project_id, sandbox_client=client)
        await _settle(manager)

    preview = await manager.project_preview_state(db_session, user, project_id)
    assert preview.state is PreviewLifeState.ALIVE
    served = [e for e in logs if e.get("event") == APP_FIRST_SERVED_EVENT]
    assert [e["observer"] for e in served] == ["idle_continuation"]


@pytest.mark.parametrize(
    "dev", [_SERVING, _RESTARTED_BY_THE_AGENT], ids=["serving", "restarted-by-the-agent"]
)
async def test_an_app_that_is_serving_is_left_alone(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    dev: DevStatus,
) -> None:
    """★ `running` ALONE IS NOT THE PROCESS FACT. `running=False` beside `ready=True` is an app the
    agent restarted itself, serving its citizen perfectly well — the reaper reads the pair the same
    way. A second `next dev` started beside it would spend memory the container does not have.

    Mutation check: read `running` alone and `dev_started` fills."""
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, f"u4-serving-{dev.running}@rvaiglobal.com"
    )
    _script_dev(monkeypatch, client, dev)

    await manager.project_workspace_check(db_session, user, project_id, sandbox_client=client)

    assert client.dev_started == []
    assert client.torn_down == []


async def test_a_supervisor_that_cannot_answer_restarts_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ A READING THAT FAILED IS NOT A STOPPED APP. A supervisor that errors or times out has not
    found the process dead — it has not been asked — and a start sent over a network blip may land
    beside a server that is running."""
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, "u4-blind@rvaiglobal.com"
    )

    async def _boom(handle: SandboxHandle) -> DevStatus:
        raise SandboxError("dev/status failed with status 502")

    monkeypatch.setattr(client, "dev_status", _boom)

    state = await manager.project_workspace_check(
        db_session, user, project_id, sandbox_client=client
    )

    assert state is WorkspaceState.INTACT, "and the integrity answer is unaffected"
    assert client.dev_started == []
    assert client.torn_down == []


async def test_a_stopped_app_is_never_restarted_under_a_live_turn(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ A TURN MAY HAVE STOPPED THE DEV SERVER ON PURPOSE. The agent restarts it with `pkill` and
    `nohup`, and between the two the process is genuinely dead — so a tab asking at that moment
    must not start a second server under the turn that is using the container.

    Mutation check: drop the live-session refusal and `dev_started` fills mid-turn."""
    user, project_id = await _mk(db_session, "u4-stopped-turn@rvaiglobal.com")
    manager = SessionManager()
    client = DevServerDownUntilStarted()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    await _seed_saved(fake_storage, session.app_id)
    _script_dev(monkeypatch, client, _STOPPED)

    await manager.project_workspace_check(db_session, user, project_id, sandbox_client=client)

    assert client.dev_started == []
    assert client.torn_down == []


async def test_a_stopped_app_is_never_restarted_while_its_lease_is_held(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease is the cross-process half of the same refusal: a turn running in another process
    holds it while its agent works in this container.

    Mutation check: drop the lease refusal and `dev_started` fills."""
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, "u4-stopped-lease@rvaiglobal.com"
    )
    assert await renew_liveness_lease(fake_redis, user.id)

    await manager.project_workspace_check(db_session, user, project_id, sandbox_client=client)

    assert client.dev_started == []


async def test_a_stopped_app_is_never_restarted_while_a_start_holds_the_workspace(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    """★ A START, A RESTORE AND A DISCARD EACH BRING THEIR OWN DEV SERVER, under this user's start
    lock, so a dead reading taken inside that window is a server that has not been started yet. The
    supervisor's own "already running" check is not atomic: starting another beside it would put
    two `next dev` in a container already short of memory. And the tab asking must not wait behind
    a restore to learn that — its poll is what shows the app arriving.

    Mutation check: wait for the lock instead of refusing, and this call hangs behind the start;
    drop the refusal, and `dev_started` fills."""
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, "u4-stopped-start@rvaiglobal.com"
    )
    start = manager._start_lock_for(user.id)

    await start.acquire()
    try:
        await asyncio.wait_for(
            manager.project_workspace_check(db_session, user, project_id, sandbox_client=client),
            timeout=5,
        )
    finally:
        start.release()

    assert client.dev_started == []
    reg = await read_registry(fake_redis, user.id)
    assert reg is not None and stamp_is_proven(reg), "the proof was left for the start to settle"


async def test_a_registry_that_moves_on_before_the_restart_restarts_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reading is taken outside the start lock, and the registry can stop naming this app
    READY before the lock is held: a reap has marked it ending, or another project took the slot.
    Starting a server in a container the platform is letting go of is the one thing this must not
    do.

    Mutation check: drop the registry refusal and `dev_started` fills."""
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, "u4-stopped-ending@rvaiglobal.com"
    )

    async def _read_then_the_registry_moves_on(handle: SandboxHandle) -> DevStatus:
        await mark_registry_ending(fake_redis, user.id)
        return _STOPPED

    monkeypatch.setattr(client, "dev_status", _read_then_the_registry_moves_on)

    await manager.project_workspace_check(db_session, user, project_id, sandbox_client=client)

    assert client.dev_started == []


async def test_a_restart_the_supervisor_refuses_is_logged_and_destroys_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused start is the same fact the restore and the Discard already handle: logged, never
    raised, and the watcher and the reconciler under it report whatever the container does next.
    It is never a reason to tear the container down.

    Mutation check: let the refusal raise and the check fails instead of returning."""
    monkeypatch.setattr(manager_module, "_COLD_READY_BUDGET_SECONDS", 0.0)
    manager, client, user, project_id = await _a_stopped_app(
        db_session, fake_redis, fake_storage, "u4-stopped-refused@rvaiglobal.com"
    )

    async def _refused(handle: SandboxHandle, **_kw: object) -> int:
        raise SandboxError("dev/start failed with status 500")

    monkeypatch.setattr(client, "dev_start", _refused)

    with capture_logs() as logs:
        state = await manager.project_workspace_check(
            db_session, user, project_id, sandbox_client=client
        )
    await _settle(manager)

    assert state is WorkspaceState.INTACT
    assert client.torn_down == []
    assert [e["arm"] for e in logs if e["event"] == "put_back_tree_dev_start_failed"] == ["idle"]
    stopped = [e for e in logs if e["event"] == APP_STOPPED_WHILE_IDLE_EVENT]
    assert [e["restarted"] for e in stopped] == [False]


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
    await _seed_saved(fake_storage, app_id)
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
    await _seed_saved(fake_storage, app_id)
    client.exec_handler = _answers(head, commits=commits, ancestry=ancestry)
    raised: list[str] = []
    monkeypatch.setattr(manager_module._log, "error", lambda event, **kw: raised.append(event))

    state = await manager.project_workspace_check(
        db_session, user, project_id, sandbox_client=client
    )

    assert state is expected
    assert WORKSPACE_LOST_WHILE_IDLE_EVENT not in raised
