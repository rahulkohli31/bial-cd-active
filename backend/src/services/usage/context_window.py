"""How much of the model's context window a conversation already occupies.

★ THIS IS NOT A SPEND MEASUREMENT, AND THE SEPARATE MODULE IS THE POINT. `weighted_spend` and
`billable_spend` next door deliberately discount a cache READ to a tenth of a fresh token,
because that is what it costs. A cached token still OCCUPIES the window — it is in the prompt,
byte for byte, whatever it was billed at. Routing a window check through the billing weights
would report a 190k conversation as a 30k one and the guardrail would never fire. This repo's
own record has that class of confusion recurring three times, with the rule that token
accounting route through a shared, PURPOSE-NAMED function; so this is that function, and its
name says which question it answers.

WHY A HEURISTIC RATHER THAN THE MODEL'S OWN COUNT. pydantic-ai 2.5.0 exposes
`Model.count_tokens()`, but it is a network round trip to the Foundry-routed client and its
Foundry compatibility is unverified — the same shape as the Files API, which turned out not to
work on Foundry at all. This measures what is already in memory: the `list[ModelMessage]` that
`load_history` loads on every turn anyway, plus the message about to be sent. No call, no DB
read, no new failure mode on the send path.

WHAT IT COUNTS, and where it is deliberately imprecise:

* Every string reachable from the messages, at four characters to the token — the same ratio
  the retired client-side guardrail used, so the browser and the server describe one thing.
* A `BinaryContent` at a flat nominal rather than its byte length. An image is worth roughly
  a thousand tokens however many megabytes it is; charging base64 length would read a 5 MB
  photo as 1.7 MILLION tokens and refuse every conversation that contained one.
* A structural walk (list → dict → dataclass), not a per-part-type table. The part union is
  pydantic-ai's and it grows; a table would silently stop counting whatever it did not know
  about, which is the failure mode that hurts — under-counting is what lets a conversation
  past the guard. The walk shape is `messages/store.py::_assert_binaries_attributed`'s.
* It over-counts a little: `ModelResponse` carries a model name and a provider id that never
  travel back to the model. Tens of characters per turn, and in the safe direction.
* It does NOT see the per-run system prompt, which is composed inside the engine after this
  gate has already decided. That is what `SYSTEM_PROMPT_RESERVE` is for.

So the number is an estimate, and it is used to decide one thing: whether a conversation has
grown past the boundary an administrator set. It is not billing, and nothing is charged from it.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from typing import Any, Final

from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user_limit import UserLimit
from src.services.usage.limits import effective_context

CHARS_PER_TOKEN: Final = 4
"""The estimate's one constant. Four characters to the token is the ratio the retired
`useClaudeAPI.ts` guardrail used against the same 200k window, kept so the browser's warning
and the server's refusal are two readings of one scale rather than two different scales."""

NOMINAL_BINARY_TOKENS: Final = 1_600
"""What one attached image or PDF is charged, regardless of its size.

For an IMAGE this is honest. Vision content costs roughly a thousand tokens per image and does
not scale with the file's byte length, so a flat charge is the right shape — byte length is the
WRONG number by orders of magnitude: base64 of a 5 MB photo is ~6.7 M characters, which at four
characters to the token would read as 1.7 M tokens and refuse the conversation outright.

★ FOR A MULTI-PAGE PDF IT IS A KNOWN UNDER-COUNT, AND THE LARGEST ONE HERE. A document is read
page by page, so a 40-page PDF costs roughly forty times what this charges it, and
`_shared.resolve_binaries` admits `application/pdf` beside `image/*`. A citizen who attaches
documents can therefore carry a conversation past the hard limit while this measures it as
comfortably inside — which is the opaque provider-side failure the guardrail exists to replace,
not a conservative estimate. It is recorded rather than fixed because the honest fix needs a
page count the platform does not store yet (persisted at upload, as the deck branch already
does for its own reasons); charging by byte length instead would resurrect the 1.7 M-token
absurdity above. Until then: prose conversations are guarded, document-heavy ones are not."""

SYSTEM_PROMPT_RESERVE: Final = 8_000
"""Room held back for what this function cannot see.

The per-run system prompt is composed inside the turn engine, AFTER this gate has decided, so
it is not in the history and not in the prompt. Measured at the time of writing: the Plan
segment composes to ~1,800 tokens and the Build segment — the larger — to ~4,400, before the
tool schemas the run also carries. 8,000 covers the larger of the two with room for the
schemas and for both to grow.

Without it the gate is quietly permissive at exactly the setting that matters most: the
DEFAULT hard limit equals the model's own window, so a conversation measured at 199,000 would
be waved through into a prompt that is really 204,000 and fail at the model instead — which is
the opaque failure this whole guardrail exists to replace."""


class ContextWindowExceededError(Exception):
    """This conversation is past the hard limit in force for its owner.

    Raised BEFORE anything is persisted, so the citizen's message is refused whole rather than
    half-recorded. Carries the numbers for the log and the test; the sentence the citizen reads
    is `copy.CHAT_TOO_LONG_TEXT`, and it deliberately states neither."""

    def __init__(self, *, occupied: int, hard_limit: int) -> None:
        super().__init__("conversation context window exceeded")
        self.occupied = occupied
        self.hard_limit = hard_limit


def _tokens_in(node: Any) -> int:
    """The estimate over one live object, recursively. See the module docstring for the rules.

    `BinaryContent` is tested BEFORE the generic dataclass arm because it IS a dataclass, and
    descending into it would charge its `data` field by byte length — the exact over-count the
    flat nominal exists to avoid."""
    if isinstance(node, BinaryContent):
        return NOMINAL_BINARY_TOKENS
    if isinstance(node, str):
        return -(-len(node) // CHARS_PER_TOKEN)  # ceil, without importing math for one call
    if isinstance(node, (list, tuple)):  # fmt: skip  # ruff py314 strips parens
        return sum(_tokens_in(item) for item in node)
    if isinstance(node, dict):
        # Values only. Keys are structural — they are a few characters of JSON framing, and
        # counting them would make the estimate a function of pydantic-ai's field names.
        return sum(_tokens_in(value) for value in node.values())
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        return sum(
            _tokens_in(getattr(node, field.name))
            for field in dataclasses.fields(node)
            if field.name != "part_kind"  # pydantic-ai's union tag, not content
        )
    return 0


def _tokens_in_message(message: ModelMessage) -> int:
    """One message's contribution: its PARTS, and only its parts.

    THE MESSAGE ENVELOPE IS NOT WALKED, deliberately. `ModelResponse` carries a dozen
    bookkeeping fields — `kind`, `state`, `model_name`, `provider_name`,
    `provider_response_id`, `finish_reason` — none of which the model ever reads back, and all
    of which are strings. Walking them added a constant to every message, so a long
    conversation's measurement would be part pydantic-ai's own field values, and would MOVE when
    the library added a field.

    Descending into `parts` rather than reading each part type by name keeps the property that
    matters: a part shape this module has never heard of is still measured, because the walk
    below is structural. Under-counting is the direction that hurts — it is what lets an
    over-long conversation past the guard."""
    return _tokens_in(message.parts)


def occupied_window(
    history: Sequence[ModelMessage],
    prompt: object = None,
) -> int:
    """How full the window will be when this turn runs: the stored history, the message about
    to be sent, and the reserve for the system prompt this cannot see.

    `prompt` is whatever the route is about to hand the agent — a string, a mixed
    `[str | BinaryContent]` list, or `None` on a regenerate, where the trailing request in
    `history` IS the prompt and counting it twice would be wrong."""
    stored = sum(_tokens_in_message(message) for message in history)
    return stored + _tokens_in(prompt) + SYSTEM_PROMPT_RESERVE


async def enforce_context_limit(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    history: Sequence[ModelMessage],
    prompt: object = None,
) -> None:
    """Raise `ContextWindowExceededError` when this conversation is past its owner's hard limit.

    THE ONE PREFLIGHT, called from BOTH routes that start a conversation turn — `turns.start_turn`
    and `transition.build_from_plan`. The daily cap next door is hand-copied at three call sites,
    and the cost of that is on record: a gate wired to one of two send paths is not a gate, it is
    a detour sign. This is a single function precisely so the second entry point cannot drift
    away from the first.

    IT IS NOT "EVERY ROUTE THAT REACHES A MODEL", and this docstring used to say so. `POST
    /v1/build-sessions` starts a model-driven build without consulting this (its per-step spend
    is capped inside `orchestrator/harness.py`, but its context is not bounded here). That route
    sends a caller-supplied prompt rather than a conversation history, so it is not a turn on a
    conversation — but a reader who took the wider claim at face value would go looking for a
    gate that is not there.

    Called AFTER `load_history` and BEFORE `persist_user_turn`, the same slot
    `enforce_daily_limit` occupies — so a refused turn leaves no row to roll back and no claim
    to release."""
    override = await db.scalar(select(UserLimit).where(UserLimit.user_id == user_id))
    _soft, hard = effective_context(override)
    occupied = occupied_window(history, prompt)
    if occupied >= hard:
        raise ContextWindowExceededError(occupied=occupied, hard_limit=hard)
