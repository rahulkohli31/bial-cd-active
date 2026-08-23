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

from src.core.prompt_blocks import NARRATION_VOICE, PORTAL_SURFACES, WRITE_IDENTITY
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
from src.services.orchestrator.prompt import (
    BUILD_SYSTEM_PROMPT,
    BUILD_WORKING_RULES_HEAD,
    BUILD_WORKING_RULES_TAIL,
    DATA_INTEGRITY_RULES,
)

_CONTEXT = PromptContext(
    user_name="Asha",
    project_name="Visitor Log",
    project_description="Tracks visitors at the airport office.",
)

# The READ modes. Write is a mode like any other now (KTD-5) and composes fine — it is listed
# separately only where a property is genuinely about reading rather than building (e.g. "stays
# lean": Write is the one mode that SHOULD carry the build environment blocks).
_CHAT_MODES = [ConversationMode.ASK, ConversationMode.PLAN]

_SEGMENT_HEADERS = {
    ConversationMode.ASK: "ASK MODE",
    ConversationMode.PLAN: "PLAN MODE",
}


@pytest.mark.parametrize("mode", _CHAT_MODES)
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


@pytest.mark.parametrize("mode", _CHAT_MODES)
def test_every_mode_carries_the_truthful_portal_self_description(
    mode: ConversationMode,
) -> None:
    """R5's other half. The relay pinned this clause for every kind
    (`test_interview_protocol.py`); the mode system serves EVERY turn-engine run and had no
    equivalent, so R5 — the walkthrough's invented-portal-features fix — would have regressed
    the moment the relay retired. It lives in BASE now, so no mode can be missing it."""
    composed = compose_mode_prompt(mode, _CONTEXT)
    assert PORTAL_SURFACES in composed
    # The two clauses that do the actual work: the closed world, and honesty over invention.
    assert "There are no other tabs" in composed
    assert "say so plainly" in composed
    # Named surfaces exist as routes in `portal/src/App.jsx` — extend clause and list together.
    for real_surface in ("Dashboard", "Projects list", "Help page", "Admin review area"):
        assert real_surface in composed
    # R10: the unified chat's right pane is the APP. The relay's retiring wording said the
    # builder view was "a chat beside a live preview"; this layout must not be re-described.
    assert "the right pane shows the app itself" in composed


def test_base_survives_an_undescribed_project() -> None:
    bare = PromptContext(user_name="Asha", project_name="Visitor Log")
    composed = compose_mode_prompt(ConversationMode.ASK, bare)
    assert 'on "Visitor Log".' in composed  # no dangling " — None"
    assert "None" not in composed.split("DATA INTEGRITY")[0]


def test_write_composes_like_any_other_mode() -> None:
    """Write is a mode, so it has a segment (KTD-5/KTD-5a). This used to raise, and that
    refusal was read as architecture when it was an unfinished seam: no `_WRITE_SEGMENT` had
    been authored, to avoid duplicating `orchestrator/prompt.py`. A shared import solves that."""
    composed = compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
    assert "WRITE MODE" in composed
    assert 'on "Visitor Log"' in composed  # the same BASE every mode carries


def test_the_write_segment_and_the_build_prompt_come_from_one_source() -> None:
    """The original objection to a Write segment — "it could only drift from the build prompt" —
    is true of a copy and false of a shared import. Assert they genuinely share the blocks, so a
    future edit to either cannot silently fork the two Write prompts."""
    composed = compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
    assert BUILD_WORKING_RULES_HEAD in composed
    assert BUILD_WORKING_RULES_TAIL in composed
    assert WRITE_IDENTITY in composed
    assert WRITE_IDENTITY in BUILD_SYSTEM_PROMPT
    assert BUILD_WORKING_RULES_HEAD in BUILD_SYSTEM_PROMPT
    # U15's audience block is shared the same way — one constant, reached by both Write prompts
    # through the TAIL they already share, so neither can grow a voice the other does not have.
    assert NARRATION_VOICE in composed
    assert NARRATION_VOICE in BUILD_SYSTEM_PROMPT


def test_write_states_the_data_integrity_rules_exactly_once() -> None:
    """The trap in composing Write from the shared blocks: `_base()` already appends
    DATA_INTEGRITY_RULES for EVERY mode, so listing it among the segment's blocks too would emit
    the whole block twice in every Write prompt — burning context and reading as a stutter."""
    composed = compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
    assert composed.count(DATA_INTEGRITY_RULES) == 1


def test_write_speaks_to_the_person_who_asked_for_the_app() -> None:
    """U15 / R20 / R22. Write mode carried NO audience instruction at all — Plan carried a full
    plain-language contract and Ask deliberately pushes the other way — and the demo build spent
    2,397 words of paths, commands, and framework nouns on a citizen. The composed Write prompt now
    carries the audience block, and the assertions pin the bar CONCRETELY (the length, the plain
    register, what stays behind the scenes, and that the failure turns are covered too) rather than
    just proving some voice text exists."""
    composed = compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
    assert NARRATION_VOICE in composed
    lowered = composed.lower()
    assert "talking to the user" in lowered
    assert "a couple of lines at each milestone" in lowered  # R22's length bar
    # The SAME register Plan already speaks — U15 matched a voice rather than inventing a second.
    assert "plain, everyday words" in lowered
    assert "keep the how-it's-built details behind the scenes" in lowered
    assert "the file and folder names, the commands you run" in lowered
    # R20 covers the hard turns as well: a failure and its recovery stay in product language.
    assert "when something goes wrong" in lowered
    # R23: the technical record is untouched, which is what lets the narration be short.
    assert "recorded step by step" in lowered


def test_the_audience_block_is_emitted_exactly_once() -> None:
    """The DATA_INTEGRITY_RULES trap, one block over. The audience wording rides
    `BUILD_WORKING_RULES_TAIL`, which `_WRITE_SEGMENT` already composes — so "adding" it to the
    segment's block list (the obvious move) would print the whole voice rule twice in every Write
    prompt while the build prompt printed it once. Counting is the point: an `in` assertion is
    green at one copy and at five."""
    composed = compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
    assert composed.count(NARRATION_VOICE) == 1
    assert composed.count("A couple of lines at each milestone") == 1
    assert composed.count("TALKING TO THE USER") == 1
    assert BUILD_SYSTEM_PROMPT.count(NARRATION_VOICE) == 1


def test_ask_mode_still_names_the_actual_files() -> None:
    """The audience block belongs to Write, not to every mode. Ask answers a question ABOUT the
    code to someone reading about code, so "name the actual files and quote the actual code" is
    correct there and must survive this unit — a future author harmonising Ask with Write would
    make its answers useless."""
    composed = compose_mode_prompt(ConversationMode.ASK, _CONTEXT)
    assert "name the actual files and quote the actual code" in _ASK_SEGMENT
    assert "name the actual files and quote the actual code" in composed
    assert NARRATION_VOICE not in composed


def test_plan_mode_keeps_its_own_contract_untouched() -> None:
    """Plan's plain-language contract is what U15 MATCHED, not what it replaced. It stays exactly
    where it was, and Plan does not inherit the Write block — which would drag build-mode framing
    ("what you are building right now") into a turn where nothing is being built yet."""
    composed = compose_mode_prompt(ConversationMode.PLAN, _CONTEXT)
    assert NARRATION_VOICE not in composed
    lowered = _PLAN_SEGMENT.lower()
    assert "plain, everyday words" in lowered
    assert "keep the how-it's-built details behind the scenes" in lowered
    assert "present_plan_options" in composed


# --- U19 / R25: the version control the agent no longer does ---------------------------------
#
# THESE THREE REPLACE `test_write_teaches_the_commit_discipline_as_a_capability`, WHICH IS FLIPPED
# RATHER THAN DELETED. It used to assert `COMMIT AS YOU WORK`, `git diff` and `git add -A` were
# all present, because the Write segment taught the agent to stage and commit each coherent slice.
# The platform commits the tree itself at every turn boundary
# (`build_sessions/snapshot._COMMIT_SCRIPT`), so that instruction bought the user nothing and cost
# them a shell round trip and a paragraph of narration per slice.
#
# TWO INERTNESS GUARDS AND ONE LIVENESS GUARD, and the third is not decoration: an inertness pair
# on its own is greenest against a Write prompt somebody deleted outright, so one rule that must
# SURVIVE every trim is asserted next to them.

_RETIRED_GIT_INSTRUCTIONS = (
    # The commit discipline itself.
    "git add",
    "git commit",
    # THE UNDO HALF, and the reason this is a set rather than one assertion. The deleted block
    # taught `git checkout` and `git revert` for backing out a bad edit; both — and `git reset`,
    # and `git stash` — leave a HEAD that is NOT a descendant of the copy on record, over a
    # perfectly good tree. That is precisely the input the workspace-integrity verdict has to
    # reason about before it may call a workspace REVERTED. The verdict closes the hazard on its
    # own (it requires the CONTENT to disagree as well as the lineage); this stops a future
    # prompt edit from feeding it self-inflicted non-descendant HEADs, and reintroducing ANY ONE
    # of the five turns it red.
    "git checkout",
    "git revert",
    "git reset",
    "git stash",
)


@pytest.mark.parametrize(
    "prompt_name", ["write_mode_segment", "build_system_prompt"], ids=["write_mode", "build"]
)
def test_neither_write_prompt_instructs_the_agent_in_git(prompt_name: str) -> None:
    """★ THE INERTNESS GUARD (U19 / R25, cross-plan constraint 9). Asserted as a SET so the
    failure names every instruction that crept back, and asserted on BOTH Write prompts because
    they compose from shared blocks and either composition site could grow one.

    Mutation check: put any of the six back into `BUILD_WORKING_RULES_HEAD` and this goes red."""
    prompt = (
        compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
        if prompt_name == "write_mode_segment"
        else BUILD_SYSTEM_PROMPT
    )
    lowered = prompt.lower()
    found = {phrase for phrase in _RETIRED_GIT_INSTRUCTIONS if phrase in lowered}
    assert found == set(), f"{prompt_name} instructs the agent in git again: {sorted(found)}"
    # The header of the deleted block, named separately so a reworded revival still trips.
    assert "commit as you work" not in lowered


def test_the_write_prompt_still_says_not_to_restart_the_dev_server() -> None:
    """★ THE LIVENESS GUARD, and the one rule this unit must not take with it.

    The agent can start a dev server of its own through `run_command` — the supervisor's child
    env carries no marker that would tell the harness's flag apart from the real one — so this
    sentence is the whole of what stops a second `next dev` racing the one the harness reads to
    verify the build. It survives every prompt trim in this plan."""
    composed = compose_mode_prompt(ConversationMode.WRITE, _CONTEXT)
    lowered = composed.lower()
    assert "the dev server (`next dev`) is already running" in lowered
    assert "do not start, restart, or kill it" in lowered


@pytest.mark.parametrize("mode", list(ConversationMode))
def test_an_approved_plan_is_a_programming_error_in_every_mode(
    mode: ConversationMode,
) -> None:
    # An approved plan is the BUILD agent's fuel — this composer has nowhere to put it, in any
    # mode, so accepting it silently would hide a mis-wired turn engine.
    with pytest.raises(ValueError, match="approved_plan has no home"):
        compose_mode_prompt(mode, _CONTEXT, approved_plan="a plan")


def test_read_modes_stay_lean() -> None:
    # Template facts / working rules are the BUILD prompt's business (plan decision): the
    # chat agent's modes never carry the build environment blocks.
    for mode in _CHAT_MODES:
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
