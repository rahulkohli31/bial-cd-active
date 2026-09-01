"""U3 / R75 / R75a / R76 — the one deliberate way the agent speaks in the middle of its work.

WHY A CHANNEL AT ALL. Prose written in the same response as a tool call does not reach the
user, in either kind (U4). That rule is right — it is what stopped 2,397 words of paths and
commands reaching a citizen — but on its own it leaves a turn that runs for two minutes with
nothing to read for the whole of it. `tell_the_user` is what the agent has instead: bounded by
the platform rather than by a sentence in a prompt, and rendered by the platform rather than by
the model choosing to write a paragraph.

★ THE PROPERTY THE WHOLE DESIGN TURNS ON is that the words sit at the position the CALL
occupies, in both emitters. Live order and reload order are then the same order by
construction rather than by two code paths agreeing to stay in step — and the tests that
matter most in this file are the ones that assert the two orders against each other, not the
ones that assert text arrived.
"""

from __future__ import annotations

import ast
import json
import pathlib
import uuid
from collections.abc import Sequence

import pytest
import sqlalchemy as sa
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartStartEvent,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.conversations.schemas import StepFrame, TextDeltaFrame
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.services.agent.conversation_tools import tell_the_user
from src.services.messages.projection import (
    UPDATE_MAX_CHARS,
    AssistantTextItem,
    DisplayItem,
    StepItem,
    project_rows,
    update_from_args,
)
from src.services.messages.store import append_batch
from src.services.turns.engine import TurnEngine, _TurnState
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_APP_WORDS = "Adding the status picker next to your search box now."


def _render_ctx() -> RunContext[None]:
    """A context the tool is handed and never reads.

    `RunContext` needs a model and pydantic-ai will not build one without it; a model that
    raises if it is ever asked for a response keeps "this tool calls nothing" honest rather
    than parking a live client in a test."""

    def _never(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("the voice channel calls no model")

    return RunContext(deps=None, model=FunctionModel(_never), usage=RunUsage())


def _state(kind: ChatKind) -> _TurnState:
    return _TurnState(
        turn_id=uuid.uuid7(),
        conversation_id=uuid.uuid7(),
        user_id=uuid.uuid7(),
        kind=kind,
    )


def _spoke(update: str, call_id: str = "s1") -> FunctionToolCallEvent:
    return FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="tell_the_user", args=json.dumps({"update": update}), tool_call_id=call_id
        )
    )


def _called(tool: str, args: str, call_id: str) -> FunctionToolCallEvent:
    return FunctionToolCallEvent(
        part=ToolCallPart(tool_name=tool, args=args, tool_call_id=call_id)
    )


def _live_shape(state: _TurnState) -> list[str]:
    """The live feed reduced to what a reader would see happen, in order.

    Text and steps only: this is the sequence a reload has to reproduce, and comparing shapes
    rather than raw frames keeps the two comparable across the frame types only one side has
    (workspace, compile, preview)."""
    shape: list[str] = []
    for frame in state.ring:
        if isinstance(frame, TextDeltaFrame):
            text = frame.text.strip()
            if text:
                shape.append(f"text:{text}")
        elif isinstance(frame, StepFrame) and frame.phase == "started":
            shape.append(f"step:{frame.item.tool}")
    return shape


def _reload_shape(items: Sequence[DisplayItem]) -> list[str]:
    shape: list[str] = []
    for item in items:
        if isinstance(item, AssistantTextItem):
            shape.append(f"text:{item.text.strip()}")
        elif isinstance(item, StepItem):
            shape.append(f"step:{item.tool}")
    return shape


async def _thread(db: AsyncSession, email: str, kind: ChatKind):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    conversation = await ConversationFactory.create(db, user.id, project_id=project.id, kind=kind)
    return user, conversation


async def _rows(db: AsyncSession, user, conversation) -> list[Message]:
    return list(
        (
            await db.scalars(
                sa.select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.seq)
            )
        ).all()
    )


# --- the bound, which is the platform's and not an instruction's ---------------------------


async def test_an_update_within_the_bound_is_acknowledged() -> None:
    """The model is told the words LANDED. Silence, or an echo, leaves it unsure whether to
    say the same thing again — and a repeated update is the one failure this channel can
    produce on its own."""
    assert (
        await tell_the_user(_render_ctx(), _APP_WORDS)
        == "Shown to the user. Carry on with the work."
    )


async def test_an_update_one_character_over_the_bound_is_refused_with_the_number() -> None:
    """★ R76 — the ceiling is enforced in the tool BODY, where nothing can route around it.

    ONE CHARACTER OVER, not obviously over: an off-by-one in the comparison is the mutation
    this is built to kill, and a 10,000-character fixture would pass against `>=`, `>`, and
    a limit accidentally doubled.

    The refusal NAMES the number and the register, so the retry has somewhere to go — a bare
    "too long" leaves the model guessing at a bound it cannot see."""
    with pytest.raises(ModelRetry) as refused:
        await tell_the_user(_render_ctx(), "x" * (UPDATE_MAX_CHARS + 1))
    assert str(UPDATE_MAX_CHARS) in str(refused.value)
    assert str(UPDATE_MAX_CHARS + 1) in str(refused.value)

    # And exactly at the bound is allowed — the other half of the off-by-one.
    assert await tell_the_user(_render_ctx(), "x" * UPDATE_MAX_CHARS)


async def test_an_empty_update_is_refused_rather_than_shown_as_nothing() -> None:
    with pytest.raises(ModelRetry):
        await tell_the_user(_render_ctx(), "   ")


def test_one_rule_decides_what_is_shown_and_both_emitters_read_it() -> None:
    """★ THE SINGLE SOURCE, asserted directly. `update_from_args` is what both emitters call,
    so a refused update renders nowhere without either of them holding its own copy of the
    ceiling. Its answers must line up with the tool body's, or a call the body refused could
    still paint text on a reloaded transcript."""
    assert update_from_args({"update": _APP_WORDS}) == _APP_WORDS
    assert update_from_args(json.dumps({"update": _APP_WORDS})) == _APP_WORDS
    assert update_from_args({"update": "x" * (UPDATE_MAX_CHARS + 1)}) is None
    assert update_from_args({"update": "  "}) is None
    assert update_from_args({"update": 7}) is None
    assert update_from_args("not json at all") is None
    assert update_from_args(None) is None


# --- live and reload, at the same position -------------------------------------------------


@pytest.mark.parametrize("kind", list(ChatKind))
def test_a_spoken_line_reaches_the_live_feed_in_either_kind(kind: ChatKind) -> None:
    """R75 — the channel is on both arms, and behaves identically on both."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke(_APP_WORDS))

    assert _APP_WORDS in "".join(state.text_parts)


@pytest.mark.parametrize("kind", list(ChatKind))
def test_speaking_is_not_a_step_and_never_shows_its_wire_name(kind: ChatKind) -> None:
    """★ The transcript shows what was SAID, never a row announcing that the agent decided to
    say it — and never `Used tell_the_user`, which is what `_step_label`'s fallback prints for
    any tool it does not know. That fallback is the live emitter's too, so a tool without an
    explicit branch leaks its wire name into a citizen's feed on both sides."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke(_APP_WORDS))

    assert _live_shape(state) == [f"text:{_APP_WORDS}"]
    assert state.steps == {}
    assert "tell_the_user" not in "".join(state.text_parts)


@pytest.mark.parametrize("kind", list(ChatKind))
def test_an_over_long_update_reaches_the_live_feed_nowhere(kind: ChatKind) -> None:
    """The refusal is structural on the live side too: the emitter asks the same function the
    body asks, so there is no window in which over-long text is painted and then retracted."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke("x" * (UPDATE_MAX_CHARS + 1)))

    assert state.text_parts == []
    assert state.steps == {}


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_live_order_and_reload_order_are_the_same_order(
    db_session: AsyncSession, kind: ChatKind
) -> None:
    """★★ THE SCENARIO THIS DESIGN EXISTS FOR (R75a / R76), and the one that would have caught
    the shape it replaces.

    A turn that reads, speaks, reads again and speaks again must produce a reloaded transcript
    whose ITEM ORDER equals the live FRAME ORDER — step, text, step, text — not merely one
    containing the same words. Rendering the spoken line at the tool RESULT event would pass a
    "the text is there" assertion and fail this one: tool bodies run concurrently and their
    results arrive in completion order, while the projection renders in part order.

    Both emitters read the stored CALL, at the position the call occupies. That is the
    `present_plan_options` shape, and it is why the two orders cannot disagree."""
    user, conversation = await _thread(db_session, f"vc-{kind.value}@rvaiglobal.com", kind)
    engine = TurnEngine()
    state = _state(kind)

    first, second = "Looking at how your visitor list works today.", _APP_WORDS
    events = [
        _called("read_file", '{"path": "app/page.tsx"}', "r1"),
        _spoke(first, "s1"),
        _called("read_file", '{"path": "app/list.tsx"}', "r2"),
        _spoke(second, "s2"),
    ]
    for event in events:
        engine._on_event(state, event)

    # The same response, persisted the way the run would persist it.
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(parts=[event.part for event in events]),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="read_file", content="1\tx", tool_call_id="r1"),
                    ToolReturnPart(tool_name="tell_the_user", content="ok", tool_call_id="s1"),
                    ToolReturnPart(tool_name="read_file", content="1\ty", tool_call_id="r2"),
                    ToolReturnPart(tool_name="tell_the_user", content="ok", tool_call_id="s2"),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=kind,
    )
    reloaded = _reload_shape(project_rows(await _rows(db_session, user, conversation)))

    expected = [
        "step:read_file",
        f"text:{first}",
        "step:read_file",
        f"text:{second}",
    ]
    assert _live_shape(state) == expected
    assert reloaded == expected


async def test_a_refused_update_renders_nothing_on_reload_either(
    db_session: AsyncSession,
) -> None:
    """The reload half of the refusal, and it does NOT consult the stored tool return.

    Reading the return would have been the obvious way to tell a refused call from an accepted
    one — and it answers the wrong question: a turn cut short before the return landed still
    SAID the words, and the citizen saw them. The argument is what decides, on both sides."""
    user, conversation = await _thread(db_session, "vc-refused@rvaiglobal.com", ChatKind.BUILD)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="tell_the_user",
                        args=json.dumps({"update": "x" * (UPDATE_MAX_CHARS + 1)}),
                        tool_call_id="s1",
                    ),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/page.tsx"}', tool_call_id="r1"
                    ),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    assert [i for i in items if isinstance(i, AssistantTextItem)] == []
    # LIVENESS: the row projected at all — an empty text list is also what a crashed
    # projection returns.
    assert [i.tool for i in items if isinstance(i, StepItem)] == ["read_file"]


async def test_the_spoken_line_survives_a_response_that_also_wrote_prose(
    db_session: AsyncSession,
) -> None:
    """★ THE INTERACTION WITH U4, in one row. A response that narrates, speaks and calls a
    tool loses the narration and keeps the spoken line — which is the whole point of having a
    channel: the drop takes the words the model wrote at itself and leaves the words it
    deliberately addressed to the user."""
    user, conversation = await _thread(db_session, "vc-both@rvaiglobal.com", ChatKind.PLAN)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content="Let me check the Drizzle schema in db/schema.ts."),
                    ToolCallPart(
                        tool_name="tell_the_user",
                        args=json.dumps({"update": _APP_WORDS}),
                        tool_call_id="s1",
                    ),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "db/schema.ts"}', tool_call_id="r1"
                    ),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == [_APP_WORDS]
    assert "Drizzle" not in " ".join(i.text for i in items if isinstance(i, AssistantTextItem))


# --- U8 / R78: nothing the platform writes privately reaches the screen ----------------------
#
# THIS CHECKS OUR OWN STRINGS, NOT THE MODEL'S VOCABULARY, and that distinction is the one R82
# and L1 both turn on. A denylist over what the agent wrote would be a word filter over model
# text, which this plan rejects everywhere. A denylist over what the PLATFORM wrote is
# legitimate precisely because we own both ends: we know exactly what we sent, so we can say
# exactly what must not come back.
#
# The `_PRIVATE` sentence exists because the model once quoted a note back at a citizen. It
# rides whatever private note survives, and it is an instruction rather than a guardrail — the
# guard is the mechanism below.

_PRIVATE_NOTES = {
    "the workspace note's frame": "<system-note>",
    "the workspace note's own instruction": "This note is between you and the platform",
    "a not-serving verdict": "the app is not currently serving",
    "a still-template verdict": "byte-for-byte the starter template",
    "the repair prompt": "The build is not green yet",
    "the continue nudge": "you ended your turn without calling `declare_done`",
    "declare_done's acknowledgement": "Acknowledged — that summary is now the closing message",
    "the voice channel's acknowledgement": "Shown to the user. Carry on with the work.",
    # THE REFUSALS ARE PRIVATE NOTES TOO, and U8's Approach names them alongside the workspace
    # note. They are written to steer the AGENT — "use `read_file`, `list_files`, and
    # `search_files` instead" is advice about a toolset the citizen cannot see and has no way
    # to act on. A refusal quoted into a reply reads as the platform telling the person who
    # asked for a visitor list that their package manager is unavailable.
    "the guest-list refusal": "is not on the guest list",
    "the guest-list refusal's advice to the agent": "Use `read_file`, `list_files`",
    "a denied-flag refusal": "is not available to a read-only `run_command`",
    "the empty-argv refusal": "pass argv tokens",
}


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_no_private_note_reaches_the_transcript_even_when_the_model_quotes_it(
    db_session: AsyncSession, kind: ChatKind
) -> None:
    """★ R78 — the mechanism, not the instruction.

    The model here does the worst thing available to it: quotes a private note back, verbatim,
    in prose beside a tool call. The drop removes it, in both kinds, in both emitters.

    ★ THE RESIDUAL GAP, NAMED RATHER THAN PAPERED OVER. This closes the case where the quote
    sits BESIDE a tool call, which is where narration lives and where the incident happened. A
    model that quoted a note in a response calling no tool would be writing it as its answer,
    and that text reaches the citizen — the `_PRIVATE` instruction is all that stands there,
    and an instruction is not a guardrail. Making it one would mean scanning model prose for
    platform strings and silently deleting the citizen's answer around them, which is a worse
    failure than the one it prevents. Observed, per R92, not asserted."""
    user, conversation = await _thread(db_session, f"pn-{kind.value}@rvaiglobal.com", kind)
    quoted = (
        "<system-note>The platform checked this app's workspace just now: the app is not "
        "currently serving. This note is between you and the platform — keep it out of your "
        "reply.</system-note> Right, let me look."
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content=quoted),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/page.tsx"}', tool_call_id="r1"
                    ),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=kind,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    rendered = " ".join(i.text for i in items if isinstance(i, AssistantTextItem))
    for where, fragment in _PRIVATE_NOTES.items():
        assert fragment not in rendered, f"{where} reached the transcript"
    # LIVENESS: the row projected at all. An empty transcript passes every absence assertion
    # above for the wrong reason, which is the failure mode this pairing exists to prevent.
    assert [i.tool for i in items if isinstance(i, StepItem)] == ["read_file"]


def test_the_live_emitter_drops_the_same_quoted_note() -> None:
    """The other emitter, on the same shape. Two emitters that disagree is the split this
    plan's whole rendering design exists to make impossible, and an absence checked on only
    one of them is an absence checked nowhere."""
    engine = TurnEngine()
    state = _state(ChatKind.BUILD)

    engine._on_event(
        state,
        PartStartEvent(
            index=0,
            part=TextPart(content="<system-note>the app is not currently serving</system-note>"),
        ),
    )
    engine._on_event(
        state,
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="read_file", args='{"path": "app/page.tsx"}', tool_call_id="r1"
            )
        ),
    )
    engine._flush_pending_text(state)

    assert state.steps, "no step — the seam under test never ran"
    assert "system-note" not in "".join(state.text_parts)


def test_a_tool_acknowledgement_is_never_user_visible_text() -> None:
    """The acknowledgements are written for the model and read like it. They travel as tool
    RETURNS, which no emitter renders as text — the reload projection reads a return only to
    decide whether a step succeeded, and the live one only to resolve a step's state."""
    from src.services.agent.conversation_tools import _SHOWN

    engine = TurnEngine()
    state = _state(ChatKind.PLAN)
    engine._on_event(
        state,
        FunctionToolResultEvent(
            part=ToolReturnPart(tool_name="tell_the_user", content=_SHOWN, tool_call_id="s1")
        ),
    )

    assert "".join(state.text_parts) == ""
    # LIVENESS, because "no text" is also what an event the handler ignored produces. The
    # channel's own words DO reach the screen from the CALL — so the same state, given the
    # call, is not silent, and this absence is about the return specifically.
    engine._on_event(state, _spoke(_APP_WORDS, "s2"))
    assert _APP_WORDS in "".join(state.text_parts)


# --- U7 / R82: on the turn that went wrong, what the user reads is ours ----------------------


def test_every_ending_this_plan_can_reach_is_a_platform_sentence() -> None:
    """★ R82's honest structural half, and the test docstring says which half.

    ON A TURN THAT ENDS BADLY, THE MODEL'S REGISTER IS NOT LOAD-BEARING, because the model
    wrote none of what the citizen reads. Every ending this plan introduces or touches resolves
    by IDENTITY to a constant in `copy.py` — asserted by identity rather than by inspecting
    words, so it cannot pass because a sentence happened to sound right.

    WHAT THIS IS NOT. It asserts over constants we wrote, so it cannot fail because of anything
    the model produced. The recorded incident was not a failure ending at all: it was a build
    that hit errors, recovered, and narrated 2,397 words while CONTINUING. That shape is
    addressed by the drop (the narration never reaches the screen) and by the voice channel
    being length-bounded and platform-rendered — structurally, where it can be — and by
    `NARRATION_VOICE` otherwise, which is observed rather than asserted (R92). The composition
    guards in `test_mode_prompts.py` prevent a real drift failure and are NOT evidence that the
    contract holds; this platform shipped that confusion once."""
    from src.services.turns import copy as copy_module

    endings = {
        copy_module.NOTHING_TO_SHOW_YET_TEXT,
        copy_module.PLAN_NOT_KEPT_TEXT,
        copy_module.DID_NOT_COME_TOGETHER_TEXT,
        copy_module.COULD_NOT_CONFIRM_TEXT,
        copy_module.NOT_RECOVERED_TEXT,
        copy_module.RECOVERED_TEXT,
        copy_module.COULD_NOT_CHECK_TEXT,
        copy_module.AT_LIMIT_TEXT,
        copy_module.SPENT_ENOUGH_TEXT,
        copy_module.REMAINDER_TEXT,
        copy_module.CANNOT_TELL_WHAT_REMAINS_TEXT,
    }
    # Every one of them is a constant IN that module, which is what puts it inside the jargon
    # guard. A sentence written inline at its call site would be outside it by construction.
    in_module = {
        value
        for name, value in vars(copy_module).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    assert endings <= in_module

    # And none of them is empty or a placeholder — an ending that says nothing is the failure
    # R77 is about, arriving through the other door.
    for ending in endings:
        assert ending.strip()


def test_the_word_prompt_appears_in_no_claim_that_the_contract_holds() -> None:
    """★ R82's inertness half. The tempting test — "the composed prompt contains the audience
    block, therefore the agent speaks plainly" — is the confusion that let a 2,397-word reply
    ship under a green suite: it proves an instruction was PRESENT, which is the one thing
    nobody doubted.

    So: no test in this file or its neighbours asserts the contract by reaching for a prompt.
    The composition guards live in `test_mode_prompts.py` and say in their own docstrings that
    they are about drift between two prompts, not about what the model then wrote."""
    here = pathlib.Path(__file__).resolve().parent
    for path in (here / "test_voice_channel.py", here / "test_scope_negotiation.py"):
        # PARSED, NOT GREPPED, because this very docstring names the function it is banning and
        # a substring scan reports the guard as its own first offender.
        called = {
            node.func.id
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "compose_kind_prompt" not in called, (
            f"{path.name} reaches for a composed prompt. Whether an instruction is present is "
            "not evidence that the contract it states holds."
        )
