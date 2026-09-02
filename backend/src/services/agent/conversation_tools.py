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

So the body's whole job is to tell the model what happened when a call cannot be honoured.
What reaches the screen is decided by `update_from_args`, which both emitters call — one rule,
one place.

NEITHER TOOL COUNTS ANY MORE. Both used to carry a numeric ceiling — 280 characters on an
update, four pieces in a first round — and the renderer carried a copy of each, so a call one
over the line was refused at the body AND deleted at the renderer: the model was taught to
retry and the citizen was shown nothing where the agent had spoken. How long a sentence should
be and how much belongs in a first round are judgements about the person waiting, which is
what the agent is for.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.db.models.harness_counter import HarnessCounter
from src.services.messages.projection import (
    PROPOSE_SLICE_TOOL as PROPOSE_SLICE_TOOL,
)
from src.services.messages.projection import (
    TELL_THE_USER_TOOL as TELL_THE_USER_TOOL,
)
from src.services.messages.projection import (
    agreed_slice,
    clean_pieces,
    finished_from_args,
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


async def tell_the_user(ctx: RunContext[Any], update: str, finished: str | None = None) -> str:
    """Speak into a GAP — a stretch of work long enough that the person waiting would
    otherwise be watching a still screen. Everything you write ordinarily reaches them in the
    order you write it, so this is not the way to talk to them; it is the one way to reach
    them while a tool is still running, before you come back. Use it when you are starting
    something slow, or when a piece has just landed: a plain sentence about their app, in the
    words they already use. When the update is that one of the pieces you agreed to build is
    done, pass that piece's name as `finished`, spelled exactly as you named it."""
    # THE CONTEXT IS READ FOR THE RECORD, AND FOR NOTHING ELSE. `ctx.messages` is how a mark
    # is checked against the slice the citizen actually agreed to (below) — the agreement lives
    # in the conversation, not in a column, so reading it back IS reading the run's messages.
    # What this body must never do is reach through the context to PUSH text: the words are
    # rendered from this call's arguments by whichever emitter is drawing, which is the
    # placement `update_from_args` exists to keep (see the module docstring).
    text = update.strip()
    if not text:
        raise ModelRetry(
            "That update was empty. Say what is happening in one or two plain sentences, or "
            "carry on working without calling this."
        )
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

    NO PIECE COUNT IS ENFORCED, in either direction. A floor of two would leave the model no
    recovery but to split something that should not be split — twenty pages describing one
    screen is one piece — and the ceiling that used to sit here refused proposals the agent had
    made well, at a number nobody could defend against a particular citizen's request. Worse,
    the renderer read the same ceiling, so a refused proposal was ALSO drawn nowhere: the model
    was told to retry and the citizen was shown silence.

    WHAT IS LEFT IS THE ONE RULE THAT IS NOT TASTE: every piece in the first round has to be a
    piece the citizen was told had been picked up. That one is about what a person reads, not
    about how many things an agent should do at once."""
    if not found:
        return (
            "List everything the user asked for in `found`, in your own words, one piece per "
            "entry. The proposal starts by saying the whole thing back to them."
        )
    if not first:
        return "Name at least one piece in `first` — the round you would actually build now."
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
    Pass every piece you picked up in `found`, the ones you would build now in `first`, one
    sentence saying why those in `why`, and exactly one question in `question`. The user is
    shown all of it, so write the piece names the way they would describe them.

    Use it for new work arriving in bulk. A question, a fix, a change to something already
    built, or the next round of something already agreed is just done — negotiating a small
    request wastes the user's turn and reads as reluctance."""
    # THE CONTEXT IS TAKEN AND NEVER READ, and the underscore is the whole comment: this call's
    # own arguments are the agreement, so there is nothing to resolve off the run and nothing
    # here should try. It is in the signature because pydantic-ai's `FunctionToolset` is typed
    # to accept a context-first callable — dropping it works at run time and fails `ty`.
    # CLEANED THE WAY THE RENDERER CLEANS, which is the whole point of borrowing its function
    # rather than restating the rule. This body's bounds decide what the model is told; the
    # renderer's decide what the citizen sees, and they run at different moments — the call
    # event draws the card before this body has executed. When the two cleaned differently they
    # disagreed: `first=["A","A","A","A","A"]` is five entries and one piece, so the body
    # refused it for naming five while the renderer drew a card for the one, leaving a citizen
    # reading a proposal the model was being told to retry.
    pieces_found = clean_pieces(found)
    pieces_first = clean_pieces(first)
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
