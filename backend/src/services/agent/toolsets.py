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

`present_plan_options` is registered here as the SEAM (name + tiny shape, no plan
payload); its turn-engine mechanics — detect-and-force-on-retry, the user's click stored
as the tool RESULT, the snapshot-SHA stamp — are U11's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic_ai import RunContext
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.function import FunctionToolset

from src.db.models.conversation import ConversationMode
from src.services.agent.read_tools import ReadOnlyWorkspace, read_only_toolset


@dataclass
class ReadDeps:
    """Per-run deps for Ask/Plan turns (U10 constructs one per turn). `workspace` is the
    turn-pinned read surface (snapshot extraction, or the live workspace when a Write
    sandbox is attached); `user_id` scopes everything downstream (ADR-0004)."""

    workspace: ReadOnlyWorkspace
    user_id: uuid.UUID


def _workspace_of(ctx: RunContext[ReadDeps]) -> ReadOnlyWorkspace:
    return ctx.deps.workspace


async def present_plan_options(ctx: RunContext[ReadDeps]) -> str:
    """Show the user the plan confirmation buttons (Build it / Keep refining). Call this
    when the plan feels ready — after it, wait for the user's choice; do not keep
    writing. Calling it again after revising the plan presents fresh options."""
    # U11 re-homes this result: the turn engine detects the call and the USER'S CLICK
    # becomes the stored tool result (refine / build / build_failed). This stub return is
    # the pre-U11 seam so the tool exists structurally in Plan mode.
    return "The options are in front of the user. Wait for their choice before continuing."


def _plan_options_toolset() -> FunctionToolset[ReadDeps]:
    return FunctionToolset[ReadDeps]([present_plan_options], id="plan-options")


def toolsets_for_mode(mode: ConversationMode) -> list[AbstractToolset[ReadDeps]]:
    """The per-run toolsets for a mode-gated Ask/Plan run. Exhaustive over the enum
    (fail-first: an unknown mode is a programming error, not a fallback)."""
    match mode:
        case ConversationMode.ASK:
            return [read_only_toolset(_workspace_of)]
        case ConversationMode.PLAN:
            return [read_only_toolset(_workspace_of), _plan_options_toolset()]
        case ConversationMode.WRITE:
            # Write runs on `build_agent` (its six tools are decorator-registered); the
            # registry adds nothing until U12 routes structured reads at the live
            # workspace. Empty is the honest answer, not an omission.
            return []
