"""U5 — the WRITE turn's sandbox lifecycle: `ensure_sandbox` / `finish_turn_sandbox`.

A Write turn allocates everything a build allocates (container, one-per-user lock, registry
entry, heartbeat) and none of what a build runs (the `run_build` task, the `build_started`
marker, attachments). These tests pin both halves of that sentence.

The starred one is `test_the_write_turn_terminal_actually_saves_the_work`. It is the P0 this
whole commit exists for: `write_snapshot` is the only thing that ever pushes the sandbox tree
to Blob storage, and before U5 the only caller was the build harness's `_do_finalize`. A Write
turn running on the chat engine with no equivalent save point would report success, show a
correct preview, and lose every edit to the next reaper sweep — silently, with nothing in any
log to say so.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.user import User
from src.services.build_sessions.locks import (
    heartbeat_is_alive,
    lock_is_held,
    read_registry,
    stay_of_execution_is_current,
)
from src.services.build_sessions.manager import (
    BuildSessionConflictError,
    NoLiveSandboxError,
    SandboxReclaimBlockedError,
    SessionManager,
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
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain, FakeSandboxClient, FakeStorage


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


# --- attach ------------------------------------------------------------------


async def test_ensure_sandbox_allocates_a_build_worth_of_state_without_the_build(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "w1@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

    # Everything a build would hold, because the reaper cannot tell the two apart.
    assert client.provisioned == [app_name_for(session.app_id)]  # fresh project -> provision
    assert manager.active_session_for(user.id) is session
    assert await lock_is_held(fake_redis, user.id) is True
    assert await heartbeat_is_alive(fake_redis, user.id) is True
    assert await read_registry(fake_redis, user.id) is not None

    # And nothing a build would run.
    assert session.task is None
    assert session.prompt == ""
    assert session.attachments == []
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
        db_session, user, project_id, sandbox_client=FakeSandboxClient()
    )
    after = await db_session.scalar(
        sa.select(AppRegistry.id).where(AppRegistry.user_id == user.id)
    )
    assert after == session.app_id


async def test_a_second_write_attach_while_one_is_live_is_a_conflict(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # One sandbox per user, whoever is asking. A Write turn and a build compete for the same
    # slot because they consume the same container budget and the same Redis lock.
    user, project_id = await _mk(db_session, "w3@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

    with pytest.raises(BuildSessionConflictError) as caught:
        await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    assert caught.value.session_id == first.session_id


async def test_a_build_cannot_start_over_a_live_write_sandbox(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The other direction of the same slot. Without the shared claim, a build would provision
    # a second container for a user who already has one and orphan whichever lost the race.
    user, project_id = await _mk(db_session, "w4@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    live = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

    with pytest.raises(BuildSessionConflictError) as caught:
        await manager.start(
            db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
        )
    assert caught.value.session_id == live.session_id


def _with_head(client: FakeSandboxClient, sha: str) -> FakeSandboxClient:
    """Script a container that is AT `sha` and bundles to it.

    Both halves matter. `git rev-parse HEAD` is what the dirty comparison reads on the
    container side; the base64 read is what `write_snapshot` uploads, and the saved head is
    parsed back OUT of those bytes — so a fake that returns an empty bundle makes every save
    look like it stored nothing parseable, and `dirty` could never settle."""
    bundle = base64.b64encode(b"# v2 git bundle\n" + sha.encode() + b" HEAD\n\nPACK").decode()

    def handler(cmd: list[str]) -> ExecResult:
        # The state probe: `<head>@@<porcelain>`. A clean tree at `sha`.
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            # `<head>@@<porcelain>@@<commit count>`. The count is 3, not 1: 1 is the
            # `bial: golden template baseline` every fresh provision seeds, and a container
            # sitting on the baseline alone is deliberately reclaimable. A test that means
            # "this workspace holds work" has to say so.
            return ExecResult(stdout=f"{sha}\n@@@@3", stderr="", exit=0)
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
    """★ THE SAVE MODEL (KTD-5e). This used to snapshot at every turn terminal, which quietly
    took the decision away from the user: every message became a new saved version, so there
    was no such thing as trying something and walking away from it. The agent commits inside
    the container as it works; the bundle is pushed only when the user asks.

    Mutation-check: put the `write_snapshot` call back in `finish_turn_sandbox` and this goes
    red."""
    user, project_id = await _mk(db_session, "w6@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
    """★ The bug this arm exists for. The golden template ships NO `.git` — `write_snapshot`
    runs `git init` itself — so `git rev-parse HEAD` fails on every brand-new project. Read as
    "unknown" that hid the Save button on exactly the projects that most need it: the user
    builds their first app and has no way to keep it."""
    user, project_id = await _mk(db_session, "w6f@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()  # default exec: exit 0, empty stdout -> no head, clean tree
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = session.handle

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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
    # The one place the Write path diverges from `_do_finalize` rather than omitting from it.
    # A build's container is scaffolding and survives only a clean success; a Write turn's
    # container IS the preview on screen, and the turn ending is not a reason for the user's
    # app to go dark mid-sentence.
    user, project_id = await _mk(db_session, "w7@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

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
    """★ THE COST OF A MESSAGE. A sandbox used to exist only for the length of a build, so
    reconcile-then-allocate ran once per build and nobody felt it. Write is a chat mode now:
    every message allocates, and that same rule tore down a HEALTHY container and rebuilt it
    from the snapshot every single time — a blocking container delete, a blocking create, an
    image pull and a bundle restore, to arrive back where it already was. The user watched
    "Getting your workspace ready…" on every message while their app sat there running.

    Mutation-check: drop the `spare_app` guard in `_holding_user_lock` and this goes red —
    `torn_down` gains the first container and `restored` gains a second entry."""
    user, project_id = await _mk(db_session, "w10@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    first = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=True)  # pardoned: container stays up
    client.attach_handle = first.handle  # the live container is attachable, as in production

    second = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

    assert second.app_id == first.app_id
    assert client.torn_down == []  # the healthy container was NOT destroyed
    assert client.restored == []  # and nothing was rebuilt from the snapshot
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first message


async def test_a_different_project_never_steals_the_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The other half, and the reason the spare is keyed on the APP NAME rather than merely
    "something is live". Attaching to whatever container happened to be up would hand project B
    project A's code. The ghost hazard the reconcile exists for stays closed.

    RE-CUT FOR #83 (was `…_still_reaps_rather_than_stealing_the_container`). The claim above is
    unchanged and still the point: B must never inherit A's workspace. What changed is the
    alternative. Refusing to STEAL the container never implied a licence to DESTROY it, but
    that is what the code did — silently, inside B's request, taking A's unsaved work with it.
    A's container survives now; the destruction moved to `release_project_sandbox`, which the
    user reaches through a prompt. See the refusal tests below."""
    user, project_a = await _mk(db_session, "w11@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    # COMMITTED work: a bare fake is a pristine template, which is deliberately reclaimable
    # (see `test_a_plan_only_project_does_not_block_a_real_one`). The refusal is about work.
    client = _with_head(FakeSandboxClient(), "a" * 40)

    first = await manager.ensure_sandbox(db_session, user, project_a, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle

    with pytest.raises(SandboxReclaimBlockedError) as caught:
        await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=client)

    assert caught.value.project_id == project_a  # names the project holding the slot
    assert client.torn_down == []  # A's container is NOT destroyed to make room
    assert app_name_for(first.app_id) not in client.restored


async def test_a_clean_incumbent_is_reclaimed_without_a_prompt(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Only unsaved work earns an interruption. With A saved there is nothing to lose, so the
    switch stays silent and costs the user nothing — the reclaim itself was never the bug."""
    user, project_a = await _mk(db_session, "w12@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = FakeSandboxClient()

    first = await manager.ensure_sandbox(db_session, user, project_a, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle
    # Saved AND unchanged since: the container's HEAD is the bundle's, so `dirty` is False.
    _with_head(client, "e" * 40)
    await manager.save_project_snapshot(db_session, user, project_a, sandbox_client=client)

    second = await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=client)

    assert second.app_id != first.app_id
    assert client.torn_down == [app_name_for(first.app_id)]  # reclaimed, as before
    assert app_name_for(second.app_id) in client.provisioned


async def test_releasing_the_incumbent_lets_the_switch_through(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The way out of the refusal: the user gives up A explicitly, then B starts. This is the
    same teardown as before — it is now something they did, not something done to them."""
    user, project_a = await _mk(db_session, "w13@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "b" * 40)  # committed work, so the refusal fires

    first = await manager.ensure_sandbox(db_session, user, project_a, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = first.handle
    with pytest.raises(SandboxReclaimBlockedError):
        await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=client)

    released = await manager.release_project_sandbox(
        db_session, user, project_a, sandbox_client=client
    )

    assert released is True
    assert client.torn_down == [app_name_for(first.app_id)]  # released on the user's say-so
    client.attach_handle = None
    second = await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=client)
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

    first = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = first.handle
    # THE USER SAVES. Nothing else writes the bundle, so without this click there would be
    # nothing for the next turn to restore — which is the save model working as specified.
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = None  # the container is gone; only the saved bundle is left

    second = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    assert second.app_id == first.app_id  # same project -> same app
    assert client.restored == [app_name_for(second.app_id)]  # RESTORED
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first attach


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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = session.handle

    async def boom(*_a: object, **_k: object) -> None:
        raise StorageError("blob is having a day")

    monkeypatch.setattr("src.services.build_sessions.manager.write_snapshot", boom)

    with pytest.raises(StorageError):
        await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)

    # And nothing was recorded as saved, so the dirty indicator keeps telling the truth.
    state = await manager.project_save_state(db_session, user, project_id, sandbox_client=client)
    assert state.dirty is True


# --- #83 follow-up: autosave to the recovery slot ------------------------------------


async def test_a_finished_write_turn_autosaves_to_recovery_not_over_the_saved_bundle(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE WHOLE POINT OF THE SEPARATE KEY. `finish_turn_sandbox` writes the platform's safety
    net so a crash, a closed laptop or the idle reaper stops costing a whole session — while
    `snapshot_key` stays exactly what the user last chose to save.

    Point the autosave at `snapshot_key` and this goes red twice over: KTD-5e is reversed (every
    message becomes a saved version again) and the assertion below that the user's bundle is
    untouched fails outright."""
    user, project_id = await _mk(db_session, "w14@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "f" * 40)

    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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

    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

    def explode(_cmd: list[str]) -> ExecResult:
        raise SandboxError("the container stopped answering")

    client.exec_handler = explode
    await manager.finish_turn_sandbox(session, client, touched=True)  # must not raise

    assert recovery_key(session.app_id) not in fake_storage.objects
    assert manager.active_session_for(user.id) is None  # the slot was freed anyway


async def test_a_plan_only_project_does_not_block_a_real_one(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A QUESTION IS NOT WORK. Found in live testing, and it made the guard worse than the bug.

    `_pin_workspace` attaches the container for every mode, so typing one Plan prompt into a
    brand-new project takes the one-per-user workspace. That container is the untouched golden
    template — nothing written, nothing committed, nothing saved — but `dirty` is True for it,
    because `_save_state_of` answers the Save button's question and a never-built project must
    show a Save button. Read as "unsaved changes" it locked the user out of the project that
    held their actual app, to protect an empty template.

    Flip any of the four conditions and this must go red: a commit BEYOND the seeded baseline,
    a tree dirty with anything outside `_FRAMEWORK_CHURN`, a saved bundle, or a recovery
    snapshot each mean there IS something to lose. Note it is not "no commits" — the sandbox
    client seeds `bial: golden template baseline` at birth, so a pristine container has exactly
    one and a no-commits check would never fire."""
    user, project_a = await _mk(db_session, "w17@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _pristine(FakeSandboxClient())

    plan_only = await manager.ensure_sandbox(db_session, user, project_a, sandbox_client=client)
    await manager.finish_turn_sandbox(plan_only, client, touched=False)  # a read-only turn
    client.attach_handle = plan_only.handle

    real = await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=client)

    assert real.app_id != plan_only.app_id  # the switch went through, no refusal
    assert client.torn_down == [app_name_for(plan_only.app_id)]


async def test_a_committed_but_unsaved_workspace_still_blocks(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The other side of the same line, so the exemption above cannot quietly widen into
    "never-saved projects are always disposable". A commit in the container IS work — it is
    what a Write turn leaves behind — and losing it is the whole point of #83."""
    user, project_a = await _mk(db_session, "w18@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "c" * 40)  # committed, never saved

    first = await manager.ensure_sandbox(db_session, user, project_a, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=False)
    client.attach_handle = first.handle

    with pytest.raises(SandboxReclaimBlockedError):
        await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=client)
    assert client.torn_down == []


# --- the guard's UNKNOWN arms (#83 review, findings 4 and 5) ----------------------
#
# Every `return` inside `_refuse_if_reclaim_would_destroy_work` lets the teardown below
# proceed, so each one asserts "nothing will be lost". These two pin the arms where the
# honest answer is "I could not tell" — which the first cut answered by reclaiming, i.e.
# #83 again with a rarer trigger.


class _UnreachableAttach(FakeSandboxClient):
    """A container the registry still names READY, whose attach cannot CONFIRM anything.

    `SandboxNotReadyError`, not `SandboxGoneError`: the real client draws that line itself
    ("a container ARM confirms is gone has nothing to lose... a container we merely cannot
    authenticate to right now must NOT be destroyed over a transient control-plane failure"),
    and the guard has to honour it rather than treat every failure as an absence."""

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        raise SandboxNotReadyError("supervisor unreachable but the container still exists")


async def test_an_unreachable_incumbent_refuses_rather_than_reclaiming(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A transient ARM blip must not become a licence to destroy a container.

    Attach failing tells us nothing about whether work is in there — a cold container and one
    holding a day's edits look identical from here. The refusal carries `dirty=None`, which the
    error, the 409 envelope and the dialog all already read as "may have unsaved changes".

    The user is NOT wedged by this: *Switch without saving* calls `release`, whose teardown goes
    through `reap_user` and needs only the registry entry, never an attach."""
    user, project_a = await _mk(db_session, "w19@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "d" * 40)

    first = await manager.ensure_sandbox(db_session, user, project_a, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=True)

    # Same registry, same live app — only the attach stops answering.
    blind = _with_head(_UnreachableAttach(), "d" * 40)
    with pytest.raises(SandboxReclaimBlockedError) as caught:
        await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=blind)

    assert caught.value.project_id == project_a  # still names the project, so the copy works
    assert caught.value.dirty is None  # UNKNOWN, never a guessed "clean"
    assert blind.torn_down == []  # and above all: nothing was destroyed


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

    first = await manager.ensure_sandbox(db_session, user, project_a, sandbox_client=client)
    await manager.finish_turn_sandbox(first, client, touched=True)
    client.attach_handle = None  # ARM says it is gone

    real = await manager.ensure_sandbox(db_session, user, project_b, sandbox_client=client)
    assert real.app_id != first.app_id  # no refusal — the switch went through


async def test_a_failed_provision_leaks_neither_lock_nor_slot(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # RENAMED from "failed_attach", which it never tested: it scripts `provision_new` to raise,
    # i.e. a failure on the CREATE arm, before any handle is assigned. That is why nothing here
    # caught #90 — no committed test took the ATTACH arm and then failed. The attach-arm
    # failures are pinned separately below.
    user, project_id = await _mk(db_session, "w5@rvaiglobal.com")
    manager = SessionManager()

    class FailingProvision(FakeSandboxClient):
        async def provision_new(self, user_id, app_name, *, app_env):
            raise SandboxError("provision blew up")

    with pytest.raises(SandboxError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=FailingProvision()
        )
    # `_holding_user_lock`'s compensation ran: nothing adopted, so nothing is held. A user
    # whose first Write turn failed to provision must not be locked out of their second.
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=FakeSandboxClient()
    )
    assert session.handle is not None


# --- #90: a failure on the ATTACH arm must not destroy the borrowed container ---
# `_resolve_sandbox` has three arms. Two CREATE a container, so compensation tearing it down is
# a genuine rollback. One ATTACHES to a container that was already serving — and the attach arm
# is the STEADY STATE for every Write message after the first, because
# `_the_live_sandbox_is_already_the_one_we_want` deliberately skips the reconcile so a second
# message does not demolish and rebuild a running app.
#
# The container's tree is the ONLY copy of everything since the user last clicked Save
# (`finish_turn_sandbox` does not snapshot — KTD-5e), so destroying it here is unrecoverable and
# silent: the preview simply stops loading and Relaunch restores the older SAVED bundle, so the
# app comes back looking healthy at an earlier state.
#
# These invert the four probes from issue #90 — they assert the container SURVIVES.


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
    first = await manager.ensure_sandbox(db, user, project_id, sandbox_client=client)
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
    """★ #90. The window between taking the handle and `scope.adopt()` holds exactly one await
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
            await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

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
            manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
            await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = session.handle

    # The user saves...
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    assert await manager.recoverable_work(session.app_id) is None  # nothing newer yet

    # ...then keeps working — the tree MOVES — and that turn's recovery copy lands after it.
    # The head must actually change: an identical tree is correctly not "work to recover",
    # however new its bundle is.
    _with_head(client, "d2" + "d" * 38)
    await manager.finish_turn_sandbox(session, client, touched=True)

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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)

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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = session.handle
    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)

    _with_head(client, "b" * 40)  # work continues past the save
    await manager.finish_turn_sandbox(session, client, touched=True)

    # The container is reclaimed.
    await fake_redis.delete(registry_key(user.id))
    resumed = _with_head(FakeSandboxClient(), "b" * 40)
    resumed.attach_handle = None

    # The next message resumes from the RECOVERY bundle, not the older saved one.
    session2 = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=resumed)
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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = session.handle

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    _with_head(client, "f2" + "f" * 38)  # the tree moves on past the save
    await manager.finish_turn_sandbox(session, client, touched=True)  # newer work lands
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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
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
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = session.handle

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    _with_head(client, "b" * 40)  # the tree moves on
    await manager.finish_turn_sandbox(session, client, touched=True)

    # Azure's resolution: both writes land in the same second.
    tie = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    fake_storage.mtimes[snapshot_key(session.app_id)] = tie
    fake_storage.mtimes[recovery_key(session.app_id)] = tie

    assert await manager.newest_restore_source(session.app_id) == recovery_key(session.app_id)
    assert await manager.recoverable_work(session.app_id) is not None


async def test_an_unchanged_tree_is_not_offered_however_new_its_bundle_is(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`touched` means "a mutating tool ran", not "the tree changed", so a read-only-ish turn
    rewrites the recovery bundle from an unchanged worktree with a newer stamp. Ordering by
    time alone then claims work that does not exist — permanently, and while `dirty` is False.
    The stamped HEAD is what settles it."""
    user, project_id = await _mk(db_session, "wsame@rvaiglobal.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)
    session = await manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client)
    client.attach_handle = session.handle

    await manager.save_project_snapshot(db_session, user, project_id, sandbox_client=client)
    await manager.finish_turn_sandbox(session, client, touched=True)  # same tree, later stamp

    saved = await fake_storage.head(snapshot_key(session.app_id))
    recovery = await fake_storage.head(recovery_key(session.app_id))
    assert saved is not None and recovery is not None
    assert saved.last_modified is not None and recovery.last_modified is not None
    assert recovery.last_modified > saved.last_modified  # setup: strictly newer bundle

    assert await manager.recoverable_work(session.app_id) is None
    assert await manager.newest_restore_source(session.app_id) is None
