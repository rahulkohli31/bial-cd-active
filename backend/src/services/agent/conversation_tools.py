"""The tools a chat has in BOTH kinds, because they are about the conversation itself.

WHY A THIRD TOOLSET RATHER THAN TWO MORE ENTRIES IN THE TWO ARMS. `read_only_toolset` is
about a workspace and `sandbox_toolset` is about a container; these are about the person
waiting. They are the only tools whose presence does not depend on what the run can DO, so
registering them once and handing the same object to both arms of `toolsets_for_kind` is what
keeps "the kind decides only the toolset" true of a tool that both kinds have — the
alternative, naming each tool twice in the registry, is two lists to keep in step and a
silent drift the moment one is edited.

THE TOOL BODIES ARE DELIBERATELY THIN, and that is the design rather than an omission. A
`tell_the_user` body that pushed text onto the live stream itself would have to be handed an
emitter through the run's deps, and — worse — it would render at the moment the body RUNS.
Tool bodies run concurrently and their results arrive in completion order, while a reloaded
transcript renders in part order, so a response that spoke and also read a file could put the
two in one order live and the other order on reload. Both emitters render from the stored
tool CALL instead, at the position the call occupies, which is the `present_plan_options`
shape and the only placement where live order and reload order cannot disagree (R75a/R76).

So the body's whole job is to enforce the bound and tell the model what happened. What
reaches the screen is decided by `update_from_args`, which both emitters call — one rule, one
place, and neither emitter re-deriving a ceiling the other might read differently.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.db.models.harness_counter import HarnessCounter
from src.services.messages.projection import (
    MAX_FIRST_SLICE,
    UPDATE_MAX_CHARS,
    agreed_slice,
    finished_from_args,
)
from src.services.messages.projection import (
    PROPOSE_SLICE_TOOL as PROPOSE_SLICE_TOOL,
)
from src.services.messages.projection import (
    TELL_THE_USER_TOOL as TELL_THE_USER_TOOL,
)

_SHOWN: Final = "Shown to the user. Carry on with the work."
"""What the model reads back. It says the words LANDED, because the alternative — silence, or
an echo of the update — leaves the model unsure whether to repeat itself, and a repeated
update is the one failure mode this channel can produce on its own."""

_PROPOSED: Final = (
    "Shown to the user, with everything you found listed above your first round. Wait for "
    "their answer before starting, unless they have already told you to go ahead."
)
"""What the model reads back after a proposal.

IT SAYS WHAT THE USER ACTUALLY SAW, because the platform rendered the message and the model
did not: without this the model has no way to know the full list was shown and may repeat it in
prose, which is the duplication this tool exists to remove."""


def _too_long(update: str) -> str:
    """The teaching refusal, naming the bound rather than scolding.

    THE BOUND IS THE PLATFORM'S, NOT AN INSTRUCTION'S (R70/R76). A prompt sentence asking for
    brevity is a request; this is the thing that actually holds when a build goes wrong, which
    is precisely when the model stops complying with requests. It names the number and the
    register, so the retry has somewhere to go."""
    return (
        f"That update is {len(update)} characters and the limit is {UPDATE_MAX_CHARS}. This "
        "channel is for one or two plain sentences about the app — what you are doing now, or "
        "what just landed. Say the short version; the detail belongs to the work itself."
    )


async def tell_the_user(ctx: RunContext[Any], update: str, finished: str | None = None) -> str:
    """Say one thing to the person waiting, in the middle of your work. Use it when you are
    starting something they would want to know about, or when a piece of it has just landed —
    one or two plain sentences about their app, in the words they already use. Anything else
    you write while you are still calling tools does not reach them, so this is how you speak
    before the turn ends. When the update is that one of the pieces you agreed to build is
    done, pass that piece's name as `finished`, spelled exactly as you named it."""
    # THE CONTEXT IS TAKEN AND NEVER READ, and the underscore is the whole comment: nothing
    # here resolves anything off the run, and nothing here should. Reaching through it to push
    # text from this body is the placement `update_from_args` exists to avoid (see the module
    # docstring). It is in the signature because pydantic-ai's `FunctionToolset` is typed to
    # accept a context-first callable — dropping it works at run time and fails `ty`.
    text = update.strip()
    if not text:
        raise ModelRetry(
            "That update was empty. Say what is happening in one or two plain sentences, or "
            "carry on working without calling this."
        )
    if len(text) > UPDATE_MAX_CHARS:
        raise ModelRetry(_too_long(text))
    if finished is not None:
        # ONE FIELD RATHER THAN A SECOND TOOL (U12). The mark and the sentence arrive together
        # — "It is in." — so splitting them would ask for two calls to report one event, and
        # two tools that differ only in shade are the overload the research warns about.
        #
        # VALIDATED AGAINST THE CONVERSATION'S OWN RECORD, read through the run's messages.
        # A mark naming a piece nobody agreed to is not a bookkeeping slip: it is what would
        # make the closing account name pieces the citizen never saw proposed, so the model is
        # told rather than the mark being silently dropped.
        marked = finished.strip()
        agreed = agreed_slice(ctx.messages)
        if not agreed:
            raise ModelRetry(
                f"There is nothing agreed to mark `{marked}` against — no first slice has been "
                "proposed in this conversation. Propose one, or leave `finished` out."
            )
        if marked not in agreed:
            raise ModelRetry(
                f"`{marked}` is not one of the pieces we agreed to build "
                f"({', '.join(agreed)}). Mark one of those, spelled the same way, or leave "
                "`finished` out."
            )
        if not _already_marked_against(ctx.messages, agreed):
            # R92's SECOND HALF, counted where the fact is rather than read out of a
            # transcript. The first mark that matches the agreed list is the observable form
            # of "they proceeded on the slice as proposed" — a fact about a tool call, which
            # is the only kind of fact this plan lets anything act on.
            await _count(HarnessCounter.FIRST_SLICE_ACCEPTED)
    return _SHOWN


def _already_marked_against(messages: Sequence[Any], agreed: Sequence[str]) -> bool:
    """Has any earlier call in this run already marked a piece of the CURRENT agreement?

    Scoped to the current agreement on purpose: a mark against a slice that has since been
    re-proposed is not evidence about the new one, so re-proposing genuinely re-opens the
    question of whether the build proceeded on what was agreed — which is exactly what the two
    counters are there to show."""
    for message in messages:
        for part in getattr(message, "parts", []):
            if getattr(part, "tool_name", None) != TELL_THE_USER_TOOL:
                continue
            marked = finished_from_args(getattr(part, "args", None))
            if marked is not None and marked in agreed:
                return True
    return False


async def _count(name: HarnessCounter) -> None:
    """Fire-and-forget, and the import is function-scoped for the package cycle.

    `src.services.build_sessions.__init__` reaches `manager` → `appdata` → `services.projects`
    → `describe`, which imports the agent package this module lives in. At module level that
    fails at interpreter start, in whichever router happens to import first, with a traceback
    pointing nowhere near the cause — the same trap `usage/gate.py` documents.

    `count` owns its own session and swallows everything, so a counter can never fail the tool
    it is counting."""
    from src.services.build_sessions.counters import count

    await count(name)


def _bad_slice(found: list[str], first: list[str]) -> str | None:
    """The teaching refusal for a proposal that cannot be honoured, or None.

    ONLY THE CEILING IS ENFORCED. A single large piece is a legitimate slice — twenty pages
    describing one screen is one piece — and a floor of two would leave the model no recovery
    but to split something that should not be split or name something it does not intend to
    build. The two-piece preference is in the prompt, where a soft preference belongs."""
    if not found:
        return (
            "List everything the user asked for in `found`, in your own words, one piece per "
            "entry. The proposal starts by saying the whole thing back to them."
        )
    if not first:
        return "Name at least one piece in `first` — the round you would actually build now."
    if len(first) > MAX_FIRST_SLICE:
        return (
            f"That first round names {len(first)} pieces and the limit is {MAX_FIRST_SLICE}. "
            "Pick the ones that give them something usable soonest; the rest stays on the list "
            "and you will come back to it."
        )
    stray = [piece for piece in first if piece not in found]
    if stray:
        return (
            f"`{stray[0]}` is in your first round but not in `found`, so the user would be "
            "shown a round containing something they were never told you had picked up. Every "
            "piece in `first` has to appear in `found`, spelled the same way."
        )
    return None


async def propose_first_slice(
    _ctx: RunContext[Any], found: list[str], first: list[str], why: str, question: str
) -> str:
    """When a request arrives with a lot of separate things in it, propose what to build first.
    Pass every piece you picked up in `found`, the two to four you would build now in `first`,
    one sentence saying why those in `why`, and exactly one question in `question`. The user is
    shown all of it, so write the piece names the way they would describe them.

    Use it for new work arriving in bulk. A question, a fix, a change to something already
    built, or the next round of something already agreed is just done — negotiating a small
    request wastes the user's turn and reads as reluctance."""
    pieces_found = [piece.strip() for piece in found if piece.strip()]
    pieces_first = [piece.strip() for piece in first if piece.strip()]
    refusal = _bad_slice(pieces_found, pieces_first)
    if refusal is not None:
        raise ModelRetry(refusal)
    if not why.strip():
        raise ModelRetry("Say in one sentence why you would start with those pieces.")
    if not question.strip():
        raise ModelRetry(
            "Ask exactly one question — the one thing you most need decided before you start."
        )
    # COUNTED AFTER THE BOUNDS, so a refused proposal counts nothing: it reached no citizen and
    # agreed nothing, and counting it would make the denominator "times the model tried".
    await _count(HarnessCounter.FIRST_SLICE_PROPOSED)
    return _PROPOSED


CONVERSATION_TOOLSET: FunctionToolset[Any] = FunctionToolset[Any](
    [tell_the_user, propose_first_slice], id="conversation"
)
"""The tools both kinds carry, as ONE object handed to both arms of the registry.

OVER `Any` AND CAST AT THE REGISTRY, following `_PLAN_OPTIONS_TOOLSET` rather than
`read_only_toolset`. The difference between those two precedents is real: `read_only_toolset`
is a factory because it CLOSES OVER a per-caller workspace accessor, and there is nothing here
to close over — no deps are read, so there is nothing a run could bind that another run must
not see."""
