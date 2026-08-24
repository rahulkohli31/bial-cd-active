"""U8 / R6 — the mode → toolset registry: gating is STRUCTURAL, at the agent layer.

Plan B criterion 1, adapted to the agent layer (the HTTP-level no-overrides test lands
with U10's turn engine): for each mode, the model's actual tool list — what
`AgentInfo.function_tools` carries into the model request — contains exactly that mode's
tools, and a FORGED wrong-mode tool call is rejected by the runtime itself (unknown tool),
never executed. Write's surface is `build_agent`'s decorator-registered six, pinned here
so the matrix holds end-to-end.
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

from src.db.models.conversation import ConversationMode
from src.services.agent.read_tools import ExtractedSnapshotWorkspace
from src.services.agent.toolsets import (
    _WRITE_STRUCTURED_READS,  # the allowlist U22's trap lives in — asserted against directly
    ReadDeps,
    registered_tool_definitions,
    toolsets_for_mode,
    workspace_from_read_deps,
)
from src.services.orchestrator.agent import build_agent
from src.services.orchestrator.deps import BuildDeps, SandboxSession
from src.services.orchestrator.progress import ProgressEmitter
from tests.services.orchestrator.conftest import CollectingSink
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_READ_TOOLS = {"read_file", "list_files", "search_files", "run_command"}
_WRITE_ONLY_TOOLS = {"write_file", "edit_file", "insert_lines", "declare_done"}
_SANDBOX_ONLY_TOOLS = _WRITE_ONLY_TOOLS | {"fetch_output_slice", "apply_schema_change"}
"""U22 / U23: `fetch_output_slice` and `apply_schema_change` are registered on `sandbox_toolset`,
so they are Write-only for exactly the same reason the four mutators are — and NOT on
`read_only_toolset`, where the `_WRITE_STRUCTURED_READS` allowlist would have filtered them out of
the only mode that runs commands, silently."""


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


async def test_ask_mode_exposes_exactly_the_read_surface(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    await agent.run(
        "hi",
        deps=_deps(workspace),
        model=_tool_listing_model(seen, [text_turn("hello")]),
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
    )
    assert seen["tool_names"] == _READ_TOOLS  # write tools structurally ABSENT


async def test_plan_mode_adds_only_present_plan_options(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    seen: dict[str, Any] = {}
    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    await agent.run(
        "hi",
        deps=_deps(workspace),
        model=_tool_listing_model(seen, [text_turn("hello")]),
        toolsets=toolsets_for_mode(ConversationMode.PLAN, workspace_from_read_deps),
    )
    assert seen["tool_names"] == _READ_TOOLS | {"present_plan_options"}


async def test_a_forged_write_tool_call_in_ask_mode_is_structurally_rejected(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # The model FORGES a write_file call while in Ask mode. The runtime must reject it as
    # an unknown tool (it is not in the run's toolsets at all) — never execute anything.
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
        toolsets=toolsets_for_mode(ConversationMode.ASK, workspace_from_read_deps),
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
    # U11: the call DEFERS — the run ends with `DeferredToolRequests` carrying it (the
    # user's click is the result, recorded later by `turns/plan_options.py`).
    from pydantic_ai.tools import DeferredToolRequests

    agent: Agent[ReadDeps, str] = Agent(deps_type=ReadDeps)
    result: Any = await agent.run(
        "the plan is ready",
        deps=_deps(workspace),
        model=_tool_listing_model({}, [tool_turn("present_plan_options", {})]),
        toolsets=toolsets_for_mode(ConversationMode.PLAN, workspace_from_read_deps),
        output_type=[str, DeferredToolRequests],
    )
    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_name for call in result.output.calls] == ["present_plan_options"]


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
    """Write's composed surface over `BuildDeps`. The workspace accessor is a captured
    fixture here rather than a live sandbox view — commit 3 introduces the live one, and
    which workspace the two structured reads resolve through is not what these tests are
    about."""
    return toolsets_for_mode(
        ConversationMode.WRITE,
        lambda _ctx: workspace,
        lambda ctx: ctx.deps.sandbox,
    )


async def test_write_mode_is_the_sandbox_set_plus_exactly_two_structured_reads(
    workspace: ExtractedSnapshotWorkspace,
) -> None:
    # U5: Write is composed HERE now, not delegated to build_agent. The surface is the sandbox
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
    assert seen["tool_names"] == _READ_TOOLS | _SANDBOX_ONLY_TOOLS


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


async def test_a_caller_that_cannot_run_write_is_told_so_rather_than_handed_no_tools() -> None:
    # The U8 agent-level `ReadDeps` surface has no sandbox to resolve. Returning `[]` would
    # hand a Write run a model with zero tools — it would produce prose and "succeed"
    # having built nothing. Fail-first instead.
    with pytest.raises(ValueError, match="sandbox accessor"):
        toolsets_for_mode(ConversationMode.WRITE, workspace_from_read_deps)


async def test_fetch_output_slice_reaches_write_the_only_mode_that_runs_commands() -> None:
    """★ THE ALLOWLIST TRAP (U22/R28), asserted where it would have fired silently.

    `_WRITE_STRUCTURED_READS` is an ALLOWLIST of exactly `list_files`/`search_files`. Register the
    slice tool on `read_only_toolset` — the natural home for something that only reads — and the
    filter drops it from Write, the ONE mode that runs commands and therefore the one mode whose
    truncation notices hand out handles. Nothing else in this suite would have gone red: the read
    modes would list a tool they can never use, and Write would quietly lose it.

    Asserted against `toolsets_for_mode` (through `registered_tool_definitions`, which enumerates
    it) rather than against a hand-kept name set, so the assertion is about the registry the model
    is actually handed."""
    write = set(await registered_tool_definitions(ConversationMode.WRITE))
    assert "fetch_output_slice" in write
    assert "run_command" in write  # the tool whose notices name it — same mode, by construction
    # It is a SANDBOX tool, not a borrowed read: it is NOT in the allowlist, and it is NOT in the
    # read-only surface either. Both halves matter — one alone is satisfied by the broken shape.
    assert "fetch_output_slice" not in _WRITE_STRUCTURED_READS
    for read_mode in (ConversationMode.ASK, ConversationMode.PLAN):
        assert "fetch_output_slice" not in set(await registered_tool_definitions(read_mode))
