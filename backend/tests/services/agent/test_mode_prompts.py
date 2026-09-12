"""The chat-kind prompt system: BASE + exactly one positive segment per run.

The property under test: a kind's composed prompt describes what that kind IS and DOES with
the tools it HAS — never prohibitions against tools the registry already makes uncallable
(`test_toolsets.py` proves the structural half; this file proves the prose half). Plus the
delivery property: composed instructions ride `@agent.instructions` per run and never reach a
persisted row (`test_store_roundtrip.py` pins the store seam).

There are two segments, Plan and Build. This file owns the properties that must hold whatever
their wording becomes.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition

from src.core.prompt_blocks import (
    BUILD_THIS_PLAN_LABEL,
    BUILD_WORKING_RULES_HEAD,
    BUILD_WORKING_RULES_TAIL,
    DATA_INTEGRITY_RULES,
    DATA_INTEGRITY_RULES_WITHOUT_THE_WRITE_MACHINERY,
    KEEP_PLANNING_LABEL,
    NARRATION_VOICE,
    PORTAL_SURFACES,
    WRITE_IDENTITY,
)
from src.db.models.conversation import ChatKind
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.mode_prompts import (
    _PLAN_SEGMENT,
    PromptContext,
    compose_kind_prompt,
    workspace_note,
)
from src.services.agent.toolsets import registered_tool_definitions

_CONTEXT = PromptContext(
    user_name="Asha",
    project_name="Visitor Log",
    project_description="Tracks visitors at the airport office.",
)

# The segment header each kind's composition must carry, and only its own — what matters here
# is that the two compositions are distinct and that neither leaks the other's segment.
_SEGMENT_HEADERS = {
    ChatKind.PLAN: "PLAN MODE",
    ChatKind.BUILD: "WRITE MODE",
}


@pytest.mark.parametrize("kind", list(ChatKind))
def test_composition_is_base_plus_exactly_its_own_segment(kind: ChatKind) -> None:
    composed = compose_kind_prompt(kind, _CONTEXT)
    # BASE: identity + project grounding + the single-sourced data-safety block.
    assert "Citizen Developer assistant for BIAL" in composed
    assert "Asha" in composed and "Visitor Log" in composed
    assert "Tracks visitors at the airport office." in composed
    # A per-kind lookup, not a shared substring: Plan's form drops two Build-machinery clauses
    # mid-sentence, so neither form contains the other. WHY they differ is
    # `test_the_sql_sentinel_and_the_migration_channel_are_named_to_build_alone` below.
    expected_integrity = {
        ChatKind.PLAN: DATA_INTEGRITY_RULES_WITHOUT_THE_WRITE_MACHINERY,
        ChatKind.BUILD: DATA_INTEGRITY_RULES,
    }[kind]
    assert expected_integrity in composed
    # Mutation-checked: swapping a segment in `compose_kind_prompt` turns this red. Parametrized
    # over the whole enum, not a hand-kept list, so a third kind added without one fails here.
    assert _SEGMENT_HEADERS[kind] in composed
    for other, header in _SEGMENT_HEADERS.items():
        if other is not kind:
            assert header not in composed


@pytest.mark.parametrize("kind", list(ChatKind))
def test_every_kind_carries_the_truthful_portal_self_description(
    kind: ChatKind,
) -> None:
    """It lives in BASE, so neither kind can be missing it — the fix for a model inventing
    portal features it does not have cannot depend on which segment was selected."""
    composed = compose_kind_prompt(kind, _CONTEXT)
    assert PORTAL_SURFACES in composed
    # The two clauses that do the actual work: the closed world, and honesty over invention.
    assert "There are no other tabs" in composed
    assert "say so plainly" in composed
    # Named surfaces exist as routes in `portal/src/App.jsx` — extend clause and list together.
    for real_surface in (
        "Dashboard",
        "Projects list",
        "Help page",
        "Marketplace",
        "Admin review area",
    ):
        assert real_surface in composed
    # The unified chat's right pane is the APP — guards against the retired relay's wording
    # ("a chat beside a live preview") being used to re-describe this layout.
    assert "the right pane shows the app itself" in composed


def test_base_survives_an_undescribed_project() -> None:
    bare = PromptContext(user_name="Asha", project_name="Visitor Log")
    composed = compose_kind_prompt(ChatKind.PLAN, bare)
    assert 'on "Visitor Log".' in composed  # no dangling " — None"
    assert "None" not in composed.split("DATA INTEGRITY")[0]


def test_build_composes_like_the_other_kind() -> None:
    """A Build chat has a segment like any other. Composing one used to raise, on the reading
    that a Write segment could only duplicate `orchestrator/prompt.py`; importing the shared
    blocks removes that reason, so the refusal was an unfinished seam and not architecture."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert "WRITE MODE" in composed
    assert 'on "Visitor Log"' in composed  # the same BASE both kinds carry


def test_the_write_segment_and_the_build_prompt_come_from_one_source() -> None:
    """The original objection to a Write segment — "it could only drift from the build prompt" —
    is true of a copy and false of a shared import, and this is the assertion that the import is
    what it is.

    THERE IS ONLY ONE WRITE PROMPT NOW. The standalone `BUILD_SYSTEM_PROMPT` and the harness that
    was its only consumer were deleted, so the drift this guarded against has no second party
    left. What it still buys is the other half of the same property: the composition assembles
    from the shared `core/prompt_blocks` constants rather than typing their text out, so a block
    edited at its source reaches the prompt, and a composition that stopped including one fails
    here."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert BUILD_WORKING_RULES_HEAD in composed
    assert BUILD_WORKING_RULES_TAIL in composed
    assert WRITE_IDENTITY in composed
    # The audience block arrives the same way — one constant, reached through `_base()`, so
    # the prompt cannot grow a voice the shared source does not have.
    assert NARRATION_VOICE in composed


def test_write_states_the_data_integrity_rules_exactly_once() -> None:
    """The trap in composing Write from the shared blocks: `_base()` already appends
    DATA_INTEGRITY_RULES for EVERY mode, so listing it among the segment's blocks too would emit
    the whole block twice in every Write prompt — burning context and reading as a stutter."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert composed.count(DATA_INTEGRITY_RULES) == 1


_SQL_SENTINEL_SENTENCE = "a destructive-SQL sentinel enforces this on `run_command`"
_MIGRATION_CHANNEL_SENTENCE = "Schema changes go through generated migrations (see DATABASE)"


def test_the_sql_sentinel_and_the_migration_channel_are_named_to_build_alone() -> None:
    """The two DATA INTEGRITY clauses that were FALSE in a Plan prompt, and only there: both
    name machinery (BUILD's `run_command` sentinel; a `BUILD_WORKING_RULES_HEAD` section) a
    Plan run doesn't have. The rules THEMSELVES are asserted present in both.

    Mutation check: make `_base` ignore its `kind` and this goes red on the Plan arm."""
    plan = compose_kind_prompt(ChatKind.PLAN, _CONTEXT)
    build = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)

    assert _SQL_SENTINEL_SENTENCE in build
    assert _SQL_SENTINEL_SENTENCE not in plan
    assert _MIGRATION_CHANNEL_SENTENCE in build
    assert _MIGRATION_CHANNEL_SENTENCE not in plan

    # Read off the registry, so a Plan run that ever gains a write or schema tool fails HERE
    # rather than shipping a prompt that lies the other way round.
    plan_tools = asyncio.run(registered_tool_definitions(ChatKind.PLAN))
    build_tools = asyncio.run(registered_tool_definitions(ChatKind.BUILD))
    assert "apply_schema_change" in build_tools
    assert "apply_schema_change" not in plan_tools
    assert "declare_done" in build_tools
    assert "declare_done" not in plan_tools

    # The rules survive intact in both — this is a removal of two claims, not of a safety rule.
    for composed in (plan, build):
        assert "Never INSERT, UPDATE, DELETE, or TRUNCATE data" in composed
        assert "Never hardcode, seed, or generate dummy" in composed


def test_write_speaks_to_the_person_who_asked_for_the_app() -> None:
    """Pins the audience block CONCRETELY — plain register, what stays behind the scenes, the
    failure turns — rather than just proving some voice text exists. No length bar is asserted
    because there is no longer one to assert."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert NARRATION_VOICE in composed
    lowered = composed.lower()
    assert "talking to the user" in lowered
    assert "plain, everyday words" in lowered
    assert "keep the how-it's-built details behind the scenes" in lowered
    assert "the file and folder names, the commands you run" in lowered
    # The hard turns are covered as well: a failure and its recovery stay in product language.
    assert "when something goes wrong" in lowered
    # The technical record is untouched, which is what lets the narration be short.
    assert "recorded step by step" in lowered


def test_the_audience_block_is_emitted_exactly_once() -> None:
    """The DATA_INTEGRITY_RULES trap, one block over: the contract is named by `_base()` for
    every kind AND rides `BUILD_WORKING_RULES_TAIL` into the Write segment, so each is a place a
    second copy could appear — and a prompt that states the same contract twice in slightly
    different places is how one wording starts drifting from the other.

    (It used to be three sites: the standalone `BUILD_SYSTEM_PROMPT` named the block itself,
    because it could not call `_base()`. That prompt went with the build harness; the two
    surviving sites are both reached through `compose_kind_prompt`, which is what this counts.)

    IT IS ALSO THE DELETION GUARD. The block restricts WHO the agent writes for, not what it may
    say, and it was removed once — twice in production consequences — before that distinction
    was written down. The pass that deleted every length cap and vocabulary rule beside it left
    this one alone deliberately, and a count of zero here is what catches the next attempt.

    COUNTING IS THE POINT, and `== 1` rather than `<= 1` is the point of the counting: the
    failure this guard exists to catch — the block being lifted out of the TAIL and never named
    at a composition site — is a count of ZERO, which every `<=` and every `in` formulation
    passes."""
    for kind in ChatKind:
        composed = compose_kind_prompt(kind, _CONTEXT)
        assert composed.count(NARRATION_VOICE) == 1
        assert composed.count("TALKING TO THE USER") == 1
        # No length bar drifts back in beside the contract it used to ride with.
        assert "HOW LONG —" not in composed


def test_the_name_the_files_instruction_went_with_the_segment_that_carried_it() -> None:
    """A real loss, recorded rather than quietly dropped: the retired Ask segment told the model
    to name files and quote code; the Plan segment a citizen now lands in says the opposite
    deliberately. Inertness only — nothing here invents prompt copy to paper over the gap."""
    for kind in ChatKind:
        assert "name the actual files and quote the actual code" not in compose_kind_prompt(
            kind, _CONTEXT
        )


def test_both_kinds_inherit_the_one_audience_contract() -> None:
    """One contract, with no per-kind half at all: the audience block lives in BASE, and neither
    kind carries its own plain-language paragraph or length clause. A per-kind half drifting
    back in alone is the failure this catches."""
    plan = compose_kind_prompt(ChatKind.PLAN, _CONTEXT)
    build = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert NARRATION_VOICE in plan
    assert NARRATION_VOICE in build

    # Asserted on the COMPOSED prompt: the wording no longer lives in the Plan segment itself.
    lowered = plan.lower()
    assert "plain, everyday words" in lowered
    assert "keep the how-it's-built details behind the scenes" in lowered
    assert "present_plan_options" in plan

    # Both halves asserted: the failure this catches is one drifting back in alone, restoring
    # the split without restoring the thing the split was for.
    assert "a plan is as long as it needs to be" not in lowered
    assert "a couple of lines at each milestone" not in lowered
    assert "a plan is as long as it needs to be" not in build.lower()
    assert "a couple of lines at each milestone" not in build.lower()

    assert plan.count(NARRATION_VOICE) == build.count(NARRATION_VOICE) == 1


# --- the version control the agent no longer does ---------------------------------
#
# The commit the platform takes instead is `build_sessions/snapshot._COMMIT_SCRIPT`.
#
# Two inertness guards plus one liveness guard below, deliberately: an inertness pair alone is
# greenest against a Write prompt somebody deleted outright, so a rule that must SURVIVE every
# trim is asserted right next to them.

_RETIRED_GIT_INSTRUCTIONS = (
    "git add",
    "git commit",
    # The undo half, checked as a set: each of these — including reset and stash below — can
    # leave a HEAD that is not a descendant of the copy on record over a perfectly good tree,
    # which is exactly the input the workspace-integrity verdict reasons about before calling a
    # workspace REVERTED. Guards against a prompt edit feeding it a self-inflicted HEAD.
    "git checkout",
    "git revert",
    "git reset",
    "git stash",
)


def test_neither_write_prompt_instructs_the_agent_in_git() -> None:
    """★ THE INERTNESS GUARD. Asserted as a SET so the failure names every instruction that
    crept back.

    IT USED TO RUN TWICE, over the composed Write prompt and the standalone `BUILD_SYSTEM_PROMPT`
    beside it, because either composition site could grow one. The standalone prompt went with
    the build harness and the composed one is the whole Write surface now, so the
    parametrization went with it — the blocks it composes are unchanged and still the place a git
    instruction would come back.

    Mutation check: put any of the six back into `BUILD_WORKING_RULES_HEAD` and this goes red."""
    lowered = compose_kind_prompt(ChatKind.BUILD, _CONTEXT).lower()
    found = {phrase for phrase in _RETIRED_GIT_INSTRUCTIONS if phrase in lowered}
    assert found == set(), f"the Write prompt instructs the agent in git again: {sorted(found)}"
    # The header of the deleted block, named separately so a reworded revival still trips.
    assert "commit as you work" not in lowered


def test_the_write_prompt_still_says_not_to_restart_the_dev_server() -> None:
    """★ THE LIVENESS GUARD, and the one rule this unit must not take with it.

    The agent can start a dev server of its own through `run_command` — the supervisor's child
    env carries no marker that would tell the platform's flag apart from the real one — so this
    sentence is the whole of what stops a second `next dev` racing the one the turn engine reads
    (through `selfheal.verify`) to decide whether the app is healthy. It survives every prompt
    trim."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    lowered = composed.lower()
    assert "the dev server (`next dev`) is already running" in lowered
    assert "do not start, restart, or kill it" in lowered


def test_the_composer_takes_no_approved_plan_at_all() -> None:
    """The parameter is GONE rather than rejected — the stronger guarantee, and checked on the
    SIGNATURE: a `TypeError` from a literal call would only prove Python rejects unknown
    keywords, and would need a type-checker suppression to compile."""
    assert "approved_plan" not in inspect.signature(compose_kind_prompt).parameters


def test_a_plan_chat_stays_lean() -> None:
    composed = compose_kind_prompt(ChatKind.PLAN, _CONTEXT)
    assert BUILD_WORKING_RULES_HEAD not in composed
    assert "declare_done" not in composed


def test_the_plan_segment_points_at_the_reader_rather_than_at_extraction() -> None:
    """★ This paragraph used to describe a mechanism that no longer exists: an
    attachment's text was extracted on the SERVER and inlined into the prompt, so the segment only
    had to say "put what it means into the plan". Nothing extracts anything now — the file sits in
    the workspace and code opens it — and a prompt still written against the old mechanism would
    leave a Plan agent with no idea a reader exists.

    The half that survived is the one the deletion did not touch: whatever the file says, the PLAN
    still names no file, no folder and no framework, and what the build chat gets is the plan
    alone.

    Mutation receipt: delete the attachment paragraph and the first two assertions go red; delete
    the audience rule with it and the third does too.
    """
    lowered = _PLAN_SEGMENT.lower()

    assert "reader" in lowered
    assert "already in your workspace" in lowered
    assert "nothing in the plan names a file" in lowered
    # The retired mechanism is not described as though it still ran.
    assert "extracted" not in lowered


_FORBIDDEN_FRUIT = (
    # Prohibition prose aimed at tools the mode doesn't have. The registry already
    # removed them, and ban text would teach the model to reason about capabilities it
    # cannot reach.
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


def test_the_plan_segment_never_speaks_of_forbidden_fruit() -> None:
    lowered = _PLAN_SEGMENT.lower()
    for phrase in _FORBIDDEN_FRUIT:
        assert phrase not in lowered, f"prohibition prose {phrase!r} crept into a segment"


def test_the_plan_still_says_nothing_technical_even_with_its_shape_freed() -> None:
    """Asserted per category rather than in general: a sentence that dropped "a command" while
    keeping the other four would still read as a no-jargon rule.

    Mutation check: delete any one of the five nouns from `_PLAN_SEGMENT` and this names it."""
    lowered = _PLAN_SEGMENT.lower()
    for banned in ("a file", "a folder", "a framework", "a library", "a command"):
        assert banned in lowered, f"the no-jargon sentence stopped naming {banned!r}"


def test_plan_segment_is_citizen_facing_not_a_developer_spec() -> None:
    """This asserts the PROMPT's shape only. Whether the model's OUTPUT stays jargon-free is
    read off a rendered Plan turn by eye — a prompt string cannot prove it."""
    lowered = _PLAN_SEGMENT.lower()
    # The real retired phrases, not the model-output headings they used to produce.
    assert "the files you would touch" not in lowered
    assert "trade-offs the user should weigh" not in lowered
    assert "trade-offs" not in lowered
    # "Plain, everyday words" is not asserted against the segment itself on purpose — it moved
    # to the shared audience block; `test_both_kinds_inherit_the_one_audience_contract` holds
    # that ground on the composed prompt, where the model actually reads it.
    assert "plain, everyday words" in compose_kind_prompt(ChatKind.PLAN, _CONTEXT).lower()
    # The mandated five-headed-section shape is gone; what a plan is FOR survives, how it is
    # arranged is the agent's.
    assert "five parts" not in lowered
    assert "what this gives you" not in lowered
    assert "present_plan_options" in _PLAN_SEGMENT
    # the read-first grounding instruction stays — only the OUTPUT register changed
    assert "read the relevant files first" in lowered


def test_the_plan_segment_says_the_plan_travels_in_the_offer_argument() -> None:
    """The buttons attach to the `plan` argument, so a plan announced beside the call leaves the
    reader nothing to press.

    Mutation check: revert that paragraph to "write the plan, then call the tool" and no other
    test in the repo goes red — the failure is a citizen reading a plan with no button."""
    lowered = _PLAN_SEGMENT.lower()
    assert "as the `plan` argument of" in lowered
    assert "not as a message beside the call" in lowered
    # The segment used to say anything written alongside a tool call "does not reach the user",
    # which stopped being true the moment the hold was deleted.
    assert "does not reach the user" not in lowered
    assert "everything else you write does reach them" in lowered


# THE PLAN REMINDERS' OWN CHECK USED TO SIT HERE, and it is not orphaned: the reminders it
# guarded no longer exist (`tests/services/turns/test_reminders.py` is their inertness guard),
# and every property it asserted — citizen-plain wording, prohibition-free, the
# `present_plan_options` contract — is asserted directly against `_PLAN_SEGMENT` above, which is
# now the only place that framing is stated. One source, one check.


def _capturing_model(captured: dict[str, str]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(respond)


async def test_relay_path_stays_verbatim_deps_system(db_session) -> None:
    captured: dict[str, str] = {}
    deps = ChatDeps(db=db_session, user_id=uuid.uuid4(), system="RELAY-PROMPT")
    result = await chat_agent.run("hi", deps=deps, model=_capturing_model(captured))
    assert captured["instructions"] == "RELAY-PROMPT"  # mode=None → byte-identical path
    assert result.output == "ok"


async def test_mode_run_composes_and_never_persists_instructions(db_session) -> None:
    """Delivery: the model RECEIVES the composition; the store's dump seam keeps it out
    of any persisted payload (the JSONB half is pinned in test_store_roundtrip)."""
    captured: dict[str, str] = {}
    deps = ChatDeps(
        db=db_session,
        user_id=uuid.uuid4(),
        kind=ChatKind.PLAN,
        prompt_context=_CONTEXT,
    )
    result = await chat_agent.run(
        "what does my app do?", deps=deps, model=_capturing_model(captured)
    )
    assert captured["instructions"] == compose_kind_prompt(ChatKind.PLAN, _CONTEXT)

    from src.services.messages.store import dump_for_row

    dumped = dump_for_row(result.new_messages())
    for message in dumped:
        assert message.get("instructions") is None  # the composed prompt never lands in a row


async def test_a_kind_without_context_fails_first(db_session) -> None:
    deps = ChatDeps(db=db_session, user_id=uuid.uuid4(), kind=ChatKind.PLAN)
    with pytest.raises(ValueError, match="composed without a PromptContext"):
        await chat_agent.run("hi", deps=deps, model=_capturing_model({}))


@pytest.mark.parametrize("kind", list(ChatKind))
def test_no_segment_promises_an_emptiness_signal_that_never_arrives(kind: ChatKind) -> None:
    """The retired Ask segment promised an emptiness signal that never arrives — every project
    gets a live container holding the golden template, so reads come back FULL. Widened to both
    surviving segments because the promise was wrong about the platform, not about Ask."""
    lowered = compose_kind_prompt(kind, _CONTEXT).lower()
    assert "your tools will tell you truthfully" not in lowered
    assert "if there is no app yet" not in lowered
    # Plan's composition carries the retired sentence's ACTION half; Build's — which shares the
    # workspace note's FACT but not the segment — does not.
    if kind is ChatKind.PLAN:
        assert "talk about what could be built for them" in lowered
    else:
        assert "talk about what could be built for them" not in lowered


async def test_a_plan_turn_carries_the_notes_fact_and_the_segments_instruction_together(
    db_session,
) -> None:
    """The integration half: the workspace note's FACT and the Plan segment's ACTION, each
    unit-tested alone (`test_reminders.py` and the test above) but never together — assembled
    here the way `turns/engine.py` actually does, to prove both reach the one model call."""
    captured_instructions = ""
    captured_messages: list[ModelMessage] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal captured_instructions, captured_messages
        captured_instructions = info.instructions or ""
        captured_messages = list(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    note = workspace_note(serving=True, still_the_template=True)
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=note)])]
    deps = ChatDeps(
        db=db_session,
        user_id=uuid.uuid4(),
        kind=ChatKind.PLAN,
        prompt_context=_CONTEXT,
    )

    await chat_agent.run(
        "what should we build?",
        deps=deps,
        model=FunctionModel(respond),
        message_history=history,
    )

    # The segment's INSTRUCTION, on the instructions channel.
    assert "talk about what could be built for them" in captured_instructions
    # The note's FACT, on the message-history channel — checked as literal text rather than
    # the private constant so this fails the way a reviewer reading the actual request would.
    sent_notes = [
        part.content
        for message in captured_messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert any("still byte-for-byte the starter template" in str(text) for text in sent_notes)


def test_no_prompt_surface_names_a_button_the_interface_does_not_draw() -> None:
    """A prompt naming a button the interface doesn't draw fails nothing on its own — the prompt
    composes, the turn runs, only the reader is stuck. Tool descriptions are checked too:
    `present_plan_options`' docstring is prompt copy pydantic-ai sends on every tool schema, but
    it never appears in any composed prompt string, so a prompt-only guard would read clean
    while the model is told the old labels."""
    retired = ("Keep refining", "keep refining", "Build it")
    surfaces: dict[str, str] = {
        f"composed {kind.value} prompt": compose_kind_prompt(kind, _CONTEXT) for kind in ChatKind
    }
    for kind in ChatKind:
        for name, definition in (await_definitions(kind)).items():
            surfaces[f"{kind.value} tool `{name}`"] = definition.description or ""

    for where, text in surfaces.items():
        for label in retired:
            assert label not in text, f"{where} still names the retired button {label!r}"

    offer = (await_definitions(ChatKind.PLAN))["present_plan_options"].description or ""
    assert BUILD_THIS_PLAN_LABEL in offer
    assert KEEP_PLANNING_LABEL in offer
    plan_prompt = compose_kind_prompt(ChatKind.PLAN, _CONTEXT)
    assert BUILD_THIS_PLAN_LABEL in plan_prompt
    assert KEEP_PLANNING_LABEL in plan_prompt


def await_definitions(kind: ChatKind) -> dict[str, ToolDefinition]:
    """`registered_tool_definitions` without the await, for a sync test — the renderer is async
    only because pydantic-ai's `get_tools` is; it performs no I/O and calls no model."""
    return asyncio.run(registered_tool_definitions(kind))
