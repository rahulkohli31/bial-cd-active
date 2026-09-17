"""The WRITE turn's sandbox lifecycle: `ensure_sandbox` / `finish_turn_sandbox`.

A Write turn allocates everything a build allocates (container, lock, registry, heartbeat) and
none of what a build runs (`run_build`, `build_started`, attachments). These tests pin both
halves, plus the save model: `write_snapshot` is the only path that pushes the tree to Blob
storage, and a Write turn with no reachable save point would report success while silently
losing every edit to the next reaper sweep.

`may_write` mirrors the turn's toolset (`toolsets_for_kind` gives the mutating `sandbox_toolset`
only to `ChatKind.BUILD`), so `may_write=False` implies `touched=False` in production — a test
pairing `may_write=False` with `touched=True` pins nothing. Where a scenario needs both a Save and
a mutating turn, end the turn first and save between turns, as `save_project_snapshot` expects."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ChatKind
from src.db.models.pending_teardown import PendingTeardown
from src.db.models.user import User
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.alarms import RECOVERY_WRITE_DID_NOT_LAND_EVENT
from src.services.build_sessions.locks import (
    heartbeat_is_alive,
    lock_is_held,
    read_registry,
    stay_of_execution_is_current,
)
from src.services.build_sessions.manager import (
    BuildSessionConflictError,
    NoLiveSandboxError,
    SessionManager,
    StopOutcome,
    app_name_for,
)
from src.services.redis import registry_key
from src.services.sandbox import (
    ExecResult,
    SandboxError,
    SandboxHandle,
    SandboxNotReadyError,
)
from src.services.sandbox.config import SandboxConfig
from src.services.storage import StorageError, recovery_key, snapshot_key
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle


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


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)


@pytest.fixture
def _fresh_engine():
    """THE ENGINE `stop_active_work` ACTUALLY REACHES. `_stop_the_held_session` asks
    `get_turn_engine()` to settle the user's turn, so a test holding a real turn open has to
    register the engine that turn is running in — otherwise the stop talks to a global engine
    with nothing in it and reads a verdict off a session nobody stopped. Not autouse: only the
    handful of tests that hold a turn open need it."""
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture
def session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


@pytest.fixture(autouse=True)
def handed_over(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every container a start in this file hands to the shutdown routine, recorded not run.

    Left to run, the routine reaches into this file's own doubles at an arbitrary await point
    and tears the outgoing container down mid-assertion — so `torn_down` would depend on
    scheduling. What it does once spawned is `test_shutdown.py`'s subject; what these tests are
    about is what the START does and does not do."""
    spawned: list[object] = []

    def _record(owed: object, **_aimed_at: object) -> None:
        spawned.append(owed)

    monkeypatch.setattr(manager_module, "shut_it_down_in_the_background", _record)
    return spawned


# --- attach ------------------------------------------------------------------


async def test_ensure_sandbox_allocates_a_build_worth_of_state_without_the_build(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "w1@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    # Everything a build would hold, because the reaper cannot tell the two apart.
    assert client.provisioned == [app_name_for(session.app_id)]  # fresh project -> provision
    assert manager.active_session_for(user.id) is session
    assert await lock_is_held(fake_redis, user.id) is True
    assert await heartbeat_is_alive(fake_redis, user.id) is True
    assert await read_registry(fake_redis, user.id) is not None

    # And nothing a build would run. `attachments` used to be the fourth of these; the field
    # itself is gone from `BuildSession` now that nothing can populate it, so its absence is
    # structural rather than something a test has to keep watching.
    assert session.task is None
    assert session.prompt == ""
    assert session.started_seq is None
    assert session.conversation_id is None


async def test_ensure_sandbox_mints_the_app_row_a_fresh_project_lacks(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # `turns.py`'s liveness pre-check reads the app id WITHOUT minting, deliberately — the
    # row appears only once a Write turn commits to actually running. This is the site that
    # commits to it, so this is the site that mints.
    user, project_id = await _mk(db_session, "w2@rvaiglobal.com")
    before = await db_session.scalar(
        sa.select(sa.func.count()).select_from(AppRegistry).where(AppRegistry.user_id == user.id)
    )
    assert before == 0

    session = await SessionManager().ensure_sandbox(
        db_session, user, project_id, sandbox_client=FakeSandboxClient(), may_write=True
    )
    after = await db_session.scalar(
        sa.select(AppRegistry.id).where(AppRegistry.user_id == user.id)
    )
    assert after == session.app_id


async def test_a_second_write_attach_while_one_is_live_is_a_conflict(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # One sandbox per user, whoever is asking. Every turn — Ask, Plan or Write — competes for
    # the same slot, because they consume the same container budget and the same Redis lock.
    #
    # THIS USED TO HAVE A TWIN asserting the OTHER direction, a build refused over a live Write
    # sandbox. There is no other direction any more: `_claim_the_one_build_slot` had exactly two
    # callers and `_start_locked` is deleted, so `ensure_sandbox` is now both sides of the race
    # and this test IS the pair. The claim it shared is unchanged and still asserted here.
    user, project_id = await _mk(db_session, "w3@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    with pytest.raises(BuildSessionConflictError) as caught:
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )
    assert caught.value.session_id == first.session_id


def _with_head(client: FakeSandboxClient, sha: str) -> FakeSandboxClient:
    """Script a container that is AT `sha` and bundles to it.

    Both halves matter. `git rev-parse HEAD` is what the dirty comparison reads on the
    container side; the base64 read is what `write_snapshot` uploads, and the saved head is
    parsed back OUT of those bytes — so a fake that returns an empty bundle makes every save
    look like it stored nothing parseable, and `dirty` could never settle."""
    bundle = base64.b64encode(b"# v2 git bundle\n" + sha.encode() + b" HEAD\n\nPACK").decode()

    def handler(cmd: list[str]) -> ExecResult:
        # `<head>@@<porcelain>@@<commit count>@@<ancestry>`. Count is 3, not 1, so this reads as
        # "holds work" rather than the 1-commit seeded baseline, which is deliberately reclaimable.
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            # Ancestry answers only when the probe asked (`merge-base` in the command) — answering
            # unconditionally would be a judgement `Ancestry.NOT_ASKED` exists to prevent.
            answered = "0 0" if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{sha}\n@@@@3@@{answered}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


def _pristine(client: FakeSandboxClient) -> FakeSandboxClient:
    """A container as a fresh provision leaves it: the single `bial: golden template baseline`
    commit the sandbox client seeds, and nothing else. This is what a Plan or Ask turn leaves
    behind, and it must never block another project."""
    baseline = "0" * 40

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            return ExecResult(stdout=f"{baseline}\n@@@@1", stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


async def test_the_turn_terminal_does_not_save_because_saving_is_the_users_call(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE SAVE MODEL: the agent commits inside the container as it works; the bundle is
    pushed only when the user clicks Save, never automatically at a turn's end.

    Mutation-check: put the `write_snapshot` call back in `finish_turn_sandbox` and this goes
    red."""
    user, project_id = await _mk(db_session, "w6@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    await manager.finish_turn_sandbox(session, client, touched=True)

    assert snapshot_key(session.app_id) not in fake_storage.objects
    assert session.snapshot_committed is False


async def test_the_user_clicking_save_is_what_writes_the_bundle(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ The other half. Save works BETWEEN turns — which is the only time anyone clicks it —
    so it must not require an in-process session. It attaches through the registry instead."""
    user, project_id = await _mk(db_session, "w6b@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=True)  # slot freed, no session
    client.attach_handle = session.handle

    outcome = await manager.save_project_snapshot(
        db_session, user, project_id, sandbox_client=client
    )

    assert outcome.app_id == session.app_id
    assert outcome.head_sha == "a" * 40
    assert snapshot_key(session.app_id) in fake_storage.objects


async def test_save_refuses_rather_than_reporting_success_with_no_workspace(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A Save button that says "saved" having stored nothing is the worst outcome available
    here — the user walks away believing their work is kept."""
    user, project_id = await _mk(db_session, "w6c@rvaiglobal.com")
    with pytest.raises(NoLiveSandboxError):
        await SessionManager().save_project_snapshot(
            db_session, user, project_id, sandbox_client=FakeSandboxClient()
        )


async def test_unsaved_work_reads_as_dirty_and_a_save_settles_it(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Compared by COMMIT, not a local flag — the only comparison that survives a reload, a
    second tab and a process restart, all of which lose in-memory state."""
    user, project_id = await _mk(db_session, "w6d@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "b" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=False
    )
    client.attach_handle = session.handle

    # Never saved, but there IS a container: dirty, and the most important time to prompt.
    before = await manager.project_save_state(db_session, user, project_id, sandbox_client=client)
    assert before.dirty is True
    assert before.saved_head is None

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    after = await manager.project_save_state(db_session, user, project_id, sandbox_client=client)
    assert after.dirty is False
    assert after.container_head == after.saved_head == "b" * 40


async def test_a_brand_new_project_offers_a_save_rather_than_reading_unknown(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ The bug this arm exists for: the golden template ships NO `.git`, so `git rev-parse
    HEAD` fails on every brand-new project. Read as "unknown", that hid the Save button on
    exactly the projects that most need it."""
    user, project_id = await _mk(db_session, "w6f@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    # SAID EXPLICITLY: the fake's default now HOLDS work, so a test meaning "no repository
    # here" must override it rather than lean on the old empty default (which hid this branch).
    client.exec_handler = lambda cmd: ExecResult(stdout="", stderr="", exit=0)

    state = await manager.project_save_state(db_session, user, project_id, sandbox_client=client)

    assert state.container_head is None  # no commit yet, which is normal here
    assert state.dirty is True  # …and there IS something to save


async def test_uncommitted_work_is_dirty_even_when_the_commits_match(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ The lie a commit-only comparison tells. The prompt asks the agent to commit each
    coherent slice, but that is guidance, not a guarantee — and the moment it skips one, HEAD
    still matches the saved bundle while the user's files sit uncommitted in the tree. Reported
    as "All changes saved", that is the indicator actively misleading them."""
    user, project_id = await _mk(db_session, "w6g@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "d" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=False
    )
    client.attach_handle = session.handle
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    assert (
        await manager.project_save_state(db_session, user, project_id, sandbox_client=client)
    ).dirty is False

    # Same commit, but the agent has now written files it did not commit.
    def dirty_tree(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            return ExecResult(stdout=f"{'d' * 40}\n@@ M app/page.tsx", stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = dirty_tree
    after = await manager.project_save_state(db_session, user, project_id, sandbox_client=client)
    assert after.dirty is True


async def test_no_workspace_reads_as_unknown_never_as_clean(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`dirty=None` is a distinct answer from False. A UI that renders unknown as clean tells
    the user their work is safe when nothing checked."""
    user, project_id = await _mk(db_session, "w6e@rvaiglobal.com")
    state = await SessionManager().project_save_state(
        db_session, user, project_id, sandbox_client=FakeSandboxClient()
    )
    assert state.dirty is None


async def test_the_terminal_pardons_the_container_so_the_preview_outlives_the_turn(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The Write path diverges from `_do_finalize` here rather than omitting from it: a build's
    # container is scaffolding that survives only a clean success, but a Write turn's container
    # IS the preview on screen — the turn ending is not a reason for it to go dark.
    user, project_id = await _mk(db_session, "w7@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    await manager.finish_turn_sandbox(session, client, touched=True)

    assert client.torn_down == []  # still up
    assert await read_registry(fake_redis, user.id) is not None  # the sweep can still find it
    assert await stay_of_execution_is_current(fake_redis, user.id) is True  # lease owns it now
    assert await lock_is_held(fake_redis, user.id) is False  # the slot is free
    assert manager.active_session_for(user.id) is None
    assert session.status == BuildSessionStatus.ENDED


async def test_a_second_message_attaches_instead_of_rebuilding_the_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE COST OF A MESSAGE: Write is a chat mode, so every message calls `ensure_sandbox` —
    and without a guard, the same reconcile-then-allocate rule that ran once per build would
    tear down and rebuild a HEALTHY container on every single message.

    Mutation-check: drop the `spare_app` guard in `_holding_user_lock` and this goes red —
    `torn_down` gains the first container and `restored` gains a second entry."""
    user, project_id = await _mk(db_session, "w10@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)  # pardoned: container stays up
    client.attach_handle = first.handle  # the live container is attachable, as in production

    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert second.app_id == first.app_id
    assert client.torn_down == []  # the healthy container was NOT destroyed
    assert client.restored == []  # and nothing was rebuilt from the snapshot
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first message


async def test_a_different_project_never_steals_the_container(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[object],
) -> None:
    """The other half: the spare is keyed on the APP NAME, not merely "something is live" —
    attaching to whatever container happened to be up would hand project B project A's code.

    Refusing to STEAL the container never implied a licence to DESTROY it either. The start now
    goes through — a second project is a switch, not a conflict — but the incumbent leaves by
    being handed to the shutdown routine, which writes its tree back before deleting it. What
    must never happen here is the start reaching A's container itself, in either direction."""
    user, project_a = await _mk(db_session, "w11@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle

    second = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert second.app_id != first.app_id  # B got its own container, never A's
    assert client.torn_down == []  # and the start destroyed nothing to get it
    assert app_name_for(first.app_id) not in client.restored
    assert len(handed_over) == 1  # A left by the one door that saves it first


async def test_a_clean_incumbent_is_shown_out_rather_than_asked_about(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[object],
) -> None:
    """A saved-and-clean incumbent is neither silently destroyed nor asked about: the citizen's
    second project starts, and the first is handed to the routine that writes it back and closes
    it.

    A CLEAN INCUMBENT IS THE EASIEST ONE TO GET WRONG, because there is provably nothing to
    lose — which is the exact reasoning that justifies destroying it inline, and inline is where
    a container gets deleted out from under the routine reading it."""
    user, project_a = await _mk(db_session, "w12@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = FakeSandboxClient()

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle
    # Saved AND unchanged since: the container's HEAD is the bundle's, so `dirty` is False.
    _with_head(client, "e" * 40)
    await manager.save_project_snapshot(db_session, user, project_a, sandbox_client=client)

    second = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert second.project_id == project_b
    assert len(handed_over) == 1
    assert client.torn_down == [], "the start must not delete what it hands over"


async def test_giving_up_a_project_explicitly_still_destroys_it_on_the_spot(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The deliberate exit survives the switch. `release` is the one route that destroys a
    container on purpose, and it is no longer the way out of a refusal — nothing refuses — so
    what it now has to keep proving is that an EXPLICIT give-up is still immediate and still
    frees the slot for the next project."""
    user, project_a = await _mk(db_session, "w13@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "b" * 40)

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle

    released = await manager.release_project_sandbox(
        db_session, user, project_a, sandbox_client=client
    )

    assert released is True
    assert client.torn_down == [app_name_for(first.app_id)]  # released on the user's say-so
    client.attach_handle = None
    second = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )
    assert second.app_id != first.app_id
    assert app_name_for(second.app_id) in client.provisioned


async def test_the_next_write_turn_restores_the_tree_the_last_one_saved(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The save proved end-to-end rather than by the presence of a blob: turn two RESTORES,
    # never provisions fresh. A fresh provision here would silently hand the model a blank
    # template and let it commit that over the user's real app.
    user, project_id = await _mk(db_session, "w8@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = first.handle
    await manager.finish_turn_sandbox(first, client, touched=True)
    # THE USER SAVES, after the turn. Nothing else writes the SAVED bundle — the turn terminal
    # writes only the recovery copy — so without this click there would be nothing here for the
    # next turn to restore, which is the save model working as specified.
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = None  # the container is gone; the bundles are all that is left

    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    assert second.app_id == first.app_id  # same project -> same app
    assert client.restored == [app_name_for(second.app_id)]  # RESTORED
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first attach
    # ...from the SAVED key: `newest_restore_source` found nothing newer to prefer, because the
    # user's click landed after the turn's recovery copy. This pins the SOURCE SELECTION only —
    # `FakeSandboxClient` hands back the same constant bundle whichever key is read, so it says
    # nothing about the bytes. The e2e twin
    # (`test_s5_a_reaped_container_resumes_the_work_not_the_last_save`) proves the tree itself.
    assert client.restored_from[-1] is None


async def test_a_storage_failure_during_save_reaches_the_user(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This test used to live on the turn terminal, where a storage blip had to be swallowed so
    it could not strand the user's slot. Saving is an explicit click now, and the calculus
    inverts completely: a Save that swallows its failure tells the user their work is stored
    when it is not. The error propagates, and the button reports it."""
    user, project_id = await _mk(db_session, "w9@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "c" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=False
    )
    client.attach_handle = session.handle

    async def boom(*_a: object, **_k: object) -> None:
        raise StorageError("blob is having a day")

    monkeypatch.setattr("src.services.build_sessions.manager.write_snapshot", boom)

    with pytest.raises(StorageError):
        await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)

    # And nothing was recorded as saved, so the dirty indicator keeps telling the truth.
    state = await manager.project_save_state(db_session, user, project_id, sandbox_client=client)
    assert state.dirty is True


# --- autosave to the recovery slot ------------------------------------


async def test_a_finished_write_turn_autosaves_to_recovery_not_over_the_saved_bundle(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE WHOLE POINT OF THE SEPARATE KEY. `finish_turn_sandbox` writes the platform's safety
    net so a crash, a closed laptop or the idle reaper stops costing a whole session — while
    `snapshot_key` stays exactly what the user last chose to save.

    Point the autosave at `snapshot_key` and this goes red twice over: the save model is reversed
    (every message becomes a saved version again) and the assertion below that the user's bundle is
    untouched fails outright."""
    user, project_id = await _mk(db_session, "w14@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "f" * 40)

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=True)

    assert recovery_key(session.app_id) in fake_storage.objects  # the net caught it
    assert snapshot_key(session.app_id) not in fake_storage.objects  # ...and saved nothing


async def test_a_read_only_turn_writes_no_recovery_bundle(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`touched=False` — an Ask or Plan turn changed nothing, so there is nothing to protect.
    Autosaving anyway would burn a bundle upload on every question the user asks."""
    user, project_id = await _mk(db_session, "w15@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=False)

    assert recovery_key(session.app_id) not in fake_storage.objects


async def test_a_failing_autosave_never_fails_the_turn(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A safety net that can fail a turn is not a safety net. The container is still pardoned
    and the slot still freed — a user must never see their message fail because a background
    convenience could not reach storage."""
    user, project_id = await _mk(db_session, "w16@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    def explode(_cmd: list[str]) -> ExecResult:
        raise SandboxError("the container stopped answering")

    client.exec_handler = explode
    await manager.finish_turn_sandbox(session, client, touched=True)  # must not raise

    assert recovery_key(session.app_id) not in fake_storage.objects
    assert manager.active_session_for(user.id) is None  # the slot was freed anyway


async def test_a_plan_only_project_does_not_block_a_real_one(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[object],
) -> None:
    """A QUESTION IS NOT WORK. A Plan prompt into a brand-new project takes the one-per-user
    workspace (`_pin_workspace` attaches for every mode) and the container behind it holds
    nothing but the golden template — which must never be what stands between a citizen and the
    project holding their real app.

    WHAT IS PINNED IS THE CITIZEN REACHING THAT PROJECT, which is now simply a start."""
    user, project_a = await _mk(db_session, "w17@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _pristine(FakeSandboxClient())

    plan_only = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(plan_only, client, touched=False)  # a read-only turn
    client.attach_handle = plan_only.handle

    real = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert real.project_id == project_b
    assert client.torn_down == []  # the pristine container leaves by the door that reads it
    assert len(handed_over) == 1


async def test_a_committed_but_unsaved_workspace_is_written_back_not_abandoned(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[object],
) -> None:
    """The other side of the same line, so the exemption above cannot quietly widen into
    "never-saved projects are disposable". A commit in the container IS work — it is what a
    Write turn leaves behind — and losing it is what the whole hand-over exists to prevent: the
    switch does not stop for it, but it does not walk away from it either. The debt names the
    outgoing app, which is what makes the write-back reachable once the registry has moved on."""
    user, project_a = await _mk(db_session, "w18@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "c" * 40)  # committed, never saved

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=False)
    client.attach_handle = first.handle

    await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert client.torn_down == []
    owed = await db_session.scalar(
        sa.select(PendingTeardown.app_name).where(PendingTeardown.user_id == user.id)
    )
    assert owed == app_name_for(first.app_id)


# --- an incumbent nobody can question ----------------------
#
# The start no longer probes the container it is leaving, so "I could not tell" has stopped
# being a question the citizen is asked. What it must still never become is a licence to
# destroy: these two pin the two certainties either side of it.


class _UnreachableAttach(FakeSandboxClient):
    """A container the registry still names READY, whose attach cannot CONFIRM anything.

    `SandboxNotReadyError`, not `SandboxGoneError`: the real client draws that line itself
    ("a container ARM confirms is gone has nothing to lose... a container we merely cannot
    authenticate to right now must NOT be destroyed over a transient control-plane failure"),
    and every reader has to honour it rather than treat a failure as an absence."""

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        raise SandboxNotReadyError("supervisor unreachable but the container still exists")


async def test_an_unreachable_incumbent_is_handed_over_rather_than_reclaimed(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[object],
) -> None:
    """A transient ARM blip must not become a licence to destroy a container — the property
    survives the refusal that used to carry it.

    Attach failing tells us nothing about whether work is in there: a cold container and one
    holding a day's edits look identical from here. So the start asks nothing, destroys nothing,
    and hands the container to the routine — which has its own retry budget for exactly this,
    and spares a container it cannot read rather than deleting it unread."""
    user, project_a = await _mk(db_session, "w19@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "d" * 40)

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)

    # Same registry, same live app — only the attach stops answering.
    blind = _with_head(_UnreachableAttach(), "d" * 40)
    started = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=blind, may_write=True
    )

    assert started.project_id == project_b
    assert blind.torn_down == []  # above all: nothing was destroyed on a failed probe
    assert len(handed_over) == 1


async def test_a_confirmed_gone_container_still_reclaims_silently(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The other side of that line, so the refusal above cannot widen into "any attach failure
    blocks forever". `SandboxGoneError` is a CERTAIN answer — ARM confirms the revision does not
    exist — and a container that is provably gone has nothing to lose, so the switch proceeds
    with no prompt. `FakeSandboxClient.attach_existing` raises exactly this when there is no
    handle to hand back."""
    user, project_a = await _mk(db_session, "w20@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "e" * 40)

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = None  # ARM says it is gone

    real = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )
    assert real.app_id != first.app_id  # no refusal — the switch went through


# --- an agent is writing in there RIGHT NOW: the mid-build switch -----------------
#
# The case the first cut of the guard got wrong. A live session means the incumbent is not
# idle-with-unsaved-work, it is BEING WRITTEN TO — and `release_project_sandbox` and
# `save_project_snapshot` both refuse while one is live. Reporting it as the ordinary refusal
# offered the user Save and Switch, and the server declined both. Observed live.


class _Blocking:
    """Holds a live WRITE TURN open so the switch lands mid-write.

    THE ONLY KIND OF WORK LEFT TO CATCH MID-FLIGHT. This used to hold a build session open by
    parking a `FakeBrain` the manager had spawned and could cancel itself; that whole path went
    with `SessionManager.start`. A turn on the real `TurnEngine` is what holds the workspace
    now, so the hold lives where production's does — inside the streaming model. `stepped` says
    the turn is genuinely under way, `gate` is the test's hand on the tap, and a stop cancels
    the `gate.wait()`, which is the shape a real agent mid-write takes."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.stepped = asyncio.Event()

    def model(self) -> FunctionModel:
        async def _stall(_messages: list[ModelMessage], _info: AgentInfo):
            yield "working on it"
            self.stepped.set()
            await self.gate.wait()
            raise RuntimeError("halted by the test")

        return FunctionModel(stream_function=_stall)


async def _a_turn_holding_the_workspace(
    db: AsyncSession,
    engine: TurnEngine,
    session_factory,
    manager: SessionManager,
    client: FakeSandboxClient,
    user: User,
    project_id: uuid.UUID,
    turn: _Blocking,
) -> None:
    """Start a real Write turn on `project_id` and return once it is genuinely streaming.

    The turn pins the project's container through `manager.ensure_sandbox`, so the manager's
    one-per-user slot is held by work `stop_active_work` can actually reach — which is what
    makes its `STOPPED` a statement about anything."""
    conversation = await ConversationFactory.create(
        db, user.id, project_id=project_id, kind=ChatKind.BUILD
    )
    await engine.start_turn(
        conversation=conversation,
        user_id=user.id,
        prompt="build it",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=project_id,
        model=turn.model(),
        session_factory=session_factory,
        persist_user_turn=_nothing_to_persist,
        manager=manager,
        sandbox_client=client,
    )
    await asyncio.wait_for(turn.stepped.wait(), timeout=10)


async def _nothing_to_persist() -> None:
    """The user's row is the route's job, not the engine's — and no test here reads it."""
    return None


async def test_a_project_being_built_does_not_refuse_the_next_one(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE REFUSAL WITH NO WAY THROUGH, retired. A citizen mid-build who opened another project
    was told "still being built" and offered a Stop — on the project they had just left, about
    work they had not asked to interrupt, in a dialog they had to answer before doing anything
    else. The build is still stopped, but by the routine and at its own boundary, and the
    citizen's next project starts while that happens.

    Mutation-check: restore the writing-session arm and the preflight raises again."""
    user, project_a = await _mk(db_session, "w21@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "f" * 40)

    # A WRITING session, held and not finished — the state the retired arm read.
    await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )

    await manager.reclaim_preflight(db_session, user, project_b, sandbox_client=client)

    assert client.torn_down == []  # the agent keeps working until the routine stops it


async def test_saving_a_project_mid_build_is_refused(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE DATA-INTEGRITY HALF, and a real bug this found rather than a hypothetical.

    `save_project_snapshot` had no session guard, so "Save and switch" on a building project
    SUCCEEDED — bundling whatever the agent had on disk mid-edit as the version Relaunch
    restores — and only then failed on the release, leaving a corrupted saved bundle.

    Mutation-check: drop the `_live_session_holds` check in `save_project_snapshot` and this
    goes green with a bundle in storage."""
    user, project_a = await _mk(db_session, "w22@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "0" * 40)

    session = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )

    with pytest.raises(BuildSessionConflictError):
        await manager.save_project_snapshot(db_session, user, project_a, sandbox_client=client)
    # Nothing was written — the saved bundle is not a photograph of a workshop mid-swing.
    assert snapshot_key(session.app_id) not in fake_storage.objects


async def test_stop_active_work_settles_the_build_so_the_switch_can_proceed(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """The first of three steps: while the build runs, save and release both refuse, and after
    `stop_active_work` returns the slot must be free — `_active_by_user` empty ON RETURN is the
    whole contract, not merely "asked to settle".

    `STOPPED` used to be a bare `True` returned unconditionally, holding even if the turn had
    not actually unwound; it is now derived from exactly what the two lines below check."""
    user, project_a = await _mk(db_session, "w23@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "1" * 40)
    turn = _Blocking()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )
    assert manager.active_session_for(user.id) is not None

    # The gate stays SHUT: the stop has to be what ends this, not the model finishing on its
    # own. Opening it first would let the turn settle by itself and the assertions below
    # would pass without `stop_active_work` having done anything — the turn cancels inside
    # `gate.wait()`, which is the shape a real agent mid-write takes.
    stopped = await manager.stop_active_work(db_session, user, project_a, sandbox_client=client)

    assert stopped is StopOutcome.STOPPED
    assert manager.active_session_for(user.id) is None
    assert user.id not in manager._active_by_user


async def test_stopping_a_project_that_is_not_building_is_a_quiet_success(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`NOTHING_WAS_RUNNING`, not an error: the caller's goal is "settled", and it already is —
    a 409 here would fail the dialog's own first step on the common path where the build
    finished while the user was reading.

    ITS OWN STATE, not the absence of a stop: the boolean this replaces spelt this case `False`
    and a *timeout* `True`, so the one answer that must never proceed shared a face with the one
    that always may. Named separately, both proceed-able answers stay proceed-able."""
    user, project_a = await _mk(db_session, "w24@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)  # settled, pardoned

    assert (
        await manager.stop_active_work(db_session, user, project_a, sandbox_client=client)
    ) is StopOutcome.NOTHING_WAS_RUNNING


async def test_stop_active_work_will_not_stop_a_different_project(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Scoped to the project the caller named, even though the slot is per-user and only one
    thing can be live. Stopping is destructive to work in progress; stopping a project the
    user did not point at because it happened to hold the slot is the silent-action failure
    this whole issue is about."""
    user, project_a = await _mk(db_session, "w25@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "2" * 40)

    await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )

    # Asking B to stop must not stop A's agent. NOT VACUOUS WITHOUT A RUNNING TURN: an
    # unscoped `stop_active_work` would find A's session through the per-user slot and answer
    # `STILL_RUNNING`, which is the state this assertion refuses — only a read scoped to the
    # project the caller named reaches `NOTHING_WAS_RUNNING` here.
    stopped = await manager.stop_active_work(db_session, user, project_b, sandbox_client=client)
    assert stopped is StopOutcome.NOTHING_WAS_RUNNING
    assert manager.active_session_for(user.id) is not None  # A still holds the workspace


# --- a QUESTION is not a build ---------------------------------------------------
#
# `_pin_workspace` attaches the live container for EVERY mode, so "a session is attached"
# is true throughout an ordinary Ask or Plan turn. Reading that as "an agent is writing"
# put a hammer icon and two Stop buttons in front of someone who had asked a question, and
# made the Save button answer "your app is still being built" while they waited for a chat
# reply. `may_write` comes from the mode's toolset instead — `toolsets_for_kind` hands Ask
# and Plan a `read_only_toolset`, so a non-writing turn CANNOT touch the tree.


async def test_a_read_only_turn_holding_the_workspace_refuses_nothing_either(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[object],
) -> None:
    """An Ask or Plan turn pins the container exactly as a Write turn does, so it used to earn
    the ordinary unsaved-work refusal — the narrower of the two, but still a stop sign in front
    of a citizen who had asked a question somewhere else. It hands over like any other.

    THE KIND OF TURN STILL MATTERS, one layer down: a read-only turn is cut where it stands
    while a Build turn is asked to stop at a boundary — see `test_switch.py`."""
    user, project_a = await _mk(db_session, "w26@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "3" * 40)

    session = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=False
    )
    client.attach_handle = session.handle

    await manager.reclaim_preflight(db_session, user, project_b, sandbox_client=client)
    await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert client.torn_down == []
    assert len(handed_over) == 1


async def test_a_read_only_turn_does_not_block_the_save_button(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE ONE A USER MEETS FIRST. Save is not gated on a turn being in flight, so refusing
    on "a session exists" made the ordinary Save button 409 mid-question — with copy telling
    the user their app was being built when nothing was.

    Mutation-check: swap `_writing_session_holds` back to `_live_session_holds` in
    `save_project_snapshot` and this goes red with `BuildSessionConflictError`."""
    user, project_id = await _mk(db_session, "w27@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "4" * 40)

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=False
    )
    client.attach_handle = session.handle

    out = await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    assert out.head_sha == "4" * 40  # it really saved, mid-question
    assert snapshot_key(session.app_id) in fake_storage.objects


async def test_a_read_only_turn_on_an_empty_project_blocks_nothing_at_all(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE REGRESSION THIS FILE KEEPS COMING BACK TO, now answered outright. A Plan question
    typed into a brand-new project pins the one workspace, and the container behind it holds
    nothing but the golden template — so the citizen was first locked out of their real app,
    then shown a dialog about a container nobody would miss.

    The clean-container reasoning is gone with the arm that needed it: no probe, no verdict, no
    dialog. The citizen reaches their real project."""
    user, project_a = await _mk(db_session, "w28@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _pristine(FakeSandboxClient())

    plan_only = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=False
    )
    client.attach_handle = plan_only.handle

    await manager.reclaim_preflight(db_session, user, project_b, sandbox_client=client)
    real = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert real.project_id == project_b
    assert client.torn_down == []


async def test_a_write_turn_no_longer_stops_the_citizens_next_project(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The other side of the line, so retiring the arms cannot be mistaken for retiring the
    distinction they drew: a Write turn genuinely holds a container with an agent that can write
    into it, and everything that protects THAT container is still in place. It is simply not the
    citizen's problem to arbitrate any more — the start goes through, and Save still refuses
    mid-write, because a bundle taken mid-edit is a corrupt saved version."""
    user, project_a = await _mk(db_session, "w29@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "5" * 40)

    session = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle

    await manager.reclaim_preflight(db_session, user, project_b, sandbox_client=client)

    # ...and Save still refuses while that agent writes.
    with pytest.raises(BuildSessionConflictError):
        await manager.save_project_snapshot(db_session, user, project_a, sandbox_client=client)


async def test_stopping_still_covers_a_read_only_turn(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`stop_active_work` keeps the BROAD predicate on purpose: an Ask turn holds the container
    just as firmly as a build, so narrowing this to `may_write` would put read-only modes back in
    the dead end the flow exists to remove — and is the mutation that turns the final assertion
    red with `NOTHING_WAS_RUNNING`.

    It reports `STILL_RUNNING` rather than a bare `True`: this fixture pins the container the way
    a read-only turn does, and nothing owns the engine turn the pin exists for, so the pin outlives
    the stop. `release` refuses on that still-true predicate, making "still running" accurate."""
    user, project_id = await _mk(db_session, "w30@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=False
    )
    assert manager.active_session_for(user.id) is not None

    stopped = await manager.stop_active_work(db_session, user, project_id, sandbox_client=client)
    # The BROAD predicate: a narrowed one would have seen nothing here at all.
    assert stopped is not StopOutcome.NOTHING_WAS_RUNNING
    assert stopped is StopOutcome.STILL_RUNNING
    # ...and the refusal that verdict predicts is real, which is what makes it the honest one.
    with pytest.raises(BuildSessionConflictError):
        await manager.release_project_sandbox(db_session, user, project_id, sandbox_client=client)


async def test_a_failed_provision_leaks_neither_lock_nor_slot(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # RENAMED from "failed_attach", which it never tested: it scripts `provision_new` to raise,
    # i.e. a failure on the CREATE arm, before any handle is assigned. That is why nothing here
    # caught the bug — no committed test took the ATTACH arm and then failed. The attach-arm
    # failures are pinned separately below.
    user, project_id = await _mk(db_session, "w5@rvaiglobal.com")
    manager = SessionManager()

    class FailingProvision(FakeSandboxClient):
        async def provision_new(self, user_id, app_name, *, app_env):
            raise SandboxError("provision blew up")

    with pytest.raises(SandboxError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=FailingProvision(), may_write=True
        )
    # `_holding_user_lock`'s compensation ran: nothing adopted, so nothing is held. A user
    # whose first Write turn failed to provision must not be locked out of their second.
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=FakeSandboxClient(), may_write=True
    )
    assert session.handle is not None


# --- a failure on the ATTACH arm must not destroy the borrowed container ---
# `_resolve_sandbox` has three arms. Two CREATE a container, so compensation tearing it down is
# a genuine rollback. One ATTACHES to a container that was already serving — and the attach arm
# is the STEADY STATE for every Write message after the first, because
# `_the_live_sandbox_is_already_the_one_we_want` deliberately skips the reconcile so a second
# message does not demolish and rebuild a running app.
#
# The container's tree is the ONLY copy of everything since the user last clicked Save
# (`finish_turn_sandbox` does not snapshot), so destroying it here is unrecoverable and
# silent: the preview simply stops loading and Relaunch restores the older SAVED bundle, so the
# app comes back looking healthy at an earlier state.
#
# The two `spares_the_attached_container` tests below invert the earlier teardown probes —
# they assert the container SURVIVES. The third checks that a container THIS request created
# is still torn down, so the sparing is scoped to the attach arm rather than blanket.


class _RecordingClient(FakeSandboxClient):
    """Records the handle OBJECT each teardown was handed, so "was the attached container
    destroyed" is answered by identity rather than by a matching name."""

    def __init__(self) -> None:
        super().__init__()
        self.attached: list[str] = []
        self.torn_down_handles: list[SandboxHandle] = []

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        handle = await super().attach_existing(user_id)
        self.attached.append(handle.app_name)
        return handle

    async def teardown(self, handle: SandboxHandle) -> None:
        self.torn_down_handles.append(handle)
        await super().teardown(handle)


async def _a_container_that_is_already_up(
    db: AsyncSession, manager: SessionManager, user: User, project_id: uuid.UUID
) -> tuple[_RecordingClient, SandboxHandle]:
    """Leave the world in the state the attach arm exists for: one healthy READY container for
    this user serving THIS project's app, no live session, the registry still naming it — the
    between-messages state every turn after the first arrives into."""
    client = _RecordingClient()
    first = await manager.ensure_sandbox(
        db, user, project_id, sandbox_client=client, may_write=True
    )
    # The turn terminal PARDONS the container (it is the preview on screen) and frees the slot.
    await manager.finish_turn_sandbox(first, client, touched=True)
    assert first.handle is not None
    client.attach_handle = first.handle  # as in production: the live container is attachable
    assert client.provisioned == [app_name_for(first.app_id)]
    assert client.torn_down == []
    return client, first.handle


async def test_a_redis_blip_seeding_the_heartbeat_spares_the_attached_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The window between taking the handle and `scope.adopt()` holds exactly one await
    — the heartbeat seed, deliberately unguarded — so a single `RedisError` there used to run
    compensation against a container this request had merely borrowed.

    Mutation-check: drop the `spare()` from `_LockScope.take` and this goes red."""
    user, project_id = await _mk(db_session, "w90a@rvaiglobal.com")
    manager = SessionManager()
    client, live = await _a_container_that_is_already_up(db_session, manager, user, project_id)

    async def redis_is_having_a_day(*_a: object, **_k: object) -> None:
        raise RedisError("heartbeat seed blew up")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("src.services.build_sessions.manager.write_heartbeat", redis_is_having_a_day)
        with pytest.raises(RedisError):
            await manager.ensure_sandbox(
                db_session, user, project_id, sandbox_client=client, may_write=True
            )

    # Did we actually reach the attach arm? Without these the teardown assertion means nothing.
    assert client.attached == [live.app_name], "attach_existing was NOT the arm taken"
    assert client.restored == [], "restore ran: NOT the attach arm"
    # THE POINT: a container this request did not create survives this request's rollback.
    assert client.torn_down == [], "the borrowed container was destroyed"
    assert client.torn_down_handles == []


async def test_stopping_a_turn_mid_heartbeat_spares_the_attached_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The other trigger: the Stop button is a plain `task.cancel()`, and compensation runs on
    `except BaseException` — which includes `CancelledError`."""
    user, project_id = await _mk(db_session, "w90b@rvaiglobal.com")
    manager = SessionManager()
    client, live = await _a_container_that_is_already_up(db_session, manager, user, project_id)

    in_flight = asyncio.Event()
    never = asyncio.Event()

    async def a_heartbeat_that_hangs(*_a: object, **_k: object) -> None:
        in_flight.set()
        await never.wait()  # Stop cancels the turn task right about here

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "src.services.build_sessions.manager.write_heartbeat", a_heartbeat_that_hangs
        )
        task = asyncio.create_task(
            manager.ensure_sandbox(
                db_session, user, project_id, sandbox_client=client, may_write=True
            )
        )
        await asyncio.wait_for(in_flight.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Compensation runs in its own task under `shield`; a cancelled caller re-raises before
        # it finishes, so give the loop a bounded chance to drain it before asserting.
        for _ in range(200):
            if client.torn_down:
                break
            await asyncio.sleep(0.01)

    assert client.attached == [live.app_name], "attach_existing was NOT the arm taken"
    assert client.torn_down == [], "Stop destroyed the app the user was looking at"
    never.set()


async def test_a_container_this_request_created_is_still_torn_down_on_failure(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The other half of the contract, so the fix cannot be "never tear anything down". On the
    CREATE arm the container IS this request's to roll back, and leaving it up would orphan it
    under a registry entry the next start overwrites."""
    user, project_id = await _mk(db_session, "w90c@rvaiglobal.com")
    manager = SessionManager()
    client = _RecordingClient()  # no attach_handle → the birth arm

    async def redis_is_having_a_day(*_a: object, **_k: object) -> None:
        raise RedisError("heartbeat seed blew up")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("src.services.build_sessions.manager.write_heartbeat", redis_is_having_a_day)
        with pytest.raises(RedisError):
            await manager.ensure_sandbox(
                db_session, user, project_id, sandbox_client=client, may_write=True
            )

    assert client.attached == [], "this test must ride the CREATE arm"
    assert client.provisioned == client.torn_down, "a container we created must be rolled back"
    assert client.torn_down != []


# --- the terminal ------------------------------------------------------------


async def test_a_recovery_copy_leaves_the_save_button_exactly_where_it_was(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE POINT OF THE SEPARATE KEY. `dirty` is computed against the SAVED bundle, so a
    recovery copy landing on `snapshot_key` would make `_saved_head` match the container, flip
    `dirty` to False, and take the Save button away — the user's unsaved work would be reported
    as saved, by a write they never asked for.

    Mutation-check: change `_write_recovery_copy` to target `snapshot_key` and this goes red."""
    user, project_id = await _mk(db_session, "w6s@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "c" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle

    await manager.finish_turn_sandbox(session, client, touched=True)

    state = await manager.project_save_state(db_session, user, project_id, sandbox_client=client)
    assert state.dirty is True, "the recovery copy was mistaken for a save"
    assert state.saved_head is None, "nothing the user asked to save has been saved"


async def test_work_from_after_the_last_save_is_offered_back(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE HONEST RESTORE. The user saves, keeps working, then the container dies. Relaunch
    would otherwise restore the SAVED bundle and present the app as healthy at an older state —
    the loss made invisible by the recovery affordance itself. This is the signal that lets the
    portal ask instead."""
    user, project_id = await _mk(db_session, "w6v@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "d" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    await manager.finish_turn_sandbox(session, client, touched=True)

    # The user saves, between turns, which is when the Save button is reachable...
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    # That turn's recovery copy is the same tree and older: nothing to offer back yet.
    assert await manager.recoverable_work(session.app_id) is None

    # ...then keeps working — the tree MOVES — and that turn's recovery copy lands after it.
    # The head must actually change: an identical tree is correctly not "work to recover",
    # however new its bundle is.
    _with_head(client, "d2" + "d" * 38)
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(second, client, touched=True)

    offer = await manager.recoverable_work(session.app_id)
    assert offer is not None, "work newer than the save was not offered back"
    assert offer.app_id == session.app_id
    assert offer.written_at is not None


async def test_a_save_newer_than_the_recovery_copy_offers_nothing(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The ordinary case, and it must stay quiet. A user who just saved has nothing to be asked
    about — prompting there would train them to dismiss the prompt that matters."""
    user, project_id = await _mk(db_session, "w6w@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "e" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle

    await manager.finish_turn_sandbox(session, client, touched=True)  # recovery copy first
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)

    assert await manager.recoverable_work(session.app_id) is None


async def test_nothing_is_offered_when_there_is_no_recovery_copy(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Fails CLOSED. Every unknown — no copy, an unreadable store, a missing timestamp — reads
    as "nothing to offer", because promising work we cannot produce is worse than silence."""
    user, project_id = await _mk(db_session, "w6x@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert await manager.recoverable_work(session.app_id) is None


async def test_a_reaped_container_comes_back_with_the_work_not_the_last_save(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE ONE THAT MATTERS. The user saves tree A, works on to tree B, and their container
    is reclaimed. They then do the only thing the product offers: send another message.

    The restore arm used to pull `snapshot_key` unconditionally, so that message rebuilt their
    app from A — and that same turn's recovery write then overwrote the recovery bundle with A.
    Tree B existed nowhere: the copy this whole mechanism writes survived exactly one turn.

    Mutation-check: pass `source_key=None` in `_restore_or_provision` and this goes red."""
    user, project_id = await _mk(db_session, "w6z@rvaiglobal.com")
    manager = SessionManager()

    client = _with_head(FakeSandboxClient(), "a" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    await manager.finish_turn_sandbox(session, client, touched=True)
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)

    _with_head(client, "b" * 40)  # work continues past the save, on the next turn
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(second, client, touched=True)

    # The container is reclaimed.
    await fake_redis.delete(registry_key(user.id))
    resumed = _with_head(FakeSandboxClient(), "b" * 40)
    resumed.attach_handle = None

    # The next message resumes from the RECOVERY bundle, not the older saved one.
    session2 = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=resumed, may_write=True
    )
    assert resumed.restored_from == [recovery_key(session2.app_id)]

    # ...and the user's save state is untouched: this was a resumption, not a promotion.
    state = await manager.project_save_state(db_session, user, project_id, sandbox_client=resumed)
    assert state.dirty is True, "resuming must not read as saved"


async def test_relaunch_puts_the_saved_version_back_only_when_asked(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The read side, both directions. Relaunch resumes the newest tree by default; going back
    to the last saved version is the explicit request, because restoring an older tree over a
    newer one is the direction that costs the user work."""
    user, project_id = await _mk(db_session, "w7a@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "f" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    await manager.finish_turn_sandbox(session, client, touched=True)

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    _with_head(client, "f2" + "f" * 38)  # the tree moves on past the save
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(second, client, touched=True)  # newer work lands
    assert await manager.recoverable_work(session.app_id) is not None

    await fake_redis.delete(registry_key(user.id))
    client.attach_handle = None
    await manager.relaunch_preview(db_session, user, project_id, client)
    assert client.restored_from[-1] == recovery_key(session.app_id)

    await fake_redis.delete(registry_key(user.id))
    client.attach_handle = None
    await manager.relaunch_preview(db_session, user, project_id, client, prefer_saved=True)
    assert client.restored_from[-1] is None, "the user asked for their saved version"


async def test_a_user_who_never_saved_can_still_get_their_work_back(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A citizen developer builds across several turns and never clicks Save — the expected
    behaviour for a non-developer, not an edge case. The relaunch gate checked `snapshot_key`
    alone, so they were told to "build the app first" while save-state reported their work
    existed."""
    user, project_id = await _mk(db_session, "w7b@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "c" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    await manager.finish_turn_sandbox(session, client, touched=True)  # never saved

    assert snapshot_key(session.app_id) not in fake_storage.objects
    await fake_redis.delete(registry_key(user.id))
    client.attach_handle = None

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)
    assert relaunched.app_id == session.app_id
    assert client.restored_from[-1] == recovery_key(session.app_id)


async def test_a_same_second_tie_resumes_the_newer_work_not_the_save(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ FOUND BY A LIVE RUN, not by this suite. Azure stamps `last_modified` in WHOLE
    SECONDS, so a Save and a turn-boundary write inside one second compare EQUAL — and a
    strict `>` resolved that to "the save wins", restoring the older tree over the user's
    newer work. This suite could not see it: `FakeStorage` stamps microseconds, so its writes
    never tie.

    Pinned here at the store's real resolution by forcing the stamps equal."""
    user, project_id = await _mk(db_session, "wtie@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    await manager.finish_turn_sandbox(session, client, touched=True)

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    _with_head(client, "b" * 40)  # the tree moves on, on the next turn
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(second, client, touched=True)

    # Azure's resolution: both writes land in the same second.
    tie = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    fake_storage.mtimes[snapshot_key(session.app_id)] = tie
    fake_storage.mtimes[recovery_key(session.app_id)] = tie

    assert await manager.newest_restore_source(session.app_id) == recovery_key(session.app_id)
    assert await manager.recoverable_work(session.app_id) is not None


async def test_an_unchanged_tree_is_not_offered_however_new_its_bundle_is(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`touched` means "a mutating tool ran", not "the tree changed". Ordering by time alone
    would claim work that does not exist — permanently, and while `dirty` is False. The stamped
    HEAD is what settles it.

    The newer bundle is placed directly here (`finish_turn_sandbox`'s guarded write now skips
    rewriting an unchanged recovery bundle, see `test_finish_turn.py`), but `recoverable_work`'s
    guard still has to hold: the recovery slot has other writers — the operator promote among
    them — and a newer object over an identical tree is still not work to recover."""
    user, project_id = await _mk(db_session, "wsame@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle
    await manager.finish_turn_sandbox(session, client, touched=True)

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    # A later write of the SAME tree into the recovery slot: a newer object, no new work.
    await fake_storage.put(
        recovery_key(session.app_id), a_git_bundle("a" * 40), metadata={"head_sha": "a" * 40}
    )

    saved = await fake_storage.head(snapshot_key(session.app_id))
    recovery = await fake_storage.head(recovery_key(session.app_id))
    assert saved is not None and recovery is not None
    assert saved.last_modified is not None and recovery.last_modified is not None
    assert recovery.last_modified > saved.last_modified  # setup: strictly newer bundle

    assert await manager.recoverable_work(session.app_id) is None
    assert await manager.newest_restore_source(session.app_id) is None


async def test_a_recovery_write_that_fails_outright_is_alarmed_not_swallowed_silently(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The third way a turn's work fails to reach a durable copy, and the only one the call
    site can see. The swallow stays — a safety net that can fail a turn is not a safety net —
    but it is no longer SILENT: a write that never landed used to leave no trace an operator
    would ever look for, making it unfalsifiable whether the platform had failed to CHECK the
    workspace or failed to make it DURABLE.

    Mutation check: drop the event back to a `warning` with a prose message and this goes red."""
    user, project_id = await _mk(db_session, "wboom@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "f" * 40)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    async def boom(*args: object, **kwargs: object) -> None:
        raise StorageError("the upload did not complete")

    monkeypatch.setattr(manager_module, "write_recovery_copy", boom)
    raised: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        manager_module._log, "error", lambda event, **kw: raised.append((event, kw))
    )

    # The turn still ends cleanly — that is the half that must not regress.
    await manager.finish_turn_sandbox(session, client, touched=True)

    assert [event for event, _ in raised] == [RECOVERY_WRITE_DID_NOT_LAND_EVENT]
    assert raised[0][1]["reason"] == "failed"
    assert raised[0][1]["app_id"] == str(session.app_id)


# --------------------------------------------------------------------------------------
# The asking is unconditional, and EXACTLY TWO EXITS WIDENED
# --------------------------------------------------------------------------------------
#
# The unit's own framing: "an implementer who reads 'always ask' as 'delete the silent path'
# produces five bugs at once." Four of the guard's other exits are not "another project holds it"
# at all — they are "NOTHING IS BEING TAKEN" — and the fifth is a ghost registry entry with no
# project to name, so a dialog there would render a blank where the name goes.
#
# The three tests above pin the two exits that DID widen. These pin the ones that must not.


async def test_starting_the_app_that_already_holds_the_workspace_raises_nothing(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE FIRST EXIT THAT MUST NOT CHANGE, and the most common press in the product: a citizen
    presses start on the project whose container is already up.

    "The live sandbox is already the one we want" means nothing is being taken — there is no other
    project, no hand-over and nothing to ask about. Widening this turns every ordinary reattach
    into a dialog about the project you are already in."""
    user, project_a = await _mk(db_session, "w94-same@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)

    first = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle

    # No refusal: same user, same project, same container.
    await manager.reclaim_preflight(db_session, user, project_a, sandbox_client=client)


async def test_no_live_container_at_all_raises_nothing(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE SECOND EXIT THAT MUST NOT CHANGE. No registry entry means nothing is live, so a start is
    an ordinary cold start. A dialog here would be a dialog about nothing — and it would fire on
    the first press of every project in the product."""
    user, project_a = await _mk(db_session, "w94-cold@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    await manager.reclaim_preflight(db_session, user, project_a, sandbox_client=client)


async def test_a_ghost_registry_entry_raises_nothing_because_it_has_no_project_to_name(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE SHARP EXIT — the one the unit singles out. The registry names a container whose app maps
    to no project this user owns: a leftover the reconcile will clear.

    The dialog is required to NAME the project being stopped. There is no project here, so
    widening this exit renders a dialog with a blank where the name goes — worse than the silence
    it replaced, because it asks a person to make a decision about something it cannot describe."""
    user, project_a = await _mk(db_session, "w94-ghost@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    # A registry entry for an app name that belongs to nothing this user owns.
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            "state": "ready",
            "app_name": app_name_for(uuid.uuid4()),
            "fqdn": "ghost.example",
            "token_ref": "tok",
            "created_at": datetime.now(UTC).isoformat(),
        },
    )

    await manager.reclaim_preflight(db_session, user, project_a, sandbox_client=client)


async def test_an_unreadable_registry_still_propagates_rather_than_being_swallowed(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The widening must not turn a DELIBERATELY unguarded read into a swallowed one.

    `read_registry` is one of the answer-bearing primitives kept bare on purpose: swallowing a
    `RedisError` here would manufacture a certain-looking answer out of an ambiguous store — a
    phantom "no sandbox" that permits a teardown. It has to reach the routers' 503 seam, which is a
    true statement, rather than becoming a reclaim dialog or a silent pass."""
    user, project_a = await _mk(db_session, "w94-redis@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    async def the_registry_will_not_answer(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise RedisError("connection reset")

    # `monkeypatch.setattr` rather than a hand-rolled save/restore: it undoes itself at teardown
    # even if the assertion below raises, and it is the one form the static gates accept for
    # replacing a bound method on a client object.
    monkeypatch.setattr(fake_redis, "hgetall", the_registry_will_not_answer)

    with pytest.raises(RedisError):
        await manager.reclaim_preflight(db_session, user, project_a, sandbox_client=client)


async def test_neither_refusal_code_reaches_a_citizens_own_second_project(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`build_session_already_active` and `sandbox_reclaim_blocked` shared a 409 and one
    audience: a citizen being asked to arbitrate their own workspace. BOTH are gone from this
    path, and asserting the pair together is the point — the slot claim raised the first ABOVE
    the guard that raised the second, so retiring either alone leaves the citizen refused by the
    other with no way to tell which.

    Mutation-check: restore either arm and exactly one of these two calls raises."""
    user, project_a = await _mk(db_session, "w94-codes@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "9" * 40)

    session = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    client.attach_handle = session.handle

    # An agent is mid-write in A — the state that used to raise `sandbox_reclaim_blocked` here
    # and `build_session_already_active` one line into the start.
    await manager.reclaim_preflight(db_session, user, project_b, sandbox_client=client)
    started = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert started.project_id == project_b
