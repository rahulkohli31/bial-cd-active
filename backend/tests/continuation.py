"""Driving one chat through the turn engine the way the send route does, for the tests that
stop, reclaim and then continue it.

`Chat.send` is the route's order: history through `load_history` (the one loader), then the
engine claims, persists the user's message and runs. Every send checks the history it hands the
engine with `assert_wire_valid`, so no scenario can continue on a history the provider would
refuse without failing.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageEntryKind
from src.db.models.user import User
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.messages.store import append_batch, load_history
from src.services.sandbox.base import FileCreate, FileOp, FileResult, SandboxHandle
from src.services.turns.engine import TurnEngine
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient
from tests.wire import assert_wire_valid

CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)
WRITE_ARGS = '{"path": "app/page.tsx", "file_text": "x"}'
WROTE_THE_PAGE = "Wrote `app/page.tsx`."


class Workspace(FakeSandboxClient):
    """Records every file a turn wrote, and can hold a write in flight until let go."""

    def __init__(self, *, hold_writes: bool = False) -> None:
        super().__init__()
        self.written: list[str] = []
        self.hold_writes = hold_writes
        self.inside = asyncio.Event()
        self.let_go = asyncio.Event()

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        if isinstance(op, FileCreate):
            if self.hold_writes:
                self.inside.set()
                await self.let_go.wait()
            self.written.append(op.path)
        return await super().files(handle, op)


class Chat:
    """One conversation, and everything needed to send to it again."""

    def __init__(
        self,
        engine: TurnEngine,
        db: AsyncSession,
        session_factory: Any,
        user: User,
        conversation: Conversation,
        workspace: Workspace,
    ) -> None:
        self.engine = engine
        self.db = db
        self.session_factory = session_factory
        self.user = user
        self.conversation = conversation
        self.workspace = workspace
        self.manager = SessionManager()

    async def send(self, text: str, model: FunctionModel) -> uuid.UUID:
        history = await self.history()
        assert_wire_valid([*history, user_message(text)], sending=True)

        async def persist_user_turn() -> None:
            await append_batch(
                self.db,
                user_id=self.user.id,
                conversation_id=self.conversation.id,
                messages=[user_message(text)],
                entry_kind=MessageEntryKind.TURN,
                kind=self.conversation.kind,
            )

        return await self.engine.start_turn(
            conversation=self.conversation,
            user_id=self.user.id,
            prompt=text,
            history=history,
            prompt_context=CTX,
            app_id=None,
            project_id=self.conversation.project_id,
            model=model,
            session_factory=self.session_factory,
            persist_user_turn=persist_user_turn,
            manager=self.manager,
            sandbox_client=self.workspace,
        )

    async def history(self) -> list[ModelMessage]:
        return await load_history(
            self.db,
            user_id=self.user.id,
            conversation_id=self.conversation.id,
            rehydrate=no_rehydration,
        )

    async def settled(self):
        return await settled_turn(self.engine, self.conversation.id)

    async def stored_messages(self) -> list[Any]:
        """Every message a payload carries, in seq order: what a later turn could replay."""
        payloads: Sequence[list[Any]] = (
            await self.db.scalars(
                sa.select(Message.payload)
                .where(Message.conversation_id == self.conversation.id)
                .order_by(Message.seq)
            )
        ).all()
        return [message for payload in payloads for message in payload]

    async def continues(self, text: str = "carry on") -> list[ModelMessage]:
        """Send again and finish the turn; returns what the model was handed."""
        model, seen = answering()
        await self.send(text, model)
        state = await self.settled()
        assert state.status == "completed", state.error_message
        assert len(seen) == 1
        assert_wire_valid(seen[0], sending=True)
        assert user_texts(seen[0])[-1] == text
        return seen[0]


async def new_chat(
    engine: TurnEngine,
    db: AsyncSession,
    session_factory: Any,
    kind: ChatKind,
    *,
    workspace: Workspace | None = None,
) -> Chat:
    user = await UserFactory.create(db, email=f"{uuid.uuid4().hex[:8]}@rvaiglobal.com")
    project = await ProjectFactory.create(db, user.id)
    conversation = await ConversationFactory.create(db, user.id, project_id=project.id, kind=kind)
    return Chat(engine, db, session_factory, user, conversation, workspace or Workspace())


async def settled_turn(engine: TurnEngine, conversation_id: uuid.UUID):
    """The conversation's newest turn once it has finished, however it finished."""
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    return state


async def no_rehydration(_refs: Sequence[str]) -> dict[str, tuple[str, str]]:
    return {}


def user_message(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def user_texts(history: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


def tool_answers(history: list[ModelMessage]) -> dict[str, str]:
    return {
        part.tool_call_id: str(part.content)
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


def answering(reply: str = "Carrying on.") -> tuple[FunctionModel, list[list[ModelMessage]]]:
    seen: list[list[ModelMessage]] = []

    async def _stream(messages: list[ModelMessage], _info: AgentInfo):
        seen.append(list(messages))
        yield reply

    return FunctionModel(stream_function=_stream), seen


def stalls_after(first: str, streaming: asyncio.Event) -> FunctionModel:
    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield first
        streaming.set()
        await asyncio.Event().wait()  # only a cancel leaves this

    return FunctionModel(stream_function=_stream)


def builds_a_page() -> FunctionModel:
    """Writes one file, then declares the build done: a build turn that completes."""
    requests = {"n": 0}

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        requests["n"] += 1
        if requests["n"] == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="write_file", json_args=WRITE_ARGS, tool_call_id="c-write-1"
                    )
                }
            )
        elif requests["n"] == 2:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="declare_done",
                        json_args='{"summary": "Added the page."}',
                        tool_call_id="c-done-1",
                    )
                }
            )
        else:
            yield "Done."

    return FunctionModel(stream_function=_stream)


def writes_then(second_request: asyncio.Event, *, then: str | None = None) -> FunctionModel:
    """Asks for one file write, then either hangs in the next request or answers `then`."""
    requests = {"n": 0}

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        requests["n"] += 1
        if requests["n"] > 1:
            second_request.set()
            if then is None:
                await asyncio.Event().wait()
            yield then or ""
            return
        yield DeltaToolCalls(
            {0: DeltaToolCall(name="write_file", json_args=WRITE_ARGS, tool_call_id="c-write-1")}
        )

    return FunctionModel(stream_function=_stream)
