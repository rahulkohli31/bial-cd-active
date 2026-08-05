"""The mode → toolset registry (U8 / R6 / D1): tool gating AT THE SERVER.

The registry keys on the server-owned `conversation.mode` — never anything the client
sends. Structural gating, not prompt gating: the unified Ask/Plan agent is constructed
with NO tools, and every run passes exactly its mode's toolsets via `agent.run(...,
toolsets=...)` (pydantic-ai 2.5.0: per-run toolsets are ADDITIVE, so an empty agent plus
a mode's list IS the mode's whole surface). A wrong-mode tool is absent from the model's
tool list AND uncallable — a forged call gets the runtime's unknown-tool rejection.

The mode → tool matrix (plan, confirmed):

| Mode  | read/list/search tools | run_command       | write tools | present_plan_options |
|-------|------------------------|-------------------|-------------|----------------------|
| Ask   | yes (snapshot or live) | allowlisted, read | —           | —                    |
| Plan  | yes                    | allowlisted, read | —           | yes                  |
| Write | yes (live workspace)   | full (+SQL guard) | yes         | —                    |

WRITE's surface is COMPOSED here, from two factories: the six sandbox tools
(`orchestrator/tools.sandbox_toolset`, resolved through the run's attached
`SandboxSession`) plus exactly `list_files`/`search_files` off `read_only_toolset`. The
read-only side is `.filtered()` down to those two names by an ALLOWLIST — its `read_file`
and `run_command` are dropped, so no name is ever registered twice (duplicate tool names
are a pydantic-ai `UserError`) and the version Write gets is the sandbox-routed one. The
allowlist direction matters: a tool added to `read_only_toolset` later stays OUT of Write
until it is named, so the wrong-direction failure is a missing tool, never a silently
shadowed one (a read-only `run_command` winning would leave Write unable to run anything).

GENERIC over the deps type (U10): the registry itself is deps-agnostic — the caller
supplies the accessor that resolves the run's workspace from ITS deps. `ReadDeps` (+
`workspace_from_read_deps`) is the minimal agent-level shape the U8 tests exercise; the
turn engine passes a `ChatDeps`-typed accessor for real traffic.

`present_plan_options` is registered here as the SEAM (name + tiny shape, no plan
payload); its turn-engine mechanics — detect-and-force-on-retry, the user's click stored
as the tool RESULT, the snapshot-SHA stamp — are U11's.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from pydantic_ai import RunContext
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.function import FunctionToolset

from src.db.models.conversation import ConversationMode
from src.services.agent.read_tools import ReadOnlyWorkspace, read_only_toolset
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.tools import sandbox_toolset

PLAN_OPTIONS_TOOL = "present_plan_options"
"""The Plan-mode confirmation tool's wire name — U11's detect-and-force logic and the
projection's resolution-state derivation both key on it."""


@dataclass
class ReadDeps:
    """Minimal per-run deps for an Ask/Plan agent-level run (the U8 test surface).
    `workspace` is the turn-pinned read surface (snapshot extraction, or the live
    workspace when a Write sandbox is attached); `user_id` scopes everything downstream
    (ADR-0004)."""

    workspace: ReadOnlyWorkspace
    user_id: uuid.UUID


def workspace_from_read_deps(ctx: RunContext[ReadDeps]) -> ReadOnlyWorkspace:
    """The `ReadDeps` accessor (agent-level tests; U10 supplies its own for `ChatDeps`)."""
    return ctx.deps.workspace


async def present_plan_options(ctx: RunContext[Any]) -> str:
    """Show the user the plan confirmation buttons (Build it / Keep refining). Call this
    when the plan feels ready — it ends your turn; the user's choice arrives as the tool
    result when they decide. Calling it again after revising the plan presents fresh
    options."""
    # U11: the call DEFERS — the run ends with the call unanswered (pydantic-ai
    # `DeferredToolRequests` output), because the answer is the USER'S CLICK, minutes or
    # days later. The stored resolution (refine / build / build_failed:<reason>) is
    # written by `services/turns/plan_options.py` and rides the next run's history as
    # this call's return. Deps-agnostic on purpose: the tool's meaning lives in the
    # engine's handling of the CALL, not here.
    raise CallDeferred


_PLAN_OPTIONS_TOOLSET: FunctionToolset[Any] = FunctionToolset[Any](
    [present_plan_options], id="plan-options"
)


def plan_options_only_toolset() -> list[AbstractToolset[Any]]:
    """JUST the options tool — the U11 forced-retry surface: combined with the
    `ToolOrOutput` restriction, the retry run has no other tool to reach for. Deps-`Any`
    because the tool reads nothing from deps (callers narrow at the run boundary)."""
    return [cast(AbstractToolset[Any], _PLAN_OPTIONS_TOOLSET)]


_WRITE_STRUCTURED_READS: Final = frozenset({"list_files", "search_files"})
"""The ONLY read-only tools Write may borrow: the two structured reads the sandbox six do
not have. `read_file` and `run_command` exist on both sides, and Write must get the
sandbox-routed ones."""


def _structured_reads_only(_ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
    """ALLOWLIST, not a denylist: a tool added to `read_only_toolset` later stays out of
    Write until it is named in `_WRITE_STRUCTURED_READS`. Wrong-direction failure is a
    missing tool, never a shadowed one."""
    return tool_def.name in _WRITE_STRUCTURED_READS


def toolsets_for_mode[DepsT](
    mode: ConversationMode,
    workspace_of: Callable[[RunContext[DepsT]], ReadOnlyWorkspace],
    sandbox_of: Callable[[RunContext[DepsT]], SandboxSession] | None = None,
) -> list[AbstractToolset[DepsT]]:
    """The per-run toolsets for a mode-gated run, over whatever deps type the caller's
    accessors resolve the workspace (and, for Write, the attached sandbox) from.
    Exhaustive over the enum (fail-first: an unknown mode is a programming error, not a
    fallback)."""
    match mode:
        case ConversationMode.ASK:
            return [read_only_toolset(workspace_of)]
        case ConversationMode.PLAN:
            return [
                read_only_toolset(workspace_of),
                cast(AbstractToolset[DepsT], _PLAN_OPTIONS_TOOLSET),
            ]
        case ConversationMode.WRITE:
            if sandbox_of is None:
                raise ValueError(
                    "a Write run needs a sandbox accessor; None means this caller cannot "
                    "run Write (the U8 agent-level ReadDeps surface)."
                )
            # `.filtered()` filters at `get_tools` time, so the model never even sees the
            # read-only `read_file`/`run_command` — the duplicate-name `UserError` is
            # structurally unreachable rather than merely avoided by convention.
            return [
                sandbox_toolset(sandbox_of),
                read_only_toolset(workspace_of).filtered(_structured_reads_only),
            ]
