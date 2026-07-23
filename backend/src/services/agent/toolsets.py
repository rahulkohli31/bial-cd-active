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

WRITE's surface is `build_agent`'s six decorator-registered tools (`orchestrator/
tools.py`) — the harness runs it directly, so this registry contributes NO additional
toolsets for Write today. The structured `list_files`/`search_files` additions ride U12's
live-workspace seam: `read_only_toolset` is generic over deps, so U12 passes an accessor
that resolves the attached sandbox's workspace. (Only those two may be added there —
`read_file` and `run_command` already exist on `build_agent`, and duplicate tool names
are a pydantic-ai `UserError`.)

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
from typing import Any, cast

from pydantic_ai import RunContext
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.function import FunctionToolset

from src.db.models.conversation import ConversationMode
from src.services.agent.read_tools import ReadOnlyWorkspace, read_only_toolset

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
    when the plan feels ready — after it, wait for the user's choice; do not keep
    writing. Calling it again after revising the plan presents fresh options."""
    # U11 re-homes this result: the turn engine detects the call and the USER'S CLICK
    # becomes the stored tool result (refine / build / build_failed). This stub return is
    # the pre-U11 seam so the tool exists structurally in Plan mode. Deps-agnostic on
    # purpose: the tool's meaning lives in the engine's handling of the CALL, not here.
    return "The options are in front of the user. Wait for their choice before continuing."


_PLAN_OPTIONS_TOOLSET: FunctionToolset[Any] = FunctionToolset[Any](
    [present_plan_options], id="plan-options"
)


def toolsets_for_mode[DepsT](
    mode: ConversationMode,
    workspace_of: Callable[[RunContext[DepsT]], ReadOnlyWorkspace],
) -> list[AbstractToolset[DepsT]]:
    """The per-run toolsets for a mode-gated Ask/Plan run, over whatever deps type the
    caller's accessor resolves the workspace from. Exhaustive over the enum (fail-first:
    an unknown mode is a programming error, not a fallback)."""
    match mode:
        case ConversationMode.ASK:
            return [read_only_toolset(workspace_of)]
        case ConversationMode.PLAN:
            return [
                read_only_toolset(workspace_of),
                cast(AbstractToolset[DepsT], _PLAN_OPTIONS_TOOLSET),
            ]
        case ConversationMode.WRITE:
            # Write runs on `build_agent` (its six tools are decorator-registered); the
            # registry adds nothing until U12 routes structured reads at the live
            # workspace. Empty is the honest answer, not an omission.
            return []
