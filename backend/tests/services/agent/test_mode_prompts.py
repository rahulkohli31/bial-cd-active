"""U9 / D4 / R13 — the chat-kind prompt system: BASE + exactly one positive segment per run.

The R13 property under test: a kind's composed prompt describes what that kind IS and DOES
with the tools it HAS — never prohibitions against tools the registry already makes
uncallable (`test_toolsets.py` proves the structural half; this file proves the prose
half). Plus the D4 delivery property: composed instructions ride `@agent.instructions`
per run and never reach a persisted row (`test_store_roundtrip.py` pins the store seam).

TWO SEGMENTS NOW, NOT THREE. The Ask segment went with the third enum value it existed for;
what it said about reading the app is what the Plan segment already says, and the one thing it
said that Plan does not — what a brand-new project's files actually look like — is recorded as
a hand-off below rather than quietly dropped. Plan C owns the two surviving segments' wording;
this file owns the properties that must hold whatever the wording becomes.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.core.prompt_blocks import NARRATION_VOICE, PORTAL_SURFACES, WRITE_IDENTITY
from src.db.models.conversation import ChatKind
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.mode_prompts import (
    _PLAN_SEGMENT,
    PromptContext,
    compose_kind_prompt,
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

# The segment header each kind's composition must carry, and only its own. The Build arm still
# reads "WRITE MODE" because the segment's own wording is Plan C's to rewrite, not this change's
# — what matters here is that the two compositions are distinct and that neither leaks the
# other's segment.
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
    assert DATA_INTEGRITY_RULES in composed
    # Exactly this kind's segment, not the other's (mutation-checked: swapping a segment in
    # `compose_kind_prompt` turns this red). Parametrized over the WHOLE enum rather than a
    # hand-kept list, so a third kind added without a segment fails here.
    assert _SEGMENT_HEADERS[kind] in composed
    for other, header in _SEGMENT_HEADERS.items():
        if other is not kind:
            assert header not in composed


@pytest.mark.parametrize("kind", list(ChatKind))
def test_every_kind_carries_the_truthful_portal_self_description(
    kind: ChatKind,
) -> None:
    """R5's other half. It lives in BASE, so neither kind can be missing it — the
    walkthrough's invented-portal-features fix does not depend on which segment is selected."""
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
    # R10: the unified chat's right pane is the APP. The relay's retiring wording said the
    # builder view was "a chat beside a live preview"; this layout must not be re-described.
    assert "the right pane shows the app itself" in composed


def test_base_survives_an_undescribed_project() -> None:
    bare = PromptContext(user_name="Asha", project_name="Visitor Log")
    composed = compose_kind_prompt(ChatKind.PLAN, bare)
    assert 'on "Visitor Log".' in composed  # no dangling " — None"
    assert "None" not in composed.split("DATA INTEGRITY")[0]


def test_build_composes_like_the_other_kind() -> None:
    """A Build chat has a segment like any other (KTD-5/KTD-5a). This used to raise, and that
    refusal was read as architecture when it was an unfinished seam: no `_WRITE_SEGMENT` had
    been authored, to avoid duplicating `orchestrator/prompt.py`. A shared import solves that."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert "WRITE MODE" in composed
    assert 'on "Visitor Log"' in composed  # the same BASE both kinds carry


def test_the_write_segment_and_the_build_prompt_come_from_one_source() -> None:
    """The original objection to a Write segment — "it could only drift from the build prompt" —
    is true of a copy and false of a shared import. Assert they genuinely share the blocks, so a
    future edit to either cannot silently fork the two Write prompts."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
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
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert composed.count(DATA_INTEGRITY_RULES) == 1


def test_write_speaks_to_the_person_who_asked_for_the_app() -> None:
    """U15 / R20 / R22. Write mode carried NO audience instruction at all — Plan carried a full
    plain-language contract and Ask deliberately pushes the other way — and the demo build spent
    2,397 words of paths, commands, and framework nouns on a citizen. The composed Write prompt now
    carries the audience block, and the assertions pin the bar CONCRETELY (the length, the plain
    register, what stays behind the scenes, and that the failure turns are covered too) rather than
    just proving some voice text exists."""
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
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
    """The DATA_INTEGRITY_RULES trap, one block over, and now at three sites instead of one.

    The contract is named by `_base()` (both kinds) and separately by `BUILD_SYSTEM_PROMPT`
    (which cannot call `_base`), while its length half rides `BUILD_WORKING_RULES_TAIL`. Each of
    those is a place a second copy could appear, and two prompts that state the same contract
    twice in slightly different places are how two prompts start drifting.

    COUNTING IS THE POINT, and `== 1` rather than `<= 1` is the point of the counting: the
    failure this guard was extended to catch — the block being lifted out of the TAIL and never
    named at the standalone build prompt — is a count of ZERO, which every `<=` and every `in`
    formulation passes."""
    for kind in ChatKind:
        composed = compose_kind_prompt(kind, _CONTEXT)
        assert composed.count(NARRATION_VOICE) == 1
        assert composed.count("TALKING TO THE USER") == 1
        assert composed.count("HOW LONG —") == 1
    build = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert build.lower().count("a couple of lines at each milestone") == 1
    assert BUILD_SYSTEM_PROMPT.count(NARRATION_VOICE) == 1
    assert BUILD_SYSTEM_PROMPT.lower().count("a couple of lines at each milestone") == 1


def test_the_name_the_files_instruction_went_with_the_segment_that_carried_it() -> None:
    """A REAL LOSS, recorded rather than quietly dropped.

    The retired Ask segment told the model to "name the actual files and quote the actual code",
    because Ask answered a question ABOUT the code to someone who had asked about code. There
    are two kinds now, and a citizen who asks what their app does lands in a Plan chat — whose
    segment says the opposite, deliberately: keep file and folder names behind the scenes and
    describe everything in words the user already knows.

    That follows from the origin's decision about what the two kinds are for; it is not a defect
    this change introduced, and it is not a gap this change may paper over by inventing prompt
    copy. So the guard is inertness only: the instruction is gone from every composition, and
    Plan C — which owns the two segments' wording — decides whether any version of "when the
    user is genuinely asking about the code, answer about the code" belongs in the Plan
    segment."""
    for kind in ChatKind:
        assert "name the actual files and quote the actual code" not in compose_kind_prompt(
            kind, _CONTEXT
        )


def test_a_plan_chat_inherits_the_contract_and_only_the_length_differs() -> None:
    """★ AE44 / R79 — the two kinds are told the same thing about their reader.

    A Plan chat used to carry its OWN plain-language paragraph, saying what the audience block
    says in different words. Two wordings of one contract is the drift R79 forbids, and the
    earlier version of this test enforced the split: it asserted the shared block was ABSENT
    from a Plan prompt, on the grounds that build framing ("what you are building right now")
    had no place in a turn where nothing is being built yet.

    That objection was real and it is what the split fixed — the build framing was one SENTENCE,
    about length, and it is now the one per-kind variable. The rest was never build-specific.
    So the assertion inverts: the shared block is present in both, and what differs is which
    length clause the segment carried in."""
    plan = compose_kind_prompt(ChatKind.PLAN, _CONTEXT)
    build = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    assert NARRATION_VOICE in plan
    assert NARRATION_VOICE in build

    # The register survives the move — asserted on the COMPOSED prompt, because the wording no
    # longer lives in the Plan segment; deleting the duplicate is the point of the unit.
    lowered = plan.lower()
    assert "plain, everyday words" in lowered
    assert "keep the how-it's-built details behind the scenes" in lowered
    assert "present_plan_options" in plan

    # The one difference, in both directions.
    assert "a plan is as long as it needs to be" in lowered
    assert "a couple of lines at each milestone" not in lowered
    assert "a couple of lines at each milestone" in build.lower()
    assert "a plan is as long as it needs to be" not in build.lower()

    # AE44's other half: apart from the length clause, the two prompts say the same thing about
    # voice. Nothing in the shared block is reachable from only one kind.
    assert plan.count(NARRATION_VOICE) == build.count(NARRATION_VOICE) == 1


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
        compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
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
    composed = compose_kind_prompt(ChatKind.BUILD, _CONTEXT)
    lowered = composed.lower()
    assert "the dev server (`next dev`) is already running" in lowered
    assert "do not start, restart, or kill it" in lowered


def test_the_composer_takes_no_approved_plan_at_all() -> None:
    """The parameter is GONE rather than rejected, which is the stronger version of the same
    guarantee.

    It used to be accepted and then raised on, so that a mis-wired caller failed loudly instead
    of having its plan silently swallowed. A plan reaches a Build chat as its first user MESSAGE
    now — never spliced into the system prompt — so there is no caller left to mis-wire and no
    argument for one to pass. An inertness guard on the SIGNATURE rather than on a raise: the
    parameter is not there to reject anything, so asserting a `TypeError` from a literal call
    would only prove that Python rejects unknown keywords — and would need a suppression on
    every type checker to compile at all."""
    assert "approved_plan" not in inspect.signature(compose_kind_prompt).parameters


def test_a_plan_chat_stays_lean() -> None:
    # Template facts / working rules are the BUILD prompt's business (plan decision): a Plan
    # chat never carries the build environment blocks.
    composed = compose_kind_prompt(ChatKind.PLAN, _CONTEXT)
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


def test_the_plan_segment_never_speaks_of_forbidden_fruit() -> None:
    lowered = _PLAN_SEGMENT.lower()
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
    # citizen framing is present: outcome-first + see/do + the options contract. "Plain,
    # everyday words" is NO LONGER asserted here on purpose — it moved to the shared audience
    # block, and a Plan chat inherits it through `_base` rather than restating it. Asserting it
    # against the segment again would recreate the second copy R79 exists to prevent;
    # `test_a_plan_chat_inherits_the_contract_and_only_the_length_differs` holds that ground on
    # the composed prompt, where the model actually reads it.
    assert "plain, everyday words" in compose_kind_prompt(ChatKind.PLAN, _CONTEXT).lower()
    assert "will do" in lowered  # "what the app or this change will DO for them"
    assert "see and be able to do" in lowered
    assert "present_plan_options" in _PLAN_SEGMENT
    # the read-first grounding instruction stays — only the OUTPUT register changed
    assert "read the relevant files first" in lowered


# THE PLAN REMINDERS' OWN F9 CHECK USED TO SIT HERE, and it is not orphaned: the reminders it
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
    assert captured["instructions"] == "RELAY-PROMPT"  # mode=None → byte-identical U7 path
    assert result.output == "ok"


async def test_mode_run_composes_and_never_persists_instructions(db_session) -> None:
    """D4 delivery: the model RECEIVES the composition; the store's dump seam keeps it out
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
    """★ U20 / R26 — THE PROMISE IS GONE BECAUSE THE SIGNAL NEVER ARRIVES.

    The retired Ask segment told the model "If there is no app yet, your tools will tell you
    truthfully." `EmptyProjectWorkspace` — the only workspace that answers that way — was
    reachable from one branch of `turns/engine._workspace_for`, and that branch required
    `sandbox_client is None`: NO SANDBOX SERVICE CONFIGURED. In the configured deployment a
    brand-new project gets the live container like every other project, and the container holds
    the golden template — so the reads come back FULL, of template files, and a model waiting
    for an emptiness signal spends round-trips looking for one that is not coming.

    WIDENED TO BOTH SURVIVING SEGMENTS rather than deleted with the segment that carried it.
    The promise was wrong about the platform, not about Ask, so it must not reappear in either.

    HAND-OFF, STATED RATHER THAN DROPPED: the deleted segment also carried the LIVENESS half —
    a sentence saying what a fresh project actually reads as ("the starter template … talk about
    what could be built for them"), so the model was not merely denied a wrong expectation but
    given the right one. `_PLAN_SEGMENT` does not say it today, and this change deliberately
    does not author prompt copy. Plan C owns the two segments' wording and should decide whether
    the Plan segment picks that sentence up; until it does, the liveness half is not asserted
    here, because asserting it would fail for a reason this change did not cause."""
    lowered = compose_kind_prompt(kind, _CONTEXT).lower()
    assert "your tools will tell you truthfully" not in lowered
    assert "if there is no app yet" not in lowered
