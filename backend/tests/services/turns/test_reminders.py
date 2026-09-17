"""THE PER-TURN RESTATEMENT IS GONE — this file is its inertness guard.

A cadence used to restate "which mode you are in" (full reminder every 8th turn, nudge every
4th) anchored on mode-switch markers. It went because a chat's kind is now fixed at creation —
there is no mode to be in, and the ABILITIES available already carry "which chat this is". The
delivery was also the wrong tier: a `user`-role message on a per-turn cadence, a named
cache-breaking action.

This file stays as the removal trace's last link: a long conversation runs through the real
engine and no restatement rides it. The workspace note that once used the same envelope is gone
too — the agent pulls that fact with `check_the_app` — so the guard is now a COUNT of what
reached the model rather than a search for wording, which is what stops "nothing was injected at
all" from satisfying it for the wrong reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.config import settings
from src.db.models.conversation import ChatKind
from src.services.agent import mode_prompts
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every kind pins the project's LIVE container now — a Plan turn attaches a sandbox
    exactly like a Build turn — so a turn dies at the workspace pin before the model ever runs
    unless a deployment is configured. Same wiring `test_engine.py` and `test_write_turn.py`
    carry, for the same reason.

    THIS IS WHAT MAKES THE GUARD BELOW HONEST rather than vacuously green: without it every
    assertion about "what reached the model" would be asserting over an empty list, because
    nothing reached the model at all."""
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
async def _sandbox_dependencies(fake_redis, fake_storage) -> None:
    """The liveness lease (Redis) and the attach's storage reads both need a backing fake
    now that every turn attaches a live container. One deployment fact, two fixtures."""
    return None


# What the retired cadence's anchors were. Kept as literals rather than imported — importing
# them is what this file exists to prove is impossible — so "run a long conversation" means the
# same lengths it always did, including the two that used to be guaranteed to speak.
_RETIRED_NUDGE_EVERY = 4
_RETIRED_FULL_EVERY = 8


def _turn(n: int) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=f"question {n}")]),
        ModelResponse(parts=[TextPart(content=f"answer {n}")]),
    ]


def _turns(count: int) -> list[ModelMessage]:
    return [message for n in range(count) for message in _turn(n)]


# --- the symbols themselves ------------------------------------------------------------


def test_no_restatement_machinery_survives_anywhere() -> None:
    """The named symbols are gone from both modules that carried them.

    Named one by one rather than as a `dir()` sweep: each of these was a separate decision to
    delete, and an implementer working from a "six reminders" description would leave the two
    HOLDING variants behind, unreferenced, where nothing would ever notice them."""
    for retired in (
        "mode_reminder",
        "_ASK_SEGMENT",
        "_ASK_REMINDER_FULL",
        "_ASK_REMINDER_NUDGE",
        "_PLAN_REMINDER_FULL",
        "_PLAN_REMINDER_NUDGE",
        "_PLAN_REMINDER_FULL_HOLDING",
        "_PLAN_REMINDER_NUDGE_HOLDING",
        "_WRITE_REMINDER_FULL",
        "_WRITE_REMINDER_NUDGE",
        # The workspace note went the same way and for a related reason: it was a `user`-role
        # message composed per turn and spliced ahead of the citizen's prompt. The agent pulls
        # the fact with `check_the_app` instead.
        "_PRIVATE",
        "workspace_note",
        "_WORKSPACE_NOTE_HEAD",
        "_WORKSPACE_NOTE_TAIL",
        "_WORKSPACE_UNKNOWN",
        "_WORKSPACE_NOT_SERVING",
        "_WORKSPACE_STILL_TEMPLATE",
        "_WORKSPACE_LIVE",
    ):
        assert not hasattr(mode_prompts, retired), retired
    for retired in (
        "_reminder_text",
        "REMINDER_FULL_EVERY",
        "REMINDER_NUDGE_EVERY",
        "_turns_since_mode_anchor",
        "_MODE_MARKER_PREFIX",
    ):
        assert not hasattr(engine_module, retired), retired


# --- the engine seam -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_engine():
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


async def _noop_persist() -> None:
    return None


async def _settle(engine: TurnEngine, conversation_id: uuid.UUID) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _run_with_history(
    engine: TurnEngine,
    db_session,
    session_factory,
    history: list[ModelMessage],
) -> tuple[list[list[ModelMessage]], uuid.UUID]:
    """One Plan turn through the real engine with a capturing model: returns every
    request-message list the model saw, and the conversation id.

    Run through the real engine rather than by calling the injection helper directly, because
    the thing being proved is what reaches the model — not what a helper returns."""
    seen: list[list[ModelMessage]] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        seen.append(list(messages))
        yield "noted."

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="and one more thing",
        history=history,
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        manager=SessionManager(),
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        sandbox_client=FakeSandboxClient(),
    )
    await _settle(engine, conv.id)
    return seen, conv.id


def _injected_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


@pytest.mark.parametrize("length", [0, 1, 3, _RETIRED_NUDGE_EVERY, _RETIRED_FULL_EVERY, 40])
async def test_no_turn_at_any_length_carries_a_restatement(
    _fresh_engine, db_session, session_factory, length: int
) -> None:
    """Including at both retired anchors, and including a forty-turn conversation.

    The two middle lengths are the ones that used to be GUARANTEED to speak — a nudge at four,
    a full restatement at eight — so a cadence left half-deleted would still fire here. Forty
    covers the "long conversation" the cadence existed for in the first place."""
    seen, _ = await _run_with_history(_fresh_engine, db_session, session_factory, _turns(length))
    dumped = ModelMessagesTypeAdapter.dump_json(seen[0]).decode()
    assert "mode is active" not in dumped
    assert "Plan mode" not in dumped
    assert "Write mode" not in dumped
    assert "Ask mode" not in dumped


async def test_nothing_at_all_is_spliced_between_the_history_and_the_citizens_prompt(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ THE PROPERTY THE WHOLE CACHING TRACK RESTS ON, at what used to be a cadence anchor: the
    request the model is handed is the stored history and this turn's prompt, and nothing else.

    A COUNT, NOT A SUBSTRING SEARCH. "No note" is satisfied by a note whose wording changed;
    "the history's own turns plus exactly one more, and that one is the prompt" is not. Anything
    appended at the tail of `message_history` sits AHEAD of the citizen's prompt — and the
    citizen's prompt is persisted while the appendage is not, so next turn the bytes vanish from
    the middle of the history and every message after them shifts.

    LIVENESS IS THE SAME ASSERTION. The count is `_RETIRED_FULL_EVERY + 1`, so a turn that
    reached the model with nothing at all fails it exactly as a turn that reached it with one
    thing too many does."""
    seen, _ = await _run_with_history(
        _fresh_engine, db_session, session_factory, _turns(_RETIRED_FULL_EVERY)
    )
    prompts = _injected_prompts(seen[0])
    assert prompts[-1] == "and one more thing"
    assert len(prompts) == _RETIRED_FULL_EVERY + 1, (
        f"the history's {_RETIRED_FULL_EVERY} turns plus this one's prompt is "
        f"{_RETIRED_FULL_EVERY + 1} user messages; the model was handed {prompts!r}"
    )


def test_a_system_prompt_bearing_request_is_never_persisted() -> None:
    """★ THE DOOR THE USER-PROMPT RULE DOES NOT CLOSE, guarded before anything walks through it.

    A turn-scoped system message is ephemeral by intent, but unlike injected history it would
    arrive on the run's OWN `new_messages()` — so the user-prompt exclusion never sees it and it
    lands in a row. Replayed next turn, pydantic-ai hoists the opening system parts of the first
    request into the top-level `system` field: the message changes tier AND position on rebuild,
    which is the prefix divergence this whole track exists to remove.

    The tool-return request beside it is the liveness half — a predicate that dropped everything
    would satisfy the first assertion and quietly stop persisting real results.

    Mutation check: drop `SystemPromptPart` from the predicate's exclusion and the first
    assertion goes red."""
    turn_scoped = ModelRequest(parts=[SystemPromptPart(content="the app is serving right now")])
    a_result = ModelRequest(
        parts=[ToolReturnPart(tool_name="read_file", content="export default", tool_call_id="a")]
    )
    kept = engine_module._persistable_messages([turn_scoped, a_result])
    assert turn_scoped not in kept, "a turn-scoped system message fossilized into the transcript"
    assert kept == [a_result]
