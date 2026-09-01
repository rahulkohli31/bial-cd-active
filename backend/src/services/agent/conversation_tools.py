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

from typing import Any, Final

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.services.messages.projection import (
    TELL_THE_USER_TOOL as TELL_THE_USER_TOOL,
)
from src.services.messages.projection import UPDATE_MAX_CHARS

_SHOWN: Final = "Shown to the user. Carry on with the work."
"""What the model reads back. It says the words LANDED, because the alternative — silence, or
an echo of the update — leaves the model unsure whether to repeat itself, and a repeated
update is the one failure mode this channel can produce on its own."""


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


async def tell_the_user(_ctx: RunContext[Any], update: str) -> str:
    """Say one thing to the person waiting, in the middle of your work. Use it when you are
    starting something they would want to know about, or when a piece of it has just landed —
    one or two plain sentences about their app, in the words they already use. Anything else
    you write while you are still calling tools does not reach them, so this is how you speak
    before the turn ends."""
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
    return _SHOWN


CONVERSATION_TOOLSET: FunctionToolset[Any] = FunctionToolset[Any](
    [tell_the_user], id="conversation"
)
"""The tools both kinds carry, as ONE object handed to both arms of the registry.

OVER `Any` AND CAST AT THE REGISTRY, following `_PLAN_OPTIONS_TOOLSET` rather than
`read_only_toolset`. The difference between those two precedents is real: `read_only_toolset`
is a factory because it CLOSES OVER a per-caller workspace accessor, and there is nothing here
to close over — no deps are read, so there is nothing a run could bind that another run must
not see."""
