"""THE PER-TURN RESTATEMENT IS GONE — this file is its inertness guard (R17).

WHAT USED TO BE HERE. A cadence: a full restatement of "which mode you are in" every eighth
turn in the mode, a one-line nudge every fourth between, anchored on the hidden mode-switch
marker rows so a switch reset the count and the new mode's first turn got an immediate full
reminder. Eight reminder strings, two engine constants, a marker scanner, and a card-state
gate that decided which of the Plan variants was safe to send.

WHY IT WENT. It was restating a thing that no longer exists. A chat's kind is fixed when it is
created, so there is no mode to be in and no boundary to re-anchor on; and having a different
set of ABILITIES is what carries "which chat this is" — the model cannot call a tool it was not
handed, whatever it was last told. The delivery was also the wrong tier: a `user`-role message
on a per-turn cadence, which is a named cache-breaking action.

WHY THIS FILE STAYS. Deleting a test suite deletes the evidence that the thing is gone. The
repo's convention (`docs/solutions/conventions/cleanly-removing-dead-ui-controls-2026-06-23.md`)
is that the last link of a removal trace is a guard, not an absence. So: a long conversation
runs through the real engine and no restatement rides it — and the workspace note, which shares
the same envelope and the same injection mechanism but is a different claim entirely, still
rides EVERY turn. That second half is what stops this guard from passing for the wrong reason,
because "nothing was injected at all" would satisfy the first half on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import Message
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
    """Every kind pins the project's LIVE container now (R18) — a Plan turn attaches a sandbox
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
    """The R10 liveness lease (Redis) and the attach's storage reads both need a backing fake
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


def test_the_private_note_marker_outlived_them_and_still_composes_the_workspace_note() -> None:
    """`_PRIVATE` is the one constant from that block that stays, and this says why.

    It is composed into the workspace note's tail as well, so an implementer deleting "the
    reminder constants" as a group takes with it the sentence that tells the model the
    workspace note is internal — and the model narrates it back at the citizen, which is the
    recorded defect that put the sentence there."""
    note = mode_prompts.workspace_note(serving=True, still_the_template=False)
    assert "between you and the platform" in note


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


async def test_exactly_one_thing_is_injected_and_it_is_the_workspace_note(
    _fresh_engine, db_session, session_factory
) -> None:
    """At what used to be a cadence anchor: one extra history message, not two.

    A count, not a substring search. "No reminder" is satisfied by a reminder whose wording
    changed; "exactly one injected message, and it is the note" is not."""
    seen, _ = await _run_with_history(
        _fresh_engine, db_session, session_factory, _turns(_RETIRED_FULL_EVERY)
    )
    prompts = _injected_prompts(seen[0])
    # The history's own user turns, then the note, then this turn's prompt.
    assert prompts[-1] == "and one more thing"
    assert "checked this app's workspace just now" in prompts[-2]
    assert len(prompts) == _RETIRED_FULL_EVERY + 2


async def test_the_workspace_note_still_rides_a_turn_off_any_anchor(
    _fresh_engine, db_session, session_factory
) -> None:
    """This is the half that keeps the guard honest: a change that stopped injecting anything
    at all would pass every assertion above.

    A PLAN CHAT, and the Build half is asserted where the Build harness lives
    (`test_write_turn.py::test_the_workspace_note_rides_a_build_turn_too`) rather than here.
    The note is injected once, above the branch that picks the run loop, so both kinds get the
    same message — but a Build turn takes the node loop and needs a provisioned container to
    reach its first model request, and standing that up in this file to re-prove one line would
    duplicate a harness rather than test anything new."""
    seen, _ = await _run_with_history(_fresh_engine, db_session, session_factory, _turns(3))
    dumped = ModelMessagesTypeAdapter.dump_json(seen[0]).decode()
    assert "checked this app's workspace just now" in dumped


async def test_nothing_ephemeral_reaches_a_persisted_row(
    _fresh_engine, db_session, session_factory
) -> None:
    """The durable side, at what used to be a cadence point.

    `new_messages()` structurally excludes injected history, so this holds by construction
    rather than by a filter — and it is worth keeping now that the note is the only passenger,
    because the note is the one thing left that a future author could be tempted to persist."""
    _, conversation_id = await _run_with_history(
        _fresh_engine, db_session, session_factory, _turns(_RETIRED_FULL_EVERY)
    )
    rows = (
        await db_session.scalars(
            sa.select(Message).where(Message.conversation_id == conversation_id)
        )
    ).all()
    assert rows, "the reply row must have landed"
    dumped = json.dumps([row.payload for row in rows])
    assert "system-note" not in dumped
    assert "mode is active" not in dumped
