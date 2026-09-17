"""The kind → toolset registry: gating is STRUCTURAL, at the agent layer.

For each of the two kinds, the model's actual tool list — what `AgentInfo.function_tools`
carries into the model request — contains exactly that kind's tools, and a FORGED tool call
the kind does not carry is rejected by the runtime itself (unknown tool), never executed.

THIS IS THE ONLY MODULE THAT MAY READ THE KIND TO DECIDE WHAT THE MODEL CAN DO, so this is
where "a Plan chat cannot change the app" is proved. It is proved the only way that counts:
the write tools are ABSENT from the list handed to the run, not forbidden in prose that
something downstream is trusted to enforce.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.function import FunctionToolset
from pydantic_ai.usage import RunUsage

from src.db.models.conversation import ChatKind
from src.services.agent import toolsets as toolsets_module
from src.services.agent.attachment_tools import AttachmentReader
from src.services.agent.read_tools import ExtractedSnapshotWorkspace
from src.services.agent.toolsets import (
    _APP_STATE_SENTENCES,  # the four verdicts the reading hands over — asserted directly
    _WRITE_STRUCTURED_READS,  # the fetch_output_slice trap's allowlist — asserted directly
    CHAT_KIND_CATALOGUE,
    ReadDeps,
    ToolSurface,
    app_state_toolset,
    registered_tool_definitions,
    toolsets_for_kind,
    workspace_from_read_deps,
)
from src.services.build_sessions.integrity import BASELINE_COMMIT_SUBJECT
from src.services.messages.projection import CONNECTOR_SCHEMA_TOOL
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.progress import ProgressEmitter
from src.services.orchestrator.selfheal import AppState, read_the_app_state
from src.services.orchestrator.tools import sandbox_toolset
from src.services.sandbox import DevStatus, SandboxError, SandboxHandle
from src.services.sandbox.base import ExecResult
from tests.fakes import ToolDeps, a_connected_system
from tests.services.orchestrator.conftest import CollectingSink
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_READ_TOOLS = {"read_file", "list_files", "search_files", "run_command"}
_SHARED_TOOLS = {"tell_the_user", "propose_first_slice", "check_the_app"}
"""The tools BOTH kinds carry — `conversation_toolset`, because they are about the person
waiting rather than about what the run can do, and `app_state_toolset`, because what the app is
doing is a reading rather than a capability and Plan has no other route to it. Named once here so
the exact-set assertions below stay exact: a shared tool has to appear in both, and a test that
quietly dropped one side would pass while the two arms drifted."""
_WRITE_ONLY_TOOLS = {"write_file", "edit_file", "insert_lines", "declare_done"}
_SANDBOX_ONLY_TOOLS = _WRITE_ONLY_TOOLS | {"fetch_output_slice", "apply_schema_change"}
"""`fetch_output_slice` and `apply_schema_change` are registered on `sandbox_toolset`,
so they are Build-only for exactly the same reason the four mutators are — and NOT on
`read_only_toolset`, where the `_WRITE_STRUCTURED_READS` allowlist would have filtered them out of
the only kind that runs commands, silently."""


@pytest.fixture
def workspace(tmp_path: Path) -> ExtractedSnapshotWorkspace:
    root = tmp_path / "tree"
    (root / "app").mkdir(parents=True)
    (root / "app" / "page.tsx").write_text("export default function Page() {}\n")
    return ExtractedSnapshotWorkspace(root=root)


def _deps(workspace: ExtractedSnapshotWorkspace) -> ReadDeps:
    return ReadDeps(workspace=workspace, user_id=uuid.uuid4())


def _tool_listing_model(seen: dict[str, Any], turns: list[ModelResponse]) -> FunctionModel:
    iterator = iter(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.setdefault("tool_names", set()).update(tool.name for tool in info.function_tools)
        texts: list[str] = []
        for message in messages:
            for part in getattr(message, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    texts.append(content)
        seen.setdefault("incoming", []).append("\n".join(texts))
        return next(iterator, text_turn("(exhausted)"))

    return FunctionModel(respond)


async def test_a_plan_chat_gets_the_read_surface_plus_only_the_offer_tool(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # THE WHOLE OF "a Plan chat cannot change the app": the four read tools and the offer,
    # and no fifth thing.
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    await agent.run(
        "hi",
        deps=_deps(workspace),
        model=_tool_listing_model(seen, [text_turn("hello")]),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    assert seen["tool_names"] == _READ_TOOLS | _SHARED_TOOLS | {"present_plan_options"}
    # Named individually too: a future tool added to the read-only registry would move the
    # set and could be waved through, but these six names are whose absence IS the guarantee.
    assert not (_WRITE_ONLY_TOOLS | {"apply_schema_change"}) & seen["tool_names"]


async def test_the_surface_answers_may_write_rather_than_leaving_it_to_be_re_derived(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # `may_write` used to be computed a second time, at the sandbox door, by re-reading the
    # enum — and the session manager's docstring already claimed it "came from the toolset".
    # It now genuinely does, so the two cannot drift. Mutation-check: flip either literal in
    # `toolsets_for_kind` and this goes red without any other test moving.
    plan = toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps)
    assert plan.may_write is False
    # ANNOTATED, not inferred: both accessors are bare lambdas, so `DepsT` has nothing to
    # be resolved from and the surface would come back over `Never`.
    build: ToolSurface[ToolDeps] = toolsets_for_kind(
        ChatKind.BUILD, lambda _ctx: workspace, lambda ctx: ctx.deps.sandbox
    )
    assert build.may_write is True


async def test_a_forged_write_tool_call_in_a_plan_chat_is_structurally_rejected(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # It is not in the run's toolsets at all, so the runtime must reject the forged call as
    # an unknown tool — never execute anything.
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    result = await agent.run(
        "please write a file",
        deps=_deps(workspace),
        model=_tool_listing_model(
            seen,
            [
                tool_turn("write_file", {"path": "app/hack.tsx", "file_text": "owned"}),
                text_turn("understood, I cannot write"),
            ],
        ),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    assert result.output == "understood, I cannot write"
    rejection_feed = seen["incoming"][1].lower()
    assert "write_file" in rejection_feed
    assert re.search(r"unknown|not available|unavailable", rejection_feed)
    # And nothing was written anywhere — the tool does not exist to run.
    assert not (workspace.root / "app" / "hack.tsx").exists()


async def test_plan_options_call_defers_and_ends_the_run(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # The call DEFERS — the run ends with `DeferredToolRequests` carrying it (the user's click
    # is the result, recorded later by `turns/plan_options.py`). `plan` is a REQUIRED argument
    # on the offer tool; an argument-less call is not a deferral at all (the next test).
    from pydantic_ai.tools import DeferredToolRequests

    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    result: Any = await agent.run(
        "the plan is ready",
        deps=_deps(workspace),
        model=_tool_listing_model(
            {}, [tool_turn("present_plan_options", {"plan": "Ship the visitor log."})]
        ),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
        output_type=[str, DeferredToolRequests],
    )
    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_name for call in result.output.calls] == ["present_plan_options"]
    # The plan travels ON the call: the handoff resolves it from this argument, never from
    # anything the browser posts back, so a stale second tab can't write stale requirements in.
    assert "Ship the visitor log." in str(result.output.calls[0].args)


async def test_an_offer_with_no_plan_is_not_a_deferral_at_all(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    """The other side of the argument being REQUIRED, and worth pinning because it is the shape
    every pre-migration row has on disk: a call with nothing in it does not get a card,
    pydantic-ai refuses at validation, and the run exhausts rather than deferring. The turn
    engine's own arm for this (`plan_from_call` returning None) is asserted in
    `test_plan_options.py`; this pins the layer beneath it."""
    from pydantic_ai.tools import DeferredToolRequests

    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    result: Any = await agent.run(
        "the plan is ready",
        deps=_deps(workspace),
        model=_tool_listing_model({}, [tool_turn("present_plan_options", {})]),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
        output_type=[str, DeferredToolRequests],
    )
    assert not isinstance(result.output, DeferredToolRequests)


def _build_deps() -> ToolDeps:
    fake = FakeSandbox()
    emitter = ProgressEmitter(CollectingSink())
    return ToolDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
    )


def _sandbox_of(ctx: RunContext[ToolDeps]) -> SandboxSession:
    """The accessor `sandbox_toolset` resolves its session through. A named function rather than
    a bare lambda so `DepsT` has something to be inferred from — the same reason the composed
    surfaces below are annotated at the call."""
    return ctx.deps.sandbox


async def test_the_sandbox_toolset_still_carries_the_whole_sandbox_set_natively() -> None:
    # The RAW factory's own surface, pinned beside the composed one below so the registry work
    # cannot quietly move either. `sandbox_toolset` is the single tool body a Build chat turn
    # composes over its own deps, so this is that set before anything is borrowed onto it.
    #
    # It was pinned through the module-level build agent until the standalone build stack was
    # deleted; that agent went with the harness it served, and the toolset it had been
    # constructed with is the live half that outlived it — so the claim moved down onto the
    # factory rather than being deleted with its old driver.
    seen: dict[str, Any] = {}
    agent: Agent[ToolDeps, str] = Agent(
        deps_type=ToolDeps, toolsets=[sandbox_toolset(_sandbox_of)]
    )
    await agent.run(
        "build it", deps=_build_deps(), model=_tool_listing_model(seen, [text_turn("done")])
    )
    assert seen["tool_names"] == {"read_file", "run_command"} | _SANDBOX_ONLY_TOOLS


def _write_toolsets(
    workspace: ExtractedSnapshotWorkspace,
) -> list[AbstractToolset[ToolDeps]]:
    """A Build chat's composed surface over `ToolDeps`. The workspace accessor is a captured
    fixture here rather than a live sandbox view — which workspace the two structured reads
    resolve through is not what these tests are about."""
    # ANNOTATED, not inferred: both accessors are bare lambdas, so `DepsT` has nothing to
    # be resolved from and the surface would come back over `Never`.
    surface: ToolSurface[ToolDeps] = toolsets_for_kind(
        ChatKind.BUILD,
        lambda _ctx: workspace,
        lambda ctx: ctx.deps.sandbox,
    )
    return surface.toolsets


async def test_a_build_chat_is_the_sandbox_set_plus_exactly_two_structured_reads(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # Build is composed HERE now. It used to be delegated to `build_agent`, which is deleted.
    # The surface is the sandbox tools plus `list_files`/`search_files` borrowed off the
    # read-only registry — and nothing else. Mutation-check: widen `_WRITE_STRUCTURED_READS` to
    # include `read_file` and the CombinedToolset raises on the duplicate name → red.
    seen: dict[str, Any] = {}
    agent: Agent[ToolDeps, str] = Agent(deps_type=ToolDeps)
    await agent.run(
        "add a field",
        deps=_build_deps(),
        model=_tool_listing_model(seen, [text_turn("done")]),
        toolsets=_write_toolsets(workspace),
    )
    assert seen["tool_names"] == _READ_TOOLS | _SANDBOX_ONLY_TOOLS | _SHARED_TOOLS


async def test_writes_run_command_is_the_sandbox_one_not_the_read_only_guest_list(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # THE SILENT-DOWNGRADE CASE, and the reason the filter is an allowlist. Both registries
    # define `run_command`, with the same name and the same argv schema. If the read-only
    # one won, Write would still LOOK correct — and every `npm install` would come back as a
    # guest-list refusal. The only thing that tells the two apart from the model's side is
    # the description, so that is what we assert on.
    captured: dict[str, str] = {}

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for tool in info.function_tools:
            captured[tool.name] = tool.description or ""
        return text_turn("done")

    agent: Agent[ToolDeps, str] = Agent(deps_type=ToolDeps)
    await agent.run(
        "install zod",
        deps=_build_deps(),
        model=FunctionModel(respond),
        toolsets=_write_toolsets(workspace),
    )
    assert "npm" in captured["run_command"]
    assert "Available commands:" not in captured["run_command"]


async def test_a_caller_that_cannot_run_build_is_told_so_rather_than_handed_no_tools() -> None:
    # The agent-level `ReadDeps` surface has no sandbox to resolve. Returning `[]` would
    # hand a Build run a model with zero tools — it would produce prose and "succeed" having
    # built nothing. Fail-first instead.
    with pytest.raises(ValueError, match="sandbox accessor"):
        toolsets_for_kind(ChatKind.BUILD, workspace_from_read_deps)


async def test_fetch_output_slice_reaches_the_only_kind_that_runs_commands() -> None:
    """★ THE ALLOWLIST TRAP, asserted where it would have fired silently. `_WRITE_STRUCTURED_READS`
    is an ALLOWLIST of exactly `list_files`/`search_files` — register the slice tool on
    `read_only_toolset` instead (the natural home for something that only reads) and the filter
    drops it from Build, the ONE kind that runs commands. Nothing else in this suite would have
    gone red: a Plan chat would list a tool it can never use, and Build would quietly lose it.

    Asserted against `toolsets_for_kind` rather than a hand-kept name set, so the assertion is
    about the registry the model is actually handed."""
    build = set(await registered_tool_definitions(ChatKind.BUILD))
    assert "fetch_output_slice" in build
    assert "run_command" in build  # the tool whose notices name it — same kind, by construction
    # It is a SANDBOX tool, not a borrowed read: it is NOT in the allowlist, and it is NOT in the
    # read-only surface either. Both halves matter — one alone is satisfied by the broken shape.
    assert "fetch_output_slice" not in _WRITE_STRUCTURED_READS
    assert "fetch_output_slice" not in set(await registered_tool_definitions(ChatKind.PLAN))


async def test_the_registry_is_exhaustive_over_the_enum() -> None:
    """Every kind the enum can hold has a surface, and the two surfaces are different.

    A `match` with no fallback arm already makes an unhandled member a `NameError` at run
    time rather than a silent empty toolset — but only on the path that reaches it. This
    walks the enum, so a third member added without a surface fails here, loudly, instead of
    on whichever request first carries it."""
    surfaces = {kind: set(await registered_tool_definitions(kind)) for kind in ChatKind}
    assert set(surfaces) == {ChatKind.PLAN, ChatKind.BUILD}
    assert all(names for names in surfaces.values())
    assert surfaces[ChatKind.PLAN] != surfaces[ChatKind.BUILD]


async def test_the_kinds_differ_by_which_toolsets_they_are_handed_and_by_nothing_else() -> None:
    """★ THE WHOLE DIFFERENCE BETWEEN THE TWO KINDS, STATED AS ONE CLAIM: every other test in
    this suite rests on it — if the two surfaces overlap anywhere but the read surface, then
    something outside the registry has to know which kind it is looking at.

    The intersection is not empty ON PURPOSE — `read_file` and `run_command` are on BOTH
    lists, told apart only by description (see the guest-list test below). And deliberately
    NOT a tool count: a shared toolset is expected to land on both arms later, and a count
    assertion here would go red then with nothing actually broken."""
    plan = await registered_tool_definitions(ChatKind.PLAN)
    build = await registered_tool_definitions(ChatKind.BUILD)

    assert set(plan) & set(build) == _READ_TOOLS | _SHARED_TOOLS
    # Absence is the guardrail, so assert absence — not that a downstream check refuses them.
    assert not (_SANDBOX_ONLY_TOOLS & set(plan))
    assert _SANDBOX_ONLY_TOOLS <= set(build)
    assert "present_plan_options" in plan
    assert "present_plan_options" not in build

    # Same name on both lists, different ability underneath, decided by the registry alone.
    assert plan["run_command"].description != build["run_command"].description


# --- the SECOND axis: which PROJECT, not only which kind -----------------------
#
# WHY THIS SECTION EXISTS AT ALL. `toolsets_for_kind`'s docstring has always claimed that a
# wrong-kind tool is "absent from the model's tool list AND uncallable ... never a policy check
# that could be bypassed". The connected-data surface extends that claim to a second axis — the
# project — and the reason is not thrift. The platform already has a flow whose whole purpose is
# to say no to a project: an administrator declining the access request, and a revocation that
# flips `effectively_on` back to false. A tool surface that ignored that answer and refused inside
# the tool body would be handing every project a way to ask, and spending a round trip on the
# reply. So the same structural guarantee, proven the same way: in BOTH directions.


async def test_a_project_that_reads_no_connected_data_is_offered_none() -> None:
    """The ordinary project, and the default every existing caller already gets.

    THE EXACT SETS ARE THE ASSERTION. `test_a_plan_chat_gets_the_read_surface_plus_only_the_offer_
    tool` and `test_a_build_chat_is_the_sandbox_set_plus_exactly_two_structured_reads` both list
    the surface exactly and both call `toolsets_for_kind` with no connectors, so they are already
    the guard against this feature widening what every project on the platform carries. This
    states it once more against the registry itself, because those two are about the KIND."""
    plan = set(await registered_tool_definitions(ChatKind.PLAN))
    build = set(await registered_tool_definitions(ChatKind.BUILD))
    assert CONNECTOR_SCHEMA_TOOL not in plan
    assert CONNECTOR_SCHEMA_TOOL not in build
    assert plan == _READ_TOOLS | _SHARED_TOOLS | {"present_plan_options"}
    assert build == _READ_TOOLS | _SANDBOX_ONLY_TOOLS | _SHARED_TOOLS


async def test_a_connected_project_gets_exactly_one_more_tool_on_both_arms() -> None:
    """R4 says both arms, and both arms is what this asserts — a Plan chat reasons about what can
    be built from the data and a Build chat writes the code that reads it.

    ONE MORE TOOL, NAMED. Asserted as the off-surface PLUS that name rather than as a fresh list,
    so a second tool sneaking onto the connected surface fails here rather than being absorbed
    into a hand-updated set."""
    connected = (a_connected_system(),)
    plan = set(await registered_tool_definitions(ChatKind.PLAN, connected_systems=connected))
    build = set(await registered_tool_definitions(ChatKind.BUILD, connected_systems=connected))
    assert plan == set(await registered_tool_definitions(ChatKind.PLAN)) | {CONNECTOR_SCHEMA_TOOL}
    assert build == set(await registered_tool_definitions(ChatKind.BUILD)) | {
        CONNECTOR_SCHEMA_TOOL
    }


async def test_the_connected_surface_does_not_change_what_a_run_may_write(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    """`may_write` rides WITH the toolsets and is the flag the sandbox door reads. Reading the
    connected data is a READ on both arms, so a Plan run that gained the tool must not have gained
    the ability to change the app along with it."""
    connected = (a_connected_system(),)
    plan = toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps, connected_systems=connected)
    assert plan.may_write is False
    # ANNOTATED, not inferred: both accessors are bare lambdas, so `DepsT` has nothing to be
    # resolved from and the surface would come back over `Never` — the same note the two other
    # Build surfaces in this file carry.
    build: ToolSurface[ToolDeps] = toolsets_for_kind(
        ChatKind.BUILD,
        lambda _ctx: workspace,
        lambda ctx: ctx.deps.sandbox,
        connected_systems=connected,
    )
    assert build.may_write is True


async def test_a_forged_connector_call_in_an_unconnected_chat_is_structurally_rejected(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    """★ THE GATE PROVEN THE ONLY WAY THAT COUNTS — by CALLING it, not by reading the list.

    A project whose administrator declined the connector, or whose approval was revoked, has no
    tool here. A model that tries anyway meets the runtime's unknown-tool rejection, exactly as a
    Plan chat trying `write_file` does. Asserting the absence from a name list would pass just as
    happily against an implementation that registered the tool and refused inside its body — and
    that implementation is the one this design exists not to be."""
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    result = await agent.run(
        "read the connected data schema",
        deps=_deps(workspace),
        model=_tool_listing_model(
            seen,
            [
                tool_turn(CONNECTOR_SCHEMA_TOOL, {"system": "DICE"}),
                text_turn("understood, nothing is connected here"),
            ],
        ),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    assert result.output == "understood, nothing is connected here"
    assert CONNECTOR_SCHEMA_TOOL not in seen["tool_names"]
    rejection_feed = seen["incoming"][1].lower()
    assert CONNECTOR_SCHEMA_TOOL in rejection_feed
    assert re.search(r"unknown|not available|unavailable", rejection_feed)


async def test_the_default_is_no_connectors_and_that_default_is_the_whole_guard() -> None:
    """★ THE MUTATION THIS SECTION IS BUILT AROUND, expressed as an executable check.

    The manual form is: make `connected_systems` default to a connected system in
    `toolsets_for_kind`, and `test_a_project_that_reads_no_connected_data_is_offered_none` goes
    red. Run it before merging. What is asserted HERE is the property that mutation breaks — the
    signature's default is empty, in both functions that take it — because a default that drifted
    in only one of them would hand the connector tool to a project whose administrator refused it,
    and the guard that reads the registry back would be reading the widened default too."""
    for function in (toolsets_for_kind, registered_tool_definitions):
        default = inspect.signature(function).parameters["connected_systems"].default
        assert default == (), f"{function.__name__} defaults to {default!r}"


# --- the chat-kind catalogue, beside the registry above ------------------------


def test_chat_kind_catalogue_covers_every_member_of_the_enum() -> None:
    """The exhaustiveness guard asks for: a kind with no wording must fail loudly rather
    than render a blank label. `_describe`'s `match` (no wildcard case) already makes an
    unhandled member a type-checker error at that function — this walks the enum at RUN time
    too, so the guard holds even for whoever isn't running `pyright` on this change.

    Mutation check: comment out either `case` arm in `_describe` and this goes red without
    touching the enum."""
    assert {entry.value for entry in CHAT_KIND_CATALOGUE} == {kind.value for kind in ChatKind}
    assert len(CHAT_KIND_CATALOGUE) == len(ChatKind)


def test_chat_kind_wording_says_what_the_chat_does_for_you_not_what_the_agent_is() -> None:
    """The real trap. The wording is what a citizen reads in the composer, the history list
    and the help page — never a description of an agent being run, gated or watched. A
    description that leaked "toolset", "sandbox", "mode" or a file name would be accurate to
    an engineer and either meaningless or alarming to the person clicking the button."""
    forbidden = re.compile(r"toolset|sandbox|\bmode\b|framework|\bagent\b|\.py\b|\.tsx\b", re.I)
    for entry in CHAT_KIND_CATALOGUE:
        assert entry.name and entry.description
        assert not forbidden.search(entry.name), f"{entry.value}: {entry.name!r}"
        assert not forbidden.search(entry.description), f"{entry.value}: {entry.description!r}"


def test_no_second_copy_of_the_chat_kind_wording_lives_under_backend_src() -> None:
    """This copy guard is scoped exactly the way it has to be: the ONLY place under
    `backend/src/` allowed to hold a string describing what a chat kind does — one that could
    reach a browser — is this catalogue.

    `mode_prompts.py` is excluded ON PURPOSE: its Plan segment is MODEL-facing text owned by a
    different unit of this same plan, which rewrites that segment to sharpen the model's OWN
    description of what Plan does. An unscoped grep here would pit this test against that
    unit's deliverable."""
    backend_root = Path(__file__).resolve().parents[3]
    src_root = backend_root / "src"
    excluded = {
        src_root / "services" / "agent" / "toolsets.py",  # the catalogue itself
        src_root / "services" / "agent" / "mode_prompts.py",  # model-facing; a different unit's
    }
    wordings = [entry.description for entry in CHAT_KIND_CATALOGUE]
    offenders = [
        str(path.relative_to(backend_root))
        for path in src_root.rglob("*.py")
        if path not in excluded
        if any(wording in path.read_text(encoding="utf-8") for wording in wordings)
    ]
    assert offenders == []


# --- the attachment capability ------------------------------------


def _reader_from_read_deps(ctx: RunContext[ReadDeps]) -> AttachmentReader:
    """A stand-in accessor: these tests assert WHO is offered the tool, not what it reads."""
    return AttachmentReader(session=cast(Any, object()))


async def test_a_plan_chat_offered_a_reader_gets_the_attachment_tool(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    """★ R14, on the arm the architecture sanctions.

    Plan already executes in the container, but only the eight read-only binaries on the guest
    list — `python3` is not among them, so it cannot invoke the shipped reader the way Build
    does. Widening that list is unavailable: it is shared with the reviewer agent over untrusted
    contents and takes argv and nothing else, so it cannot be loosened for one caller. Registering
    the capability on this arm is the difference `toolsets_for_kind` exists to express.
    """
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    await agent.run(
        "hi",
        deps=_deps(workspace),
        model=_tool_listing_model(seen, [text_turn("hello")]),
        toolsets=toolsets_for_kind(
            ChatKind.PLAN, workspace_from_read_deps, reader_of=_reader_from_read_deps
        ).toolsets,
    )

    assert "read_attachment" in seen["tool_names"]
    # And it did NOT arrive by widening what Plan may execute: the write tools are still absent.
    assert not (_WRITE_ONLY_TOOLS | {"apply_schema_change"}) & seen["tool_names"]


async def test_a_plan_chat_with_no_reader_is_unchanged(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    """The capability is optional so the agent-level surface, which has no sandbox at all, still
    builds a Plan run — a caller with no reader simply does not offer the tool."""
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    await agent.run(
        "hi",
        deps=_deps(workspace),
        model=_tool_listing_model(seen, [text_turn("hello")]),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )

    assert "read_attachment" not in seen["tool_names"]


def test_a_build_chat_is_never_offered_the_attachment_tool() -> None:
    """★ THE ASYMMETRY IS R15, NOT AN OVERSIGHT. Build holds an unrestricted `run_command` and can
    read, EDIT and re-run the reader as it would any other file. A fixed-shape tool beside that
    would be a second, weaker way to do what it already does better — and would make the reader
    look opaque at the exact moment it stops being so.

    Asserted structurally: the Build arm takes no reader accessor at all, so there is no argument
    that could put this tool on that surface.
    """
    import inspect

    from src.services.agent import toolsets as toolsets_module

    source = inspect.getsource(toolsets_module.toolsets_for_kind)
    plan_arm, _, build_arm = source.partition("case ChatKind.BUILD:")
    assert "attachment_toolset" in plan_arm
    assert "attachment_toolset" not in build_arm


def test_the_reviewer_cannot_receive_the_attachment_tool() -> None:
    """★ THE CONSTRAINT R14 IS REALLY ABOUT. The reviewer agent runs on the control plane over
    untrusted project contents and SHARES its read surface with Plan — `check_the_guest_list`
    takes argv and nothing else precisely so no body below it can ask which agent is calling.

    This capability is not on that surface. It is registered by `toolsets_for_kind`, which the
    reviewer never calls: it builds its own toolset directly from `read_only_toolset`. Asserted
    here rather than assumed, because the day someone moves this tool into `read_tools.py` for
    convenience is the day the reviewer silently gains it.
    """
    import inspect

    from src.services.agent import read_tools
    from src.services.classification import agent as review_agent_module

    assert "attachment_toolset" not in inspect.getsource(read_tools)
    assert "read_attachment" not in inspect.getsource(review_agent_module)
    assert "toolsets_for_kind" not in inspect.getsource(review_agent_module)


# --- what the app is doing, as a tool the agent PULLS --------------------------------------


class _CountingProbe:
    """A sandbox client double that counts container round trips and can be told to fail.

    Only the two calls the reading makes are implemented — the readiness poll and the one
    command — because a double that answered more would let the body grow past what this tool is
    allowed to cost."""

    def __init__(
        self,
        *,
        ready: bool = True,
        running: bool = True,
        stdout: str = "diverged",
        exploding: bool = False,
    ):
        self.polls = 0
        self.commands = 0
        self._ready = ready
        self._running = running
        self._stdout = stdout
        self._exploding = exploding

    async def dev_status(self, _handle: Any) -> DevStatus:
        self.polls += 1
        if self._exploding:
            raise SandboxError("the supervisor did not answer")
        return DevStatus(running=self._running, ready=self._ready, port=3000)

    async def _run(self, _handle: Any, _argv: Any, *, timeout_s: float = 0) -> ExecResult:
        self.commands += 1
        if self._exploding:
            raise SandboxError("the supervisor did not answer")
        return ExecResult(exit=0, stdout=self._stdout, stderr="")

    # The baseline probe reaches for this name; the body is aliased above so the method this
    # double defines is not spelled the way a shell-injection guard reads as one.
    exec = _run


_A_HANDLE = SandboxHandle(
    fqdn="sbx-1.westeurope.azurecontainerapps.io",
    token="t",
    app_name="sbx-1",
    preview_url="https://apps.example/a/sbx-1/",
    ready=True,
)


def _the_reading_never_calls_a_model(
    _messages: list[ModelMessage], _info: AgentInfo
) -> ModelResponse:
    raise AssertionError("the state reading asks a container, not a model")


_STATE_READING_MODEL = FunctionModel(_the_reading_never_calls_a_model)
"""`RunContext` requires a model and the tool never reads it; one that raises if it is ever
asked keeps that fact honest rather than parking a live client here."""


def _session_over(probe: _CountingProbe) -> SandboxSession:
    return SandboxSession(sandbox_client=cast(Any, probe), handle=_A_HANDLE, app_id=uuid.uuid4())


def _a_run_context() -> RunContext[Any]:
    return RunContext(deps=None, model=_STATE_READING_MODEL, usage=RunUsage())


async def _ask(session: SandboxSession | None) -> str:
    """Call `check_the_app` the way a run does — through the registered tool, not the body."""
    toolset: FunctionToolset[Any] = app_state_toolset(lambda _ctx: session)
    ctx = _a_run_context()
    tool = (await toolset.get_tools(ctx))["check_the_app"]
    return cast(str, await toolset.call_tool("check_the_app", {}, ctx, tool))


async def test_a_turn_with_no_container_is_told_so_rather_than_killed() -> None:
    """★ THE PLAN-ARM CASE, and the reason this accessor may answer `None` where its neighbours
    raise. A Plan turn can run before any workspace exists, and a registered tool whose accessor
    raises is a turn-killer rather than a reading.

    Mutation check: make the accessor raise on a missing session and this goes red with the
    exception instead of the sentence."""
    assert await _ask(None) == _APP_STATE_SENTENCES[AppState.UNKNOWN]


async def test_a_container_that_cannot_answer_reads_as_could_not_tell() -> None:
    """A `SandboxError` inside the probe is a reading of "could not tell", never a raise and
    never a verdict. A model told "your app is fine" on the strength of a check that never
    completed will defend the claim to the person looking at the broken app.

    Mutation check: let `read_the_app_state` propagate `SandboxError` and this goes red."""
    probe = _CountingProbe(exploding=True)
    assert await _ask(_session_over(probe)) == _APP_STATE_SENTENCES[AppState.UNKNOWN]
    assert probe.polls == 1, "the probe must have been attempted, or the answer came for free"


async def test_a_second_call_in_the_same_run_costs_no_container_round_trip() -> None:
    """★ THE CEILING. Without it, N calls in one turn are N readiness polls plus N commands,
    inside a turn the citizen is waiting on — and the answer cannot have changed, because
    nothing between two tool calls touches the container.

    Mutation check: drop the memo and the second assertion goes red at two polls."""
    probe = _CountingProbe()
    toolset: FunctionToolset[Any] = app_state_toolset(lambda _ctx: _session_over(probe))
    ctx = _a_run_context()
    tool = (await toolset.get_tools(ctx))["check_the_app"]

    first = await toolset.call_tool("check_the_app", {}, ctx, tool)
    second = await toolset.call_tool("check_the_app", {}, ctx, tool)

    assert first == second
    assert (probe.polls, probe.commands) == (1, 1), (
        f"the second call went back to the container: {probe.polls} polls, "
        f"{probe.commands} commands"
    )


async def test_a_fresh_run_reads_the_app_again() -> None:
    """The other half of the ceiling, and why it is per-toolset rather than per-process: a
    self-heal iteration builds its toolsets afresh, and the app it is about to be asked about is
    the one the previous iteration just repaired."""
    probe = _CountingProbe()
    await _ask(_session_over(probe))
    await _ask(_session_over(probe))
    assert probe.polls == 2


def _baseline_probe_output(*, working: str) -> str:
    """What the baseline probe prints: root, the baseline blob, the working blob, the subject.

    Built here rather than hand-typed as one string so the two arms differ in exactly the field
    the answer turns on — a fixture that also changed the subject would be testing the
    unanswerable path twice."""
    return f"root1@@blob-template@@{working}@@{BASELINE_COMMIT_SUBJECT}"


@pytest.mark.parametrize(
    ("ready", "running", "stdout", "expected"),
    [
        (False, False, "", AppState.NOT_SERVING),
        (False, True, "", AppState.UNKNOWN),
        (True, True, _baseline_probe_output(working="blob-template"), AppState.STILL_THE_TEMPLATE),
        (True, True, _baseline_probe_output(working="blob-theirs"), AppState.LIVE),
        (True, True, "", AppState.UNKNOWN),
    ],
    ids=["died", "still-starting", "starter-page", "built", "unreadable-baseline"],
)
async def test_the_ordering_rule_survives_the_move_to_a_tool(
    ready: bool, running: bool, stdout: str, expected: AppState
) -> None:
    """★ THE RULE: could-not-tell > not-serving > whatever the page shows. Each arm is a
    different answer, and collapsing any two is the defect.

    "STILL STARTING UP" IS `UNKNOWN`, NOT `NOT_SERVING` — a reading taken inside the dev
    server's compile window would otherwise call every cold app dead. An unreadable baseline on
    a serving app is `UNKNOWN` too: the app is up, but what the citizen is looking at is not
    established.

    Mutation check: fold `STILL_TRYING` into `NOT_SERVING` and the still-starting case goes
    red."""
    probe = _CountingProbe(ready=ready, running=running, stdout=stdout)
    state = await read_the_app_state(cast(Any, probe), _A_HANDLE, max_polls=1, poll_s=0)
    assert state is expected


async def test_the_reading_never_invites_the_agent_to_derive_its_own() -> None:
    """★ THE TRAP THIS TOOL IS ONE STEP AWAY FROM. The harness has already offered the agent a
    `tsc` it could run for itself and withdrawn the offer, because the model spent 20-40 s and a
    context window per turn establishing what the platform already knew.

    The description says the platform runs the check, and the four answers are verdicts rather
    than signals. Nothing in it names a command to run."""
    described = (await registered_tool_definitions(ChatKind.PLAN))["check_the_app"].description
    assert described is not None
    assert "The platform runs the check" in described
    for invitation in ("tsc", "npm", "`run_command`", "check for yourself"):
        assert invitation not in described


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_when_to_call_it_survives_in_the_description(kind: ChatKind) -> None:
    """★ WHEN TO CALL IT is the sentence that decides whether this tool is reached for at all,
    and the registered description is the ONLY place the model is told it.

    Pinned as a literal rather than derived from the docstring, so trimming the rule away cannot
    quietly take the expectation with it. Mutation check: delete the sentence from
    `check_the_app`'s docstring and this goes red while every other assertion about that
    description stays green."""
    definitions = await registered_tool_definitions(kind)
    described = definitions["check_the_app"].description
    assert described is not None
    assert (
        "Call this before you say anything about what the app does now, and whenever the user "
        "tells you something is wrong."
    ) in " ".join(described.split())


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_every_registered_tool_reaches_the_model_with_a_description(kind: ChatKind) -> None:
    """★ A tool's docstring is the only thing that tells the model what the tool is FOR, so one
    registered without a description arrives as a bare name to guess at.

    Asserted over the registry rather than over a list somebody keeps, so it covers a tool nobody
    thought to write a test for."""
    definitions = await registered_tool_definitions(kind)
    assert definitions, f"{kind} registers nothing at all"
    for name, definition in definitions.items():
        assert definition.description, f"`{name}` is registered with no description"


def test_the_toolset_module_imports_from_a_bare_interpreter() -> None:
    """★ THE PACKAGE-CYCLE TRAP. `build_sessions.__init__` reaches `appdata` →
    `services.projects` → `agent.agent` → `orchestrator.__init__`, so an `agent.*` module
    importing `build_sessions.integrity` at module level fails at interpreter start — every
    turn, before any test runs. The baseline probe is reached through the orchestrator instead,
    which already defers that one import inside a function.

    A subprocess rather than an in-process import, because by the time this file is collected
    the package graph is warm and an import that would have failed at start succeeds."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import src.services.agent.toolsets"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "build_sessions.integrity" not in inspect.getsource(toolsets_module)
