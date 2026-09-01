"""The Plan → Build handoff (U7/U8): a NEW chat, the plan verbatim, and nothing stored either way.

WHAT THESE PIN, and why each is here rather than being obvious:

* **The plan the citizen read is the plan the build gets.** It comes from the offer call's own
  stored argument, never from the request body — so a stale second tab cannot write stale
  requirements into a permanent first message, and there is no second copy to disagree with.
* **The ORDERING that makes a failed handoff leave nothing behind.** The conversation row is
  flushed and not committed; the shared turn starter's first durable write commits it together
  with the first message; the answer to the offer is written last, after that. Two of the tests
  below exist only to hold that order in place, because both wrong versions of it look correct
  in review and one of them is issue #72 with a new subject.
* **No linkage, in either direction.** Idempotency comes from the client-minted conversation id
  colliding with itself, not from anything recorded against the plan — which is what lets the
  same offer be pressed again next week and produce a second, different Build chat.
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
    run_build_dependency,
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.api.v1.conversations._shared import MAX_MESSAGE_TEXT_CHARS
from src.api.v1.conversations.transition import NO_PLAN_CODE, PLAN_TOO_LONG_CODE
from src.config import settings
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageVisibility
from src.db.models.user_limit import UserLimit
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
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from src.services.turns.plan_options import (
    find_pending,
    resolution_of,
    resolve_pending_as_refine,
)
from src.services.usage.gate import record_usage
from tests.api.v1.build_sessions.conftest import _sandbox_config
from tests.api.v1.conversations.test_turn_stream import _headers
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeBrain, FakeSandboxClient

_PLAN = (
    "Here is what your visitor log will do.\n\n"
    "You will see a list of everyone who signed in today, newest first, and a form to add "
    "someone. The app will remember each visitor's name, who they came to see, and the time "
    "they arrived."
)


@pytest.fixture(autouse=True)
def _fresh_engine():
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.conversations._shared import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):
    def _set(model) -> None:
        from src.api.v1.conversations._shared import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set


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
    app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
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
    """★ AE9. The whole plan, visible, with nothing before or after it.

    Nothing before: no "execute the approved plan" prefix, which is what the retired seed
    carried. Nothing after: no planning history copied across. And VISIBLE, not hidden — the
    old seed was hidden precisely because it was the platform talking, and this one is not."""
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


async def test_a_plan_past_the_browsers_cap_still_opens_a_chat_with_all_of_it(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ AE19. The server keeps its own, higher ceiling (R42a), and this is why it matters:
    the handoff materialises a message the browser never typed, so a limit chosen for a text
    box would refuse a plan nobody could have shortened."""
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
    """AN ANTI-LOSS GUARD, and labelled as one.

    Deleting the seed prefix deleted a real instruction — follow the code's reality where it
    differs from what the plan assumed, and say what changed. It moved into the Build chat's own
    prompt segment, and this asserts it is still IN the composed prompt. It is deliberately NOT
    offered as evidence that the agent reconciles: that is behavioural, belongs to the voice
    work, and asserting an instruction's presence and calling it done is a failure this platform
    has already shipped once."""
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
    """★ AE10/AE11. The double press, the retry, and the reload are ONE case, because they all
    carry the id the browser minted for that press.

    The second call answers `already_started` with whatever turn is live, so a second tab
    attaches to the same run — and it starts nothing, which is the half that matters: a second
    turn would be a second bill for one press."""
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
    """★ AE12's second half. Nothing archives the offer and nothing records that it was
    pressed, so next week's press — a NEW minted id — gets a new Build chat.

    That is the behaviour "no stored linkage" buys, stated as a feature rather than as an
    absence: the citizen who built something last month can build it again from the same plan
    without the platform having kept a note about it."""
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
    """★ N3, asserted over every row on both sides rather than over the schema.

    Including the tool answer's own content, which is the one place the guarantee could leak by
    accident: the natural thing to write into a `ToolReturnPart` is *what happened*, and "what
    happened" is one careless edit away from being the new chat's id."""
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

    # And the answer says the CHOICE, nothing more.
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
    """★ AE12's first half (R27). One write in the Plan chat — the answer — and no marker, no
    archive flag, no kind change, and no new visible item in its transcript."""
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
    assert reloaded is not None and reloaded.kind is ChatKind.PLAN  # nothing flipped
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
    """★ R29, reached by a failure that CAN ACTUALLY HAPPEN.

    The obvious two conditions cannot occur on a freshly minted id, and writing this test with
    either would green-pass against a missing rollback: `ConversationBusyError` comes from an
    in-process claim registry a new id is not in, and `SeqContentionError` needs a competing
    writer on a conversation that has no rows. So the raise goes at the durable-write seam the
    shared starter actually calls — the `append_batch` of the first user message, which is the
    write whose commit is the only thing making the flushed conversation row durable.

    The assertion is the OBSERVABLE, not the mechanism: no conversation row for the minted id."""
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building"))
    minted = uuid.uuid4()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("the durable write did not land")

    monkeypatch.setattr("src.api.v1.conversations.turns.append_batch", _boom)

    await _the_press_fails(client, _build_url(plan_chat), headers, minted, "did not land")

    # THE ROLLBACK, MADE OBSERVABLE. In production the request-scoped session is rolled back by
    # `get_db` when a handler raises; this suite hands every request the TEST's session, so
    # that rollback never happens and the flushed row would sit there looking committed. Doing
    # it explicitly is the stand-in — and it is a real discriminator rather than a formality,
    # because the failure this test exists to catch is the route COMMITTING the conversation
    # row before starting the turn (issue #72's shape). A committed row survives this rollback;
    # a flushed one does not.
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
    """★ U8's reason for existing, reached THROUGH THE ENDPOINT rather than by calling a
    recorder.

    The retired `build_failed` state existed to re-arm a card a failed press had burned. The
    press cannot burn one any more — the answer is the LAST write, after the turn has started —
    so a failure leaves the offer exactly as it was and pressable, with nothing to compensate."""
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
    """★ THE OTHER DIRECTION, and the reason the answer goes last.

    A failure here leaves a Build chat that is correct and complete and a Plan chat with an
    unanswered call — which is recoverable twice over (the next send resolves the card, and the
    dangling-call repair stitches the history valid regardless). The alternative ordering leaves
    a permanent empty Build chat, which is issue #72."""
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
    """★ THE FIRST OF TWO DEFENCES, and the stronger one: an unhonourable offer is never
    written at all.

    A call with an empty plan produces NO card — it is stripped from what is persisted, so
    neither reader can find one: not `plan_options._scan`, which looks in the row meta, and not
    the projection, which draws the card from the stored call. The citizen gets one
    platform-authored line instead of a button with nothing behind it.

    This is why the handoff's own refusal below has to be SEEDED rather than provoked: after
    this, the only rows that can still reach it are ones written before the argument existed."""
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
    """★ THE SECOND DEFENCE, for the rows the first one cannot reach: every offer presented
    before the plan became the tool's argument.

    The previous implementation built on a stand-in sentence ("Build what the user planned in
    this conversation"), so a build could start from text nobody wrote, against a plan nobody
    could point to. A named refusal is a worse moment and a better outcome."""
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
    """★ R44. REFUSED, never trimmed — a plan cut mid-sentence is one the citizen agreed to and
    the build would never see the end of.

    Seeded directly rather than produced by a model, because the engine now refuses to record an
    over-ceiling offer at write time: this is the defence in depth behind that, for a row written
    before it existed."""
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
    """★ The collision arm's guard. The id is CLIENT-MINTED, so without an ownership and
    parentage predicate this arm would hand anyone who guesses a colliding id the existence of —
    and a live turn id for — somebody else's conversation.

    One arm, one message, and nothing in the body about the row that exists."""
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
    """R19's first refusal, carrying the code that tells it apart from the other 409 on this
    route — same status, different cause, different remedy."""
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    elsewhere = await ConversationFactory.create(db_session, user.id, kind=ChatKind.BUILD)
    # The in-process claim, planted the way the manager itself records one: an id in the
    # per-user index and the session it points at. Reached through `active_session_for`, which
    # is what the route actually asks.
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
    """★ AE10. THE RETRY THAT ARRIVES LATE, which is the ordinary one: a reload, or a resend
    after the first response was dropped.

    It is the same case as the fast double press, but it reaches the route in a different
    world — the first press's turn has attached its sandbox by now, so the user HAS a live
    session. The busy check reads that session and compares it against the PLAN chat, and a
    handoff's session can never belong to the plan chat: it belongs to the Build chat, which
    is a different conversation by construction. So the comparison is true for every late
    retry, forever, and the citizen was told `409 already_building_here` — "another chat is
    using your workspace" — about their own build, in a chat they were never taken to.

    The fix is an ORDERING one, which is why this test plants the session rather than mocking
    the refusal: the idempotency read has to run before every capacity question, because none
    of them applies to a press that already succeeded.
    """
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    set_chat_model(_streaming_text("building it now"))
    minted = uuid.uuid4()

    first = await client.post(_build_url(plan_chat), headers=headers, json={"chatId": str(minted)})
    assert first.status_code == 200, first.text

    # The manager's own shape for a turn's session. `ensure_sandbox` — the one attach path
    # every Plan and Build turn takes — never threads a conversation id through, so this is
    # `None` in production; either way it is not the plan chat's id, which is the whole point.
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
    """★ R19's SECOND refusal, on this route rather than the send route.

    The two are different questions with the same status: the first is "one of your own chats
    holds the workspace", this is "taking the workspace would destroy unsaved work in another
    project". The send route's copy of this block is tested (`test_turn_stream.py`); the
    handoff's own copy was not, so deleting it here — or letting the exception escape as a
    500 — passed the whole suite while a Build press quietly reclaimed and destroyed a
    different project's live sandbox.
    """
    _user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )

    async def _blocked(*a: object, **k: object) -> None:
        raise SandboxReclaimBlockedError(
            project_id=uuid.uuid4(), project_name="Visitor Log", app_id=uuid.uuid4(), dirty=True
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
    assert error["code"] == "sandbox_reclaim_blocked"  # NOT the generic try-again-shortly
    assert error["projectName"] == "Visitor Log"  # it names what is in the way
    assert await _chats_with_id(db_session, minted) == 0  # and nothing was created


async def test_a_minted_id_that_is_the_users_own_plan_chat_is_one_flat_409(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """The other two disjuncts of the collision guard, which the owner test does not reach.

    A client-minted id can collide with any conversation, including the caller's OWN. Answering
    `already_started` for one would hand back a live turn id for a chat that has nothing to do
    with this press — a Plan chat, or a Build chat in a different project. All three disjuncts
    produce the same flat 409 for the same reason: the answer must not distinguish."""
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
        # THE SAME SENTENCE FOR BOTH, and the same one an id belonging to a stranger gets. It
        # says nothing about what the id turned out to be, and it carries no turn id — which
        # is the actual leak an `already_started` answer here would be.
        assert resp.json()["error"]["message"] == "This conversation id is already in use."
        assert "turnId" not in resp.text


async def test_a_raced_refine_leaves_exactly_one_return_on_the_wire(
    client, db_session, set_chat_model, wire, _fresh_engine, fake_redis, fake_storage
) -> None:
    """★ The Build-it vs turn-start race, from the route rather than from the recorder.

    A free-text send in the Plan chat resolves the open offer as `refine`. If a Build press for
    that same card then answers off the snapshot it opened with, it writes a SECOND real
    `ToolReturnPart` for one call id — and the two readers disagree about what happened: the
    model's history takes the first (`repair_dangling_tool_calls` dedupes first-wins) and the
    citizen's card takes the last. The build DID start, so the resolution has to say `build`;
    it just cannot say it on the wire twice. A hidden overlay is how it says it once.
    """
    user, plan_chat, headers = await _plan_chat_with_offer(
        client, db_session, set_chat_model, _fresh_engine
    )
    # The racing resolution, written the way a concurrent free-text send writes it.
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
# Two presses in flight at once both find nothing at the idempotency read and one loses the
# insert; the route catches that, ROLLS BACK, and answers with the existing chat. Reaching it
# from this file means blinding the idempotency read once — which works — and then the route's
# own `db.rollback()` unwinds the connection-level transaction every test here shares with the
# app, so the next statement dies on a `MissingGreenlet` before any assertion runs. What such a
# test would prove is the fixture, not the arm.
#
# Covering it honestly needs a request holding its OWN session (a live server and two real
# concurrent posts), which is an integration-lane shape this suite does not have. Until then
# the arm is reviewed rather than pinned, and a mutation to it — `except Exception: raise`, or
# dropping the rollback — survives this file. It is written down here so the next reader does
# not mistake the sequential double-press test above for coverage of it.


# --- the retired state ------------------------------------------------------------------


def test_no_resolution_value_exists_that_a_user_cannot_produce() -> None:
    """★ U8. `build_failed` is gone from every surface it appeared on, and the three are named
    individually because they are three independent declarations that had to agree."""
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
    """★ U5's reload half, asserted at the seam the handoff reads from.

    One stored copy — the call's own argument — renders as the plan and then the card, in that
    order. If this ever drifts from what the live stream pushed, the citizen agreed to one text
    and the build started from another."""
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
