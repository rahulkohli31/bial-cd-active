"""A plan chat left with its offer on screen, reopened two days later and continued.

The offer is a stored call with no answer and no expiry, so nothing here should depend on how
long ago it was made. Each test makes the offer through a real plan turn, then ages every row
the chat holds by two days and swaps in a fresh turn engine, which is what the same chat looks
like to a process two days on. Time is seeded rather than frozen: the routes stamp `now()` as
usual, so continuation is asserted from what each send returns, never from timestamps after it.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid

import pytest
import sqlalchemy as sa
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message
from src.services.build_sessions import manager as manager_module
from src.services.storage import accessor as storage_accessor
from src.services.storage.errors import StorageError
from src.services.turns.engine import (
    _TURN_FAILED_MESSAGE,
    TurnEngine,
    get_turn_engine,
    set_turn_engine_for_tests,
)
from tests.api.v1.conversations.conftest import _headers
from tests.continuation import answering, settled_turn
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage
from tests.wire import assert_wire_valid

pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")

_TWO_DAYS = datetime.timedelta(days=2)
_PLAN = "Your visitor log will list today's visitors, newest first, with a form to add one."
_UNREADABLE = "Your saved app could not be loaded just now"


def _offering(*call_ids: str) -> FunctionModel:
    """A plan turn that writes a line and then presents the plan, one offer id per request."""
    remaining = list(call_ids)

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield "Here is the plan.\n"
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="present_plan_options",
                    json_args=json.dumps({"plan": _PLAN}),
                    tool_call_id=remaining.pop(0),
                )
            }
        )

    return FunctionModel(stream_function=_stream)


async def _send(client, user, conversation_id: uuid.UUID, text: str):
    return await client.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=_headers(user),
        json={"message": {"text": text, "attachmentTexts": [], "attachmentIds": []}},
    )


async def _sent_and_settled(client, user, conversation_id: uuid.UUID, text: str):
    resp = await _send(client, user, conversation_id, text)
    assert resp.status_code == 202, resp.text
    return await settled_turn(get_turn_engine(), conversation_id)


async def _reopen(client, user, conversation_id: uuid.UUID) -> dict:
    resp = await client.get(f"/v1/conversations/{conversation_id}", headers=_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cards(detail: dict) -> list[tuple[str, str]]:
    return [
        (item["toolCallId"], item["state"])
        for item in detail["projection"]
        if item["type"] == "plan_options"
    ]


async def _two_days_pass(db_session, conversation_id: uuid.UUID) -> datetime.datetime:
    """Every row of the chat moved two days back, and the engine that ran its turns replaced."""
    await db_session.execute(
        sa.update(Message)
        .where(Message.conversation_id == conversation_id)
        .values(
            created_at=Message.created_at - _TWO_DAYS,
            updated_at=Message.updated_at - _TWO_DAYS,
        )
    )
    then = datetime.datetime.now(datetime.UTC) - _TWO_DAYS
    await db_session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(created_at=then, updated_at=then)
    )
    set_turn_engine_for_tests(TurnEngine())
    return then


async def _a_plan_offered_two_days_ago(client, db_session, set_chat_model, *call_ids: str):
    """A plan chat that answered one message per offer id, each answer ending in an offer."""
    user = await UserFactory.create(db_session, email=f"{uuid.uuid4().hex[:8]}@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    then = datetime.datetime.now(datetime.UTC) - _TWO_DAYS
    conv = await ConversationFactory.create(
        db_session,
        user.id,
        project_id=project.id,
        kind=ChatKind.PLAN,
        created_at=then,
        updated_at=then,
    )
    set_chat_model(_offering(*call_ids))
    for index, _ in enumerate(call_ids):
        state = await _sent_and_settled(client, user, conv.id, f"plan step {index}")
        assert state.status == "completed"
    await _two_days_pass(db_session, conv.id)
    return user, conv


# --- the offer survives the wait --------------------------------------------------------------


async def test_an_offer_reopened_two_days_later_is_still_the_one_to_act_on(
    client, db_session, set_chat_model
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")

    detail = await _reopen(client, user, conv.id)

    assert _cards(detail) == [("opt-1", "pending")]
    assert detail["activeTurn"] is None
    listed = await client.get(
        f"/v1/conversations?projectId={conv.project_id}", headers=_headers(user)
    )
    updated = datetime.datetime.fromisoformat(listed.json()["conversations"][0]["updatedAt"])
    assert datetime.datetime.now(datetime.UTC) - updated > datetime.timedelta(days=1)


async def test_a_message_typed_two_days_later_settles_the_offer_as_refine_and_is_answered(
    client, db_session, set_chat_model
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    model, seen = answering()
    set_chat_model(model)

    state = await _sent_and_settled(client, user, conv.id, "also track the exit time")

    assert state.status == "completed"
    assert _cards(await _reopen(client, user, conv.id)) == [("opt-1", "refine")]
    assert len(seen) == 1
    assert_wire_valid(seen[0], sending=True)
    answers = [
        part.content
        for message in seen[0]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_call_id == "opt-1"
    ]
    assert answers == ["refine"]


async def test_keep_planning_two_days_later_settles_once_and_the_chat_continues(
    client, db_session, set_chat_model
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    url = f"/v1/conversations/{conv.id}/plan-options/opt-1/resolve"

    first = await client.post(url, headers=_headers(user), json={"choice": "refine"})
    second = await client.post(url, headers=_headers(user), json={"choice": "refine"})

    assert first.json() == {"state": "refine", "alreadyResolved": False}
    assert second.json() == {"state": "refine", "alreadyResolved": True}
    set_chat_model(answering()[0])
    assert (await _sent_and_settled(client, user, conv.id, "one more thing")).status == "completed"


async def test_building_from_a_two_day_old_offer_opens_a_new_chat_and_the_plan_chat_continues(
    client, db_session, set_chat_model
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    set_chat_model(answering("Built.")[0])
    build_chat_id = uuid.uuid4()

    pressed = await client.post(
        f"/v1/conversations/{conv.id}/plan-options/opt-1/build",
        headers=_headers(user),
        json={"chatId": str(build_chat_id)},
    )

    assert pressed.status_code == 200, pressed.text
    assert pressed.json()["outcome"] == "started"
    await settled_turn(get_turn_engine(), build_chat_id)
    assert _cards(await _reopen(client, user, conv.id)) == [("opt-1", "build")]
    state = await _sent_and_settled(client, user, conv.id, "what about weekends?")
    assert state.status == "completed"
    listed = await client.get(
        f"/v1/conversations?projectId={conv.project_id}", headers=_headers(user)
    )
    assert {c["_id"] for c in listed.json()["conversations"]} == {str(conv.id), str(build_chat_id)}


async def test_only_the_newest_offer_can_be_acted_on_two_days_later(
    client, db_session, set_chat_model
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(
        client, db_session, set_chat_model, "opt-1", "opt-2"
    )

    assert _cards(await _reopen(client, user, conv.id)) == [
        ("opt-1", "refine"),
        ("opt-2", "pending"),
    ]
    old_build = await client.post(
        f"/v1/conversations/{conv.id}/plan-options/opt-1/build",
        headers=_headers(user),
        json={"chatId": str(uuid.uuid4())},
    )
    assert old_build.status_code == 409
    assert old_build.json()["error"]["message"] == "A newer plan supersedes these options."
    newest = await client.post(
        f"/v1/conversations/{conv.id}/plan-options/opt-2/resolve",
        headers=_headers(user),
        json={"choice": "refine"},
    )
    assert newest.json() == {"state": "refine", "alreadyResolved": False}


async def test_a_stop_while_a_new_offer_is_being_written_keeps_no_offer_and_the_chat_continues(
    client, db_session, set_chat_model
) -> None:
    """The card starts drawing as soon as the call begins to stream, but a plan turn stores its
    reply only when it completes, so a stop in that window leaves no offer behind."""
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    offering = asyncio.Event()

    async def _cut_off_mid_plan(_messages: list[ModelMessage], _info: AgentInfo):
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="present_plan_options",
                    json_args='{"plan": "Your visitor log will',
                    tool_call_id="opt-2",
                )
            }
        )
        offering.set()
        await asyncio.Event().wait()

    set_chat_model(FunctionModel(stream_function=_cut_off_mid_plan))
    turn_id = (await _send(client, user, conv.id, "rethink it")).json()["turnId"]
    await asyncio.wait_for(offering.wait(), timeout=10)

    stop = await client.post(
        f"/v1/conversations/{conv.id}/turns/{turn_id}/stop", headers=_headers(user)
    )
    assert stop.status_code == 200
    assert (await settled_turn(get_turn_engine(), conv.id)).status == "stopped"

    detail = await _reopen(client, user, conv.id)
    assert _cards(detail) == [("opt-1", "refine")]
    assert detail["activeTurn"] is None
    set_chat_model(answering()[0])
    assert (await _sent_and_settled(client, user, conv.id, "try again")).status == "completed"


# --- what else two days can change --------------------------------------------------------------


@pytest.fixture
def workspace(app) -> FakeSandboxClient:
    from src.api.v1.build_sessions.deps import sandbox_dependency

    client = app.dependency_overrides[sandbox_dependency]()
    assert isinstance(client, FakeSandboxClient)
    return client


async def test_a_reclaimed_workspace_that_was_never_saved_comes_back_fresh_and_the_chat_continues(
    client, db_session, set_chat_model, workspace, fake_storage, monkeypatch
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    monkeypatch.setattr(storage_accessor, "_backend_singleton", fake_storage)
    workspace.by_name.clear()
    provisioned_before = len(workspace.provisioned)
    set_chat_model(answering()[0])

    state = await _sent_and_settled(client, user, conv.id, "still there?")

    assert state.status == "completed"
    assert len(workspace.provisioned) == provisioned_before + 1
    assert workspace.restored == []


class _UnreadableStore(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.readable = False

    async def head(self, key):
        if not self.readable:
            raise StorageError("the store did not answer")
        return await super().head(key)


async def test_a_saved_app_that_cannot_be_read_ends_the_turn_with_news_and_a_later_send_works(
    client, db_session, set_chat_model, workspace, monkeypatch
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    store = _UnreadableStore()
    monkeypatch.setattr(storage_accessor, "_backend_singleton", store)

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(manager_module, "_asleep", _no_wait)
    workspace.by_name.clear()
    set_chat_model(answering()[0])

    refused = await _sent_and_settled(client, user, conv.id, "still there?")

    assert refused.status == "failed"
    assert refused.error_message is not None and _UNREADABLE in refused.error_message
    assert _cards(await _reopen(client, user, conv.id)) == [("opt-1", "refine")]
    store.readable = True
    assert (await _sent_and_settled(client, user, conv.id, "try now")).status == "completed"


async def test_rows_stored_under_an_older_payload_version_still_continue(
    client, db_session, set_chat_model
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    await db_session.execute(
        sa.update(Message).where(Message.conversation_id == conv.id).values(schema_version=1)
    )
    model, seen = answering()
    set_chat_model(model)

    state = await _sent_and_settled(client, user, conv.id, "carry on")

    assert state.status == "completed"
    assert_wire_valid(seen[0], sending=True)
    assert _cards(await _reopen(client, user, conv.id)) == [("opt-1", "refine")]


async def test_a_model_deployment_that_refuses_the_old_history_ends_the_turn_with_a_message(
    client, db_session, set_chat_model
) -> None:
    """Simulated as the provider refusing the request; a scripted model cannot reproduce what a
    real deployment does with reasoning signed by its predecessor."""
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")

    async def _refuses(_messages: list[ModelMessage], _info: AgentInfo):
        raise ModelHTTPError(400, "a-newer-deployment", {"error": {"type": "invalid_request"}})
        yield ""

    set_chat_model(FunctionModel(stream_function=_refuses))

    refused = await _sent_and_settled(client, user, conv.id, "carry on")

    assert refused.status == "failed"
    assert refused.error_message == _TURN_FAILED_MESSAGE
    set_chat_model(answering()[0])
    assert (await _sent_and_settled(client, user, conv.id, "again")).status == "completed"


# --- isolation ---------------------------------------------------------------------------------


async def test_another_user_cannot_read_settle_build_or_send_and_the_owner_can(
    client, db_session, set_chat_model
) -> None:
    user, conv = await _a_plan_offered_two_days_ago(client, db_session, set_chat_model, "opt-1")
    other = await UserFactory.create(db_session, email="someone-else@rvaiglobal.com")
    resolve = f"/v1/conversations/{conv.id}/plan-options/opt-1/resolve"
    build = f"/v1/conversations/{conv.id}/plan-options/opt-1/build"

    assert (
        await client.get(f"/v1/conversations/{conv.id}", headers=_headers(other))
    ).status_code == 404
    assert (
        await client.post(resolve, headers=_headers(other), json={"choice": "refine"})
    ).status_code == 404
    assert (
        await client.post(build, headers=_headers(other), json={"chatId": str(uuid.uuid4())})
    ).status_code == 404
    assert (await _send(client, other, conv.id, "hello")).status_code == 404

    assert _cards(await _reopen(client, user, conv.id)) == [("opt-1", "pending")]
    owned = await client.post(resolve, headers=_headers(user), json={"choice": "refine"})
    assert owned.json() == {"state": "refine", "alreadyResolved": False}
    set_chat_model(answering()[0])
    assert (await _sent_and_settled(client, user, conv.id, "mine")).status == "completed"
