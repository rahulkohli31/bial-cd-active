"""The one shutdown routine: claim, stop, write back, destroy — by name and by instance.

WHAT THESE TESTS ARE ACTUALLY ABOUT. The routine runs detached, minutes long, while the citizen's
next project starts underneath it and rewrites every user-keyed thing it might reach for. So the
subjects here are the four ways that goes wrong: a debt taken and then lost, a teardown that
touches the incoming container's coordination state, a delete aimed at a container somebody
reopened in the meantime, and an owed deletion that costs a citizen their next project.

The first test in the file is the slot-leak regression, and it is first on purpose: the row is a
release handle, and a routine that raises above the line publishing it leaks a container forever.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import dataclasses
import inspect
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog.testing
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.message import MessageEntryKind, MessageVisibility
from src.db.models.pending_teardown import PendingTeardown
from src.db.models.user import User
from src.services.build_sessions import app_name_for
from src.services.build_sessions import shutdown as shutdown_module
from src.services.build_sessions.manager import SessionManager
from src.services.build_sessions.shutdown import (
    SANDBOX_DESTROYED_UNREAD_EVENT,
    OwedTeardown,
    ShutdownOutcome,
    ShutdownReason,
    claim_the_teardown_we_owe,
    run_the_shutdown,
    shut_it_down_in_the_background,
    sweep_owed_teardowns,
)
from src.services.messages.projection import TURN_TERMINAL_KIND
from src.services.redis import REGISTRY_STATE_READY, lease_key, lock_key, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    cooperative_stop_key,
)
from src.services.sandbox import SandboxHandle, SandboxNotReadyError
from src.services.sandbox.base import KIND_BUILD_SANDBOX, TAG_CREATED_AT, TAG_KIND, ExecResult
from src.services.storage import StorageError, snapshot_key
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    MessageFactory,
    ProjectFactory,
    UserFactory,
)
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle, a_sandbox_name

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: The sha the saved copy carries, the sha the container reports, and the sha the commit-then-
#: bundle step produces. Three values because the guarded write compares the first two and stamps
#: the third — one reused value would hide which of them a write actually used.
SAVED = "b" * 40
LIVE_HEAD = "a" * 40
BUNDLED = "c" * 40


class _Sandbox(FakeSandboxClient):
    """The shared double plus the one thing the shutdown path asks and the reap never did: a
    container's ARM tags, which is where its honest birthday lives."""

    def __init__(self) -> None:
        super().__init__()
        self.tags_by_name: dict[str, dict[str, str]] = {}

    async def get_app_tags(self, *, name: str) -> dict[str, str] | None:
        return self.tags_by_name.get(name)


@pytest.fixture(autouse=True)
def _a_stop_that_does_not_take_ten_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real bound is the slow-tool budget, which no test may sit through."""
    monkeypatch.setattr(shutdown_module, "_the_stop_bound", lambda: 0.2)
    monkeypatch.setattr(shutdown_module, "_TERMINAL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(shutdown_module, "_UNWIND_AFTER_THE_CUT_SECONDS", 0.05)


class _Scene:
    """One citizen, one project, one app, and the container name that follows from it."""

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        factory: SessionFactory,
    ) -> None:
        self.user_id = user_id
        self.app_id = app_id
        self.project_id = project_id
        self.factory = factory
        self.app_name = app_name_for(app_id)


@pytest.fixture
async def scene(db_session: AsyncSession) -> _Scene:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    return _Scene(
        user_id=user.id, app_id=app.id, project_id=project.id, factory=lambda: _session()
    )


def _born_at(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


async def _seed_registry(
    redis: aioredis.Redis, user_id: uuid.UUID, *, app_name: str, created_at: datetime
) -> None:
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example.io",
            REGISTRY_FIELD_TOKEN_REF: "ref-123",
            REGISTRY_FIELD_CREATED_AT: created_at.isoformat(),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_SERVING_SINCE: "",
        },
    )


def _answers_by_name[Client: _Sandbox](
    client: Client,
    app_name: str,
    *,
    head: str = LIVE_HEAD,
    bundles_to: str = BUNDLED,
    ancestry: str = "0 0",
    porcelain: str = " M page.tsx",
    born: datetime | None = None,
) -> Client:
    """A container reachable ONLY by name, answering the whole snapshot ladder.

    `attach_handle` is deliberately left `None`, so the double's `attach_existing` raises: a
    routine that reached for the per-user registry instead of the name fails loudly rather than
    quietly bundling whatever that record currently points at."""
    client.by_name[app_name] = SandboxHandle(
        fqdn=f"{app_name}.example.io",
        token="tok",
        app_name=app_name,
        preview_url=f"https://{app_name}.example.io/",
        ready=True,
    )
    client.tags_by_name[app_name] = {
        TAG_KIND: KIND_BUILD_SANDBOX,
        TAG_CREATED_AT: (born or _born_at(10)).isoformat(),
    }
    bundle = base64.b64encode(a_git_bundle(bundles_to)).decode()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            answered = ancestry if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{head}@@{porcelain}@@4@@{answered}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


def _wont_answer(app_name: str) -> _Sandbox:
    """ARM finds the container; the supervisor does not answer. A reach failure, never absence."""
    client = _Sandbox()
    client.by_name[app_name] = SandboxHandle(
        fqdn="", token="", app_name=app_name, preview_url="", ready=False
    )
    client.unreachable_by_name.add(app_name)
    return client


async def _saved_copy(store: FakeStorage, app_id: uuid.UUID, sha: str = SAVED) -> None:
    await store.put(snapshot_key(app_id), a_git_bundle(sha), metadata={"head_sha": sha})


async def _owe(
    scene: _Scene,
    *,
    instance_ref: datetime,
    conversation_id: uuid.UUID | None = None,
    app_id: uuid.UUID | None = None,
    app_name: str | None = None,
) -> OwedTeardown:
    owning = app_id or scene.app_id
    async with scene.factory() as db:
        return await claim_the_teardown_we_owe(
            db,
            user_id=scene.user_id,
            app_id=owning,
            app_name=app_name or app_name_for(owning),
            project_id=scene.project_id,
            instance_ref=instance_ref,
            conversation_id=conversation_id,
        )


async def _rows_for(scene: _Scene) -> list[PendingTeardown]:
    async with scene.factory() as db:
        return list(
            (
                await db.execute(
                    sa.select(PendingTeardown).where(PendingTeardown.user_id == scene.user_id)
                )
            )
            .scalars()
            .all()
        )


async def _write_a_terminal_row(
    scene: _Scene, conversation_id: uuid.UUID, *, seq: int = 900
) -> None:
    """The one row the routine watches for: the hidden system event a turn writes as it ends."""
    async with scene.factory() as db:
        await MessageFactory.create(
            db,
            scene.user_id,
            conversation_id,
            seq=seq,
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            visibility=MessageVisibility.HIDDEN,
            payload=[],
            meta={"kind": TURN_TERMINAL_KIND, "turnId": str(uuid.uuid4()), "status": "completed"},
        )


async def _lapse_the_claim(scene: _Scene, row_id: uuid.UUID) -> None:
    async with scene.factory() as db:
        await db.execute(
            sa.update(PendingTeardown)
            .where(PendingTeardown.id == row_id)
            .values(claimed_until=datetime.now(UTC) - timedelta(minutes=1))
        )
        await db.commit()


# =============================================================================
# The release handle
# =============================================================================


def test_the_owed_row_is_published_above_every_step_that_can_fail() -> None:
    """★ THE SLOT-LEAK RULE, APPLIED TO A NEW RESOURCE. A release handle published below a
    fallible step leaks its resource forever, and this is that shape one door along: reaching the
    container, stopping the turn, bundling the tree and the ARM delete can all raise, and a raise
    above the row means a container nothing will ever collect.

    Read off the source because that is where the rule lives — a behavioural test can only prove
    the orderings it happens to drive, and the next `await` somebody adds at the top of this
    function is exactly the one no test would have covered.

    Mutation check: move the insert below any other await in `claim_the_teardown_we_owe` and this
    goes red."""
    tree = ast.parse(inspect.getsource(claim_the_teardown_we_owe).strip())
    awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
    assert awaits, "the claim performs no await at all, which cannot be right"
    first = ast.unparse(awaits[0])
    assert "pg_insert" in first, f"the first await is not the row insert: {first}"


async def test_the_routine_blowing_up_leaves_the_debt_on_the_books(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """★ The same rule, driven: something nobody modelled raises on the routine's first reach at
    the container, and the deletion is still owed afterwards.

    Mutation check: have the detached wrapper delete the row on any exit and this goes red — the
    container is left standing with nothing naming it."""

    class _AReachThatExplodes(_Sandbox):
        async def attach_by_name(self, *, app_name: str) -> SandboxHandle:
            raise RuntimeError("something nobody modelled")

    owed = await _owe(scene, instance_ref=_born_at(20))
    client = _answers_by_name(_AReachThatExplodes(), scene.app_name)

    await shut_it_down_in_the_background(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PROJECT_SWITCHED,
        session_factory=scene.factory,
    )

    assert [row.app_name for row in await _rows_for(scene)] == [scene.app_name]
    assert client.torn_down == []


async def test_two_call_sites_on_one_request_produce_one_row(scene: _Scene) -> None:
    """Both start doors can reach the claim on a single request. A second claim for a container
    that already has one is a no-op returning the row that is there — never an integrity error a
    citizen's start would have to survive."""
    first = await _owe(scene, instance_ref=_born_at(20))
    second = await _owe(scene, instance_ref=_born_at(3))

    assert second.id == first.id
    assert second.instance_ref == first.instance_ref  # the standing claim is not overwritten
    assert len(await _rows_for(scene)) == 1


# =============================================================================
# The happy paths
# =============================================================================


async def test_a_switch_stops_the_turn_bundles_the_tree_and_destroys_the_container(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    scene: _Scene,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The whole routine on the ordinary switch: the outgoing Build turn is asked to stop, the
    tree is bundled FROM A NAME-DERIVED HANDLE, the container goes and the debt is settled.

    The double's `attach_existing` raises, so a routine reading the per-user registry could not
    have produced this bundle at all — which is the point: on a switch that record already names
    the incoming project's container."""
    conversation = await ConversationFactory.create(
        db_session, scene.user_id, project_id=scene.project_id
    )
    born = _born_at(20)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    owed = await _owe(scene, instance_ref=born, conversation_id=conversation.id)

    stopped: list[uuid.UUID] = []
    from src.services.turns import engine as engine_module

    async def _publish_and_let_the_turn_end(conversation_id: uuid.UUID) -> None:
        stopped.append(conversation_id)
        await _write_a_terminal_row(scene, conversation_id)

    monkeypatch.setattr(engine_module, "publish_cooperative_stop", _publish_and_let_the_turn_end)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PROJECT_SWITCHED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.DESTROYED
    assert stopped == [conversation.id]  # keyed by THIS conversation, never by the citizen
    meta = await fake_storage.head(snapshot_key(scene.app_id))
    assert meta is not None and (meta.metadata or {})["head_sha"] == BUNDLED
    assert client.torn_down == [scene.app_name]
    assert await _rows_for(scene) == []
    assert await fake_redis.exists(registry_key(scene.user_id)) == 0


@pytest.mark.parametrize(
    "reason", [ShutdownReason.PRESENCE_LAPSED, ShutdownReason.PAST_THE_CEILING]
)
async def test_the_lapse_and_the_ceiling_differ_only_in_the_reason_they_record(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    scene: _Scene,
    reason: ShutdownReason,
) -> None:
    """Three triggers, one implementation. Neither of these two has a turn to stop, so neither
    publishes a stop or sits through the wait — waiting on an ending that was never coming would
    add the whole stop budget to the path every sweep takes."""
    born = _born_at(200)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=reason,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.DESTROYED
    assert client.torn_down == [scene.app_name]
    assert await _rows_for(scene) == []
    assert await fake_redis.exists(cooperative_stop_key(scene.project_id)) == 0


async def test_a_clean_tree_is_destroyed_without_rewriting_the_saved_copy(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """Most departures have nothing to store. Re-stamping the copy anyway would make its own
    timestamp lie about when the citizen last did any work."""
    born = _born_at(30)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id, sha=LIVE_HEAD)
    written_at = fake_storage.mtimes[snapshot_key(scene.app_id)]
    client = _answers_by_name(_Sandbox(), scene.app_name, porcelain="")
    owed = await _owe(scene, instance_ref=born)

    assert (
        await run_the_shutdown(
            owed,
            redis=fake_redis,
            sandbox_client=client,
            reason=ShutdownReason.PRESENCE_LAPSED,
            session_factory=scene.factory,
        )
        is ShutdownOutcome.DESTROYED
    )
    assert fake_storage.mtimes[snapshot_key(scene.app_id)] == written_at
    assert client.torn_down == [scene.app_name]


# =============================================================================
# What the routine must never touch
# =============================================================================


async def test_no_key_belonging_to_the_incoming_container_is_touched(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """★ THE DEFECT A LATCHED BOOLEAN PRODUCES. The registry names the outgoing container when the
    row is claimed and the INCOMING one by the time the delete returns — a window of milliseconds
    in the janitor and of minutes here. Every Redis write re-checks, so the incoming project's
    record, lease and lock are all left exactly as they were.

    Mutation check: compute `ours` once at the top and act on it at each later write, and this
    goes red on the incoming project's registry hash being deleted."""
    incoming = a_sandbox_name("incoming")
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=incoming, created_at=_born_at(1))
    await fake_redis.set(lease_key(scene.user_id), "9999999999", ex=600)
    await fake_redis.set(lock_key(scene.user_id), "the-incoming-token", ex=900)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PROJECT_SWITCHED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.DESTROYED
    assert client.torn_down == [scene.app_name]  # the OUTGOING container, by name
    reg = await fake_redis.hgetall(registry_key(scene.user_id))
    assert reg[REGISTRY_FIELD_APP_NAME] == incoming
    assert reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_READY  # never marked `ending`
    assert await fake_redis.get(lease_key(scene.user_id)) == "9999999999"
    assert await fake_redis.get(lock_key(scene.user_id)) == "the-incoming-token"


async def test_a_project_reopened_during_the_wait_refuses_the_delete_and_drops_the_row(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """★ NAMES ARE STABLE ACROSS TEARDOWN AND RECREATE, so the name alone cannot say WHICH
    container it is. The citizen reopened this very project while the routine was waiting, and the
    container now answering is a new instance the start path owns — deleting it would take away
    the app they are looking at.

    Mutation check: drop the `instance_ref` comparison and this goes red, with the reopened
    container torn down."""
    born = _born_at(40)
    # The start path reaped the old container and registered a fresh one under the same name.
    await _seed_registry(
        fake_redis, scene.user_id, app_name=scene.app_name, created_at=_born_at(1)
    )
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PROJECT_SWITCHED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.NOT_THIS_INSTANCE
    assert client.torn_down == []
    assert await _rows_for(scene) == []  # the start path owns that name now; a retry finds it too
    assert await fake_redis.exists(registry_key(scene.user_id)) == 1


async def test_the_wait_watches_the_outgoing_conversation_and_not_the_citizen(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    scene: _Scene,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE PER-USER SIGNAL THAT LOOKS LIKE A PER-TURN ONE. The incoming project's first turn runs
    under the same citizen: it renews the liveness lease this routine would otherwise wait to see
    released, and its own ending writes a terminal row of its own. Neither is the turn being
    stopped.

    Mutation check: satisfy the wait from any terminal row this citizen has, or from the liveness
    lease, and this goes red — the routine reports the outgoing turn ended while it is still
    mid-tool-call."""
    outgoing = await ConversationFactory.create(
        db_session, scene.user_id, project_id=scene.project_id
    )
    other_project = await ProjectFactory.create(db_session, user_id=scene.user_id)
    incoming = await ConversationFactory.create(
        db_session, scene.user_id, project_id=other_project.id
    )
    born = _born_at(40)
    await _seed_registry(
        fake_redis, scene.user_id, app_name=a_sandbox_name("incoming"), created_at=_born_at(1)
    )
    await fake_redis.set(lease_key(scene.user_id), "9999999999", ex=600)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    owed = await _owe(scene, instance_ref=born, conversation_id=outgoing.id)

    from src.services.turns import engine as engine_module

    async def _the_incoming_turn_ends_instead(_conversation_id: uuid.UUID) -> None:
        await _write_a_terminal_row(scene, incoming.id)

    monkeypatch.setattr(engine_module, "publish_cooperative_stop", _the_incoming_turn_ends_instead)

    with structlog.testing.capture_logs() as logs:
        await run_the_shutdown(
            owed,
            redis=fake_redis,
            sandbox_client=client,
            reason=ShutdownReason.PROJECT_SWITCHED,
            session_factory=scene.factory,
        )

    said = [line["event"] for line in logs]
    assert "the outgoing turn ended at its boundary" not in said
    assert any("the boundary was not reached" in line for line in said)
    assert await fake_redis.get(lease_key(scene.user_id)) == "9999999999"


async def test_the_stop_is_published_against_the_conversation_on_the_row(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    scene: _Scene,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask reaches the turn through a CONVERSATION-keyed key, published before the wait
    begins. A user-keyed one would be read by whichever of this citizen's turns looked first,
    including the incoming project's fresh one."""
    outgoing = await ConversationFactory.create(
        db_session, scene.user_id, project_id=scene.project_id
    )
    elsewhere = await ConversationFactory.create(
        db_session, scene.user_id, project_id=scene.project_id
    )
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    owed = await _owe(scene, instance_ref=born, conversation_id=outgoing.id)

    standing: list[tuple[bool, bool]] = []

    async def _look_at_what_was_published(*_args: object, **_kwargs: object) -> bool:
        standing.append(
            (
                await fake_redis.exists(cooperative_stop_key(outgoing.id)) == 1,
                await fake_redis.exists(cooperative_stop_key(elsewhere.id)) == 1,
            )
        )
        return True

    monkeypatch.setattr(shutdown_module, "_wait_for_the_turn_to_end", _look_at_what_was_published)

    await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PROJECT_SWITCHED,
        session_factory=scene.factory,
    )

    assert standing == [(True, False)]
    assert client.torn_down == [scene.app_name]


def test_the_routine_never_reaches_a_container_through_the_per_user_registry() -> None:
    """★ `attach_existing` reads the record a switch overwrites, so inside this routine it names
    the INCOMING container. The ban is structural rather than a convention: there is exactly one
    reach here, it is by name, and it is built once at the top."""
    tree = ast.parse(inspect.getsource(shutdown_module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "attach_existing" not in called
    assert "attach_by_name" in called
    reaches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "attach_by_name"
    ]
    assert len(reaches) == 1


# =============================================================================
# The terminal arms
# =============================================================================


async def test_a_diverted_tree_still_loses_its_container(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """★ The guard refused to promote this tree and PARKED it under the divert key before saying
    so, which means the bytes are already safe. Sparing here would hold a container open for work
    that is already stored.

    Mutation check: spare on `DIVERTED` and this goes red — a container that reverted to its baked
    image becomes immortal, which is one of the populations the ceiling exists to bound."""
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id)
    # `0 1`: the reference exists and this tree is NOT descended from it.
    client = _answers_by_name(_Sandbox(), scene.app_name, ancestry="0 1")
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PRESENCE_LAPSED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.DESTROYED
    assert client.torn_down == [scene.app_name]
    # The saved copy is untouched, and the refused tree is parked under a key of its own.
    meta = await fake_storage.head(snapshot_key(scene.app_id))
    assert meta is not None and (meta.metadata or {})["head_sha"] == SAVED
    assert any(key != snapshot_key(scene.app_id) for key in fake_storage.objects)


async def test_a_container_that_will_not_answer_is_spared_inside_its_budget(
    fake_redis: aioredis.Redis, scene: _Scene
) -> None:
    """A probe timeout is not a death certificate. The first refusals buy another attempt, and
    what could not be read is recorded on the row for whoever takes the next one."""
    born = _born_at(10)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    client = _wont_answer(scene.app_name)
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PRESENCE_LAPSED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.SPARED
    assert client.torn_down == []
    rows = await _rows_for(scene)
    assert len(rows) == 1 and rows[0].last_error is not None
    assert await fake_redis.exists(registry_key(scene.user_id)) == 1


async def test_a_store_that_will_not_take_the_copy_lands_in_the_same_budget(
    fake_redis: aioredis.Redis, scene: _Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ A STORE OUTAGE IS SPARED ON A COUNT, NOT FOREVER.

    The container answers perfectly; it is the object store that will not take the copy. Left
    to raise, that escapes into the caller's catch-all, which never reaches the strike test —
    the claim still advances the count, but nothing ever compares it against the strikes, so an
    outage lasting longer than them is retried every sweep and the container billed for as long
    as the store stays down. The bound that exists to stop exactly that only applies if this
    lands inside it.

    Mutation check: drop the `except StorageError` arm and this goes red — the routine raises
    instead of answering SPARED, and the row carries no `last_error` because the debt was never
    kept through this path."""
    born = _born_at(10)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    client = _Sandbox()
    client.by_name[scene.app_name] = SandboxHandle(
        fqdn="", token="", app_name=scene.app_name, preview_url="", ready=True
    )
    owed = await _owe(scene, instance_ref=born)

    async def _the_store_is_down(*_args: object, **_kwargs: object) -> None:
        raise StorageError("the object store is unreachable")

    monkeypatch.setattr(shutdown_module, "write_saved_copy_under_guard", _the_store_is_down)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PRESENCE_LAPSED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.SPARED
    assert client.torn_down == [], "a container must not be destroyed over a store outage"
    rows = await _rows_for(scene)
    assert len(rows) == 1
    assert rows[0].last_error is not None and "stored" in rows[0].last_error


async def test_a_container_unreadable_past_the_strikes_is_destroyed_unread_and_alarmed(
    fake_redis: aioredis.Redis, scene: _Scene
) -> None:
    """★ SPARE AND REPORT IS A RETRY POLICY, NOT A TERMINAL STATE. A container whose supervisor is
    wedged answers nothing, forever — so a rule that spares on doubt makes the ceiling
    unenforceable against precisely the containers it exists to collect.

    Mutation check: spare unconditionally when the container cannot be read and this goes red —
    the container is never destroyed and nobody is ever told."""
    born = _born_at(10)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    client = _wont_answer(scene.app_name)
    owed = await _owe(scene, instance_ref=born)
    exhausted = dataclasses.replace(owed, attempts=shutdown_module._STRIKES_BEFORE_IT_GOES)

    with structlog.testing.capture_logs() as logs:
        outcome = await run_the_shutdown(
            exhausted,
            redis=fake_redis,
            sandbox_client=client,
            reason=ShutdownReason.PRESENCE_LAPSED,
            session_factory=scene.factory,
        )

    assert outcome is ShutdownOutcome.DESTROYED
    assert client.torn_down == [scene.app_name]
    alarm = next(line for line in logs if line["event"] == SANDBOX_DESTROYED_UNREAD_EVENT)
    assert alarm["log_level"] == "error"
    assert "could not be reached" in alarm["why"]
    assert await _rows_for(scene) == []


async def test_the_ceiling_outranks_the_strike_budget(
    fake_redis: aioredis.Redis, scene: _Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is measured on the CONTAINER's own birthday — the ARM tag, not a registry
    record that is re-stamped at every registration and dropped when a teardown fails. A
    container whose delete failed would otherwise come back looking newborn and earn another
    whole ceiling."""
    monkeypatch.setattr(shutdown_module, "the_ceiling_switch", lambda: (True, 2))
    born = _born_at(60 * 5)
    client = _wont_answer(scene.app_name)
    client.tags_by_name[scene.app_name] = {TAG_CREATED_AT: born.isoformat()}
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PAST_THE_CEILING,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.DESTROYED
    assert owed.attempts < shutdown_module._STRIKES_BEFORE_IT_GOES
    assert client.torn_down == [scene.app_name]


async def test_a_name_nothing_answers_to_settles_the_debt(
    fake_redis: aioredis.Redis, scene: _Scene
) -> None:
    """ARM is certain: no container answers to this name. The deletion is done however it
    happened, so the row goes rather than being retried forever against nothing."""
    born = _born_at(10)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=_Sandbox(),  # `by_name` is empty: nothing answers
        reason=ShutdownReason.PRESENCE_LAPSED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.ALREADY_GONE
    assert await _rows_for(scene) == []
    assert await fake_redis.exists(registry_key(scene.user_id)) == 0


async def test_a_failed_teardown_releases_the_slot_and_keeps_the_debt(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """★ ARM refused, so the container is still standing — and the citizen stops paying for that.
    The record, the lease and the lock all go, because holding them would spend this citizen's one
    workspace on a failure of ours. The row keeps the deletion.

    Mutation check: keep the registry here the way a reap's failure arm used to and this goes red
    on the freed slot."""
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await fake_redis.set(lease_key(scene.user_id), "9999999999", ex=600)
    await fake_redis.set(lock_key(scene.user_id), "a-dead-token", ex=900)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    client.teardown_error = SandboxNotReadyError("ARM said no")
    owed = await _owe(scene, instance_ref=born)

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PRESENCE_LAPSED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.STILL_OWED
    assert await fake_redis.exists(registry_key(scene.user_id)) == 0
    assert await fake_redis.exists(lease_key(scene.user_id)) == 0
    assert await fake_redis.exists(lock_key(scene.user_id)) == 0
    rows = await _rows_for(scene)
    assert len(rows) == 1 and rows[0].last_error is not None


async def test_a_row_that_does_not_name_one_of_our_containers_is_refused_and_dropped(
    fake_redis: aioredis.Redis, scene: _Scene
) -> None:
    """The last check before an ARM delete. A row naming something this platform could not have
    minted describes a deletion nobody should perform — and keeping it would only re-refuse
    forever."""
    owed = await _owe(scene, instance_ref=_born_at(10), app_name="pub-somebody-elses-app")
    client = _Sandbox()

    outcome = await run_the_shutdown(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PRESENCE_LAPSED,
        session_factory=scene.factory,
    )

    assert outcome is ShutdownOutcome.NOT_THIS_INSTANCE
    assert client.torn_down == []
    assert await _rows_for(scene) == []


# =============================================================================
# The sweep
# =============================================================================


async def test_the_sweep_runs_an_owed_row_whose_claim_has_lapsed(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    owed = await _owe(scene, instance_ref=born)
    await _lapse_the_claim(scene, owed.id)

    result = await sweep_owed_teardowns(fake_redis, client, session_factory=scene.factory)

    assert (result.settled, result.still_owed, result.failed) == (1, 0, 0)
    assert client.torn_down == [scene.app_name]
    assert await _rows_for(scene) == []


async def test_the_sweep_walks_past_a_row_a_routine_is_still_working_on(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """★ THE CLAIM IS THE WHOLE MUTUAL EXCLUSION — a conditional update on `claimed_until`, no
    advisory lock. A routine mid-run holds a claim in the future, so the sweep must not start a
    second teardown of the same container.

    Mutation check: `claimed_until <= now()` is asked twice, once to list and once to claim, and
    dropping EITHER alone leaves the other holding the line — dropping both goes red on a second
    ARM delete. The atomicity of the claim itself is pinned at the statement in
    `tests/db/test_pending_teardown.py`, where two claimants can actually race."""
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    await _owe(scene, instance_ref=born)  # freshly claimed: the window is still open

    result = await sweep_owed_teardowns(fake_redis, client, session_factory=scene.factory)

    assert (result.settled, result.still_owed, result.failed) == (0, 0, 0)
    assert client.torn_down == []
    assert len(await _rows_for(scene)) == 1


async def test_the_sweep_counts_a_debt_it_carried_forward_apart_from_one_it_settled(
    fake_redis: aioredis.Redis, scene: _Scene
) -> None:
    """A pass that spared everything it touched is not a pass that found nothing to do."""
    born = _born_at(10)
    client = _wont_answer(scene.app_name)
    owed = await _owe(scene, instance_ref=born)
    await _lapse_the_claim(scene, owed.id)

    result = await sweep_owed_teardowns(fake_redis, client, session_factory=scene.factory)

    assert (result.settled, result.still_owed, result.failed) == (0, 1, 0)
    rows = await _rows_for(scene)
    assert len(rows) == 1
    # The claim that ran counts as an attempt, so the strike budget actually advances.
    assert rows[0].attempts == 2
    # And the row is handed a fresh window, or the very next pass would re-run a teardown that
    # has only just been tried — turning a retry schedule into a tight loop against ARM.
    assert rows[0].claimed_until > datetime.now(UTC)


# =============================================================================
# The debt never refuses a start
# =============================================================================


async def test_a_container_we_owe_a_deletion_for_does_not_hold_the_citizens_workspace(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    scene: _Scene,
    db_session: AsyncSession,
) -> None:
    """★ THE ONE WORKSPACE COUNTS SERVING CONTAINERS. An owed deletion is the platform's failure,
    and a citizen must never pay for it with their next project — least of all during the Azure
    throttle that caused it, when the rows pile up fastest.

    The incumbent here is REACHABLE AND DIRTY, which is the one shape that actually refuses: an
    unreachable or clean one is waved through by arms that were already there, so a test driving
    either would pass against no implementation at all.

    Mutation check: cap owed rows and refuse past the cap, or drop the owed-row check from the
    reclaim preflight, and this goes red — the next project cannot start."""
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    client = _answers_by_name(_Sandbox(), scene.app_name)
    client.attach_handle = client.by_name[scene.app_name]
    await _owe(scene, instance_ref=born)
    # However many more the platform owes, the answer stays the same one.
    for _ in range(3):
        other = await AppRegistryFactory.create(db_session, user_id=scene.user_id)
        await _owe(scene, instance_ref=born, app_id=other.id)

    user = await db_session.get(User, scene.user_id)
    assert user is not None
    next_project = await ProjectFactory.create(db_session, user_id=scene.user_id)

    manager = SessionManager(session_factory=scene.factory)
    await manager.reclaim_preflight(db_session, user, next_project.id)


async def test_the_background_spawn_returns_before_the_teardown_finishes(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, scene: _Scene
) -> None:
    """Nothing a citizen waits on may block on this. The spawn hands back a task that is still
    running, which is what lets a switch answer the browser while the outgoing container is still
    being taken apart."""
    born = _born_at(40)
    await _seed_registry(fake_redis, scene.user_id, app_name=scene.app_name, created_at=born)
    await _saved_copy(fake_storage, scene.app_id)
    started = asyncio.Event()
    release = asyncio.Event()

    class _ASlowTeardown(_Sandbox):
        async def teardown(self, handle: SandboxHandle) -> None:
            started.set()
            await release.wait()
            await super().teardown(handle)

    client = _answers_by_name(_ASlowTeardown(), scene.app_name)
    owed = await _owe(scene, instance_ref=born)

    task = shut_it_down_in_the_background(
        owed,
        redis=fake_redis,
        sandbox_client=client,
        reason=ShutdownReason.PROJECT_SWITCHED,
        session_factory=scene.factory,
    )
    await started.wait()
    assert not task.done()
    assert len(await _rows_for(scene)) == 1  # the debt stands until the delete lands

    release.set()
    await task
    assert client.torn_down == [scene.app_name]
    assert await _rows_for(scene) == []
