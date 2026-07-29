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

from src.db.models.conversation import ConversationMode
from src.services.agent.read_tools import ExtractedSnapshotWorkspace
from src.services.agent.toolsets import ReadDeps, toolsets_for_mode, workspace_from_read_deps
from src.services.orchestrator.agent import build_agent
from src.services.orchestrator.deps import BuildDeps, SandboxSession
from src.services.orchestrator.progress import ProgressEmitter
from tests.services.orchestrator.conftest import CollectingSink
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import text_turn, tool_turn

_READ_TOOLS = {"read_file", "list_files", "search_files", "run_command"}
_WRITE_ONLY_TOOLS = {"write_file", "edit_file", "insert_lines", "declare_done"}


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


async def test_write_mode_is_build_agents_six_and_the_registry_adds_nothing() -> None:
    # The registry's WRITE entry is deliberately empty…
    assert toolsets_for_mode(ConversationMode.WRITE, workspace_from_read_deps) == []
    # …because build_agent carries the write surface natively. Pin the six so the matrix
    # holds where Write actually runs (its own read_file + sentinel-guarded run_command;
    # the structured list/search additions ride U12's live-workspace seam).
    seen: dict[str, Any] = {}
    fake = FakeSandbox()
    emitter = ProgressEmitter(CollectingSink())
    deps = BuildDeps(
        sandbox=SandboxSession(
            sandbox_client=fake,
            handle=fake.handle(),
            app_id=uuid.uuid4(),
            emitter=emitter,
        ),
        emitter=emitter,
        user_id=uuid.uuid4(),
    )
    await build_agent.run(
        "build it", deps=deps, model=_tool_listing_model(seen, [text_turn("done")])
    )
    assert seen["tool_names"] == {"read_file", "run_command"} | _WRITE_ONLY_TOOLS
