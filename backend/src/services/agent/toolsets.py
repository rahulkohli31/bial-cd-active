# `present_plan_options` and `check_the_app` below are registered tools: their docstrings are
# sent to the model verbatim as those tools' descriptions. Edit them as prompt text, not as
# internal notes.
"""The chat-kind → toolset registry: tool gating AT THE SERVER.

WHY THIS EXISTS. The registry keys on the server-owned `conversation.kind` — NEVER anything
the client sends. Gating is structural, not prompt-based: the single `chat_agent` is built with
NO tools, and each run passes exactly its kind's toolsets (pydantic-ai toolsets are additive).
A wrong-kind tool is therefore absent from the model's tool list AND uncallable — a forged call
gets the runtime's unknown-tool rejection, never a policy check that could be bypassed.

| Kind    | reads                | run_command       | writes | present_plan_options |
|---------|----------------------|-------------------|--------|----------------------|
| Plan    | yes (live workspace) | allowlisted, read | —      | yes                  |
| Build   | yes (live workspace) | full (+SQL guard) | yes    | —                    |
| Generic | —                    | —                 | —      | —                    |

Plan and Build also carry `CONVERSATION_TOOLSET` and `app_state_toolset` (each registered once,
so the two lists can't drift); a Generic run is handed no toolset at all.
`toolsets_for_kind` is the ONLY place permitted to read the chat kind to decide capability —
see its own docstring. Two more things live here, not the registry: the citizen-facing chat-kind
CATALOGUE (served on `GET /v1/auth/me`) and the registry read the gating guards ask their
questions through — each has its own banner below, kept beside the registry so a change to one
puts the other under your cursor.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
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

from src.core.connectors import ConnectedSystem
from src.db.models.conversation import ChatKind
from src.services.agent.attachment_tools import AttachmentReader, attachment_toolset
from src.services.agent.connector_tools import CONNECTOR_TOOLSET
from src.services.agent.conversation_tools import CONVERSATION_TOOLSET
from src.services.agent.read_tools import ReadOnlyWorkspace, read_only_toolset
from src.services.orchestrator.constants import APP_CHECK_MAX_POLLS, READINESS_POLL_S
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.selfheal import AppState, read_the_app_state
from src.services.orchestrator.tools import sandbox_toolset


@dataclass
class ReadDeps:
    """Minimal per-run deps for a Plan-kind agent-level run (the agent-level test surface).
    `workspace` is the turn-pinned read surface (the live workspace); `user_id` scopes
    everything downstream."""

    workspace: ReadOnlyWorkspace
    user_id: uuid.UUID


def workspace_from_read_deps(ctx: RunContext[ReadDeps]) -> ReadOnlyWorkspace:
    """The `ReadDeps` accessor (agent-level tests; the turn engine supplies its own for
    `ChatDeps`)."""
    return ctx.deps.workspace


@dataclass(frozen=True)
class ToolSurface[DepsT]:
    """Everything one run of a given kind is allowed to do: the toolsets it's handed, and
    whether any of them can change the app.

    `may_write` rides WITH the toolsets rather than being re-derived — it was previously
    recomputed at the sandbox door by re-reading the enum, a second copy that "convention" kept
    honest. Now the one function that decides what a run can reach also answers this, so a
    kind's surface can't drift from its write flag."""

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


# --- What the app is doing, ASKED FOR rather than pushed ------------------------------------
#
# A PULL, AND THE POSITION IS THE REASON. A tool result lands at the absolute tail of the run's
# own messages and `_persistable_messages` keeps it, so what the model was sent is what the store
# replays. Anything the platform PUSHES instead lands ahead of the citizen's persisted prompt,
# where its bytes are gone next turn and every message after them shifts.
#
# THE SENTENCES ARE THE VERDICT, NOT THE SIGNALS. `read_the_app_state` applies the ordering rule
# (could-not-tell > not-serving > the page) and the tool hands over the sentence for the answer
# it reached. Nothing here invites the model to re-derive a diagnostic: the harness has already
# offered the agent a `tsc` it could run for itself and withdrawn it, because the model spent
# 20-40 s and a context window per turn establishing what the platform already knew.

_APP_STATE_SENTENCES: Final[dict[AppState, str]] = {
    AppState.UNKNOWN: (
        "The platform could not tell what state this app is in this time. If the user says "
        "something is wrong, look at the app's files and check for yourself rather than "
        "assuming it still works."
    ),
    AppState.NOT_SERVING: (
        "The app is not currently serving. Something it needs at startup is most likely "
        "failing, so treat any question about what the app does today as a question about a "
        "broken app."
    ),
    AppState.STILL_THE_TEMPLATE: (
        "The app is serving, and its home page is still byte-for-byte the starter template the "
        "workspace was created with — nothing the user asked for is on the page they actually "
        "look at. Whatever else exists in the files, the app they see has not been built yet."
    ),
    AppState.LIVE: "The app is serving, and its home page is no longer the starter template.",
}


def _no_pinned_sandbox(_ctx: RunContext[Any]) -> SandboxSession | None:
    """The default app-state accessor: this run has no container.

    A real answer rather than a raise. A Plan turn can run before any workspace exists, and the
    agent-level surfaces (`ReadDeps`) have no container at all — so `check_the_app` is registered
    on every run and answers `unknown` where there is nothing to ask."""
    return None


def app_state_toolset[DepsT](
    sandbox_of: Callable[[RunContext[DepsT]], SandboxSession | None],
    *,
    noticed: Callable[[AppState], None] | None = None,
) -> FunctionToolset[DepsT]:
    """`check_the_app`, over whatever deps `sandbox_of` resolves the container from.

    ONE PROBE PER UNCHANGED TREE, MEMOIZED IN THE CLOSURE. Repeated calls that the model makes
    without editing anything in between return the first answer with no container round-trip —
    the same ceiling `connector_schema` puts on re-delivering its artefact, for the same reason:
    without it N calls become N readiness polls plus N execs.

    THE MEMO IS KEYED ON `session.writes`, NOT ON THE RUN, and that is what keeps the tool's
    promise honest. A Build run holds the mutating tools and this one together for up to
    `MODEL_TURN_CEILING` requests, so "memoize for the whole run" means a model that reads
    NOT_SERVING, fixes the bug, and checks again is handed its own pre-fix answer under a
    docstring that says "right now" — and then tells the citizen so in prose the platform streams
    live and never gates. Any write invalidates the reading; a run that never writes still pays
    for exactly one probe.

    `noticed` IS HANDED THE READING AS THE TOOL ANSWERS, so the turn can act on the same fact the
    model was given rather than re-deriving it from the sentence that carried it. It fires on
    every call including the memoized one: a second call in the same run is still a turn in which
    the model consulted the platform, which is the fact the detection counters count.

    The inner tool annotates `RunContext[Any]` rather than the enclosing PEP-695 type param, for
    the reason `sandbox_toolset` states: pydantic-ai resolves tool annotations with
    `get_type_hints` at registration, where that param is out of scope under deferred
    annotations. The factory signature carries the real typing; the `cast` at the return narrows
    back to it."""
    memo: list[tuple[int, AppState]] = []

    async def check_the_app(ctx: RunContext[Any]) -> str:
        """Call this before you say anything about what the app does now, and whenever the user
        tells you something is wrong.

        It tells you what this app is doing right now — whether it is serving, and whether the
        page the user actually looks at is still the starter template. Earlier messages in this
        conversation describe how the app WAS; this is how it is. The platform runs the check and
        hands you its answer, so you do not need to run a type-check or start a server to find
        out.
        """
        session = sandbox_of(ctx)
        # No session means no container to read and no writes to invalidate against, so the
        # one UNKNOWN it produces is cached under a count that can never move.
        writes = session.writes if session is not None else 0
        if not memo or memo[0][0] != writes:
            memo[:] = [
                (
                    writes,
                    AppState.UNKNOWN
                    if session is None
                    else await read_the_app_state(
                        session.sandbox_client,
                        session.handle,
                        max_polls=APP_CHECK_MAX_POLLS,
                        poll_s=READINESS_POLL_S,
                    ),
                )
            ]
        if noticed is not None:
            noticed(memo[0][1])
        return _APP_STATE_SENTENCES[memo[0][1]]

    return cast(FunctionToolset[DepsT], FunctionToolset[Any]([check_the_app], id="app-state"))


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
    """ALLOWLIST, not a denylist: a tool added to `read_only_toolset` later stays out of Write
    until it is named in `_WRITE_STRUCTURED_READS`.

    Wrong-direction failure is a missing tool, never a shadowed one — and it is SILENT, so a Build-
    only tool belongs on `sandbox_toolset` (where `fetch_output_slice` and `apply_schema_change`
    are), not here. `test_fetch_output_slice_reaches_the_only_kind_that_runs_commands` is what pins
    that."""
    return tool_def.name in _WRITE_STRUCTURED_READS


def _plus_connected_data[DepsT](
    toolsets: list[AbstractToolset[DepsT]], connected_systems: Sequence[ConnectedSystem]
) -> list[AbstractToolset[DepsT]]:
    """Append the connected-data surface when this project actually reads one, on either arm.

    ONE FUNCTION SO THE TWO ARMS CANNOT DIFFER. The rule is the same for Plan and Build — R4 says
    both arms, and the second axis this registry now gates on is the PROJECT, not the kind — so it
    is written once and both `case` arms call it."""
    if not connected_systems:
        return toolsets
    return [*toolsets, cast(AbstractToolset[DepsT], CONNECTOR_TOOLSET)]


def toolsets_for_kind[DepsT](
    kind: ChatKind,
    workspace_of: Callable[[RunContext[DepsT]], ReadOnlyWorkspace],
    sandbox_of: Callable[[RunContext[DepsT]], SandboxSession] | None = None,
    reader_of: Callable[[RunContext[DepsT]], AttachmentReader] | None = None,
    *,
    connected_systems: Sequence[ConnectedSystem] = (),
    app_state_of: Callable[[RunContext[DepsT]], SandboxSession | None] = _no_pinned_sandbox,
    app_state_noticed: Callable[[AppState], None] | None = None,
) -> ToolSurface[DepsT]:
    """The per-run tool surface for a chat kind, over whatever deps type the caller's accessors
    resolve the workspace (and, for Build, the sandbox) from.

    THIS MATCH IS THE GUARDRAIL — the only place permitted to read the chat kind to decide what
    the model can do. Plan cannot change the app because the write tools and `run_command` are
    simply absent from its list, never because something downstream notices the kind. Exhaustive
    over the enum: an unknown kind is a programming error, not a fallback.

    AND IT NOW GATES ON A SECOND AXIS: THE PROJECT. `connected_systems` is what this project may
    actually read from outside the platform, resolved once at the router off
    `resolve_window(...).effectively_on` — the project's switch AND the owner's approval. Empty is
    the ordinary case and adds nothing, so a project with no connector pays nothing for this
    feature on any turn.

    THE GATE IS REGISTRATION, NOT REFUSAL, and that is the whole reason it lives here rather than
    inside the tool. The platform already has a flow whose purpose is to say no to a project — an
    administrator declining the request, or a revocation flipping `effectively_on` back — and a
    surface that ignored it would leave the model to discover the refusal by spending a round
    trip. Absent instead: a forged call meets the runtime's unknown-tool rejection, exactly as a
    Build tool does in a Plan chat. Toolsets are built per run, so a revoked approval takes the
    tool away on the citizen's next turn with no invalidation step anywhere.

    `app_state_of` IS THE ONE ACCESSOR THAT MAY ANSWER `None`, and `check_the_app` is registered
    on BOTH arms off it. Plan has no other route to the answer — it cannot run a command that
    starts a server — and a run with no container still gets the tool, answering `unknown`.
    `app_state_noticed` rides beside it so the caller hears what the tool answered."""
    match kind:
        case ChatKind.PLAN:
            plan_toolsets: list[AbstractToolset[DepsT]] = [
                read_only_toolset(workspace_of),
                cast(AbstractToolset[DepsT], CONVERSATION_TOOLSET),
                cast(AbstractToolset[DepsT], _PLAN_OPTIONS_TOOLSET),
                app_state_toolset(app_state_of, noticed=app_state_noticed),
            ]
            # THE ATTACHMENT CAPABILITY, ON THIS ARM ALONE. Plan already executes in
            # the container, but only the eight read-only binaries on `check_the_guest_list` —
            # `python3` is deliberately absent, so it cannot invoke the shipped reader the way
            # Build does. Widening that list is not available: it is shared with the reviewer
            # agent over untrusted contents and takes argv and nothing else, precisely so no body
            # below can ask which agent is calling. Registering the capability HERE is the
            # difference the architecture already sanctions — this function is the one place
            # permitted to read the chat kind.
            #
            # Optional so the U8 agent-level surface, which has no sandbox at all, still builds a
            # Plan run; a caller with no reader simply does not offer the tool.
            if reader_of is not None:
                plan_toolsets.append(attachment_toolset(reader_of))
            return ToolSurface(
                toolsets=_plus_connected_data(plan_toolsets, connected_systems),
                may_write=False,
            )
        case ChatKind.BUILD:
            if sandbox_of is None:
                raise ValueError(
                    "a Build run needs a sandbox accessor; None means this caller cannot "
                    "run Build (the agent-level ReadDeps surface)."
                )
            # `.filtered()` filters at `get_tools` time, so the model never even sees the
            # read-only `read_file`/`run_command` — the duplicate-name `UserError` is
            # structurally unreachable rather than merely avoided by convention.
            return ToolSurface(
                toolsets=_plus_connected_data(
                    [
                        sandbox_toolset(sandbox_of),
                        read_only_toolset(workspace_of).filtered(_structured_reads_only),
                        cast(AbstractToolset[DepsT], CONVERSATION_TOOLSET),
                        app_state_toolset(app_state_of, noticed=app_state_noticed),
                    ],
                    connected_systems,
                ),
                may_write=True,
            )
        case ChatKind.GENERIC:
            # NO TOOLSET AT ALL, and no accessor is touched to build it — not the workspace, not
            # the sandbox, not the reader. That is what makes this arm reachable from a caller
            # holding none of them, where the Build arm above deliberately raises. A generic run
            # answers from its transcript and its attachments; there is nothing for it to call.
            return ToolSurface(toolsets=[], may_write=False)


# --- One catalogue of what the kinds ARE, beside the registry of what they -------------
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
        case ChatKind.GENERIC:
            return ChatKindDescription(
                value=kind.value,
                name="BIAL Chat",
                description=(
                    "Ask a question, or attach a document or a picture and ask about it. This "
                    "chat is yours rather than an app's, so nothing you do here changes an app."
                ),
            )


CHAT_KIND_CATALOGUE: Final[tuple[ChatKindDescription, ...]] = tuple(
    _describe(kind) for kind in ChatKind
)
"""Every chat kind, described once, in enum declaration order. Served verbatim on
`GET /v1/auth/me` (`api/v1/auth/router.py`) — the once-cached bootstrap the portal already
fetches before first paint — and read on the client by the single module `chatKind.ts` reads
from. No second endpoint, no second wording."""


# --- Reading the registry back, without running any of it ----------------------------------
#
# The gating rules above are only as good as something that can ask what a kind actually
# registers: that Plan is offered no write tool, that a project with no approved connector is
# offered no connector tool. Those guards read this, and they have to be able to run with no
# workspace, no sandbox and no model — so every accessor here raises if it is ever called.


def _reading_the_registry_never_calls_a_model(
    _messages: list[ModelMessage], _info: AgentInfo
) -> ModelResponse:
    raise AssertionError("reading the registry enumerates registrations; it runs nothing")


_REGISTRY_ONLY_MODEL: Final = FunctionModel(_reading_the_registry_never_calls_a_model)
"""`RunContext` requires a model; `get_tools` never reads it. A model that raises if it is
ever asked for a response keeps that fact honest rather than parking a live client here."""


def _reading_the_registry_never_calls_a_tool(_ctx: RunContext[Any]) -> Any:
    raise AssertionError("reading the registry reads tool definitions; it calls no tool")


async def registered_tool_definitions(
    kind: ChatKind, *, connected_systems: Sequence[ConnectedSystem] = ()
) -> dict[str, ToolDefinition]:
    """Exactly what `kind` registers, in registration order, as pydantic-ai hands it to the
    model — names AND descriptions, straight off `toolsets_for_kind`.

    The accessors are the ones that raise: resolving a workspace or a sandbox is what a tool
    CALL needs, and nothing here calls a tool. That is deliberate rather than convenient — a
    reader that needed a live sandbox to describe the surface could not run in a test, and a
    gating guard that cannot run is not a guard.

    `connected_systems` MIRRORS `toolsets_for_kind`'s, DEFAULT AND ALL. It has to: without it the
    connector-on registration is not expressible from a test, and a different default here would
    let the guard pass over a surface no ordinary project is ever given."""
    sandbox_of = _reading_the_registry_never_calls_a_tool if kind is ChatKind.BUILD else None
    ctx: RunContext[Any] = RunContext(deps=None, model=_REGISTRY_ONLY_MODEL, usage=RunUsage())
    definitions: dict[str, ToolDefinition] = {}
    surface = toolsets_for_kind(
        kind,
        _reading_the_registry_never_calls_a_tool,
        sandbox_of,
        connected_systems=connected_systems,
    )
    for toolset in surface.toolsets:
        for name, tool in (await toolset.get_tools(ctx)).items():
            definitions[name] = tool.tool_def
    return definitions
