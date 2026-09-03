"""U3 / R75 / R75a — the one deliberate way the agent speaks in the middle of its work.

WHY A CHANNEL AT ALL, NOW THAT EVERY PARAGRAPH REACHES THE CITIZEN. Free prose is streamed as
the model writes it, so this tool is no longer the only way words get through — it is the only
way words get through DURING A GAP. Nothing streams while a tool body runs: the model has
stopped writing and will not write again until the result comes back, so a turn that installs
a package for ninety seconds has nothing to show for the whole of it unless it said something
before it started. `tell_the_user` is what it says then, rendered by the platform from the
call's own arguments rather than left to the model choosing to write a paragraph.

WHAT THE CHANNEL NO LONGER DOES IS COUNT. It used to carry a 280-character ceiling and the
renderer carried a copy of the same number, so an update one character over it was refused at
the body AND deleted on the way out: the model was told to retry and the citizen was shown
silence exactly where the agent had spoken. How long a sentence about someone's app should be
is a judgement about the person waiting, which is the thing the agent is for. What survives is
the one refusal that is not taste — an update carrying no words at all — and this file is
arranged around that split: the long-update tests say what a citizen now reads, and the
empty-update tests say what still reaches nobody.

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
from typing import Final

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
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.services.agent.conversation_tools import tell_the_user
from src.services.messages.projection import (
    AssistantTextItem,
    StepItem,
    project_rows,
    update_from_args,
)
from src.services.messages.store import append_batch
from src.services.turns.engine import TurnEngine, _TurnState
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.transcript import live_shape, reload_shape, rendered_text

_APP_WORDS = "Adding the status picker next to your search box now."

_RETIRED_CEILING: Final = 280
"""The number that used to be a ceiling on an update, written out here because there is no
constant left to import.

It is kept as a fixture rather than dropped because "long" on its own is not a bound anything
can be tested against: one character past this number is what kills a straight re-introduction
of the rule, and the paragraph below — comfortably longer, and in the register a citizen
actually reads — is what kills one re-introduced at a more generous figure. A run of `x` would
prove the length and nothing about the words surviving."""

_LONG_APP_WORDS: Final = (
    "I have put the status picker beside your search box and wired it to the same filter the "
    "list already uses, so choosing a status narrows the list straight away. I left the empty "
    "state alone for now, which means an unfiltered list still reads exactly the way it does "
    "today. Next I am going to look at the visitor table's own filters, because those are the "
    "ones that will have to agree with this new picker."
)


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


# --- what the body still refuses, and what it now lets through -----------------------------


async def test_an_update_is_acknowledged_so_the_model_does_not_say_it_twice() -> None:
    """The model is told the words LANDED. Silence, or an echo, leaves it unsure whether to
    say the same thing again — and a repeated update is the one failure this channel can
    produce on its own."""
    assert (
        await tell_the_user(_render_ctx(), _APP_WORDS)
        == "Shown to the user. Carry on with the work."
    )


async def test_a_long_update_is_accepted_rather_than_refused_for_its_length() -> None:
    """★ U6 — the ceiling is gone from the tool BODY, where it used to be enforced.

    TWO FIXTURES, BECAUSE A REINSTATED CEILING COULD SIT ANYWHERE. One character past the
    retired number kills a straight re-introduction of it — including the off-by-one variants,
    since `>` and `>=` both refuse at 281 — and a four-hundred-character paragraph kills one
    re-introduced at a more generous figure. Both come back with the ordinary acknowledgement,
    which is the whole assertion: length is no longer something this body has an opinion about.

    Mutation check: restore a `len(text) > 280` refusal in `tell_the_user` and both calls raise
    `ModelRetry` instead of returning."""
    assert len(_LONG_APP_WORDS) > _RETIRED_CEILING, "the long fixture has shrunk under the number"
    shown = "Shown to the user. Carry on with the work."
    assert await tell_the_user(_render_ctx(), "x" * (_RETIRED_CEILING + 1)) == shown
    assert await tell_the_user(_render_ctx(), _LONG_APP_WORDS) == shown


async def test_an_empty_update_is_still_refused_rather_than_shown_as_nothing() -> None:
    """The one refusal that survived U6, and it survived because it is not a matter of taste.

    A call carrying no words has nothing for either emitter to draw, so the alternatives are
    an empty block in the transcript or a silent no-op the model never learns about. The retry
    is what tells it — and it is the arm the whitespace case rides too, because a model that
    sends a space believes it spoke."""
    with pytest.raises(ModelRetry):
        await tell_the_user(_render_ctx(), "")
    with pytest.raises(ModelRetry):
        await tell_the_user(_render_ctx(), "   ")


def test_one_rule_decides_what_is_shown_and_both_emitters_read_it() -> None:
    """★ THE SINGLE SOURCE, asserted directly. `update_from_args` is what both emitters call,
    so what a call renders is decided in one place rather than two. Its answers must line up
    with the tool body's, or a call the body refused could still paint text on a reloaded
    transcript while the model was being told to retry.

    WHAT IT DECIDES IS WHETHER THERE ARE WORDS, NEVER HOW MANY. The long update comes back
    byte-for-byte, and that is the renderer's half of U6: taking the ceiling out of the body
    alone would have taught the model it may write at length while this function went on
    deleting what it wrote — the same two-sided failure in reverse.

    Mutation check: put `if len(text) > 280: return None` back here and only the long-update
    line goes red, which is precisely how the original defect stayed invisible."""
    assert update_from_args({"update": _APP_WORDS}) == _APP_WORDS
    assert update_from_args(json.dumps({"update": _APP_WORDS})) == _APP_WORDS
    assert update_from_args({"update": _LONG_APP_WORDS}) == _LONG_APP_WORDS
    assert update_from_args({"update": "  "}) is None
    assert update_from_args({"update": 7}) is None
    assert update_from_args("not json at all") is None
    assert update_from_args(None) is None


# --- live and reload, at the same position -------------------------------------------------


@pytest.mark.parametrize("kind", list(ChatKind))
def test_a_spoken_line_reaches_the_live_feed_in_either_kind(kind: ChatKind) -> None:
    """R75 — the channel is on both arms, and behaves identically on both.

    AS ITS OWN BLOCK, which is the half a substring check would miss. A platform-rendered line
    always opens a block, so it can never be glued onto the end of whatever paragraph the model
    happened to be writing when it called."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke(_APP_WORDS))

    assert state.text_blocks() == [_APP_WORDS]


@pytest.mark.parametrize("kind", list(ChatKind))
def test_speaking_is_not_a_step_and_never_shows_its_wire_name(kind: ChatKind) -> None:
    """★ The transcript shows what was SAID, never a row announcing that the agent decided to
    say it — and never `Used tell_the_user`, which is what `_step_label`'s fallback prints for
    any tool it does not know. That fallback is the live emitter's too, so a tool without an
    explicit branch leaks its wire name into a citizen's feed on both sides."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke(_APP_WORDS))

    assert live_shape(state) == [f"text:{_APP_WORDS}"]
    assert state.steps == {}
    assert "tell_the_user" not in rendered_text(state)


@pytest.mark.parametrize("kind", list(ChatKind))
def test_a_long_update_reaches_the_live_feed_whole(kind: ChatKind) -> None:
    """★ U6 ON THE LIVE EMITTER. A four-hundred-character update arrives as ONE block holding
    every character of it: not refused, not clipped, and not silently dropped, which is what
    the emitter did for as long as it asked a function that counted.

    COMPARED AS THE WHOLE BLOCK rather than by prefix, because a ceiling reinstated as a
    truncation rather than a refusal would satisfy `startswith` and fail this."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke(_LONG_APP_WORDS))

    assert state.text_blocks() == [_LONG_APP_WORDS]
    # And it is still not a step at any length — the row this channel exists to avoid does not
    # come back just because the update got long.
    assert state.steps == {}


@pytest.mark.parametrize("kind", list(ChatKind))
def test_an_empty_update_reaches_the_live_feed_nowhere(kind: ChatKind) -> None:
    """The surviving refusal is structural on the live side too: the emitter asks the same
    function the body asks, so there is no window in which a wordless call paints an empty
    block and is then retracted."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke("   "))

    # NOTHING TOOK A POSITION AT ALL, which is stronger than "no text": a call carrying no
    # words must not leave a step ref behind either, or the turn would render an empty gap
    # where the words would have been.
    assert state.parts == []
    assert state.steps == {}
    # LIVENESS, because an empty parts list is also what an event the handler ignored produces.
    # The same state, given an update with words in it, renders it.
    engine._on_event(state, _spoke(_APP_WORDS, "s2"))
    assert state.text_blocks() == [_APP_WORDS]


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_live_order_and_reload_order_are_the_same_order(
    db_session: AsyncSession, kind: ChatKind
) -> None:
    """★★ THE SCENARIO THIS DESIGN EXISTS FOR (R75a), and the one that would have caught
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
    # Both reads open VISIBLE steps now, and a visible step arms the stillness narrator; put
    # them down rather than leave two tasks running past the test that started them.
    await engine._drain_long_operations(state)

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
    reloaded = reload_shape(project_rows(await _rows(db_session, user, conversation)))

    expected = [
        "step:read_file",
        f"text:{first}",
        "step:read_file",
        f"text:{second}",
    ]
    assert live_shape(state) == expected
    assert reloaded == expected


async def test_a_long_update_renders_whole_on_reload_too(db_session: AsyncSession) -> None:
    """★ U6's OTHER EMITTER, because a rendering checked on only one of them is checked
    nowhere. The same four-hundred-character update that reaches the live feed whole is read
    back off the stored CALL and drawn whole, so a citizen who reloads gets the sentence they
    watched arrive rather than a shorter one — or, as it used to be, nothing at all where the
    agent had spoken.

    THE STEP BESIDE IT IS THE LIVENESS HALF: a projection that crashed on the row would also
    produce no truncated text."""
    user, conversation = await _thread(db_session, "vc-long@rvaiglobal.com", ChatKind.BUILD)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="tell_the_user",
                        args=json.dumps({"update": _LONG_APP_WORDS}),
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

    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == [_LONG_APP_WORDS]
    assert [i.tool for i in items if isinstance(i, StepItem)] == ["read_file"]


async def test_an_empty_update_renders_nothing_on_reload_either(
    db_session: AsyncSession,
) -> None:
    """The reload half of the surviving refusal, and it does NOT consult the stored tool return.

    Reading the return would have been the obvious way to tell a refused call from an accepted
    one — and it answers the wrong question: a turn cut short before the return landed still
    SAID the words, and the citizen saw them. The argument is what decides, on both sides."""
    user, conversation = await _thread(db_session, "vc-empty@rvaiglobal.com", ChatKind.BUILD)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="tell_the_user",
                        args=json.dumps({"update": "   "}),
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


async def test_prose_and_a_spoken_line_in_one_response_both_land_in_the_order_written(
    db_session: AsyncSession,
) -> None:
    """★ THE INTERACTION WITH FREE PROSE, in one row, on both emitters.

    A response that writes a paragraph, speaks through the channel and then calls a tool keeps
    all three, and ORDER is the assertion: the paragraph is pushed at the TEXT event, the
    spoken line at the tool CALL event, and the step takes the position after both. The
    paragraph used to be deleted here, on the rule that prose beside a tool call is the model
    narrating its way to the call — which threw away the explanation joining the receipts. What
    the model wrote and what it deliberately addressed to the citizen read the same to a person,
    and both are theirs.

    COMPARED AS SEQUENCES, never as a joined string: a join passes whether or not the two
    blocks were interleaved with the step correctly, and the interleaving is the whole of what
    changed. That the channel still renders its own block rather than being folded into the
    paragraph above it is the last assertion."""
    user, conversation = await _thread(db_session, "vc-both@rvaiglobal.com", ChatKind.PLAN)
    narration = "Let me check the Drizzle schema in db/schema.ts."
    spoken_then_read = [
        _spoke(_APP_WORDS, "s1"),
        _called("read_file", '{"path": "db/schema.ts"}', "r1"),
    ]
    engine = TurnEngine()
    state = _state(ChatKind.PLAN)
    engine._on_event(state, PartStartEvent(index=0, part=TextPart(content=narration)))
    for event in spoken_then_read:
        engine._on_event(state, event)
    # The read opens a VISIBLE step now, which arms the stillness narrator; put it down rather
    # than leave a task running past the test that started it.
    await engine._drain_long_operations(state)

    # The same response, persisted the way the run would persist it.
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content=narration),
                    *[event.part for event in spoken_then_read],
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    expected = [f"text:{narration}", f"text:{_APP_WORDS}", "step:read_file"]
    assert live_shape(state) == expected
    assert reload_shape(items) == expected
    assert state.text_blocks() == [narration, _APP_WORDS]


# --- U8 / R78: the platform never puts one of its own notes on the wire ----------------------
#
# THIS CHECKS OUR OWN STRINGS, NOT THE MODEL'S VOCABULARY, and that distinction is the one R82
# and L1 both turn on. A denylist over what the agent wrote would be a word filter over model
# text, which this plan rejects everywhere. A denylist over what the PLATFORM wrote is
# legitimate precisely because we own both ends: we know exactly what we sent, so we can say
# exactly what must not come back.
#
# WHAT THIS SECTION NO LONGER COVERS, STATED FIRST. R78 used to be enforced by the drop: a note
# the model quoted back sat in prose beside a tool call, and prose beside a tool call was
# deleted. Deleting that hold is the whole of this change, and it takes the quote with it — a
# fence cannot be stripped back out of a token stream without re-introducing the hold, and
# scanning model prose for platform strings would mean silently deleting the citizen's answer
# around them, which is a worse failure than the one it prevents. So for a QUOTED note the
# `_PRIVATE` sentence composed into the workspace note is now the whole fence, and it is an
# instruction rather than a guardrail. Accepted knowingly.
#
# WHAT IS STILL A MECHANISM is the CARRIER each of these rides, and not one of them is a thing
# an emitter renders as the agent's voice. The acknowledgements come back as tool RETURNS and
# the refusals as RETRY PROMPTS — durable, and rendered as text by neither emitter, which is
# what the last test in this section drives. The workspace note rides an injected history tail
# `new_messages()` structurally excludes, asserted in
# `tests/services/turns/test_reminders.py::test_nothing_ephemeral_reaches_a_persisted_row`. The
# repair prompt and the continue nudge are stored by `_persist_write_reprompt` as HIDDEN rows,
# and a hidden row renders nothing. So the platform's own half is still structural everywhere;
# only the model quoting one back is not.

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
async def test_a_note_the_model_quotes_back_reaches_the_citizen_like_any_other_prose(
    db_session: AsyncSession, kind: ChatKind
) -> None:
    """★ THE CONSEQUENCE OF DELETING THE HOLD, pinned rather than left to be discovered.

    The model here does the worst thing available to it: quotes a private note back, verbatim,
    in prose beside a tool call. That prose used to be deleted — not because it was a note, but
    because a tool call followed it — and R78 rode on that deletion. The deletion is gone in
    both kinds, so the quote lands in the transcript, at the position it was written, exactly
    like every other paragraph.

    THE HALF THAT IS NOW PROMPT-ONLY, said plainly: `_PRIVATE` — "keep it out of your reply" —
    is all that asks the model not to do this. It is an instruction, not a guardrail, and this
    test is what makes that visible instead of implied. The half that is still structural has
    its own test below: the platform's own emitters never put one of these strings on the wire.

    Asserted as a SEQUENCE so the position is pinned too, and the step in it is the liveness
    half — a projection that returned nothing would satisfy a bare substring check."""
    user, conversation = await _thread(db_session, f"pn-quoted-{kind.value}@rvaiglobal.com", kind)
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

    assert reload_shape(items) == [f"text:{quoted}", "step:read_file"]


async def test_the_live_emitter_shows_the_same_quoted_note_in_the_same_place() -> None:
    """The other emitter, on the same shape. Two emitters that disagree is the split this
    plan's whole rendering design exists to make impossible, and a rendering checked on only
    one of them is a rendering checked nowhere. The quote reaches both, as one block, before
    the step it was written beside.

    ASYNC BECAUSE A VISIBLE STEP ARMS A NARRATOR. A read is no longer hidden, and opening a
    step the citizen can see starts the stillness narrator as a task — so this needs a loop to
    start one in and a drain to put it down again, rather than leaving a task alive past the
    test that owns it."""
    engine = TurnEngine()
    state = _state(ChatKind.BUILD)
    quoted = "<system-note>the app is not currently serving</system-note> Right, let me look."

    engine._on_event(state, PartStartEvent(index=0, part=TextPart(content=quoted)))
    engine._on_event(state, _called("read_file", '{"path": "app/page.tsx"}', "r1"))
    await engine._drain_long_operations(state)

    assert live_shape(state) == [f"text:{quoted}", "step:read_file"]
    assert state.text_blocks() == [quoted]


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_no_note_the_platform_wrote_is_rendered_from_the_result_it_rides_on(
    db_session: AsyncSession, kind: ChatKind
) -> None:
    """★ R78's surviving MECHANISM: the carrier, not the vocabulary.

    A tool RESULT is rendered as text by neither emitter. The reload projection indexes one
    only to decide whether a step succeeded; the live emitter reads one bit off it — return or
    retry — and drops the rest. So nothing riding a result can reach a citizen through the door
    the platform itself writes to, whatever the words are and however the model is behaving.
    That is a property of the shape, which is why it survived the hold being deleted when the
    quote guard did not.

    EVERY NOTE IN THE MAP, THROUGH BOTH RESULT CARRIERS, even though each note has only one
    real carrier: what is under test is the carrier and not the string, so the useful question
    is whether ANY of these can be made to arrive this way. A `ToolReturnPart` and a
    `RetryPromptPart` are separate code paths — the retry is the one that also marks the step
    failed — so both are driven."""
    # The one note we have a constant for is asserted against it: the rest of this map is a
    # copy of platform strings, and a copy silently rots when the original is reworded.
    from src.services.agent.conversation_tools import _SHOWN

    assert _SHOWN in _PRIVATE_NOTES.values(), "the acknowledgement was reworded under this map"

    notes = "\n".join(_PRIVATE_NOTES.values())
    user, conversation = await _thread(db_session, f"pn-carrier-{kind.value}@rvaiglobal.com", kind)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/page.tsx"}', tool_call_id="r1"
                    ),
                    ToolCallPart(
                        tool_name="write_file", args='{"path": "app/page.tsx"}', tool_call_id="w1"
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="read_file", content=notes, tool_call_id="r1"),
                    RetryPromptPart(content=notes, tool_name="write_file", tool_call_id="w1"),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=kind,
    )
    items = project_rows(await _rows(db_session, user, conversation))
    reloaded = " ".join(i.text for i in items if isinstance(i, AssistantTextItem))

    engine = TurnEngine()
    state = _state(kind)
    engine._on_event(state, _called("read_file", '{"path": "app/page.tsx"}', "r1"))
    for note in _PRIVATE_NOTES.values():
        engine._on_event(
            state,
            FunctionToolResultEvent(
                part=ToolReturnPart(tool_name="read_file", content=note, tool_call_id="r1")
            ),
        )
    await engine._drain_long_operations(state)  # the visible read armed a narrator
    live = rendered_text(state)

    for where, fragment in _PRIVATE_NOTES.items():
        assert fragment not in reloaded, f"{where} reached a reloaded transcript"
        assert fragment not in live, f"{where} reached the live feed"
    # LIVENESS ON BOTH SIDES, because "no text" is also what a crashed projection and an
    # ignored event produce. The reload emitter did read the row — it rendered both steps — and
    # the live one is not mute: given the CALL the channel travels on, it renders the words.
    assert [i.tool for i in items if isinstance(i, StepItem)] == ["read_file", "write_file"]
    engine._on_event(state, _spoke(_APP_WORDS, "s1"))
    assert state.text_blocks() == [_APP_WORDS]


# --- U7 / R82: on the turn that went wrong, what the user reads is ours ----------------------


def test_every_ending_this_plan_can_reach_is_a_platform_sentence() -> None:
    """★ R82's honest structural half, and the test docstring says which half.

    ON A TURN THAT ENDS BADLY, THE MODEL'S REGISTER IS NOT LOAD-BEARING, because the model
    wrote none of what the citizen reads. Every ending this plan introduces or touches resolves
    by IDENTITY to a constant in `copy.py` — asserted by identity rather than by inspecting
    words, so it cannot pass because a sentence happened to sound right.

    WHAT THIS IS NOT. It asserts over constants we wrote, so it cannot fail because of anything
    the model produced. The recorded incident was not a failure ending at all: it was a build
    that hit errors, recovered, and narrated 2,397 words while CONTINUING — and that shape is
    no longer held back by anything structural, which this docstring says plainly rather than
    leaving to be discovered. The drop that used to swallow it is deleted, because swallowing
    it also swallowed every explanation between the receipts and left a citizen reading a run
    of receipts with nothing joining them. What remains is `NARRATION_VOICE`, observed rather
    than asserted (R92), and nothing else: the voice channel's ceiling went the way the prompt's
    caps did, at the tool body and at the renderer together, so no number anywhere now decides
    how much of what the agent wrote a citizen may read. The composition guards in
    `test_mode_prompts.py` prevent a real drift failure and are NOT evidence that the contract
    holds; this platform shipped that confusion once."""
    from src.services.turns import copy as copy_module

    endings = {
        # A turn that produced no words now produces no assistant message at all, so the
        # sentence that used to fill one is NOT in this set — and its absence is the point. It
        # was deleted rather than reworded, because a platform line standing in for the model's
        # own is written in a voice nobody used, and the only thing that made a wordless turn
        # reachable in the first place was the narration drop this plan removed.
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
