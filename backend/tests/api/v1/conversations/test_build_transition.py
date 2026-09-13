"""The Plan → Build handoff: a NEW chat, the plan verbatim, and nothing stored either way.

Three things pinned here: the build's first message is the offer call's OWN stored plan
argument, never the request body; a failed handoff leaves nothing behind because the
conversation row is flushed-not-committed while the shared turn starter's first durable write
commits it with the first message and the offer's answer is written last (two tests hold that
order — one re-guards a bug that once left a permanent empty Build chat); and nothing links
the two chats, so the same offer can be pressed again later for a second, independent chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Sequence
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.api.v1.conversations import transition as transition_module
from src.api.v1.conversations._shared import MAX_MESSAGE_TEXT_CHARS
from src.api.v1.conversations.transition import NO_PLAN_CODE, PLAN_TOO_LONG_CODE
from src.config import settings
from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageVisibility
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.db.models.user_limit import UserLimit
from src.services.agent.mode_prompts import PromptContext, compose_kind_prompt
from src.services.build_sessions import SessionManager
from src.services.build_sessions.manager import SandboxReclaimBlockedError
from src.services.messages.projection import (
    AssistantTextItem,
    PlanOptionsItem,
    UserTextItem,
    project_rows,
)
from src.services.messages.store import load_history, load_rows
from src.services.turns.copy import ALREADY_BUILDING_HERE_CODE
from src.services.turns.plan_options import (
    find_pending,
    resolution_of,
    resolve_pending_as_refine,
)
from src.services.usage.gate import record_usage
from tests.api.v1.build_sessions.conftest import _sandbox_config
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CONNECTOR_KEY = next(iter(CONNECTORS))

_PLAN = (
    "Here is what your visitor log will do.\n\n"
    "You will see a list of everyone who signed in today, newest first, and a form to add "
    "someone. The app will remember each visitor's name, who they came to see, and the time "
    "they arrived."
)


# Turn-driving fixtures live in conftest.py but are named here, not autoused there, because
# other files in this directory drive no turns.
pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")


@pytest.fixture
def wire(app, db_session, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    manager = SessionManager(session_factory=lambda: _session())
    sbx = FakeSandboxClient()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx
    return SimpleNamespace(app=app, manager=manager, sbx=sbx)


def _streaming_text(text: str):
    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield text

    return FunctionModel(stream_function=_stream)


def _plan_model(call_id: str = "opt-build", plan: str = _PLAN):
    """A Plan turn that writes nothing free-form and offers the plan as the call's argument.

    NO PROSE BESIDE THE CALL, deliberately: the plan IS the argument now, so a model that also
    narrated would be adding a second thing to the transcript rather than demonstrating this
    one. `json_args` carries the whole plan, exactly as the wire does."""

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="present_plan_options",
                    json_args=json.dumps({"plan": plan}),
                    tool_call_id=call_id,
                )
            }
        )

    return FunctionModel(stream_function=_stream)


def _empty_plan_model(call_id: str = "opt-build"):
    """A model that calls the offer with an EMPTY plan.

    Not an OMITTED one, and the difference is worth knowing: `plan` is a required argument, so a
    call that leaves it out never reaches this code at all — pydantic-ai rejects it at
    validation, tells the model what is wrong, and retries. An empty STRING is a valid `str`, so
    it arrives, and refusing it is the platform's job."""

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="present_plan_options",
                    json_args=json.dumps({"plan": "   "}),
                    tool_call_id=call_id,
                )
            }
        )

    return FunctionModel(stream_function=_stream)


async def _settle(engine, conversation_id) -> None:
    state = engine.peek(conversation_id)
    if state is not None and state.task is not None:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(state.task, timeout=10)


async def _plan_chat_with_offer(
    client, db_session, set_chat_model, engine, *, call_id: str = "opt-build", plan: str = _PLAN
):
    """A Plan chat whose newest state is a real pending offer, produced through the genuine
    engine path (turn POST → deferred call → pending row) rather than by seeding one."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_plan_model(call_id, plan))
    headers = _headers(user)
    resp = await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=headers,
        json={
            "message": {
                "text": "plan the visitors app",
                "attachmentTexts": [],
                "attachmentIds": [],
            }
        },
    )
    assert resp.status_code == 202, resp.text
    await _settle(engine, conv.id)
    return user, conv, headers


def _build_url(conv, call_id: str = "opt-build") -> str:
    return f"/v1/conversations/{conv.id}/plan-options/{call_id}/build"


async def _seed_offer(db_session, *, args: str, call_id: str = "opt-build"):
    """An offer written STRAIGHT INTO THE ROWS, in a shape a live turn can no longer produce.

    Both callers are about rows the engine now refuses to write — an argument-less call (every
    pre-migration card) and one past the stored-message ceiling — so provoking them through a
    model would prove only that the engine refuses them, which a different test already does.
    What these need is the handoff's own defence against a row that is already on disk."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from src.db.models.message import MessageEntryKind
    from src.services.messages.store import append_batch

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="present_plan_options", args=args, tool_call_id=call_id)
                ]
            )
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
        meta={"kind": "plan_options_pending", "toolCallId": call_id},
    )
    return user, conv


async def _the_press_fails(client, url: str, headers, chat_id: uuid.UUID, marker: str) -> None:
    """Press Build and require the injected failure to reach the caller.

    Written as an explicit catch rather than `contextlib.suppress`, for two reasons. The
    failure surfaces through the ASGI transport and may arrive wrapped — a bare
    `suppress(RuntimeError)` lets a wrapped one through and fails the test for the wrong
    reason. And "the press failed" is half of what these tests assert: swallowing the
    exception silently would let a press that quietly SUCCEEDED pass a test about rollback."""
    try:
        await client.post(url, headers=headers, json={"chatId": str(chat_id)})
    except BaseException as exc:  # noqa: BLE001 — the injected failure, however it is wrapped
        assert marker in repr(exc), repr(exc)
    else:
        pytest.fail("the injected failure never reached the caller")


async def _no_rehydration(_refs: Sequence[str]) -> dict[str, tuple[str, str]]:
    """`load_history` requires a rehydrator; nothing in these fixtures carries an attachment."""
    return {}


async def _chats_with_id(db_session, chat_id: uuid.UUID) -> int:
    """A COUNT straight off the table rather than `session.get`, because the identity map
    remembers a rolled-back row and would answer from memory."""
    db_session.expunge_all()
    return int(
        await db_session.scalar(
            sa.select(sa.func.count()).select_from(Conversation).where(Conversation.id == chat_id)
        )
        or 0
    )


# --- the happy path -----------------------------------------------------------------------


async def test_the_new_chat_opens_with_the_plan_and_nothing_else(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The whole plan, visible, with nothing before or after it — no retired "execute the
    approved plan" prefix, no copied planning history, and not hidden (the old seed was
    hidden because it was the platform talking; this text is the citizen's own plan)."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building it now"))
    minted = uuid.uuid4()

    resp = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "started"
    assert body["chatId"] == str(minted)
    assert body["turnId"]
    await _settle(_fresh_engine, minted)

    build_chat = await db_session.get(Conversation, minted)
    assert build_chat is not None
    assert build_chat.kind is ChatKind.BUILD
    assert build_chat.project_id == plan_chat.project_id

    rows = list(await load_rows(db_session, user_id=user.id, conversation_id=minted))
    first = project_rows(rows)[0]
    assert isinstance(first, UserTextItem)
    assert first.text == _PLAN  # verbatim, in full — no prefix, no wrapper
    assert rows[0].visibility is MessageVisibility.VISIBLE


async def test_the_handoff_resolves_the_projects_connected_data_for_the_build_it_starts(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage, monkeypatch
) -> None:
    """★ THE SECOND CALL SITE, AND THE ONE NOTHING ELSE COVERS.

    `transition.py` builds its OWN `PromptContext` — it does not reuse the Plan chat's — so the
    connector resolution here is separate code, and deleting it is invisible everywhere else: the
    registration suite calls `toolsets_for_kind` directly, the prompt suite composes its own
    context, and the argument defaults to none. The feature would ship inert on the path by which
    a connected project actually reaches a build, which is this one: pressing Build on a plan is
    how most builds start.

    ASSERTED ON THE CONTEXT THIS ROUTE HANDS THE TURN STARTER, not on the model's tool list. That
    is the DECISION this file's route makes — everything after it is the shared turn machinery
    `test_turn_stream.py` drives end to end on both arms. Driving it here would prove the same
    thing twice and cannot in any case: a Build turn in this module's harness fails its sandbox
    attach on the fixture's already-committed transaction and never reaches the model, which is
    why no test in this file inspects `AgentInfo`.

    The rows are seeded BEFORE the plan turn: that turn runs through the real route and commits,
    closing the fixture transaction, so a write after it raises rather than seeding anything."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    db_session.add(
        ConnectorAccessRequest(
            user_id=user.id,
            connector_key=_CONNECTOR_KEY,
            status=ConnectorRequestStatus.APPROVED,
            requester_remarks="The stand board needs on-block times.",
        )
    )
    db_session.add(
        ProjectConnector(
            project_id=conv.project_id,
            connector_key=_CONNECTOR_KEY,
            enabled=True,
            window_kind=ConnectorWindowKind.RELATIVE,
            window_days=7,
        )
    )
    await db_session.flush()

    headers = _headers(user)
    set_chat_model(_plan_model())
    resp = await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=headers,
        json={
            "message": {
                "text": "plan the stand board",
                "attachmentTexts": [],
                "attachmentIds": [],
            }
        },
    )
    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conv.id)

    # WRAPPED, NOT REPLACED. The real turn still starts — the route needs its id back and the
    # rest of this module's guarantees (a chat that exists, a plan that is verbatim) must keep
    # holding around this test.
    seen: dict[str, object] = {}
    # Reached through the module object rather than imported, because the ROUTE resolves it
    # that way at call time — patching the name it imported is what makes the wrap take
    # effect. `transition` does not re-export it, so mypy is told this is deliberate.
    real_starter = transition_module.start_conversation_turn  # type: ignore[attr-defined]

    async def _capturing_starter(*args, **kwargs):
        seen["prompt_context"] = kwargs["prompt_context"]
        return await real_starter(*args, **kwargs)

    monkeypatch.setattr(transition_module, "start_conversation_turn", _capturing_starter)

    set_chat_model(_streaming_text("building it now"))
    minted = uuid.uuid4()
    resp = await client.post(_build_url(conv), headers=headers, json={"chatId": str(minted)})
    assert resp.status_code == 200, resp.text
    await _settle(_fresh_engine, minted)

    context = seen["prompt_context"]
    assert isinstance(context, PromptContext)
    assert [system.key for system in context.connected_systems] == [_CONNECTOR_KEY]
    assert context.connected_systems[0].window.effectively_on is True
    # And the stub it produces names the system, so the Build chat's first turn says so.
    assert "CONNECTED DATA" in compose_kind_prompt(ChatKind.BUILD, context)


async def test_a_plan_past_the_browsers_cap_still_opens_a_chat_with_all_of_it(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The server keeps its own, higher ceiling: the handoff materialises a message the
    browser never typed, so a limit sized for a text box would refuse a plan nobody could
    have shortened."""
    long_plan = "Your app will remember every visit. " * 900  # ~32k chars: past any typing cap
    assert len(long_plan) > 10_000
    assert len(long_plan) < MAX_MESSAGE_TEXT_CHARS
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine, plan=long_plan
    )
    set_chat_model(_streaming_text("building"))
    minted = uuid.uuid4()

    resp = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})
    assert resp.status_code == 200, resp.text
    await _settle(_fresh_engine, minted)

    rows = list(await load_rows(db_session, user_id=user.id, conversation_id=minted))
    first = project_rows(rows)[0]
    assert isinstance(first, UserTextItem)
    assert first.text == long_plan.strip()  # whole, not trimmed


async def test_a_build_run_is_still_told_to_follow_the_code_over_the_plan(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """Anti-loss guard: the retired seed prefix's instruction — follow the code's reality where
    it differs from the plan — moved into the Build prompt segment, and this asserts it is still
    IN the composed prompt. It is deliberately NOT offered as evidence that the agent reconciles:
    that is behavioural, belongs to the voice work, and asserting an instruction's presence and
    calling it done is a failure this platform has already shipped once."""
    from src.db.models.conversation import ChatKind as _Kind
    from src.services.agent.mode_prompts import PromptContext, compose_kind_prompt

    composed = compose_kind_prompt(
        _Kind.BUILD, PromptContext(user_name="Asha", project_name="Visitor Log")
    ).lower()
    assert "where the code on disk differs from what the plan assumed" in composed
    assert "follow the code's reality" in composed


# --- one press, one chat ---------------------------------------------------------------


async def test_two_presses_of_the_same_id_settle_as_one_chat_and_one_turn(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The double press, the retry, and the reload are ONE case: they all carry the id the
    browser minted for that press, so the second call attaches to the live turn instead of
    starting a second one — a second turn would be a second bill for one press."""
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building it now"))
    minted = uuid.uuid4()

    first = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})
    assert first.status_code == 200, first.text
    second = await client.post(
        _build_url(plan_chat), headers=headers, json={"chatId": str(minted)}
    )
    assert second.status_code == 200, second.text

    assert second.json()["outcome"] == "already_started"
    assert second.json()["chatId"] == str(minted)
    await _settle(_fresh_engine, minted)
    assert await _chats_with_id(db_session, minted) == 1


async def test_pressing_the_same_offer_again_later_builds_a_second_different_chat(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ Nothing archives the offer or records that it was pressed, so a later press — a new
    minted id — gets a new Build chat: a citizen can build the same plan again without the
    platform having kept a note about the first time."""
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building"))

    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    first = await client.post(
        _build_url(plan_chat), headers=headers, json={"chatId": str(first_id)}
    )
    assert first.status_code == 200, first.text
    await _settle(_fresh_engine, first_id)
    second = await client.post(
        _build_url(plan_chat), headers=headers, json={"chatId": str(second_id)}
    )
    assert second.status_code == 200, second.text
    await _settle(_fresh_engine, second_id)

    assert second.json()["outcome"] == "started"
    assert first_id != second_id
    assert await _chats_with_id(db_session, first_id) == 1
    assert await _chats_with_id(db_session, second_id) == 1


# --- nothing is stored that points from one chat to the other --------------------------


async def test_nothing_written_anywhere_references_both_conversations(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ No linkage, asserted over every row on both sides — including the tool answer's own
    content, the one place it could leak by accident: the natural thing to write into a
    `ToolReturnPart` is *what happened*, one careless edit away from being the new chat's id."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building"))
    minted = uuid.uuid4()

    await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})
    await _settle(_fresh_engine, minted)

    plan_rows = list(
        await load_rows(
            db_session, user_id=user.id, conversation_id=plan_chat.id, include_hidden=True
        )
    )
    assert str(minted) not in json.dumps([[r.payload, r.meta] for r in plan_rows])
    build_rows = list(
        await load_rows(db_session, user_id=user.id, conversation_id=minted, include_hidden=True)
    )
    assert str(plan_chat.id) not in json.dumps([[r.payload, r.meta] for r in build_rows])

    answers = [
        part
        for row in plan_rows
        for message in row.payload
        for part in message.get("parts", [])
        if part.get("part_kind") == "tool-return"
    ]
    assert [part["content"] for part in answers] == ["build"]


async def test_the_plan_chat_is_otherwise_left_exactly_as_it_was(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ One write in the Plan chat — the answer — and no marker, archive flag, kind change,
    or new visible transcript item."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    before = project_rows(
        list(await load_rows(db_session, user_id=user.id, conversation_id=plan_chat.id))
    )
    set_chat_model(_streaming_text("building"))

    await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(uuid.uuid4())})

    db_session.expunge_all()
    reloaded = await db_session.get(Conversation, plan_chat.id)
    assert reloaded is not None and reloaded.kind is ChatKind.PLAN
    after = project_rows(
        list(await load_rows(db_session, user_id=user.id, conversation_id=plan_chat.id))
    )
    assert [(item.type, getattr(item, "text", None)) for item in after] == [
        (item.type, getattr(item, "text", None)) for item in before
    ]


# --- the ordering: a failed handoff leaves nothing ---------------------------------------


async def test_a_failure_at_the_first_durable_write_leaves_no_build_chat(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage, monkeypatch
) -> None:
    """★ Reached by a failure that can actually happen on a fresh id: `ConversationBusyError`
    and `SeqContentionError` both require state a new id doesn't have, so the raise is planted
    at the real durable-write seam instead — `append_batch` of the first user message, whose
    commit is what makes the flushed conversation row durable. Asserts the OBSERVABLE (no
    conversation row), not the mechanism."""
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building"))
    minted = uuid.uuid4()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("the durable write did not land")

    monkeypatch.setattr("src.api.v1.conversations.turns.append_batch", _boom)

    await _the_press_fails(client, _build_url(plan_chat), headers, minted, "did not land")

    # Rollback made observable: production rolls back the request session on a raised handler
    # via `get_db`, but this suite shares the TEST's session across every request, so that
    # rollback never happens on its own — this stands in for it. A committed row survives this
    # rollback; a flushed one does not, which is the real discriminator against the route
    # committing the conversation row before starting the turn.
    #
    # Mutation-checked: put `await db.commit()` after the flush in `transition.build_it` and
    # this assertion goes red on its own.
    await db_session.rollback()
    assert await _chats_with_id(db_session, minted) == 0
    messages = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == minted)
    )
    assert (messages or 0) == 0


async def test_the_mirror_a_failed_handoff_leaves_the_offer_pressable(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage, monkeypatch
) -> None:
    """★ The retired `build_failed` state existed to re-arm a card a failed press had burned;
    since the answer is now the LAST write (after the turn starts), a failure can no longer
    burn the card at all, so it should be left exactly as it was and still pressable."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building"))

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("the durable write did not land")

    monkeypatch.setattr("src.api.v1.conversations.turns.append_batch", _boom)
    await _the_press_fails(client, _build_url(plan_chat), headers, uuid.uuid4(), "did not land")
    monkeypatch.undo()

    db_session.expunge_all()
    still = await find_pending(db_session, user_id=user.id, conversation_id=plan_chat.id)
    assert still is not None and still.tool_call_id == "opt-build"
    cards = [
        item
        for item in project_rows(
            list(await load_rows(db_session, user_id=user.id, conversation_id=plan_chat.id))
        )
        if isinstance(item, PlanOptionsItem)
    ]
    assert [card.state for card in cards] == ["pending"]


async def test_a_failure_of_the_answer_write_still_leaves_a_complete_build_chat(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage, monkeypatch
) -> None:
    """★ The other direction, and the reason the answer goes last: a failure here leaves a
    complete Build chat and a Plan chat with an unanswered call, recoverable twice over (the
    next send resolves it; dangling-call repair stitches history regardless) — the reverse
    ordering leaves a permanent empty Build chat instead."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building"))
    minted = uuid.uuid4()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("the answer did not land")

    monkeypatch.setattr("src.api.v1.conversations.transition.record_build_started", _boom)
    await _the_press_fails(client, _build_url(plan_chat), headers, minted, "did not land")
    monkeypatch.undo()
    await _settle(_fresh_engine, minted)

    assert await _chats_with_id(db_session, minted) == 1
    rows = list(await load_rows(db_session, user_id=user.id, conversation_id=minted))
    first = project_rows(rows)[0]
    assert isinstance(first, UserTextItem)
    assert first.text == _PLAN
    # …and the Plan chat still composes a valid history despite the unanswered call.
    history = await load_history(
        db_session, user_id=user.id, conversation_id=plan_chat.id, rehydrate=_no_rehydration
    )
    assert history


# --- the refusals -------------------------------------------------------------------------


async def test_an_empty_plan_leaves_no_offer_and_says_so_once(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The first, stronger of two defences: an unhonourable offer is never written at all —
    an empty-plan call produces no card in either reader (`plan_options._scan` or the
    projection), just one platform-authored line instead of a dead button.

    Because of this, the handoff's own refusal (below) can only still be reached by a row
    SEEDED directly — the only offers left that predate this argument check."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_empty_plan_model())
    headers = _headers(user)
    await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=headers,
        json={"message": {"text": "plan it", "attachmentTexts": [], "attachmentIds": []}},
    )
    await _settle(_fresh_engine, conv.id)

    db_session.expunge_all()
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None
    items = project_rows(
        list(await load_rows(db_session, user_id=user.id, conversation_id=conv.id))
    )
    assert not [item for item in items if isinstance(item, PlanOptionsItem)]
    assert any(
        isinstance(item, AssistantTextItem) and "nothing to build from yet" in item.text
        for item in items
    )


async def test_a_pre_migration_offer_refuses_by_name_and_creates_nothing(
    client, db_session, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The second defence, for offers presented before the plan became the tool's argument:
    the previous implementation built on a stand-in sentence, so a build could start from text
    nobody wrote. A named refusal replaces that silent stand-in."""
    user, conv = await _seed_offer(db_session, args="{}")
    minted = uuid.uuid4()

    resp = await client.post(
        _build_url(conv), headers=_headers(user), json={"chatId": str(minted)}
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == NO_PLAN_CODE
    assert await _chats_with_id(db_session, minted) == 0


async def test_a_plan_over_the_ceiling_refuses_by_name_and_truncates_nothing(
    client, db_session, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ Refused, never trimmed — a plan cut mid-sentence is one the citizen agreed to and the
    build would never see the end of. Seeded directly: the engine now refuses to record an
    over-ceiling offer at write time, so this only guards a row written before that existed."""
    huge = "x" * (MAX_MESSAGE_TEXT_CHARS + 1)
    user, conv = await _seed_offer(db_session, args=json.dumps({"plan": huge}))
    minted = uuid.uuid4()

    resp = await client.post(
        _build_url(conv), headers=_headers(user), json={"chatId": str(minted)}
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == PLAN_TOO_LONG_CODE
    assert await _chats_with_id(db_session, minted) == 0


async def test_a_minted_id_that_belongs_to_someone_else_is_one_flat_409(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The id is CLIENT-MINTED, so without an ownership check this arm would hand anyone who
    guesses a colliding id the existence of — and a live turn id for — someone else's chat.
    One arm, one message, nothing in the body about the row that exists."""
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id, kind=ChatKind.BUILD)
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building"))

    resp = await client.post(
        _build_url(plan_chat), headers=headers, json={"chatId": str(theirs.id)}
    )

    assert resp.status_code == 409, resp.text
    body = resp.text
    assert "already in use" in body
    assert str(other.id) not in body
    assert str(theirs.project_id) not in body
    assert "turnId" not in body


async def test_a_superseded_offer_is_a_409(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine, call_id="opt-old"
    )
    set_chat_model(_plan_model("opt-new"))
    await client.post(
        f"/v1/conversations/{plan_chat.id}/turns",
        headers=headers,
        json={"message": {"text": "revise it", "attachmentTexts": [], "attachmentIds": []}},
    )
    await _settle(_fresh_engine, plan_chat.id)

    resp = await client.post(
        _build_url(plan_chat, "opt-old"), headers=headers, json={"chatId": str(uuid.uuid4())}
    )
    assert resp.status_code == 409


async def test_an_unknown_card_is_a_400_and_a_stranger_a_404(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    unknown = await client.post(
        _build_url(plan_chat, "no-such-card"), headers=headers, json={"chatId": str(uuid.uuid4())}
    )
    assert unknown.status_code == 400

    stranger = await UserFactory.create(db_session)
    cross = await client.post(
        _build_url(plan_chat), headers=_headers(stranger), json={"chatId": str(uuid.uuid4())}
    )
    assert cross.status_code == 404


async def test_the_daily_cap_is_a_429_and_leaves_the_offer_pressable(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=1))
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=10)
    minted = uuid.uuid4()

    resp = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})

    assert resp.status_code == 429
    assert await _chats_with_id(db_session, minted) == 0
    db_session.expunge_all()
    assert (
        await find_pending(db_session, user_id=user.id, conversation_id=plan_chat.id) is not None
    )


async def test_a_workspace_busy_in_another_chat_is_a_coded_409(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """The workspace-busy refusal carries a code distinct from the route's other 409 — same
    status, different cause, different remedy."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    elsewhere = await ConversationFactory.create(db_session, user.id, kind=ChatKind.BUILD)
    # Plants the in-process claim the way the manager itself records one — an id in the
    # per-user index and the session it points at — reached via `active_session_for`.
    session_id = uuid.uuid4()
    wire.manager._active_by_user[user.id] = session_id  # noqa: SLF001
    wire.manager._sessions[session_id] = SimpleNamespace(  # noqa: SLF001
        conversation_id=elsewhere.id
    )
    minted = uuid.uuid4()

    resp = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == ALREADY_BUILDING_HERE_CODE
    assert await _chats_with_id(db_session, minted) == 0


# --- the refusals a retry must not be given -------------------------------------------------


async def test_a_retry_after_the_build_attached_its_sandbox_is_still_answered_idempotently(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The late retry (a reload, or a resend after the first response was dropped): by now
    the first press's turn has attached its own sandbox, so the busy check's comparison against
    the PLAN chat is true for every such retry forever, once wrongly refusing citizens their
    own build. The fix is ORDERING — idempotency must be read before any capacity check — so
    this plants the live session rather than mocking the refusal directly."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building it now"))
    minted = uuid.uuid4()

    first = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})
    assert first.status_code == 200, first.text

    # `ensure_sandbox` never threads a conversation id through, so this is `None` in
    # production — either way it is not the plan chat's id, which is the whole point.
    session_id = uuid.uuid4()
    wire.manager._active_by_user[user.id] = session_id  # noqa: SLF001
    wire.manager._sessions[session_id] = SimpleNamespace(conversation_id=None)  # noqa: SLF001

    second = await client.post(
        _build_url(plan_chat), headers=headers, json={"chatId": str(minted)}
    )

    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "already_started"
    assert second.json()["chatId"] == str(minted)
    await _settle(_fresh_engine, minted)
    assert await _chats_with_id(db_session, minted) == 1


async def test_unsaved_work_in_another_project_refuses_the_handoff_with_its_own_code(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The handoff's own copy of the unsaved-work refusal, distinct from the send route's
    (tested in `test_turn_stream.py`): taking the workspace would destroy unsaved work in
    another project. This copy went untested once — deleting it, or letting the exception
    escape as a 500, passed the whole suite while a Build press destroyed another project's
    live sandbox."""
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )

    async def _blocked(*a: object, **k: object) -> None:
        raise SandboxReclaimBlockedError(
            project_id=uuid.uuid4(),
            project_name="Visitor Log",
            app_id=uuid.uuid4(),
            dirty=True,
            agent_working=True,
        )

    minted = uuid.uuid4()
    original = SessionManager.reclaim_preflight
    SessionManager.reclaim_preflight = _blocked  # type: ignore[method-assign]
    try:
        resp = await client.post(
            _build_url(plan_chat), headers=headers, json={"chatId": str(minted)}
        )
    finally:
        SessionManager.reclaim_preflight = original  # type: ignore[method-assign]

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "sandbox_reclaim_blocked"  # not the generic try-again-shortly
    assert error["projectName"] == "Visitor Log"  # it names what is in the way
    # The 2nd of three entry points into the hand-over dialog — all route through one
    # responder, so "correct on the one that was tested" can't happen here.
    assert error["agentWorking"] is True
    assert await _chats_with_id(db_session, minted) == 0


async def test_a_minted_id_that_is_the_users_own_plan_chat_is_one_flat_409(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """The other two disjuncts of the collision guard: a client-minted id can also collide with
    the caller's OWN plan chat or a Build chat in a different project. All three get the same
    flat 409, for the same reason — the answer must not distinguish."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    other_project = await ProjectFactory.create(db_session, user.id)
    elsewhere = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.BUILD, project_id=other_project.id
    )

    for colliding in (plan_chat.id, elsewhere.id):
        resp = await client.post(
            _build_url(plan_chat), headers=headers, json={"chatId": str(colliding)}
        )
        assert resp.status_code == 409, resp.text
        # Same sentence for both, and for a stranger's id too — it says nothing about what the
        # id turned out to be, and carries no turn id (the actual leak an `already_started`
        # answer here would be).
        assert resp.json()["error"]["message"] == "This conversation id is already in use."
        assert "turnId" not in resp.text


async def test_a_raced_refine_leaves_exactly_one_return_on_the_wire(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The Build-it vs turn-start race: a free-text send resolves the open offer as `refine`,
    then a Build press for the same card answers off its stale snapshot — writing a SECOND real
    `ToolReturnPart` for one call id, which the two readers would disagree about (history takes
    the first, the card takes the last). The build did start, so the card must still say
    `build`, just not on the wire twice — a hidden overlay says it once."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    # The racing resolution, written the way a concurrent free-text send would.
    await resolve_pending_as_refine(db_session, user_id=user.id, conversation_id=plan_chat.id)

    set_chat_model(_streaming_text("building it now"))
    minted = uuid.uuid4()
    resp = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})
    assert resp.status_code == 200, resp.text
    await _settle(_fresh_engine, minted)

    rows = list(
        await load_rows(
            db_session, user_id=user.id, conversation_id=plan_chat.id, include_hidden=True
        )
    )
    returns = [
        part
        for row in rows
        for message in (row.payload if isinstance(row.payload, list) else [])
        if isinstance(message, dict)
        for part in message.get("parts", [])
        if isinstance(part, dict)
        and part.get("part_kind") == "tool-return"
        and part.get("tool_call_id") == "opt-build"
    ]
    assert len(returns) == 1  # the refine, and nothing stacked on top of it
    # And the card still reads what actually happened, off the hidden overlay.
    assert resolution_of(rows, "opt-build") == "build"


# THE GENUINE-RACE ARM IS NOT COVERED HERE, AND IT IS NOT AN OVERSIGHT.
#
# Two presses in flight both find nothing at the idempotency read; one loses the insert, the
# route catches that, ROLLS BACK, and answers with the existing chat. Reaching it from this file
# means blinding the idempotency read once, but the route's own `db.rollback()` then unwinds the
# connection-level transaction every test here shares with the app, so the next statement dies
# on a `MissingGreenlet` before any assertion runs — such a test would prove the fixture, not
# the arm.
#
# Covering it honestly needs a request holding its OWN session (a live server, two real
# concurrent posts) — an integration-lane shape this suite doesn't have. Until then a mutation
# to it (`except Exception: raise`, or dropping the rollback) survives this file; don't mistake
# the sequential double-press test above for coverage of it.


# --- the retired state ------------------------------------------------------------------


def test_no_resolution_value_exists_that_a_user_cannot_produce() -> None:
    """★ `build_failed` is gone from every surface it appeared on — named individually because
    the three were independent declarations that had to agree."""
    import typing

    from src.api.v1.conversations.turns import ResolvePlanOptionsResponse
    from src.services.messages.projection import PlanOptionsItem as _Item
    from src.services.turns import plan_options

    assert set(typing.get_args(plan_options.PlanChoice)) == {"refine", "build"}
    assert set(typing.get_args(_Item.model_fields["state"].annotation)) == {
        "pending",
        "refine",
        "build",
    }
    assert set(typing.get_args(ResolvePlanOptionsResponse.model_fields["state"].annotation)) == {
        "refine",
        "build",
    }
    assert not hasattr(plan_options, "record_build_failure")


async def test_a_stray_build_failed_overlay_reads_as_spent_not_as_live(db_session) -> None:
    """The one input that can still carry the retired string: an overlay written before it was
    retired. It must read as SPENT — showing it as live would offer a button with nothing behind
    it, which is the exact defect the state was invented to paper over."""
    from src.db.models.message import MessageEntryKind
    from src.services.messages.store import append_batch
    from src.services.turns.plan_options import find_pending

    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        messages=[],
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        kind=ChatKind.PLAN,
        visibility=MessageVisibility.HIDDEN,
        meta={"kind": "plan_options_pending", "toolCallId": "old-1", "synthesized": True},
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        messages=[],
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        kind=ChatKind.PLAN,
        visibility=MessageVisibility.HIDDEN,
        meta={
            "kind": "plan_options_resolved",
            "toolCallId": "old-1",
            "choice": "build_failed:provision",
        },
    )

    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None
    cards = [
        item
        for item in project_rows(
            list(
                await load_rows(
                    db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
                )
            )
        )
        if isinstance(item, PlanOptionsItem)
    ]
    assert [card.state for card in cards] == ["refine"]


async def test_the_offer_renders_its_plan_above_the_card(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The reload half: one stored copy — the call's own argument — renders as the plan then
    the card, in that order. If this drifts from what the live stream pushed, the citizen
    agreed to one text and the build started from another."""
    user, plan_chat, _headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    items = project_rows(
        list(await load_rows(db_session, user_id=user.id, conversation_id=plan_chat.id))
    )
    plans = [i for i in items if isinstance(i, AssistantTextItem)]
    assert [p.text for p in plans] == [_PLAN]
    assert items.index(plans[0]) < items.index(
        next(i for i in items if isinstance(i, PlanOptionsItem))
    )
