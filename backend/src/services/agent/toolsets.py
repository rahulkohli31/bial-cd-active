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

Write additionally gets `fetch_output_slice` (U22/R28) and `apply_schema_change` (U23/R29), and
both reach Write the ONLY way they could: registered on `sandbox_toolset`, beside the
`run_command` whose truncation notice hands out the slice handles and whose two-step migration
sequence the composite replaces. Putting either on `read_only_toolset` would have been the silent
failure this file's allowlist is designed to produce — `_WRITE_STRUCTURED_READS` names two tools
and nothing else, so a tool added there would be filtered out of the one mode that runs commands,
with no test going red. `test_toolsets.py` asserts their membership against `toolsets_for_mode`
directly.

WRITE's surface is COMPOSED here, from two factories: the eight sandbox tools
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

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from pydantic_ai import RunContext
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.function import FunctionToolset
from pydantic_ai.usage import RunUsage

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
"""The ONLY read-only tools Write may borrow: the two structured reads the sandbox eight do
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


# --- U20 / R26: the prompt's TOOL SURFACE block is GENERATED, never hand-written ---------
#
# WHY. Prose drifts in two directions and a name list only catches one of them. The
# hand-written block named SIX tools while the Write arm registered eight (`list_files` and
# `search_files` were simply missing), and — the class a name comparison is blind to — U18
# changed what `declare_done` DOES while the sentence describing it still promised a
# follow-up round-trip. Rendering the block from the registry closes both: a tool the mode
# does not register cannot be named, one it does register cannot be missed, and no line can
# describe a tool differently from how the model is told it behaves, because the line and
# the tool schema are the same string.
#
# WHERE IT LANDS. `core/prompt_blocks.py` is a LEAF module by construction (see its
# docstring): a `services.*` import from there closes a real cycle, and this module is on
# it — toolsets → orchestrator.tools → orchestrator.sql_guard → core.prompt_blocks. So the
# prompt carries a checked-in SNAPSHOT of this renderer's output (`WRITE_TOOL_SURFACE`) and
# `tests/services/orchestrator/test_prompt.py` goes red the moment the two disagree.
# Regenerate the snapshot with:
#
#   uv run python -c "import asyncio;from src.db.models.conversation import ConversationMode\
# ;from src.services.agent.toolsets import render_tool_surface as r\
# ;print(asyncio.run(r(ConversationMode.WRITE)))"
#
# THE FIRST SENTENCE, NOT THE WHOLE DOCSTRING — the one decision this unit left to
# implementation. pydantic-ai already sends every description IN FULL on the tool schema of
# every request, so rendering the whole docstring here would put each one in front of the
# model TWICE per turn: ~350 extra words on the request that this plan is otherwise spending
# units trimming. The first sentence is a roll-call — "these are the tools you have, this is
# what each is for" — and the registration carries the detail. Both are slices of the one
# string, so the two can restate each other but can never contradict each other, which is
# the property R26 actually asks for.


def _the_renderer_never_calls_a_model(
    _messages: list[ModelMessage], _info: AgentInfo
) -> ModelResponse:
    raise AssertionError("the tool-surface renderer enumerates registrations; it runs nothing")


_RENDER_ONLY_MODEL: Final = FunctionModel(_the_renderer_never_calls_a_model)
"""`RunContext` requires a model; `get_tools` never reads it. A model that raises if it is
ever asked for a response keeps that fact honest rather than parking a live client here."""


def _the_renderer_never_calls_a_tool(_ctx: RunContext[Any]) -> Any:
    raise AssertionError("the tool-surface renderer reads tool definitions; it calls no tool")


_SENTENCE_END: Final = re.compile(r"\.(?=\s|$)")
"""A period that ends a sentence: one followed by whitespace or the end of the text. Linear,
unbounded-quantifier-free (the ReDoS constraint every regex in this repo holds to)."""

_ABBREVIATIONS: Final = ("e.g.", "i.e.", "etc.", "vs.")
"""Periods that end a WORD, not a sentence. `read_tools.run_command`'s description opens
`… pass argv tokens, e.g. …` and would otherwise be cut off mid-example."""


def first_sentence(description: str) -> str:
    """The first sentence of a tool description, with its docstring line breaks flattened."""
    flattened = " ".join(description.split())
    for match in _SENTENCE_END.finditer(flattened):
        candidate = flattened[: match.end()]
        if candidate.endswith(_ABBREVIATIONS):
            continue
        return candidate
    return flattened


async def registered_tool_definitions(mode: ConversationMode) -> dict[str, ToolDefinition]:
    """Exactly what `mode` registers, in registration order, as pydantic-ai hands it to the
    model — names AND descriptions, straight off `toolsets_for_mode`.

    The accessors are the ones that raise: resolving a workspace or a sandbox is what a tool
    CALL needs, and nothing here calls a tool. That is deliberate rather than convenient — a
    renderer that needed a live sandbox to describe the surface could not run in a test, and
    a drift check that cannot run is not a check."""
    sandbox_of = _the_renderer_never_calls_a_tool if mode is ConversationMode.WRITE else None
    ctx: RunContext[Any] = RunContext(deps=None, model=_RENDER_ONLY_MODEL, usage=RunUsage())
    definitions: dict[str, ToolDefinition] = {}
    for toolset in toolsets_for_mode(mode, _the_renderer_never_calls_a_tool, sandbox_of):
        for name, tool in (await toolset.get_tools(ctx)).items():
            definitions[name] = tool.tool_def
    return definitions


async def render_tool_surface(mode: ConversationMode) -> str:
    """The prompt's TOOL SURFACE block for `mode`, generated from the tools it registers."""
    lines = ["TOOL SURFACE:"]
    for name, definition in (await registered_tool_definitions(mode)).items():
        if not definition.description:
            raise ValueError(
                f"`{name}` is registered with no description, so the prompt has nothing "
                "truthful to say about it. Give the tool a docstring — it is prompt copy."
            )
        lines.append(f"- `{name}` — {first_sentence(definition.description)}")
    return "\n".join(lines)
