"""THE SWITCH: opening a different project starts it, and the one being left goes away quietly.

WHAT THESE TESTS ARE ABOUT. A citizen who opened a second project used to be refused twice over —
once because their own session held the one build slot, once because reclaiming the incumbent
might destroy work — and handed a dialog asking them to arbitrate their own workspace. Both
refusals are gone from both start doors, so what has to be pinned is everything that could go
wrong in their place: a start that quietly deletes the container somebody else is mid-way through
saving, two parties aimed at one container, a debt claimed twice, a citizen waiting on a teardown,
and the one refusal that must SURVIVE — a colleague's shared view, which has no hand-over.

BOTH DOORS, EVERY TIME. `relaunch_preview` is the start control and `ensure_sandbox` is the path a
first chat message takes — the only way a never-built project starts at all. A switch fixed in one
of them is a switch that still refuses the citizen who switches by typing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import sandbox_dependency, sandbox_or_none_dependency
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.pending_teardown import PendingTeardown
from src.db.models.user import User
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.manager import (
    SandboxReclaimBlockedError,
    SessionManager,
    app_name_for,
)
from src.services.build_sessions.shutdown import (
    OwedTeardown,
    ShutdownReason,
    claim_the_teardown_we_owe,
)
from src.services.redis import registry_key
from src.services.redis.keys import REGISTRY_FIELD_CREATED_AT
from src.services.sandbox.aca import AcaTransientError
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from src.services.turns.engine import ActiveTurnInfo, TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.api.v1.build_sessions.test_relaunch import RecordingAca, SupervisorScript
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage


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
def _no_turns_left_claimed() -> Iterator[None]:
    """The in-flight guard is a module-level set, so a test that marks a conversation mid-reply
    and does not clear it hands the next test a phantom running turn."""
    _mid_reply.clear()
    yield
    _mid_reply.clear()


class _Spawns:
    """Stands in for the detached shutdown routine and records what it was aimed at.

    THE ROUTINE ITSELF IS NOT UNDER TEST HERE (`test_shutdown.py` owns it), and letting the real
    one run would put a stop wait and an ARM delete inside every assertion below. What this file
    has to prove about it is narrower and entirely visible at this seam: that it is handed the
    right container, the right instance, the right turn and the right reason — and that the
    citizen's start does not wait for whatever it does next."""

    def __init__(self) -> None:
        self.owed: list[OwedTeardown] = []
        self.reasons: list[ShutdownReason] = []

    def __call__(
        self,
        owed: OwedTeardown,
        *,
        redis: aioredis.Redis,
        sandbox_client: object,
        reason: ShutdownReason,
        session_factory: object = None,
    ) -> None:
        # The task this hands back is DELIBERATELY not modelled: the start ignores it, which is
        # the property the switch depends on, and a test double that offered one to await would
        # invite a test to wait for the very thing nobody may wait for.
        self.owed.append(owed)
        self.reasons.append(reason)


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> _Spawns:
    recorder = _Spawns()
    monkeypatch.setattr(manager_module, "shut_it_down_in_the_background", recorder)
    return recorder


async def _citizen_with_two_projects(
    db: AsyncSession, email: str
) -> tuple[User, uuid.UUID, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    first = await ProjectFactory.create(db, user.id)
    second = await ProjectFactory.create(db, user.id)
    return user, first.id, second.id


def _with_head(client: FakeSandboxClient, head: str) -> FakeSandboxClient:
    """A container holding committed, unsaved work — the incumbent that used to raise the
    loudest refusal of the four, and now the one with the most to lose from a bad switch."""
    client.exec_handler = lambda cmd: _head_report(head)
    return client


def _head_report(head: str):
    from src.services.sandbox import ExecResult

    return ExecResult(stdout=f"{head}@@ M page.tsx@@4@@", stderr="", exit=0)


async def _serving(
    manager: SessionManager,
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    client: FakeSandboxClient,
) -> uuid.UUID:
    """One project up and serving with no turn running — the ordinary state a citizen leaves
    behind when they open something else."""
    session = await manager.ensure_sandbox(
        db, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=True)
    client.attach_handle = session.handle
    return session.app_id


async def _owed_rows(db: AsyncSession, user_id: uuid.UUID) -> list[PendingTeardown]:
    return list(
        (await db.execute(sa.select(PendingTeardown).where(PendingTeardown.user_id == user_id)))
        .scalars()
        .all()
    )


# --- both doors start the other project ------------------------------------------------


async def test_opening_another_project_starts_it_and_owes_the_first_one_a_teardown(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """The plain switch, through the chat door. Neither refusal fires, the incoming project
    gets its own container, and the outgoing one leaves as a debt rather than as a dialog.

    Mutation-check: restore either own-project arm and this goes red on the raise, not on an
    assertion."""
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw1@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "a" * 40)
    app_a = await _serving(manager, db_session, user, project_a, client)

    started = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert started.project_id == project_b
    assert app_name_for(started.app_id) in client.provisioned
    owed = await _owed_rows(db_session, user.id)
    assert [row.app_name for row in owed] == [app_name_for(app_a)]
    assert owed[0].project_id == project_a
    assert spawns.reasons == [ShutdownReason.PROJECT_SWITCHED]
    # NOT THE START'S JOB. The outgoing container is destroyed by the routine, after its tree is
    # written back — a delete issued here would be the second party aimed at one container.
    assert client.torn_down == []


async def test_the_start_control_switches_without_a_dialog(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    wire: SimpleNamespace,
    spawns: _Spawns,
) -> None:
    """The other door, at the wire: pressing start on a second project answers 200, not a 409 of
    either code. The old answer here was `sandbox_reclaim_blocked` naming project A."""
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw2@example.com")
    for project_id in (project_a, project_b):
        app_id = await resolve_app_for_project(db_session, user.id, project_id)
        await fake_storage.put(snapshot_key(app_id), b"BUNDLE")
    await db_session.commit()

    first = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project_a)},
        headers=auth_headers(user),
    )
    second = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project_b)},
        headers=auth_headers(user),
    )

    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert len(spawns.owed) == 1
    assert spawns.owed[0].project_id == project_a


async def test_a_first_message_on_another_project_switches_inside_the_same_request(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """The citizen who switches by TYPING, into a project that was never built.

    This is the door `reclaim_preflight` guards and `ensure_sandbox` walks through, and it is the
    only way a never-built project starts at all — so a switch that only works from the start
    control leaves this citizen refused. The debt is claimed in the same call that starts B, not
    on some later sweep: by the time the turn has a container, nothing but that row still names
    the container it displaced."""
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw3@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "b" * 40)
    app_a = await _serving(manager, db_session, user, project_a, client)

    started = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert started.app_id != app_a
    assert [row.app_name for row in await _owed_rows(db_session, user.id)] == [app_name_for(app_a)]


# --- a turn in flight ------------------------------------------------------------------


async def test_a_switch_out_of_a_build_turn_names_that_turn_rather_than_refusing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """A Build turn mid-flight used to be the refusal with no way through: the slot claim raised
    `build_session_already_active` ABOVE the reclaim preflight, so a mid-turn switch was refused
    whatever the preflight said.

    THE ROW CARRIES THE CONVERSATION, which is what makes the stop per-turn. The liveness lease
    is per user, so a user-keyed stop would be read by the incoming project's own first turn."""
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw4@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "c" * 40)
    building = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    client.attach_handle = building.handle
    thread = await ConversationFactory.create(
        db_session, user.id, project_id=project_a, kind=ChatKind.BUILD
    )
    _mid_reply.add(thread.id)

    started = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert started.project_id == project_b
    assert spawns.owed[0].conversation_id == thread.id
    assert spawns.owed[0].app_id == building.app_id
    assert client.torn_down == []


async def test_the_outgoing_turn_ending_late_does_not_take_the_incoming_workspace(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """The order a switch actually produces: B is started and holding the slot BEFORE A's turn
    finishes unwinding. A release by user id alone would hand B's live workspace away here, and
    every guard that reads that map would then answer "nothing is running" about a running turn.
    """
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw5@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "d" * 40)
    outgoing = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )
    client.attach_handle = outgoing.handle
    incoming = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    await manager.finish_turn_sandbox(outgoing, client, touched=True)

    assert manager.active_session_for(user.id) is incoming
    # The outgoing session still leaves memory, though the slot it would have released is B's.
    assert outgoing.session_id not in manager._sessions  # noqa: SLF001
    assert incoming.session_id in manager._sessions  # noqa: SLF001


async def test_a_switch_out_of_a_plan_turn_cuts_it_where_it_stands(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """A Plan turn has no tool-result boundary to stop at — it is one agent run — so waiting for
    one would delay every ordinary switch to buy an ending that was never coming. It is cancelled
    here instead, and the row carries no conversation so the routine waits on nothing.

    Mutation-check: put the conversation on the row for a read-only turn and the assertion below
    that `conversation_id is None` goes red, with the routine then sitting through the whole stop
    bound for a turn that cannot answer it."""
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw6@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    asking = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=False
    )
    client.attach_handle = asking.handle
    thread = await ConversationFactory.create(
        db_session, user.id, project_id=project_a, kind=ChatKind.PLAN
    )
    _mid_reply.add(thread.id)
    engine = _EngineThatRecordsCuts(thread.id)
    set_turn_engine_for_tests(engine)
    try:
        await manager.ensure_sandbox(
            db_session, user, project_b, sandbox_client=client, may_write=True
        )
    finally:
        set_turn_engine_for_tests(None)

    assert engine.cut == [thread.id]
    assert spawns.owed[0].conversation_id is None


class _EngineThatRecordsCuts(TurnEngine):
    """A turn engine with one running turn, which records the cut rather than cancelling a task.

    Subclassed rather than faked: `get_turn_engine`'s seam is typed to the real engine, and a
    stub that only happened to have the two methods would drift from it silently."""

    def __init__(self, conversation_id: uuid.UUID) -> None:
        super().__init__()
        self._running_in = conversation_id
        self._turn_id = uuid.uuid7()
        self.cut: list[uuid.UUID] = []

    def active_turn_info(self, conversation_id: uuid.UUID) -> ActiveTurnInfo | None:
        if conversation_id != self._running_in:
            return None
        return ActiveTurnInfo(turn_id=self._turn_id, last_seq=0)

    async def stop_turn(self, conversation_id: uuid.UUID, turn_id: uuid.UUID) -> bool:
        self.cut.append(conversation_id)
        return True


# --- what must NOT happen --------------------------------------------------------------


async def test_reopening_the_project_that_holds_the_slot_owes_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """Opening the project that is already up is not a switch, and the difference is a whole
    container: a hand-over here would claim a debt against the very container the second message
    is about to attach to, and then destroy it under the citizen."""
    user, project_a, _ = await _citizen_with_two_projects(db_session, "sw7@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "e" * 40)
    app_a = await _serving(manager, db_session, user, project_a, client)

    again = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=True
    )

    assert again.app_id == app_a
    assert spawns.owed == []
    assert await _owed_rows(db_session, user.id) == []
    assert client.torn_down == []
    assert client.provisioned == [app_name_for(app_a)]  # attached, never rebuilt


async def test_two_starts_in_one_interaction_owe_one_teardown_not_two(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """The portal presses start and sends a first message, so both doors can reach the hand-over
    for one switch — asked here as the seam itself, twice, which is the same thing and is the
    only way to stage it without racing two starts.

    `app_name` is unique on the ledger, so a second claim that wrote rather than read would be an
    integrity error: the citizen's first message would 500 behind a start that worked. The second
    call has to see the container as already leaving and say so without claiming again."""
    user, project_a, _ = await _citizen_with_two_projects(db_session, "sw8@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "f" * 40)
    app_a = await _serving(manager, db_session, user, project_a, client)

    # `spare_app=None` is what a never-built incoming project resolves to — no app row yet.
    pressed = await manager._show_the_outgoing_project_the_door(  # noqa: SLF001 - the seam itself
        db_session, user, spare_app=None, sandbox_client=client
    )
    typed = await manager._show_the_outgoing_project_the_door(  # noqa: SLF001 - the seam itself
        db_session, user, spare_app=None, sandbox_client=client
    )

    assert pressed is True
    assert typed is True, "the second door must still read the container as leaving"
    rows = await _owed_rows(db_session, user.id)
    assert [row.app_name for row in rows] == [app_name_for(app_a)]
    assert len(spawns.owed) == 1, "the second call found the debt already claimed"


async def test_a_start_beside_a_shutdown_already_running_deletes_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """A's shutdown is already mid-write-back when B's start runs — the window in which two
    parties could both be aimed at one container.

    The start takes the lock-only branch instead of reconciling. A reap-through here runs
    `certified_dead=True`, which skips every sparing arm and issues an ARM delete measured in
    tens of seconds, so B's start would BLOCK on it and destroy A's container out from under the
    write-back that is reading it. It also claims no second debt: the row already on the books is
    the claim, and a second routine would be a second delete.

    Mutation-check: drop `incumbent_is_leaving` from the lock's branch and `torn_down` names A.
    """
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw9@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "0" * 40)
    app_a = await _serving(manager, db_session, user, project_a, client)
    reg = await fake_redis.hgetall(registry_key(user.id))
    await claim_the_teardown_we_owe(
        db_session,
        user_id=user.id,
        app_id=app_a,
        app_name=app_name_for(app_a),
        project_id=project_a,
        instance_ref=datetime.fromisoformat(_text(reg[REGISTRY_FIELD_CREATED_AT])),
        conversation_id=None,
    )

    await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert client.torn_down == [], "the start issued a delete for a container being written back"
    assert spawns.owed == [], "a debt already claimed is not claimed again"
    assert len(await _owed_rows(db_session, user.id)) == 1
    # A is still reachable BY NAME, which is the only handle the write-back has left: the
    # per-user registry has moved on to B.
    assert app_name_for(app_a) in client.by_name


async def test_the_start_returns_before_the_teardown_finishes(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing the citizen waits on may block on the outgoing teardown. The routine runs for as
    long as a stop wait, a bundle read and an ARM delete take — minutes on a throttled
    subscription — and the whole point of the switch is that the citizen does not sit through
    it."""
    still_going = asyncio.Event()
    spawned: list[asyncio.Task[None]] = []

    def _slow(owed: OwedTeardown, **kwargs: object) -> asyncio.Task[None]:
        async def _wait() -> None:
            await still_going.wait()

        task = asyncio.ensure_future(_wait())
        spawned.append(task)
        return task

    monkeypatch.setattr(manager_module, "shut_it_down_in_the_background", _slow)
    user, project_a, project_b = await _citizen_with_two_projects(db_session, "sw10@example.com")
    manager = SessionManager()
    client = _with_head(FakeSandboxClient(), "1" * 40)
    await _serving(manager, db_session, user, project_a, client)

    started = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    assert started.project_id == project_b
    assert spawned and not spawned[0].done(), "the start waited for the teardown"
    still_going.set()
    await asyncio.gather(*spawned)


# --- the refusal that survives ---------------------------------------------------------


async def test_a_colleagues_shared_view_still_refuses_with_its_dialog(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    spawns: _Spawns,
) -> None:
    """The one refusal left, and it is unchanged. A shared view is somebody else's restore of
    somebody else's saved bundle — there is no tree of the recipient's to write back and no
    hand-over built for it — so taking it would simply remove a colleague's screen.

    `is_shared_view` is the discriminator the client branches on; after the switch landed, a
    refusal WITHOUT it is a backend bug rather than a state to render."""
    owner = await UserFactory.create(db_session, email="sw11-owner@example.com")
    shared_project = await ProjectFactory.create(db_session, owner.id)
    owner_app = await resolve_app_for_project(db_session, owner.id, shared_project.id)
    await db_session.commit()
    await db_session.refresh(shared_project)
    await fake_storage.put(snapshot_key(owner_app), b"BUNDLE")
    recipient = await UserFactory.create(db_session, email="sw11-recipient@example.com")
    own_project = await ProjectFactory.create(db_session, recipient.id)
    manager = SessionManager()
    client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, shared_project, client)

    with pytest.raises(SandboxReclaimBlockedError) as refusal:
        await manager.ensure_sandbox(
            db_session, recipient, own_project.id, sandbox_client=client, may_write=True
        )

    assert refusal.value.is_shared_view is True
    assert refusal.value.project_id == shared_project.id
    assert spawns.owed == [], "a shared view is refused, never handed over"
    assert client.torn_down == []


# --- reopening a name Azure may still be holding ---------------------------------------


@pytest.fixture
async def real_aca(wire: SimpleNamespace) -> AsyncIterator[SimpleNamespace]:
    """`wire`, with the canned double swapped for the real client over a recording control
    plane — the only way to reach the create ladder, which is where a name-lock is survived."""
    aca = RecordingAca()
    configured = settings.sandbox
    assert configured is not None  # the autouse fixture above binds it
    sandbox = AcaSandboxClient(
        configured,
        transport=httpx.MockTransport(SupervisorScript()),
        aca=aca,
    )
    wire.app.dependency_overrides[sandbox_dependency] = lambda: sandbox
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: sandbox
    yield SimpleNamespace(app=wire.app, manager=wire.manager, aca=aca, sandbox=sandbox)
    await sandbox.aclose()


async def test_a_refused_create_is_retried_under_the_same_name(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    real_aca: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coming back to a project whose container was just destroyed asks Azure for the name it
    has always had, and a refusal on the way in is survived by ASKING AGAIN — never by inventing
    a second name.

    ONE MECHANISM, DELIBERATELY. A suffix would work around a name-lock and leave two containers
    a citizen's registry cannot tell apart, and every later reach — the by-name teardown most of
    all — is keyed on the name being derivable from the app id.

    WHAT THIS DOES NOT PROVE is which refusals reach the ladder. `is_transient` admits 429 and
    5xx; a 409 carrying a being-deleted `error_code` is classified terminal today and would reach
    the citizen as a failed start rather than a second attempt."""
    user, project, _ = await _citizen_with_two_projects(db_session, "sw12@example.com")
    app_id = await resolve_app_for_project(db_session, user.id, project)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"BUNDLE")
    name = app_name_for(app_id)
    refusals = {"left": 1}
    real_create = real_aca.aca.create_app

    async def refuses_once(**kwargs: object) -> str:
        if refusals["left"]:
            refusals["left"] -= 1
            real_aca.aca.create_calls.append(kwargs["name"])
            raise AcaTransientError("the name has not been released yet")
        return await real_create(**kwargs)

    monkeypatch.setattr(real_aca.aca, "create_app", refuses_once)
    monkeypatch.setattr("src.services.sandbox.client._asleep", _no_backoff)

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project)},
        headers=auth_headers(user),
    )

    assert resp.status_code == 200, resp.text
    assert real_aca.aca.create_calls == [name, name], "asked twice, under one name"


def _text(raw: bytes | str) -> str:
    """The fake Redis answers bytes or str depending on its decode setting."""
    return raw.decode() if isinstance(raw, bytes) else raw


async def _no_backoff(_seconds: float) -> None:
    return None
