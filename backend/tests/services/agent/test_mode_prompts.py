"""U9 / D4 / R13 — the mode prompt system: BASE + exactly one positive segment per run.

The R13 property under test: a mode's composed prompt describes what the mode IS and DOES
with the tools it HAS — never prohibitions against tools the registry already makes
uncallable (`test_toolsets.py` proves the structural half; this file proves the prose
half). Plus the D4 delivery property: composed instructions ride `@agent.instructions`
per run and never reach a persisted row (`test_store_roundtrip.py` pins the store seam).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.db.models.conversation import ConversationMode
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.mode_prompts import (
    _ASK_SEGMENT,
    _PLAN_REMINDER_FULL,
    _PLAN_REMINDER_NUDGE,
    _PLAN_SEGMENT,
    PromptContext,
    compose_mode_prompt,
)
from src.services.orchestrator.prompt import BUILD_WORKING_RULES_HEAD, DATA_INTEGRITY_RULES

_CONTEXT = PromptContext(
    user_name="Asha",
    project_name="Visitor Log",
    project_description="Tracks visitors at the airport office.",
)

_SEGMENT_HEADERS = {
    ConversationMode.ASK: "ASK MODE",
    ConversationMode.PLAN: "PLAN MODE",
    ConversationMode.WRITE: "WRITE MODE",
}


@pytest.mark.parametrize("mode", list(ConversationMode))
def test_composition_is_base_plus_exactly_its_own_segment(mode: ConversationMode) -> None:
    composed = compose_mode_prompt(mode, _CONTEXT)
    # BASE: identity + project grounding + the single-sourced data-safety block.
    assert "Citizen Developer assistant for BIAL" in composed
    assert "Asha" in composed and "Visitor Log" in composed
    assert "Tracks visitors at the airport office." in composed
    assert DATA_INTEGRITY_RULES in composed
    # Exactly this mode's segment, none of the others' (mutation-checked: swapping a
    # segment in `compose_mode_prompt` turns this red).
    assert _SEGMENT_HEADERS[mode] in composed
    for other, header in _SEGMENT_HEADERS.items():
        if other is not mode:
            assert header not in composed


def test_base_survives_an_undescribed_project() -> None:
    bare = PromptContext(user_name="Asha", project_name="Visitor Log")
    composed = compose_mode_prompt(ConversationMode.ASK, bare)
    assert 'on "Visitor Log".' in composed  # no dangling " — None"
    assert "None" not in composed.split("DATA INTEGRITY")[0]


def test_write_without_a_plan_is_truthfully_from_scratch() -> None:
    composed = compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
    assert "Build from the user's request in this conversation" in composed
    assert "executing the plan the user approved" not in composed
    # The full working rules ride Write (and only Write — the read modes stay lean).
    assert BUILD_WORKING_RULES_HEAD in composed
    assert "PRESERVE WHAT WORKS" in composed


def test_write_with_an_approved_plan_executes_it() -> None:
    composed = compose_mode_prompt(
        ConversationMode.WRITE, _CONTEXT, approved_plan="1. Add a CSV export button."
    )
    assert "executing the plan the user approved" in composed
    assert "1. Add a CSV export button." in composed
    assert "Build from the user's request in this conversation" not in composed


@pytest.mark.parametrize("mode", [ConversationMode.ASK, ConversationMode.PLAN])
def test_an_approved_plan_on_a_read_mode_is_a_programming_error(
    mode: ConversationMode,
) -> None:
    with pytest.raises(ValueError, match="approved_plan composes only in Write mode"):
        compose_mode_prompt(mode, _CONTEXT, approved_plan="a plan")


def test_read_modes_stay_lean() -> None:
    # Template facts / working rules are Write-only fuel (plan decision): Ask and Plan
    # never carry the build environment blocks.
    for mode in (ConversationMode.ASK, ConversationMode.PLAN):
        composed = compose_mode_prompt(mode, _CONTEXT)
        assert BUILD_WORKING_RULES_HEAD not in composed
        assert "declare_done" not in composed


_FORBIDDEN_FRUIT = (
    # R13: prohibition prose aimed at tools the mode doesn't have. The registry already
    # removed them — ban text would teach the model to reason about absent capabilities
    # (the research doc's anti-pattern 1/2).
    "do not",
    "don't",
    "never",
    "cannot",
    "can't",
    "forbidden",
    "not allowed",
    "no access",
    "unable to",
    "must not",
)


@pytest.mark.parametrize("segment", [_ASK_SEGMENT, _PLAN_SEGMENT], ids=["ask", "plan"])
def test_read_segments_never_speak_of_forbidden_fruit(segment: str) -> None:
    lowered = segment.lower()
    for phrase in _FORBIDDEN_FRUIT:
        assert phrase not in lowered, f"prohibition prose {phrase!r} crept into a segment"


def test_plan_segment_is_citizen_facing_not_a_developer_spec() -> None:
    # F9: the plan streams as ordinary assistant TEXT, so its register is dictated entirely by
    # _PLAN_SEGMENT. The developer skeleton ("the files you would touch", "the trade-offs the
    # user should weigh") is retired; the segment now steers an outcome-first, plain-language plan,
    # while KEEPING the read-first grounding. This asserts the PROMPT's shape — the ground-truth
    # check is an eyeballed rendered Plan turn against PLAN-FORMAT-RESEARCH.md's AFTER, since a
    # prompt string cannot prove the model's OUTPUT stays jargon-free.
    lowered = _PLAN_SEGMENT.lower()
    # the retired developer-register source phrases are gone (assert the REAL phrases, not the
    # model-output headings from the research BEFORE example)
    assert "the files you would touch" not in lowered
    assert "trade-offs the user should weigh" not in lowered
    assert "trade-offs" not in lowered
    # citizen framing is present: outcome-first + see/do + plain words + the options contract
    assert "plain, everyday words" in lowered
    assert "will do" in lowered  # "what the app or this change will DO for them"
    assert "see and be able to do" in lowered
    assert "present_plan_options" in _PLAN_SEGMENT
    # the read-first grounding instruction stays — only the OUTPUT register changed
    assert "read the relevant files first" in lowered


@pytest.mark.parametrize(
    "reminder", [_PLAN_REMINDER_FULL, _PLAN_REMINDER_NUDGE], ids=["full", "nudge"]
)
def test_plan_reminders_are_citizen_plain_and_prohibition_free(reminder: str) -> None:
    # F9 in lockstep (U14): the ephemeral reminders must not re-inject the technical framing in a
    # long conversation. They stay plain-language and prohibition-free, and keep the
    # present_plan_options contract so the Build it / Keep refining buttons still surface.
    lowered = reminder.lower()
    for phrase in _FORBIDDEN_FRUIT:
        assert phrase not in lowered, f"prohibition prose {phrase!r} crept into a plan reminder"
    assert "plain, everyday words" in lowered
    assert "present_plan_options" in reminder
    assert "the files you would touch" not in lowered
    assert "trade-offs" not in lowered


def _capturing_model(captured: dict[str, str]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(respond)


async def test_relay_path_stays_verbatim_deps_system(db_session) -> None:
    captured: dict[str, str] = {}
    deps = ChatDeps(db=db_session, user_id=uuid.uuid4(), system="RELAY-PROMPT")
    result = await chat_agent.run("hi", deps=deps, model=_capturing_model(captured))
    assert captured["instructions"] == "RELAY-PROMPT"  # mode=None → byte-identical U7 path
    assert result.output == "ok"


async def test_mode_run_composes_and_never_persists_instructions(db_session) -> None:
    """D4 delivery: the model RECEIVES the composition; the store's dump seam keeps it out
    of any persisted payload (the JSONB half is pinned in test_store_roundtrip)."""
    captured: dict[str, str] = {}
    deps = ChatDeps(
        db=db_session,
        user_id=uuid.uuid4(),
        mode=ConversationMode.ASK,
        prompt_context=_CONTEXT,
    )
    result = await chat_agent.run(
        "what does my app do?", deps=deps, model=_capturing_model(captured)
    )
    assert captured["instructions"] == compose_mode_prompt(ConversationMode.ASK, _CONTEXT)

    from src.services.messages.store import dump_for_row

    dumped = dump_for_row(result.new_messages())
    for message in dumped:
        assert message.get("instructions") is None  # the composed prompt never lands in a row


async def test_mode_without_context_fails_first(db_session) -> None:
    deps = ChatDeps(db=db_session, user_id=uuid.uuid4(), mode=ConversationMode.PLAN)
    with pytest.raises(ValueError, match="composed without a PromptContext"):
        await chat_agent.run("hi", deps=deps, model=_capturing_model({}))
