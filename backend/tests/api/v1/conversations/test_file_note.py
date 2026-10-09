"""A file attached or removed mid-chat reaches the model as a hidden note at the end of the
history, never as a change to the tool list or the instructions — through the real send route,
for every chat kind."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from src.api.v1.conversations.turns import FILE_NOTE_KIND, _latest_file_note
from src.core.prompt_blocks import FILE_NOTE_HEADING
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.services.deploy.outcome import DEPLOY_DIAGNOSTIC_KIND
from src.services.messages.store import append_batch, load_rows
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, UserFactory

pytestmark = pytest.mark.usefixtures(
    "_fresh_engine", "_override_billing", "fake_analysis", "shared_storage"
)

_CSV = base64.b64encode(b"badge,name\n1,Asha\n").decode()


class _Capture:
    """The model's view of each request: the messages and the parameters it was handed."""

    def __init__(self) -> None:
        self.requests: list[tuple[list[ModelMessage], ModelRequestParameters]] = []

    def model(self) -> FunctionModel:
        async def _stream(messages: list[ModelMessage], info: AgentInfo):
            self.requests.append((list(messages), info.model_request_parameters))
            yield "noted."

        return FunctionModel(stream_function=_stream)


async def _wire(
    messages: list[ModelMessage], params: ModelRequestParameters
) -> tuple[list[bytes], list[bytes], list[bytes]]:
    """System blocks, tool definitions and messages exactly as the Anthropic mapper sends them."""
    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="offline"))
    prepared = model.customize_request_parameters(params)
    system, wire = await model._map_message(messages, prepared, {})  # noqa: SLF001
    tools = [
        model._map_tool_definition(tool, {}, visibility=prepared.visibility_of(tool.name))  # noqa: SLF001
        for tool in prepared.function_tools
    ]

    def dump(entries: Any) -> list[bytes]:
        return [json.dumps(entry, sort_keys=True, default=str).encode() for entry in entries]

    return dump(system if isinstance(system, list) else [system]), dump(tools), dump(wire)


async def _chat(db_session, kind: ChatKind):
    user = await UserFactory.create(db_session)
    return user, await ConversationFactory.create(db_session, user.id, kind=kind)


async def _upload(client, user, conv, attachment_id: str, name: str) -> None:
    resp = await client.post(
        "/v1/attachments",
        headers=_headers(user),
        json={
            "conversationId": str(conv.id),
            "attachmentId": attachment_id,
            "name": name,
            "mediaType": "text/csv",
            "base64": _CSV,
        },
    )
    assert resp.status_code == 201, resp.text


async def _turn(engine, client, user, conv, text: str, attachment_ids: list[str] | None = None):
    resp = await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=_headers(user),
        json={
            "message": {
                "text": text,
                "attachmentTexts": [],
                "attachmentIds": attachment_ids or [],
            }
        },
    )
    assert resp.status_code == 202, resp.text
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    assert state.status == "completed", state.error_message


async def _notes(db_session, user, conv) -> list[str]:
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    return [
        row.payload[0]["parts"][0]["content"]
        for row in rows
        if row.meta is not None and row.meta.get("kind") == FILE_NOTE_KIND
    ]


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD, ChatKind.GENERIC])
async def test_a_file_attached_mid_chat_changes_neither_the_tools_nor_the_instructions(
    kind: ChatKind, client, db_session, set_chat_model, _fresh_engine
) -> None:
    user, conv = await _chat(db_session, kind)
    capture = _Capture()
    set_chat_model(capture.model())

    await _turn(_fresh_engine, client, user, conv, "hello")
    await _turn(_fresh_engine, client, user, conv, "and again")
    await _upload(client, user, conv, "att_visitors", "visitors.csv")
    await _turn(_fresh_engine, client, user, conv, "read the file", ["att_visitors"])

    assert len(capture.requests) == 3, "every turn made exactly one model request"
    system_two, tools_two, _ = await _wire(*capture.requests[1])
    system_three, tools_three, live = await _wire(*capture.requests[2])
    assert tools_three == tools_two, "attaching a file changed the tool list"
    assert system_three == system_two, "attaching a file changed the instructions"

    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    notes = [row for row in rows if row.meta == {"kind": FILE_NOTE_KIND}]
    assert len(notes) == 1
    (note,) = notes
    assert note.entry_kind is MessageEntryKind.SYSTEM_EVENT
    assert note.visibility is MessageVisibility.HIDDEN
    after = next(row for row in rows if row.seq == note.seq + 1)
    assert after.entry_kind is MessageEntryKind.TURN
    assert after.payload[0]["parts"][0]["content"][0] == "read the file"
    text = note.payload[0]["parts"][0]["content"]
    assert text.startswith(FILE_NOTE_HEADING)
    assert "visitors.csv — .attachments/visitors.csv" in text
    if kind is not ChatKind.GENERIC:
        assert "on disk: /workspace/attachments/visitors.csv" in text

    assert FILE_NOTE_HEADING.encode() in live[-1]
    assert b"read the file" in live[-1]

    # The next turn replays what turn 3 sent from the store, byte for byte.
    await _turn(_fresh_engine, client, user, conv, "thanks")
    _, _, replayed = await _wire(*capture.requests[3])
    assert replayed[: len(live)] == live
    assert len(await _notes(db_session, user, conv)) == 1


async def test_each_change_to_the_files_writes_one_note_with_the_whole_current_list(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user, conv = await _chat(db_session, ChatKind.PLAN)
    set_chat_model(_Capture().model())

    await _upload(client, user, conv, "att_a", "arrivals.csv")
    await _upload(client, user, conv, "att_b", "departures.csv")
    await _turn(_fresh_engine, client, user, conv, "both files", ["att_a", "att_b"])
    assert (
        await client.delete("/v1/attachments/att_a", headers=_headers(user))
    ).status_code == 200
    await _turn(_fresh_engine, client, user, conv, "one gone")
    assert (
        await client.delete("/v1/attachments/att_b", headers=_headers(user))
    ).status_code == 200
    await _turn(_fresh_engine, client, user, conv, "all gone")

    first, second, third = await _notes(db_session, user, conv)
    assert "arrivals.csv" in first and "departures.csv" in first
    assert "arrivals.csv" not in second and "departures.csv" in second
    assert "arrivals.csv" not in third and "departures.csv" not in third
    assert "No file is attached to this conversation now." in third


async def test_the_latest_note_is_read_for_this_user_and_from_file_notes_only(
    db_session,
) -> None:
    user = await UserFactory.create(db_session)
    other = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.BUILD)

    async def _hidden(owner: uuid.UUID, text: str, kind: str) -> None:
        await append_batch(
            db_session,
            user_id=owner,
            conversation_id=conv.id,
            messages=[ModelRequest(parts=[UserPromptPart(content=text)])],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            kind=ChatKind.BUILD,
            visibility=MessageVisibility.HIDDEN,
            meta={"kind": kind},
        )

    await _hidden(other.id, "someone else's note", FILE_NOTE_KIND)
    await _hidden(user.id, "a deploy diagnosis", DEPLOY_DIAGNOSTIC_KIND)
    assert await _latest_file_note(db_session, user_id=user.id, conversation_id=conv.id) is None

    await _hidden(user.id, "the first list", FILE_NOTE_KIND)
    await _hidden(user.id, "the second list", FILE_NOTE_KIND)
    await _hidden(other.id, "someone else's later note", FILE_NOTE_KIND)
    latest = await _latest_file_note(db_session, user_id=user.id, conversation_id=conv.id)
    assert latest == "the second list"
