"""The chat-kind → toolset registry (U8 / R6 / D1): tool gating AT THE SERVER.

The registry keys on the server-owned `conversation.kind` — never anything the client
sends. Structural gating, not prompt gating: the single `chat_agent` is constructed with NO
tools, and every run passes exactly its kind's toolsets — Plan through `chat_agent.run(...,
toolsets=...)`, Build through `chat_agent.iter(..., toolsets=...)` (pydantic-ai 2.5.0:
per-run toolsets are ADDITIVE, so a tool-less agent plus a kind's list IS that kind's whole
surface). A wrong-kind tool is absent from the model's tool list AND uncallable — a forged
call gets the runtime's unknown-tool rejection.

The kind → tool matrix:

| Kind  | read/list/search tools | run_command       | write tools | present_plan_options |
|-------|------------------------|-------------------|-------------|----------------------|
| Plan  | yes (live workspace)   | allowlisted, read | —           | yes                  |
| Build | yes (live workspace)   | full (+SQL guard) | yes         | —                    |

BOTH arms additionally carry `CONVERSATION_TOOLSET` — the tools that are about the person
waiting rather than about what the run can do (`agent/conversation_tools.py`). It is the one
place this file registers the SAME object on both arms, and that is the point: a tool whose
presence does not depend on the kind must not be listed twice, or the two lists drift.

Two arms, because there are two kinds. The third row this table used to carry ("Ask") had no
arm in the code by the time anyone read it, and the two that remained were named for modes
that no longer exist — which is the failure this docstring is meant to prevent, committed by
the docstring itself.

Build additionally gets `fetch_output_slice` (U22/R28) and `apply_schema_change` (U23/R29), and
both reach Build the ONLY way they could: registered on `sandbox_toolset`, beside the
`run_command` whose truncation notice hands out the slice handles and whose two-step migration
sequence the composite replaces. Putting either on `read_only_toolset` would have been the silent
failure this file's allowlist is designed to produce — `_WRITE_STRUCTURED_READS` names two tools
and nothing else, so a tool added there would be filtered out of the one kind that runs commands,
with no test going red. `test_toolsets.py` asserts their membership through
`registered_tool_definitions(ChatKind.BUILD)` — see
`test_fetch_output_slice_reaches_the_only_kind_that_runs_commands`.

BUILD's surface is COMPOSED here, from two factories: the eight sandbox tools
(`orchestrator/tools.sandbox_toolset`, resolved through the run's attached
`SandboxSession`) plus exactly `list_files`/`search_files` off `read_only_toolset`. The
read-only side is `.filtered()` down to those two names by an ALLOWLIST — its `read_file`
and `run_command` are dropped, so no name is ever registered twice (duplicate tool names
are a pydantic-ai `UserError`) and the version Build gets is the sandbox-routed one. The
allowlist direction matters: a tool added to `read_only_toolset` later stays OUT of Build
until it is named, so the wrong-direction failure is a missing tool, never a silently
shadowed one (a read-only `run_command` winning would leave Build unable to run anything).

GENERIC over the deps type (U10): the registry itself is deps-agnostic — the caller
supplies the accessors that resolve the run's workspace, and for Build the attached sandbox,
from ITS deps. `ReadDeps` (+ `workspace_from_read_deps`) is the minimal agent-level shape the
U8 tests exercise; the turn engine passes `ChatDeps`-typed accessors for real traffic.

`present_plan_options` is registered here and CARRIES THE PLAN IN ITS ARGUMENT — the seam is
no longer a bare name. The turn-engine mechanics are `turns/plan_options.py`'s: the user's
click, minutes or days later, is stored as this call's RESULT. What used to be described here
— a detect-and-force-on-retry that re-ran the turn, and a snapshot-SHA stamp on the pending
card — is retired; see the notes at both former sites.

Two more things live in this module and are not the registry: the chat-kind CATALOGUE
(U16/R73 — what the two kinds are, served on `GET /v1/auth/me`) and the prompt's TOOL SURFACE
renderer (U20/R26 — what the Build prompt is allowed to say about its tools). Each has its own
banner below; both are here because they only stay honest with the registry if changing one
puts the other under your cursor.
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

from src.db.models.conversation import ChatKind
from src.services.agent.conversation_tools import CONVERSATION_TOOLSET
from src.services.agent.read_tools import ReadOnlyWorkspace, read_only_toolset
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.tools import sandbox_toolset

PLAN_OPTIONS_TOOL = "present_plan_options"
"""The Plan-mode confirmation tool's wire name — U11's detect-and-force logic and the
projection's resolution-state derivation both key on it."""


@dataclass
class ReadDeps:
    """Minimal per-run deps for a Plan-kind agent-level run (the U8 test surface).
    `workspace` is the turn-pinned read surface (the live workspace); `user_id` scopes
    everything downstream (ADR-0004)."""

    workspace: ReadOnlyWorkspace
    user_id: uuid.UUID


def workspace_from_read_deps(ctx: RunContext[ReadDeps]) -> ReadOnlyWorkspace:
    """The `ReadDeps` accessor (agent-level tests; U10 supplies its own for `ChatDeps`)."""
    return ctx.deps.workspace


@dataclass(frozen=True)
class ToolSurface[DepsT]:
    """Everything one run of a given kind is allowed to do — the toolsets it is handed, and
    whether any of them can change the app.

    `may_write` RIDES WITH THE TOOLSETS RATHER THAN BEING RE-DERIVED, and that is the whole
    point of returning a pair. It was previously computed a second time at the sandbox door
    (`turns/engine.py`) by re-reading the enum, and the session manager's own docstring already
    described it as "coming from the toolset" — a claim only convention kept true. Now the one
    function that decides what a run can reach is the one that answers the question, so a kind
    whose surface changes cannot leave the sandbox's write flag saying something else."""

    toolsets: list[AbstractToolset[DepsT]]
    may_write: bool


async def present_plan_options(ctx: RunContext[Any], plan: str) -> str:
    """Show the user your plan with the Build this plan / Keep planning buttons beneath it.
    Pass the whole plan as `plan` — that text is what the user reads and what a build works
    from, so it has to stand on its own. Call this when the plan is ready; it ends your turn,
    and the user's choice arrives as the result when they decide. Call it again, with the
    revised plan, after they ask for changes."""
    # THE LABELS ABOVE ARE LITERALS AND A TEST HOLDS THEM TO `prompt_blocks`. They cannot be
    # interpolated: an f-string in this position is an expression, not a docstring, so
    # `__doc__` would be None — and a registered tool with no description is what the model
    # would then be handed. `test_no_prompt_surface_names_a_button_the_interface_does_not_draw`
    # is where the single source is actually enforced, and it checks tool DESCRIPTIONS as well
    # as composed prompts: this docstring reaches the model on the tool schema and appears in
    # no prompt string, which is exactly how it survived the last relabelling untouched.
    # THE PLAN RIDES THE ARGUMENT, and the docstring above is what the model actually reads,
    # so it is the contract rather than a description of one. Free text beside a tool call no
    # longer reaches the user, so a plan announced in the same breath as the offer would
    # simply disappear — and putting it in the argument closes two defects structurally
    # instead of by a check somebody has to remember: an offer with no plan (there is nothing
    # to pass) and an offer over a half-written one (the argument is complete or the call did
    # not happen). It also ends the question of WHICH text the plan was, which is what the
    # retired prose heuristic existed to guess.
    #
    # The call still DEFERS — the run ends with it unanswered (pydantic-ai
    # `DeferredToolRequests` output), because the answer is the USER'S CLICK, minutes or days
    # later. The stored resolution (refine / build) is written by
    # `services/turns/plan_options.py` and rides the next run's history as this call's return.
    # Deps-agnostic on purpose: the tool's meaning lives in the engine's handling of the CALL,
    # not here.
    raise CallDeferred


_PLAN_OPTIONS_TOOLSET: FunctionToolset[Any] = FunctionToolset[Any](
    [present_plan_options], id="plan-options"
)


# THERE IS NO OPTIONS-ONLY TOOLSET. It existed for exactly one caller: the forced retry that
# re-issued a Plan run with `present_plan_options` as the only tool the model could reach, after
# a prose heuristic decided a plan had been written. Both are gone (see the note in
# `turns/engine.py` where the heuristic was defined), and a toolset with no caller is a second
# surface waiting to be handed to a run nobody has thought about.


_WRITE_STRUCTURED_READS: Final = frozenset({"list_files", "search_files"})
"""The ONLY read-only tools Write may borrow: the two structured reads the sandbox eight do
not have. `read_file` and `run_command` exist on both sides, and Write must get the
sandbox-routed ones."""


def _structured_reads_only(_ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
    """ALLOWLIST, not a denylist: a tool added to `read_only_toolset` later stays out of
    Write until it is named in `_WRITE_STRUCTURED_READS`. Wrong-direction failure is a
    missing tool, never a shadowed one."""
    return tool_def.name in _WRITE_STRUCTURED_READS


def toolsets_for_kind[DepsT](
    kind: ChatKind,
    workspace_of: Callable[[RunContext[DepsT]], ReadOnlyWorkspace],
    sandbox_of: Callable[[RunContext[DepsT]], SandboxSession] | None = None,
) -> ToolSurface[DepsT]:
    """The per-run tool surface for a chat kind, over whatever deps type the caller's
    accessors resolve the workspace (and, for Build, the attached sandbox) from.

    THIS MATCH IS THE GUARDRAIL, and this module is the only one permitted to read the chat
    kind in order to decide what the model can do. A Plan chat cannot change the app because
    `write_file`, `edit_file`, `insert_lines`, `apply_schema_change`, the sandbox-routed
    `run_command` and `declare_done` are not in the list handed to that run — never because
    something downstream notices which kind of chat it is. Exhaustive over the enum
    (fail-first: an unknown kind is a programming error, not a fallback)."""
    match kind:
        case ChatKind.PLAN:
            return ToolSurface(
                toolsets=[
                    read_only_toolset(workspace_of),
                    cast(AbstractToolset[DepsT], CONVERSATION_TOOLSET),
                    cast(AbstractToolset[DepsT], _PLAN_OPTIONS_TOOLSET),
                ],
                may_write=False,
            )
        case ChatKind.BUILD:
            if sandbox_of is None:
                raise ValueError(
                    "a Build run needs a sandbox accessor; None means this caller cannot "
                    "run Build (the U8 agent-level ReadDeps surface)."
                )
            # `.filtered()` filters at `get_tools` time, so the model never even sees the
            # read-only `read_file`/`run_command` — the duplicate-name `UserError` is
            # structurally unreachable rather than merely avoided by convention.
            return ToolSurface(
                toolsets=[
                    sandbox_toolset(sandbox_of),
                    read_only_toolset(workspace_of).filtered(_structured_reads_only),
                    cast(AbstractToolset[DepsT], CONVERSATION_TOOLSET),
                ],
                may_write=True,
            )


# --- U16 / R73: one catalogue of what the two kinds ARE, beside the registry of what they --
# --- CAN DO ---------------------------------------------------------------------------------
#
# WHY IT LIVES HERE, NEXT TO `toolsets_for_kind`, RATHER THAN IN THE API SCHEMA IT IS SERVED
# THROUGH. A chat kind's ABILITIES and its DESCRIPTION are two views of the same fact, and
# they only stay honest with each other if changing one puts the other under your cursor. Had
# this lived beside the auth router instead, a change to what Plan may do (the match arm
# above) and a change to what the product SAYS Plan does (a docstring in a different file,
# reached through a different route module) could drift for a release before anyone read them
# side by side.
#
# WHAT IT IS NOT. This is not the model-facing prompt text — that lives in
# `services/agent/mode_prompts.py`, is read by the model, and is owned by a different unit
# (it is deliberately outside `test_toolsets.py`'s copy-drift guard). This catalogue is
# CITIZEN-facing: it is the only place under `backend/src/` allowed to say, in plain words a
# BIAL user would recognise, what a Plan chat or a Build chat is for.


@dataclass(frozen=True)
class ChatKindDescription:
    """One entry in the catalogue: a chat kind's wire value, its display name, and the one
    line a citizen reads about what it does for them — never what the agent is, never a tool,
    sandbox, mode or file name. `value` is `ChatKind`'s own `.value`, so a client keys its
    lookup on exactly the string every other endpoint already sends for `kind`."""

    value: str
    name: str
    description: str


def _describe(kind: ChatKind) -> ChatKindDescription:
    """The catalogue entry for `kind`. EXHAUSTIVE OVER THE ENUM THE SAME WAY
    `toolsets_for_kind` IS: no wildcard case, so a third kind added without wording here is a
    type-checker error at this function rather than a blank label reaching a browser.
    `test_toolsets.py` also walks `ChatKind` at runtime, so the guard holds even for whoever
    is not running `pyright`."""
    match kind:
        case ChatKind.PLAN:
            return ChatKindDescription(
                value=kind.value,
                name="Plan",
                description=(
                    "Talk through what you want and shape it into a plan, without changing "
                    "your app yet. When the plan looks right, turn it into a build."
                ),
            )
        case ChatKind.BUILD:
            return ChatKindDescription(
                value=kind.value,
                name="Build",
                description=(
                    "Ask for changes and watch your app update as you go. This is where your "
                    "live app actually changes."
                ),
            )


CHAT_KIND_CATALOGUE: Final[tuple[ChatKindDescription, ...]] = tuple(
    _describe(kind) for kind in ChatKind
)
"""Every chat kind, described once, in enum declaration order. Served verbatim on
`GET /v1/auth/me` (`api/v1/auth/router.py`) — the once-cached bootstrap the portal already
fetches before first paint — and read on the client by the single module `chatKind.ts` reads
from. No second endpoint, no second wording."""


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
#   uv run python -c "import asyncio;from src.db.models.conversation import ChatKind\
# ;from src.services.agent.toolsets import render_tool_surface as r\
# ;print(asyncio.run(r(ChatKind.BUILD)))"
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


async def registered_tool_definitions(kind: ChatKind) -> dict[str, ToolDefinition]:
    """Exactly what `kind` registers, in registration order, as pydantic-ai hands it to the
    model — names AND descriptions, straight off `toolsets_for_kind`.

    The accessors are the ones that raise: resolving a workspace or a sandbox is what a tool
    CALL needs, and nothing here calls a tool. That is deliberate rather than convenient — a
    renderer that needed a live sandbox to describe the surface could not run in a test, and
    a drift check that cannot run is not a check."""
    sandbox_of = _the_renderer_never_calls_a_tool if kind is ChatKind.BUILD else None
    ctx: RunContext[Any] = RunContext(deps=None, model=_RENDER_ONLY_MODEL, usage=RunUsage())
    definitions: dict[str, ToolDefinition] = {}
    for toolset in toolsets_for_kind(kind, _the_renderer_never_calls_a_tool, sandbox_of).toolsets:
        for name, tool in (await toolset.get_tools(ctx)).items():
            definitions[name] = tool.tool_def
    return definitions


async def render_tool_surface(kind: ChatKind) -> str:
    """The prompt's TOOL SURFACE block for `kind`, generated from the tools it registers."""
    lines = ["TOOL SURFACE:"]
    for name, definition in (await registered_tool_definitions(kind)).items():
        if not definition.description:
            raise ValueError(
                f"`{name}` is registered with no description, so the prompt has nothing "
                "truthful to say about it. Give the tool a docstring — it is prompt copy."
            )
        lines.append(f"- `{name}` — {first_sentence(definition.description)}")
    return "\n".join(lines)
