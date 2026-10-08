"""A BIAL Chat reply that works on attached files, through the real route, engine and tools.

The session pool is the fake; the model is a scripted function model.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import zipfile
from typing import Any

import pytest
import sqlalchemy as sa
import structlog.testing
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from src.api.v1.conversations._shared import history_rehydrator
from src.core.prompt_blocks import ANALYSIS_RUN_TOOL, ATTACHMENT_READ_TOOL
from src.db.models.conversation import ChatKind
from src.db.models.token_usage import TokenUsage
from src.services.analysis import Execution, placement
from src.services.analysis import runtime as analysis_runtime
from src.services.analysis.placement import READER_NAME
from src.services.media.lanes import EXCEL_MEDIA_TYPE
from src.services.messages.store import load_history, load_rows
from src.services.orchestrator.constants import GENERIC_EFFORT
from src.services.turns import engine as engine_module
from src.services.turns.copy import ANALYSIS_CEILING_TEXT
from src.services.turns.guard import conversation_is_mid_reply
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeAnalysisRuntime, FakeStorage
from tests.pdfs import pdf_with_pages

pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")

_ANSWER = "The total is 42."


@pytest.fixture
def stored():
    """The object store both doors resolve, bound once for upload and send alike."""
    from src.services.storage import accessor

    store = FakeStorage()
    accessor._backend_singleton = store
    yield store
    accessor._backend_singleton = None


@pytest.fixture(autouse=True)
def _no_records():
    placement._records.clear()
    yield
    placement._records.clear()


@pytest.fixture
def reads(fake_analysis: FakeAnalysisRuntime) -> FakeAnalysisRuntime:
    """The fake pool, answering the reader with a manifest and any other code with `42`."""

    def _answer(_session_id: str, code: str) -> Execution:
        if READER_NAME in code:
            return Execution(
                succeeded=True, stdout=json.dumps({"ok": True, "sheets": []}), stderr=""
            )
        return Execution(succeeded=True, stdout="42\n", stderr="")

    fake_analysis.handle_run = _answer
    return fake_analysis


def _workbook() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", b"<workbook/>")
    return buffer.getvalue()


async def _chat(db_session):
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.GENERIC, project_id=None
    )
    return user, conversation


async def _upload(
    client,
    user,
    conversation,
    attachment_id: str,
    name: str = "q3.xlsx",
    data: bytes | None = None,
    media_type: str = EXCEL_MEDIA_TYPE,
):
    resp = await client.post(
        "/v1/attachments",
        headers=_headers(user),
        json={
            "conversationId": str(conversation.id),
            "attachmentId": attachment_id,
            "name": name,
            "mediaType": media_type,
            "base64": base64.b64encode(data if data is not None else _workbook()).decode(),
        },
    )
    assert resp.status_code == 201, resp.text


async def _send(
    client,
    user,
    conversation,
    text: str = "What is the total?",
    attachment_ids: list[str] | None = None,
):
    return await client.post(
        f"/v1/conversations/{conversation.id}/turns",
        headers=_headers(user),
        json={
            "message": {
                "text": text,
                "attachmentTexts": [],
                "attachmentIds": attachment_ids or [],
            }
        },
    )


async def _settle(engine, conversation_id) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _until_code_runs(runtime: FakeAnalysisRuntime) -> None:
    async def _poll() -> None:
        while not runtime.runs:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=10)


def _this_run(messages: list[ModelMessage]) -> list[ModelMessage]:
    opened = max(
        index
        for index, message in enumerate(messages)
        if isinstance(message, ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in message.parts)
    )
    return messages[opened:]


def _results(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in _this_run(messages)
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart | RetryPromptPart)
    ]


def _script(
    calls: list[tuple[str, dict[str, str]]],
    *,
    seen: dict[str, Any] | None = None,
    hold: asyncio.Event | None = None,
) -> FunctionModel:
    """Make `calls` in order, one per request, then answer. Records what the model was handed."""

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        if seen is not None:
            seen["tools"] = sorted(tool.name for tool in info.function_tools)
            seen["settings"] = info.model_settings
            seen["results"] = _results(messages)
            seen["messages"] = messages
        done = len(_results(messages))
        if done < len(calls):
            name, args = calls[done]
            yield {
                0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"c{done}")
            }
            return
        if hold is not None:
            await hold.wait()
        yield _ANSWER

    return FunctionModel(stream_function=_stream)


def _failing() -> FunctionModel:
    """A model that fails before it answers anything."""

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        raise RuntimeError("the model is unreachable")
        yield _ANSWER

    return FunctionModel(stream_function=_stream)


async def _stored(db_session, user, chat) -> list[dict[str, Any]]:
    """Every message the chat's rows hold, as written: no load-time repair."""
    rows = await load_rows(db_session, user_id=user.id, conversation_id=chat.id)
    return [message for row in rows for message in row.payload]


def _stored_calls(messages: list[dict[str, Any]]) -> list[str]:
    return [
        part["tool_name"]
        for message in messages
        if message["kind"] == "response"
        for part in message["parts"]
        if part["part_kind"] == "tool-call"
    ]


_READ = (ATTACHMENT_READ_TOOL, {"file": ".attachments/q3.xlsx"})
_RUN = (ANALYSIS_RUN_TOOL, {"code": "print(6 * 7)"})


def _uploaded(runtime: FakeAnalysisRuntime) -> list[str]:
    return [name for _, name in runtime.uploads]


# --- the ordinary reply ------------------------------------------------------------------


async def test_a_reply_about_a_workbook_reads_and_computes_in_the_chats_own_session(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    seen: dict[str, Any] = {}
    set_chat_model(_script([_READ, _RUN], seen=seen))

    resp = await _send(client, user, chat, attachment_ids=["att_q3"])

    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, chat.id)
    state = _fresh_engine.peek(chat.id)
    assert state.status == "completed"
    assert state.write_session is None
    assert seen["tools"] == sorted([ATTACHMENT_READ_TOOL, ANALYSIS_RUN_TOOL])
    assert {session for _, session in reads.calls} == {chat.id.hex}
    assert set(reads.files[chat.id.hex]) == {"q3.xlsx", READER_NAME}
    assert [code for _, code in reads.runs][1] == "print(6 * 7)"
    assert reads.operations("delete_session") == []
    assert seen["results"][1].startswith("exit 0")


async def test_a_follow_up_reply_reuses_the_session_and_copies_only_the_reader(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    set_chat_model(_script([_READ]))
    await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)
    reads.uploads.clear()

    set_chat_model(_script([_RUN]))
    await _send(client, user, chat, text="And per month?")
    await _settle(_fresh_engine, chat.id)

    assert _uploaded(reads) == [READER_NAME]


async def test_a_reply_that_calls_no_tool_makes_no_runtime_call(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    seen: dict[str, Any] = {}
    set_chat_model(_script([], seen=seen))

    resp = await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)

    assert resp.status_code == 202, resp.text
    assert _fresh_engine.peek(chat.id).status == "completed"
    assert seen["tools"] == sorted([ATTACHMENT_READ_TOOL, ANALYSIS_RUN_TOOL])
    assert reads.calls == []


async def test_a_chat_with_no_file_is_offered_no_tools(
    client, db_session, set_chat_model, _fresh_engine, reads
) -> None:
    user, chat = await _chat(db_session)
    seen: dict[str, Any] = {}
    set_chat_model(_script([], seen=seen))

    await _send(client, user, chat)
    await _settle(_fresh_engine, chat.id)

    assert seen["tools"] == []
    assert reads.calls == []


async def test_a_chat_that_used_the_tools_keeps_them_after_its_file_is_deleted(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    """The model API refuses a history carrying tool calls when no tool is defined."""
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    set_chat_model(_script([_READ]))
    await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)
    deleted = await client.delete("/v1/attachments/att_q3", headers=_headers(user))
    assert deleted.status_code == 200
    seen: dict[str, Any] = {}
    set_chat_model(_script([], seen=seen))

    await _send(client, user, chat, text="Thanks, and in general?")
    await _settle(_fresh_engine, chat.id)

    assert seen["tools"] == sorted([ATTACHMENT_READ_TOOL, ANALYSIS_RUN_TOOL])
    assert _fresh_engine.peek(chat.id).status == "completed"


async def test_a_deleted_file_leaves_the_session_on_the_next_access(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    await _upload(client, user, chat, "att_q4", name="q4.xlsx")
    set_chat_model(_script([_READ]))
    await _send(client, user, chat, attachment_ids=["att_q3", "att_q4"])
    await _settle(_fresh_engine, chat.id)
    await client.delete("/v1/attachments/att_q4", headers=_headers(user))

    set_chat_model(_script([_RUN]))
    await _send(client, user, chat, text="Again, without the second one.")
    await _settle(_fresh_engine, chat.id)

    assert set(reads.files[chat.id.hex]) == {"q3.xlsx", READER_NAME}


async def test_with_the_runtime_gone_a_plain_question_is_answered_and_a_tool_says_unavailable(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    set_chat_model(_script([]))
    await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)
    analysis_runtime._runtime_singleton = None
    seen: dict[str, Any] = {}
    set_chat_model(_script([_RUN], seen=seen))

    resp = await _send(client, user, chat, text="What is the total?")

    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, chat.id)
    assert _fresh_engine.peek(chat.id).status == "completed"
    assert seen["results"][0].startswith("error: unavailable")


async def test_a_pdf_beside_a_workbook_reaches_the_model_and_only_the_workbook_is_copied(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    await _upload(
        client,
        user,
        chat,
        "att_pdf",
        name="memo.pdf",
        data=pdf_with_pages(1),
        media_type="application/pdf",
    )
    seen: dict[str, Any] = {}
    set_chat_model(_script([_READ], seen=seen))

    await _send(client, user, chat, attachment_ids=["att_q3", "att_pdf"])
    await _settle(_fresh_engine, chat.id)

    sent = seen["messages"]
    assert any(
        isinstance(item, BinaryContent) and item.media_type == "application/pdf"
        for message in sent
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, list)
        for item in part.content
    )
    assert set(reads.files[chat.id.hex]) == {"q3.xlsx", READER_NAME}


async def test_a_send_naming_another_users_chat_touches_no_session(
    client, db_session, set_chat_model, stored, reads
) -> None:
    owner, chat = await _chat(db_session)
    stranger = await UserFactory.create(db_session)
    set_chat_model(_script([_RUN]))

    resp = await _send(client, stranger, chat)

    assert resp.status_code == 404
    assert reads.calls == []


# --- the ceilings, Stop and teardown -----------------------------------------------------


async def test_the_request_ceiling_keeps_the_steps_shows_the_sentence_and_bills(
    client, db_session, set_chat_model, _fresh_engine, stored, reads, monkeypatch
) -> None:
    monkeypatch.setattr(engine_module, "ANALYSIS_REQUEST_LIMIT", 2)
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    set_chat_model(_script([_READ, _RUN, _RUN, _RUN]))

    await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)

    state = _fresh_engine.peek(chat.id)
    assert (state.status, state.end_reason) == ("failed", "request_limit")
    assert state.error_message == ANALYSIS_CEILING_TEXT
    history = await load_history(
        db_session,
        user_id=user.id,
        conversation_id=chat.id,
        rehydrate=history_rehydrator(db_session, stored, user.id),
    )
    stored_calls = [
        part.tool_name
        for message in history
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolCallPart)
    ]
    assert stored_calls == [ATTACHMENT_READ_TOOL, ANALYSIS_RUN_TOOL]
    billed = await db_session.scalar(
        sa.select(sa.func.sum(TokenUsage.input_tokens)).where(TokenUsage.user_id == user.id)
    )
    assert billed and billed > 0
    assert reads.operations("delete_session") == []


async def test_the_request_ceiling_keeps_only_this_replys_steps_after_earlier_failed_replies(
    client, db_session, set_chat_model, _fresh_engine, stored, reads, monkeypatch
) -> None:
    """Replies that failed before answering leave adjacent requests, which pydantic-ai merges in
    the history it hands back, so the history's length overshoots where this reply begins."""
    monkeypatch.setattr(engine_module, "ANALYSIS_REQUEST_LIMIT", 2)
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    for text in ("First try", "Second try", "Third try"):
        set_chat_model(_failing())
        await _send(client, user, chat, text=text)
        await _settle(_fresh_engine, chat.id)
        assert _fresh_engine.peek(chat.id).status == "failed"
    set_chat_model(_script([_READ, _RUN, _RUN, _RUN]))

    await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)

    assert _fresh_engine.peek(chat.id).end_reason == "request_limit"
    stored_calls = _stored_calls(await _stored(db_session, user, chat))
    assert stored_calls == [ATTACHMENT_READ_TOOL, ANALYSIS_RUN_TOOL]


async def test_the_wall_clock_ends_the_reply_and_deletes_a_session_still_running_code(
    client, db_session, set_chat_model, _fresh_engine, stored, reads, monkeypatch
) -> None:
    monkeypatch.setattr(engine_module, "ANALYSIS_WALL_CLOCK_S", 0.5)
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    reads.hold_runs = asyncio.Event()
    set_chat_model(_script([_RUN]))

    await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)

    state = _fresh_engine.peek(chat.id)
    assert (state.status, state.end_reason) == ("failed", "wall_clock_deadline_exceeded")
    assert state.error_message == ANALYSIS_CEILING_TEXT
    assert reads.operations("delete_session") == [chat.id.hex]
    stored_messages = await _stored(db_session, user, chat)
    assert all(message["parts"] for message in stored_messages)
    assert _stored_calls(stored_messages) == []
    billed = await db_session.scalar(
        sa.select(sa.func.sum(TokenUsage.input_tokens)).where(TokenUsage.user_id == user.id)
    )
    assert billed and billed > 0


async def test_stop_while_code_runs_deletes_the_session_once_and_the_next_reply_refills_it(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    reads.hold_runs = asyncio.Event()
    set_chat_model(_script([_RUN]))
    turn = (await _send(client, user, chat, attachment_ids=["att_q3"])).json()["turnId"]
    await _until_code_runs(reads)

    stop = await client.post(
        f"/v1/conversations/{chat.id}/turns/{turn}/stop", headers=_headers(user)
    )
    assert stop.status_code == 200
    await _settle(_fresh_engine, chat.id)

    assert _fresh_engine.peek(chat.id).status == "stopped"
    assert reads.operations("delete_session") == [chat.id.hex]
    reads.hold_runs = None
    reads.uploads.clear()
    set_chat_model(_script([_RUN]))
    await _send(client, user, chat, text="Try again")
    await _settle(_fresh_engine, chat.id)
    assert _uploaded(reads) == ["q3.xlsx", READER_NAME]


async def test_a_session_delete_that_never_returns_still_frees_the_chat(
    client, db_session, set_chat_model, _fresh_engine, stored, reads, monkeypatch
) -> None:
    monkeypatch.setattr(placement, "ANALYSIS_DELETE_DEADLINE_S", 0.05)
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    reads.hold_runs = asyncio.Event()

    async def _hang(_session_id: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(reads, "delete_session", _hang)
    set_chat_model(_script([_RUN]))
    turn = (await _send(client, user, chat, attachment_ids=["att_q3"])).json()["turnId"]
    await _until_code_runs(reads)

    with structlog.testing.capture_logs() as logs:
        await client.post(f"/v1/conversations/{chat.id}/turns/{turn}/stop", headers=_headers(user))
        await _settle(_fresh_engine, chat.id)

    assert not conversation_is_mid_reply(chat.id)
    failed = [log for log in logs if log["event"] == "analysis_session_end_failed"]
    assert [(log["log_level"], log["error"]) for log in failed] == [("warning", "TimeoutError")]


# --- what is recorded --------------------------------------------------------------------


async def test_one_content_free_event_records_each_analysis_reply(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3", name="Salaries 2026.xlsx")
    set_chat_model(
        _script([(ATTACHMENT_READ_TOOL, {"file": ".attachments/Salaries_2026.xlsx"}), _RUN])
    )

    with structlog.testing.capture_logs() as logs:
        await _send(client, user, chat, attachment_ids=["att_q3"])
        await _settle(_fresh_engine, chat.id)

    events = [log for log in logs if log["event"] == "analysis_reply"]
    assert [(e["tool_calls"], e["fresh"], e["status"]) for e in events] == [(2, True, "completed")]
    assert "Salaries" not in repr(logs)
    assert "6 * 7" not in repr(logs)


async def test_an_analysis_reply_is_billed_once_from_the_models_counts_alone(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    """Five files cost what one costs: nothing is estimated from an attachment."""
    totals: list[tuple[int, int]] = []
    for files in (1, 5):
        user, chat = await _chat(db_session)
        ids = [f"att_{files}_{index}" for index in range(files)]
        for index, attachment_id in enumerate(ids):
            await _upload(client, user, chat, attachment_id, name=f"book{index}.xlsx")
        set_chat_model(
            _script([(ATTACHMENT_READ_TOOL, {"file": ".attachments/book0.xlsx"}), _RUN])
        )
        await _send(client, user, chat, attachment_ids=ids)
        await _settle(_fresh_engine, chat.id)
        rows = (
            await db_session.scalars(sa.select(TokenUsage).where(TokenUsage.user_id == user.id))
        ).all()
        assert len(rows) == 1
        totals.append((rows[0].input_tokens, rows[0].output_tokens))

    assert totals[0] == totals[1]
    assert totals[0][0] == 3 * 50


async def test_an_analysis_reply_asks_for_the_same_low_effort(
    client, db_session, set_chat_model, _fresh_engine, stored, reads
) -> None:
    user, chat = await _chat(db_session)
    await _upload(client, user, chat, "att_q3")
    seen: dict[str, Any] = {}
    set_chat_model(_script([_RUN], seen=seen))

    await _send(client, user, chat, attachment_ids=["att_q3"])
    await _settle(_fresh_engine, chat.id)

    settings = seen["settings"]
    assert isinstance(settings, dict)
    assert settings["anthropic_effort"] == GENERIC_EFFORT == "low"
