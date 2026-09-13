# `connector_schema` below is a registered tool: its docstring is sent to the model verbatim as
# that tool's description, on every request. Edit it as prompt text, not as an internal note.
"""The one tool that tells an agent what is in a connected system's data.

WHY IT IS A TOOL AND NOT A PROMPT BLOCK. The answer is ~8,700 tokens. Resident on every turn of
every connector-enabled project it would be the largest thing in the prompt and would be paid for
by turns that never touch the data; fetched, it is paid for once and then replayed at the
cache-read rate, because the library moves a cache breakpoint forward over history as a
conversation grows (and since PR #231 both the Plan arm and the Build arm set that marker).

WHY THERE IS ONE TOOL AND NOT TWO. The design this came from had a second, `connector_column_
values`, for the value lists the block could not afford to carry — at 408 columns those were 835
KB. The 10 September cut to 131 removed the reason. Counted off the shipped artefact: 21 columns
carry their values INLINE, 68 have no vocabulary to state at all, 13 hold more distinct values
than any code list plausibly has (timestamps written as text), and the remaining 29 are airport
codes, aircraft registrations, parking stands and airline names — which a generated app should
read with `SELECT DISTINCT` at runtime rather than be handed 2,652 of. A second tool would have
answered a question the app should ask the data.

  (The plan's own table says 21 / 26 / 84. Those numbers predate the rendering this ships and do
  not add up against it; they are corrected here rather than carried forward, because they are
  the justification for the one-tool cut and a stale justification is how a retired design gets
  re-argued.)

WHY IT READS NO DATABASE. `ChatDeps.db` is a live session on the Plan arm and `None` on the Build
arm — a Build turn runs for minutes and must not pin a pooled connection idle-in-transaction — so
a tool that needed the database would work on one arm and fail on the other. Everything this tool
needs was resolved once at the router and rides `PromptContext.connected_systems`.

WHY REGISTRATION IS THE GATE AND THIS BODY IS ONLY THE BELT. `toolsets_for_kind` appends this
toolset for a project whose connector is effectively on and for no other, so a project whose
administrator refused the connector — or whose approval was revoked — has no tool to call, and a
forged call meets the runtime's unknown-tool rejection rather than a policy check somebody could
find a way past. The refusals below are the belt to that braces: they are unreachable through
registration, and they are asserted rather than assumed.

THE CONNECTOR IS NEVER NAMED HERE. The artefact is `connector_catalogue/<key>.txt`, the key comes
off the registry, and no literal in this module says which system it is (R9). The model's own
argument never reaches the filesystem: it is matched against the registry entries this project
actually has on, and only a MATCHED entry's key is used to build a path.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any, Final

import structlog
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.toolsets.function import FunctionToolset

from src.core.connectors import ConnectedSystem
from src.services.agent.mode_prompts import PromptContext

logger = structlog.get_logger()

_CATALOGUE: Final = Path(__file__).resolve().parent / "connector_catalogue"
"""Beside its only reader, not under `core/`.

An earlier design gave this a package of its own with a loader, a README and a test file. That was
earned while TWO consumers read the artefact — a summary line in the prompt stub and the tools —
and both went: the 11 September ruling deleted the summary line, and the same day's ruling deleted
the second tool. One reader is left, so the loader is four lines inside it. The README went too:
it documented adding a second connector, which a test pins at one, and `COPY src ./src` was
shipping it inside the production image."""

_UNAVAILABLE: Final = (
    "The connected system's schema is not available on this server, so there is nothing "
    "truthful to tell you about its columns. Do not guess column names or invent a table "
    "shape — stop and say the connected data cannot be read right now."
)
"""What the model reads when the artefact is missing or unreadable.

A PLAIN STRING, NOT A `ModelRetry`. Retrying is for what the model can fix by calling again with
different arguments; a file that is not in the image is a packaging break a human must fix, and
inviting a retry would spend the citizen's turn discovering that twice. The instruction it carries
instead is the one that matters: STOP, do not guess. The server-side ERROR beside it is what
actually gets the file back."""

_NO_CONTEXT: Final = (
    "This run has no connected systems resolved for it, so there is no schema to fetch."
)
"""The kindless run (`describe.py` composes its own prompt and passes no `PromptContext`).
Refused rather than raised: a tool that raises on a shape the platform created is a 500 in a
citizen's turn for something the citizen did not do."""

MAX_DELIVERIES_PER_CONVERSATION: Final = 2
"""How many full copies of a schema one conversation is handed before a further call is pointed
back at the copy it already holds.

THE CONVERSATION'S OWN HISTORY IS THE STATE. Every turn reloads the whole conversation and sends it
to the model, and pydantic-ai hands that same list to this tool as `ctx.messages`, so "was it
already delivered?" is a count over what is already in memory: no table, no Redis, nothing to keep
in step. A new conversation starts empty and gets the schema again, which is the case the
description's "every time" is written for.

TWO, NOT ONE — owner ruling, 2026-09-11. Across the connected-data E2E campaign the model never
asked twice in one conversation, so this is a ceiling on a failure not yet seen rather than a fix
for one. Past it, each further call would append another full copy (~8,700 tokens) to a history
every later turn replays."""

_ALREADY_LOADED: Final = (
    "This schema is already in this conversation: earlier `connector_schema` calls returned it in "
    "full, and it has not changed since. Use that earlier result instead of fetching it again."
)
"""What the model reads instead of another copy. A plain string, not a `ModelRetry`: calling again
is exactly what it should not do."""


@cache
def _load(key: str) -> str:
    """The rendered schema block for one connector key, read once and held for the process.

    `@cache` rather than a module-global dict so the "read it once" property is the decorator's
    rather than a hand-rolled guard somebody has to keep correct. A failed read raises and is NOT
    cached, so a file restored under a running process is picked up.

    `key` is a registry key, never a model argument — see the module docstring."""
    return (_CATALOGUE / f"{key}.txt").read_text(encoding="utf-8")


def _match(system: str, on: tuple[ConnectedSystem, ...]) -> ConnectedSystem | None:
    """The connected system the model meant, matched case-insensitively on the registry key or
    the display name.

    BOTH SPELLINGS, BECAUSE THE PROMPT SHOWS ONE AND THE CODE STORES THE OTHER. The CONNECTED DATA
    stub lists `display_name`, so that is what a model reads and passes back; the key is what
    names the artefact. Accepting either is argument parsing at the boundary, not a policy branch
    — nothing about WHETHER this project may read the system is decided here."""
    wanted = system.strip().casefold()
    for candidate in on:
        if wanted in {candidate.key.casefold(), candidate.connector.display_name.casefold()}:
            return candidate
    return None


def _deliveries(messages: Sequence[ModelMessage], artefact: str) -> int:
    """How many earlier tool results in this conversation carried exactly this artefact.

    EXACT CONTENT, NOT THE TOOL NAME. The unreadable-file answer and the not-switched-on refusal
    come back under the same tool name, and counting them would spend the ceiling on calls that
    handed over nothing. Matching the artefact itself also keeps one system's deliveries from
    counting against another's."""
    return sum(
        1
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.content == artefact
    )


async def connector_schema(ctx: RunContext[Any], system: str) -> str:
    """Get everything known about a connected system's data before you write code against it.

    You get its table, every column with its type and the values it holds, the airport's own KPI
    definitions, and the rules that make a query correct rather than merely runnable — which
    column to filter dates on, how to collapse repeated rows to one per flight, and which text
    needs trimming. Pass the connected system's name exactly as your instructions list it.

    Call this BEFORE writing any code that reads the connected data, every time, including when
    the app already reads it and you are only adding a column: the file in front of you shows what
    a previous turn used, not what exists. Column names cannot be guessed from ordinary ones, and
    a wrong one is a query that runs and reports the wrong number.

    Use what you learn to write correct queries. Do NOT copy KPI formulas, SLA targets or
    threshold numbers into the app's source or its on-screen text — a published app is listed to
    everyone in the organisation, which is a wider audience than the one person whose access an
    administrator approved.
    """
    prompt_context = getattr(ctx.deps, "prompt_context", None)
    if not isinstance(prompt_context, PromptContext) or not prompt_context.connected_systems:
        return _NO_CONTEXT

    on = prompt_context.connected_systems
    match = _match(system, on)
    if match is None:
        # THE MODEL CAN FIX THIS by calling again with a name from its instructions, so it is the
        # retryable kind — and the refusal names what IS connected rather than only what is not.
        raise ModelRetry(
            f"`{system}` is not a system this project can read. "
            f"It can read: {', '.join(entry.connector.display_name for entry in on)}."
        )
    if not match.window.effectively_on:
        # UNREACHABLE THROUGH REGISTRATION — `connected_systems_for_project` returns only systems
        # that are effectively on, and the toolset is registered only when that set is non-empty.
        # Asserted rather than assumed, and NOT retryable: whether this project may read a system
        # is an administrator's decision, not something a better argument reaches.
        return (
            f"{match.connector.display_name} is not switched on for this project, so its schema "
            "cannot be read here."
        )

    try:
        artefact = _load(match.key)
    except (OSError, UnicodeDecodeError) as exc:
        # NAMED IN THE SERVER LOG, NEVER IN THE MODEL'S ANSWER. The path is the one thing a human
        # needs to fix this and the one thing the model has no use for.
        #
        # BOTH WAYS THE READ CAN FAIL. `OSError` is the missing or unreadable file;
        # `UnicodeDecodeError` is the file that exists and is corrupt, which is NOT an OSError and
        # would otherwise escape as an unhandled exception — failing the citizen's whole turn
        # instead of telling the model to stop rather than guess column names.
        #
        # `exc_info=True` because the module docstring calls this a packaging break a human must
        # fix, and that is exactly when the traceback is the thing they need.
        logger.error(
            "connector_catalogue_unreadable",
            connector_key=match.key,
            path=str(_CATALOGUE / f"{match.key}.txt"),
            error=str(exc),
            exc_info=True,
        )
        return _UNAVAILABLE

    delivered = _deliveries(ctx.messages, artefact)
    if delivered >= MAX_DELIVERIES_PER_CONVERSATION:
        # A WARNING, NOT AN INFO LINE. A model asking again for something its context already holds
        # twice is either a model regression or history that did not reach the model the way this
        # list says it did, and this is the one place either is visible from.
        logger.warning(
            "connector_schema_already_delivered",
            connector_key=match.key,
            deliveries=delivered,
        )
        return _ALREADY_LOADED
    return artefact


CONNECTOR_TOOLSET: FunctionToolset[Any] = FunctionToolset[Any]([connector_schema], id="connector")
"""The connected-data surface, registered on BOTH kinds of a project that has one and on nothing
else.

THE FIRST TOOLSET IN THIS REGISTRY THAT IS NOT DEPS-AGNOSTIC, and worth knowing before you write a
test against it: the body reads `prompt_context` off the run's deps, so exercising it needs
`ChatDeps`-shaped deps rather than the `ReadDeps`/`ToolDeps` the surface-listing tests use. Over
`Any` and cast at the registry, following `_PLAN_OPTIONS_TOOLSET` and `CONVERSATION_TOOLSET`.

Both arms, because a Plan chat reasons about what can be built from the data and a Build chat
writes the code that reads it, and neither can do that from invented column names.

THE REGISTERED NAME AND THE NAME THE STUB TELLS THE AGENT TO CALL ARE ONE STRING. pydantic-ai
registers a function under its `__name__`, and `projection.CONNECTOR_SCHEMA_TOOL` is what the
CONNECTED DATA block interpolates, so the two are only equal by agreement — there is no place to
write the function's name down. `test_mode_prompts.py` holds them equal, off the REGISTERED
definition rather than off `__name__`, so a rename that pydantic-ai would honour goes red there. A
module-level `assert` was the obvious alternative and is exactly what `.claude/rules/
fail-first-python.md` forbids: `python -O` strips it, so the guard would be absent from the one
environment where a missing tool is a citizen's problem."""
