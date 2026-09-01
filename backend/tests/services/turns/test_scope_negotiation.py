"""U10 / U12 — proposing a first slice, and saying afterwards what was agreed and not built.

TWO HALVES OF ONE RECORD, which is why they share a file. The proposal call's arguments ARE the
agreement — there is no column, no table and nothing stored against the project — and the
closing remainder is that list minus what the agent marked finished. Reading the agreement back
is reading the conversation.

★ THE TEST THAT MATTERS MOST is `test_no_marks_and_work_landed_says_it_could_not_tell`. The
finished half is agent-supplied, so an agent that built everything and marked nothing looks
exactly like one that built nothing. Stating "these four remain" in the platform's own voice on
that evidence would be a false fact the citizen has no reason to doubt — worse than the agent's
own recollection, which is what this unit exists to replace.

WHAT THESE TESTS DO NOT PROVE. Every one runs against a scripted transcript, so the script
decides that the tool was called. Whether the model actually proposes against a nine-screen
message, and declines to against a small one, is prompt-carried and observed in traffic (R92) —
not asserted here. The assertions pin what the PLATFORM does with a given proposal.
"""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.services.agent.conversation_tools import propose_first_slice, tell_the_user
from src.services.messages.projection import (
    MAX_FIRST_SLICE,
    AssistantTextItem,
    agreed_slice,
    project_rows,
    proposal_from_args,
)
from src.services.messages.store import append_batch
from src.services.turns.copy import (
    CANNOT_TELL_WHAT_REMAINS_TEXT,
    PROPOSAL_EVERYTHING_LEAD,
    PROPOSAL_FIRST_LEAD,
    PROPOSAL_REST_TEXT,
    REMAINDER_TEXT,
)
from src.services.turns.engine import TurnEngine, _TurnState
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_NINE = [
    "Sign-in for staff",
    "A visitor list",
    "A sign-out button",
    "A badge to print",
    "A weekly report",
    "Email alerts for overdue visitors",
    "A search box",
    "A photo on each visitor",
    "An export to spreadsheet",
]
_THREE = ["A visitor list", "A sign-out button", "A search box"]
_WHY = "Those three give you a working log you can use on the front desk from day one."
_QUESTION = "Should the sign-out button be on every row, or only on the ones still inside?"


def _args(found: list[str], first: list[str], why: str = _WHY, question: str = _QUESTION) -> str:
    return json.dumps({"found": found, "first": first, "why": why, "question": question})


def _proposed(found: list[str], first: list[str], call_id: str = "p1") -> FunctionToolCallEvent:
    return FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="propose_first_slice", args=_args(found, first), tool_call_id=call_id
        )
    )


def _marked(piece: str, update: str = "That one is in.", call_id: str = "m1"):
    return FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="tell_the_user",
            args=json.dumps({"update": update, "finished": piece}),
            tool_call_id=call_id,
        )
    )


def _ctx(messages: list[ModelMessage] | None = None) -> RunContext[None]:
    def _never(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("scope negotiation calls no model")

    return RunContext(
        deps=None, model=FunctionModel(_never), usage=RunUsage(), messages=messages or []
    )


def _state(kind: ChatKind = ChatKind.BUILD) -> _TurnState:
    return _TurnState(
        turn_id=uuid.uuid7(),
        conversation_id=uuid.uuid7(),
        user_id=uuid.uuid7(),
        kind=kind,
    )


# --- the bound: the ceiling is code, the floor is not --------------------------------------


async def test_a_slice_of_four_is_allowed_and_five_is_refused() -> None:
    """★ R83's binding half, at the boundary. FOUR passes and FIVE is refused, so an off-by-one
    in the comparison dies here — a fixture of nine would pass against `>` and `>=` alike.

    The refusal NAMES the bound and says what to do about it, because a model that trips it has
    to choose differently, not merely try again."""
    assert await propose_first_slice(_ctx(), _NINE, _NINE[:MAX_FIRST_SLICE], _WHY, _QUESTION)
    with pytest.raises(ModelRetry) as refused:
        await propose_first_slice(_ctx(), _NINE, _NINE[: MAX_FIRST_SLICE + 1], _WHY, _QUESTION)
    assert str(MAX_FIRST_SLICE) in str(refused.value)


async def test_a_slice_of_exactly_one_large_piece_is_allowed() -> None:
    """★ R83's SOFT half, and the reason the floor is prompt copy rather than a check.

    Twenty pages describing one screen is one piece (R84). A hard floor of two would refuse an
    honest single-piece slice inside the tool body and leave the model no recovery except to
    split something that should not be split, or to name a piece it does not intend to build —
    which then shows up as a padded remainder in the closing account.

    Mutation check: add `len(first) < 2` to `_bad_slice` and this goes red while every other
    test in this file stays green, which is exactly how the defect would have shipped."""
    one = ["A visitor check-in screen"]
    assert await propose_first_slice(_ctx(), one, one, _WHY, _QUESTION)


async def test_a_first_slice_naming_a_piece_nobody_found_is_refused_by_name() -> None:
    """The user would otherwise be shown a round containing something they were never told had
    been picked up — a proposal that quietly adds work while appearing to narrow it."""
    with pytest.raises(ModelRetry) as refused:
        await propose_first_slice(
            _ctx(), _NINE, ["A visitor list", "A dark mode toggle"], _WHY, _QUESTION
        )
    assert "A dark mode toggle" in str(refused.value)


async def test_a_proposal_with_no_question_is_refused() -> None:
    with pytest.raises(ModelRetry):
        await propose_first_slice(_ctx(), _NINE, _THREE, _WHY, "   ")


# --- the rendered message is the platform's shape, not the model's prose --------------------


def test_the_platform_renders_every_found_piece_the_first_slice_and_one_question() -> None:
    """★ R85 — the shape is a property of the RENDERER.

    "Lists everything back, names the first slice, says what happens to the rest, asks one
    question" is true here by construction. Asked for in a prompt it would be true most of the
    time, and the times it was not would be the times a citizen felt refused."""
    rendered = proposal_from_args(_args(_NINE, _THREE))
    assert rendered is not None

    assert PROPOSAL_EVERYTHING_LEAD in rendered
    for piece in _NINE:  # ALL of them, including the six not being built
        assert f"- {piece}" in rendered
    assert PROPOSAL_FIRST_LEAD in rendered
    assert _WHY in rendered
    assert PROPOSAL_REST_TEXT in rendered
    assert rendered.endswith(_QUESTION)


def test_a_slice_that_covers_everything_promises_no_next_round() -> None:
    """The conditional half of the frame. A slice covering everything found has no remainder,
    and a sentence promising to come back to nothing is the platform inventing an outstanding
    item — the same class of false fact U12's tri-state exists to avoid."""
    rendered = proposal_from_args(_args(_THREE, _THREE))
    assert rendered is not None
    assert PROPOSAL_REST_TEXT not in rendered


def test_a_proposal_the_tool_would_refuse_renders_nothing() -> None:
    """One rule, read by the body and by both emitters. A five-piece slice never reaches a
    screen even if a call carrying one somehow reached a stored row."""
    assert proposal_from_args(_args(_NINE, _NINE[:5])) is None
    assert proposal_from_args(_args(_NINE, ["not in the found list"])) is None
    assert proposal_from_args("not json") is None


def test_a_piece_named_twice_is_one_piece() -> None:
    """The citizen reads a list; a repeated line reads as two things to do."""
    # The duplicate is in `found` and the first slice names something else, so the one bullet
    # this counts is unambiguously the de-duplicated one rather than the same piece appearing
    # once in each section — which is correct and would make the count 2 for the wrong reason.
    rendered = proposal_from_args(
        _args(["A visitor list", "A visitor list", "A search box"], ["A search box"])
    )
    assert rendered is not None
    assert rendered.count("- A visitor list") == 1


# --- the agreement lives in the conversation, latest wins -----------------------------------


def test_the_agreement_is_the_latest_honourable_proposal_in_the_conversation() -> None:
    """★ R90 — no stored linkage anywhere. The agreed list is read back out of the rows the
    citizen already has, which is the same bounded route the plan itself travels.

    LATEST WINS, matching the offer's own rule: re-proposing mid-conversation replaces the
    agreement without a column that could go stale when a later build quietly delivers a
    deferred piece. A call the tool body would have refused is not an agreement at all."""
    later = ["A search box", "A weekly report"]
    messages: list[ModelMessage] = [
        ModelResponse(parts=[ToolCallPart("propose_first_slice", _args(_NINE, _THREE), "p1")]),
        ModelResponse(parts=[ToolCallPart("propose_first_slice", _args(_NINE, later), "p2")]),
    ]
    assert agreed_slice(messages) == later

    # A refused shape does not become the agreement — it silently replaced a good one before.
    messages.append(
        ModelResponse(parts=[ToolCallPart("propose_first_slice", _args(_NINE, _NINE), "p3")])
    )
    assert agreed_slice(messages) == later
    assert agreed_slice([]) == []


async def test_a_mark_naming_a_piece_nobody_agreed_to_is_refused_by_name() -> None:
    """A stray mark is not a bookkeeping slip: it is what would make the closing account name
    pieces the citizen never saw proposed. The model is told rather than the mark dropped."""
    history: list[ModelMessage] = [
        ModelResponse(parts=[ToolCallPart("propose_first_slice", _args(_NINE, _THREE), "p1")])
    ]
    assert await tell_the_user(_ctx(history), "That one is in.", "A visitor list")
    with pytest.raises(ModelRetry) as refused:
        await tell_the_user(_ctx(history), "That one is in.", "A weekly report")
    assert "A weekly report" in str(refused.value)


async def test_a_mark_with_nothing_agreed_says_so_rather_than_being_ignored() -> None:
    with pytest.raises(ModelRetry) as refused:
        await tell_the_user(_ctx([]), "That one is in.", "A visitor list")
    assert "no first slice has been proposed" in str(refused.value)


# --- what remains, computed from the record -------------------------------------------------


def _remainder(state: _TurnState, *, touched: bool) -> str | None:
    """`workspace_touched` is the one platform-held fact the rule turns on, so the tests pass
    it directly rather than standing up a sandbox to carry it."""
    return TurnEngine()._what_is_still_outstanding(state, workspace_touched=touched)


def test_marks_landed_names_what_is_left() -> None:
    """★ AE47 — the names come from the AGREED list, in the order the citizen agreed to them,
    never from anything the agent wrote at the end."""
    state = _state()
    state.agreed_pieces = list(_THREE)
    state.finished_pieces = {"A visitor list"}

    line = _remainder(state, touched=True)

    assert line == REMAINDER_TEXT.format(pieces="A sign-out button, A search box")


def test_everything_marked_produces_no_remainder_line() -> None:
    state = _state()
    state.agreed_pieces = list(_THREE)
    state.finished_pieces = set(_THREE)
    assert _remainder(state, touched=True) is None


def test_no_marks_and_nothing_touched_names_the_whole_agreed_list() -> None:
    """True, and platform-derived: nothing was written, so nothing was built."""
    state = _state()
    state.agreed_pieces = list(_THREE)
    assert _remainder(state, touched=False) == REMAINDER_TEXT.format(pieces=", ".join(_THREE))


def test_no_marks_and_work_landed_says_it_could_not_tell() -> None:
    """★★ THE ONE THAT WOULD HAVE SHIPPED A LIE, and the likeliest real trace.

    An agent that built all three pieces and marked none is indistinguishable, FROM THE MARKS
    ALONE, from one that built nothing. The tempting rendering — "these three remain" — is a
    false fact in the platform's own voice, which the citizen has no reason to doubt and which
    is strictly worse than the agent's own recollection.

    So the claim is keyed on `workspace_touched`, which the platform genuinely holds, and the
    honest answer when work landed but nothing was marked is that it could not tell.

    Mutation check: drop the `workspace_touched` branch and fall through to naming the agreed
    list; every other test in this file stays green and the product starts telling citizens
    that finished work is outstanding."""
    state = _state()
    state.agreed_pieces = list(_THREE)

    line = _remainder(state, touched=True)

    assert line == CANNOT_TELL_WHAT_REMAINS_TEXT
    for piece in _THREE:
        assert piece not in line  # it names NOTHING as outstanding


def test_a_turn_with_no_proposal_produces_no_remainder_at_all() -> None:
    """Most turns never propose a slice. An empty section appended to every build is noise on
    all of them, and an empty list rendered as a sentence would be a claim about nothing."""
    assert _remainder(_state(), touched=True) is None
    assert _remainder(_state(), touched=False) is None


def test_a_piece_marked_twice_counts_once() -> None:
    engine, state = TurnEngine(), _state()
    engine._on_event(state, _proposed(_NINE, _THREE))
    engine._on_event(state, _marked("A visitor list", call_id="m1"))
    engine._on_event(state, _marked("A visitor list", call_id="m2"))

    assert state.finished_pieces == {"A visitor list"}
    assert _remainder(state, touched=True) == REMAINDER_TEXT.format(
        pieces="A sign-out button, A search box"
    )


def test_a_second_proposal_replaces_the_agreement_and_its_marks() -> None:
    """★ The latest-wins rule, at the seam where it would corrupt the account.

    Re-proposing mid-turn replaces what was agreed. The marks have to go with it: a piece
    marked against the OLD slice is not evidence about the new one, and carrying it over would
    silently subtract a piece the agent never claimed to have finished."""
    engine, state = TurnEngine(), _state()
    engine._on_event(state, _proposed(_NINE, _THREE))
    engine._on_event(state, _marked("A visitor list"))
    engine._on_event(
        state, _proposed(_NINE, ["A weekly report", "A badge to print"], call_id="p2")
    )

    assert state.agreed_pieces == ["A weekly report", "A badge to print"]
    assert state.finished_pieces == set()
    assert _remainder(state, touched=False) == REMAINDER_TEXT.format(
        pieces="A weekly report, A badge to print"
    )


# --- live and reload agree, and no branch reads the kind ------------------------------------


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_the_proposal_reads_the_same_live_and_after_reload_in_either_kind(
    db_session: AsyncSession, kind: ChatKind
) -> None:
    """★ AE45 / R88 — the proposal is a property of the toolset, not of the chat kind.

    Both kinds carry the tool, both render it identically, and nothing in either path consults
    the kind. What differs is the ENDING each kind can reach — a planning chat has the offer
    tool and no write tools, a build chat the reverse — and that difference is the toolset's,
    with no conditional anywhere to assert against."""
    user = await UserFactory.create(db_session, email=f"sn-{kind.value}@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=kind
    )
    engine, state = TurnEngine(), _state(kind)
    event = _proposed(_NINE, _THREE)
    engine._on_event(state, event)

    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(parts=[event.part]),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="propose_first_slice", content="ok", tool_call_id="p1"
                    )
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=kind,
    )
    rows = list(
        (
            await db_session.scalars(
                sa.select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.seq)
            )
        ).all()
    )
    reloaded = [i.text for i in project_rows(rows) if isinstance(i, AssistantTextItem)]

    assert reloaded == ["".join(state.text_parts)]
    assert PROPOSAL_EVERYTHING_LEAD in reloaded[0]
    # And it is not a step — the transcript shows the proposal, never `Used propose_first_slice`.
    assert "propose_first_slice" not in reloaded[0]
