"""U8 / R6 — the kind → toolset registry: gating is STRUCTURAL, at the agent layer.

For each of the two kinds, the model's actual tool list — what `AgentInfo.function_tools`
carries into the model request — contains exactly that kind's tools, and a FORGED tool call
the kind does not carry is rejected by the runtime itself (unknown tool), never executed.

THIS IS THE ONLY MODULE THAT MAY READ THE KIND TO DECIDE WHAT THE MODEL CAN DO, so this is
where "a Plan chat cannot change the app" is proved. It is proved the only way that counts:
the write tools are ABSENT from the list handed to the run, not forbidden in prose that
something downstream is trusted to enforce.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets.abstract import AbstractToolset

from src.db.models.conversation import ChatKind
from src.services.agent.read_tools import ExtractedSnapshotWorkspace
from src.services.agent.toolsets import (
    _WRITE_STRUCTURED_READS,  # the allowlist U22's trap lives in — asserted against directly
    CHAT_KIND_CATALOGUE,
    ReadDeps,
    ToolSurface,
    registered_tool_definitions,
    toolsets_for_kind,
    workspace_from_read_deps,
)
from src.services.orchestrator.agent import build_agent
from src.services.orchestrator.deps import BuildDeps, SandboxSession
from src.services.orchestrator.progress import ProgressEmitter
from tests.services.orchestrator.conftest import CollectingSink
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_READ_TOOLS = {"read_file", "list_files", "search_files", "run_command"}
_SHARED_TOOLS = {"tell_the_user"}
"""`conversation_toolset` — the tools BOTH kinds carry, because they are about the person
waiting rather than about what the run can do. Named once here so the exact-set assertions
below stay exact: a shared tool has to appear in both, and a test that quietly dropped one
side would pass while the two arms drifted."""
_WRITE_ONLY_TOOLS = {"write_file", "edit_file", "insert_lines", "declare_done"}
_SANDBOX_ONLY_TOOLS = _WRITE_ONLY_TOOLS | {"fetch_output_slice", "apply_schema_change"}
"""U22 / U23: `fetch_output_slice` and `apply_schema_change` are registered on `sandbox_toolset`,
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
    # and no fifth thing. The retired third value (Ask) had exactly this list minus the
    # offer, which is the difference that stopped being worth a whole enum member.
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    await agent.run(
        "hi",
        deps=_deps(workspace),
        model=_tool_listing_model(seen, [text_turn("hello")]),
        toolsets=toolsets_for_kind(ChatKind.PLAN, workspace_from_read_deps).toolsets,
    )
    assert seen["tool_names"] == _READ_TOOLS | _SHARED_TOOLS | {"present_plan_options"}
    # Named individually as well as by set equality: a future tool added to the read-only
    # registry would move the set and could be waved through, but these six names are the
    # ones whose absence IS the guarantee.
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
    build: ToolSurface[BuildDeps] = toolsets_for_kind(
        ChatKind.BUILD, lambda _ctx: workspace, lambda ctx: ctx.deps.sandbox
    )
    assert build.may_write is True


async def test_a_forged_write_tool_call_in_a_plan_chat_is_structurally_rejected(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # The model FORGES a write_file call in a Plan chat. The runtime must reject it as an
    # unknown tool (it is not in the run's toolsets at all) — never execute anything.
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
    # is the result, recorded later by `turns/plan_options.py`).
    #
    # THE CALL CARRIES A PLAN, and it must: `plan` is a REQUIRED argument on the offer tool now,
    # so an argument-less call is not a deferral at all — pydantic-ai validates the missing
    # argument, hands the model a retry prompt, and the run exhausts. That is the correct
    # behaviour and is asserted below; this arm is the happy path.
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
    # …and the plan travels ON the call, which is what makes it the single stored copy: the
    # handoff resolves the plan from this argument rather than from anything the browser posts
    # back, so a stale second tab cannot write stale requirements into a permanent first message.
    assert "Ship the visitor log." in str(result.output.calls[0].args)


async def test_an_offer_with_no_plan_is_not_a_deferral_at_all(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    """The other side of the argument being REQUIRED, and worth pinning because it is the shape
    every pre-migration row has on disk.

    A model that calls the offer with nothing in it does not get a card: pydantic-ai refuses the
    call at validation, and the run exhausts rather than deferring. The turn engine's own arm for
    this (`plan_from_call` returning None → the offer is not recorded and the citizen is told the
    plan was not kept) is asserted in `test_plan_options.py`; this pins the layer beneath, which
    is that the runtime itself will not hand back a deferral for a call with no plan in it."""
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


def _build_deps() -> BuildDeps:
    fake = FakeSandbox()
    emitter = ProgressEmitter(CollectingSink())
    return BuildDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
    )


async def test_build_agent_still_carries_the_whole_sandbox_set_natively() -> None:
    # The harness path is unchanged by U5's convergence: `build_agent` is constructed with
    # `sandbox_toolset` and offers exactly that set. Pinned here so the registry work below
    # cannot quietly move the harness's surface too.
    seen: dict[str, Any] = {}
    await build_agent.run(
        "build it", deps=_build_deps(), model=_tool_listing_model(seen, [text_turn("done")])
    )
    assert seen["tool_names"] == {"read_file", "run_command"} | _SANDBOX_ONLY_TOOLS


def _write_toolsets(
    workspace: ExtractedSnapshotWorkspace,
) -> list[AbstractToolset[BuildDeps]]:
    """A Build chat's composed surface over `BuildDeps`. The workspace accessor is a captured
    fixture here rather than a live sandbox view — which workspace the two structured reads
    resolve through is not what these tests are about."""
    # ANNOTATED, not inferred: both accessors are bare lambdas, so `DepsT` has nothing to
    # be resolved from and the surface would come back over `Never`.
    surface: ToolSurface[BuildDeps] = toolsets_for_kind(
        ChatKind.BUILD,
        lambda _ctx: workspace,
        lambda ctx: ctx.deps.sandbox,
    )
    return surface.toolsets


async def test_a_build_chat_is_the_sandbox_set_plus_exactly_two_structured_reads(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # U5: Build is composed HERE now, not delegated to build_agent. The surface is the sandbox
    # tools plus `list_files`/`search_files` borrowed off the read-only registry —
    # and nothing else. Mutation-check: widen `_WRITE_STRUCTURED_READS` to include
    # `read_file` and the CombinedToolset raises on the duplicate name → red.
    seen: dict[str, Any] = {}
    agent: Agent[BuildDeps, str] = Agent(deps_type=BuildDeps)
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

    agent: Agent[BuildDeps, str] = Agent(deps_type=BuildDeps)
    await agent.run(
        "install zod",
        deps=_build_deps(),
        model=FunctionModel(respond),
        toolsets=_write_toolsets(workspace),
    )
    # The sandbox version teaches `npm install`; the read-only version publishes a closed
    # command list. Exactly one of these can be true.
    assert "npm" in captured["run_command"]
    assert "Available commands:" not in captured["run_command"]


async def test_a_caller_that_cannot_run_build_is_told_so_rather_than_handed_no_tools() -> None:
    # The U8 agent-level `ReadDeps` surface has no sandbox to resolve. Returning `[]` would
    # hand a Build run a model with zero tools — it would produce prose and "succeed" having
    # built nothing. Fail-first instead.
    with pytest.raises(ValueError, match="sandbox accessor"):
        toolsets_for_kind(ChatKind.BUILD, workspace_from_read_deps)


async def test_fetch_output_slice_reaches_the_only_kind_that_runs_commands() -> None:
    """★ THE ALLOWLIST TRAP (U22/R28), asserted where it would have fired silently.

    `_WRITE_STRUCTURED_READS` is an ALLOWLIST of exactly `list_files`/`search_files`. Register the
    slice tool on `read_only_toolset` — the natural home for something that only reads — and the
    filter drops it from Build, the ONE kind that runs commands and therefore the one kind whose
    truncation notices hand out handles. Nothing else in this suite would have gone red: a Plan
    chat would list a tool it can never use, and Build would quietly lose it.

    Asserted against `toolsets_for_kind` (through `registered_tool_definitions`, which enumerates
    it) rather than against a hand-kept name set, so the assertion is about the registry the model
    is actually handed."""
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
    """★ U1 / R69 / N2 — the whole difference between the two kinds, stated as one claim.

    Every other unit in this plan rests on this: if the two surfaces overlap somewhere other
    than the read surface, then something outside the registry has to know which kind it is
    looking at, and "the kind decides only the toolset" stops being true.

    THE INTERSECTION IS NOT EMPTY, AND THAT IS THE INTERESTING PART. `read_file` and
    `run_command` are on BOTH lists, under one name each, doing different things — Plan reads
    and runs through the read-only registry, Build through the sandbox. That is not a leak in
    the rule, it is the rule working: the same ABILITY, routed by the toolset the kind
    resolved, with no caller anywhere asking which kind it was. The two are told apart the
    only way the model can tell them apart — by their description, which is why
    `test_writes_run_command_is_the_sandbox_one_not_the_read_only_guest_list` exists.

    Deliberately NOT a tool count. This plan adds a shared toolset to both arms (U3, U10) and
    a count assertion would go red two units from now with nothing wrong."""
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


# --- U16 / R73: the chat-kind catalogue, beside the registry above ------------------------


def test_chat_kind_catalogue_covers_every_member_of_the_enum() -> None:
    """The exhaustiveness guard R73 asks for: a kind with no wording must fail loudly rather
    than render a blank label. `_describe`'s `match` (no wildcard case) already makes an
    unhandled member a type-checker error at that function — this walks the enum at RUN time
    too, so the guard holds even for whoever isn't running `pyright` on this change.

    Mutation check: comment out either `case` arm in `_describe` and this goes red without
    touching the enum."""
    assert {entry.value for entry in CHAT_KIND_CATALOGUE} == {kind.value for kind in ChatKind}
    assert len(CHAT_KIND_CATALOGUE) == len(ChatKind)


def test_chat_kind_wording_says_what_the_chat_does_for_you_not_what_the_agent_is() -> None:
    """R73's real trap. The wording is what a citizen reads in the composer, the history list
    and the help page — never a description of an agent being run, gated or watched. A
    description that leaked "toolset", "sandbox", "mode" or a file name would be accurate to
    an engineer and either meaningless or alarming to the person clicking the button."""
    forbidden = re.compile(r"toolset|sandbox|\bmode\b|framework|\bagent\b|\.py\b|\.tsx\b", re.I)
    for entry in CHAT_KIND_CATALOGUE:
        assert entry.name and entry.description
        assert not forbidden.search(entry.name), f"{entry.value}: {entry.name!r}"
        assert not forbidden.search(entry.description), f"{entry.value}: {entry.description!r}"


def test_no_second_copy_of_the_chat_kind_wording_lives_under_backend_src() -> None:
    """R73's copy guard, scoped exactly the way it has to be: the ONLY place under
    `backend/src/` allowed to hold a string describing what a chat kind does — one that could
    reach a browser — is this catalogue.

    `mode_prompts.py` is excluded ON PURPOSE, not by oversight: its Plan segment is
    MODEL-facing text owned by a different unit of this same plan, which rewrites that segment
    to sharpen the model's OWN description of what Plan does. An unscoped grep here would put
    this test and that unit's deliverable on opposite sides of one assertion — which is
    precisely the mistake this scoping exists to avoid."""
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
