"""The turn-scoped system message: one short operator sentence at the absolute tail.

WHAT IT IS
A `SystemPromptPart` appended to the end of the request the model is about to read, preceded by
a pinned cache breakpoint, and never persisted. It says one fact — the transcript records how
the app WAS, and `check_the_app` answers how it IS — because a conversation's messages age while
the container they describe keeps changing.

WHY `wrap_model_request` AND NOT `before_model_request`
Both hooks hand over a message list, and only one of them is ephemeral. What
`before_model_request` returns is written back into the run's own history, so a sentence added
there reaches `new_messages()`, and the request it rode — the one carrying the tool results —
is then refused wholesale by the persistence predicate, which drops those results and orphans
the calls they answer. `wrap_model_request` runs after that write-back, on the list built for
the wire alone, and this hook rebuilds the tail request with `dataclasses.replace` rather than
mutating it. The sentence therefore exists only in the bytes of one request. Running that late
also means the library's history cleanup and its `prepare_messages` pass have both already
happened, so nothing merges the sentence into a neighbouring message or moves it off the tail.

A splice into `message_history` would do the opposite of all of this — it lands AHEAD of the
citizen's persisted prompt, the position that breaks the cached prefix on the following turn.

THE PIN IS NOT OPTIONAL
Without a breakpoint immediately before it, a sentence at the tail of request *k* sits where
request *k+1* puts its tool results, so *k+1* cannot read *k*'s write and falls back to the
tool-definitions boundary for the rest of the turn. The `CachePoint` authored just before the
sentence marks the last block of the user content, so the cached prefix ends where the sentence
begins and every later request in the turn still hits it. It claims the single message-slot
breakpoint the request budget leaves (`tests/services/turns/test_cache_breakpoints.py` pins the
arithmetic).

NOTHING UNTRUSTED MAY RIDE THIS CHANNEL
System content carries operator authority, so the sentence is a module constant and this class
takes no text argument at all. There is no parameter through which sandbox output, a tool
result or an attachment's contents could reach the system role.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass, replace

from pydantic_ai.capabilities.abstract import AbstractCapability, WrapModelRequestHandler
from pydantic_ai.messages import (
    CachePoint,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.tools import RunContext

from src.services.agent.agent import ChatDeps
from src.services.orchestrator.constants import CACHE_TTL

PULL_THE_APP_STATE = (
    "This app's state can change between turns, and the messages above record how it was "
    "rather than how it is; check_the_app answers how it is now."
)
"""The one sentence this channel ever carries.

OPERATOR VOICE, STATING A FACT. Anthropic's guidance on the system role is that content phrased
as a command overriding the user is resisted, because the model is trained to take the user's
side against instructions that work against them. A sentence that says what is true of the
platform is read; "always call check_the_app first" is argued with.

One sentence is also the price. Nothing clears a sent system message — no pydantic-ai version
expresses the API's `clear_at` — so each one stays in the array it was sent in for the rest of
the conversation. Length is the only lever there is, and the trigger's per-turn cooldown is the
other."""


def _carries_a_system_entry(model: object) -> bool:
    """Whether this model will render a mid-conversation `SystemPromptPart` as its own entry.

    `AbstractModel` covers realtime sessions, which never reach this hook; anything that is not
    a `Model` has no profile to ask, and the honest answer for it is no."""
    return isinstance(model, Model) and bool(
        model.profile.get("supports_inline_system_prompts", False)
    )


@dataclass
class TurnScopedSystemMessage(AbstractCapability[ChatDeps]):
    """Appends `PULL_THE_APP_STATE` to the tail of one request per turn, pin first.

    THE TRIGGER IS NOT HERE, AND THAT IS DELIBERATE. `should_send` is called once per outgoing
    request and answers from the turn's own tool-call facts; `on_sent` closes the turn's
    cooldown. Both belong to the turn rather than to this instance, so a fresh capability per
    `agent.iter` run — which is what a self-heal loop produces — still sends once for the whole
    turn. A sentence on every request inside a turn re-breaks the prefix at each step, which is
    worse than the defect it addresses."""

    should_send: Callable[[], bool]
    on_sent: Callable[[], None]

    _: KW_ONLY

    id: str | None = "turn-scoped-system-message"

    async def wrap_model_request(
        self,
        ctx: RunContext[ChatDeps],
        *,
        request_context: ModelRequestContext,
        handler: WrapModelRequestHandler,
    ) -> ModelResponse:
        self._append_to_the_tail(request_context)
        return await handler(request_context)

    def _append_to_the_tail(self, request_context: ModelRequestContext) -> None:
        if not _carries_a_system_entry(request_context.model):
            # FAIL CLOSED, AND THIS IS THE GUARD THE WHOLE MECHANISM HANGS ON. A model that
            # cannot serve a mid-conversation `system` entry does not drop the part: this late
            # in the pipeline the adapter hoists it into the request's TOP-LEVEL system
            # parameter, which is the one block every turn of every conversation shares and the
            # one the instructions breakpoint caches. A Foundry deployment whose name the model
            # profile does not recognise lands here, so this is a live configuration branch
            # rather than a defensive one.
            return
        if not self.should_send():
            return
        messages = request_context.messages
        tail = messages[-1] if messages else None
        if not isinstance(tail, ModelRequest):
            # Every path into this hook has already had the framework insist the history ends
            # in a `ModelRequest`, so this is unreachable rather than tolerated — but appending
            # to whatever else arrived would put the sentence mid-conversation.
            return
        messages[-1] = replace(
            tail,
            parts=[
                *tail.parts,
                UserPromptPart(content=[CachePoint(ttl=CACHE_TTL)]),
                SystemPromptPart(content=PULL_THE_APP_STATE),
            ],
        )
        self.on_sent()
