"""The ONE per-conversation in-flight guard (U7 → U10).

One reply at a time per conversation, whoever generates it: the U7 relay and the U10 turn
engine both claim HERE, so a conversation can never run a relay turn and an engine turn
concurrently during the U10→U13 window where both surfaces exist. Plain in-process set —
the single-replica invariant `SessionManager._active_by_user` leans on (one process is the
sole writer; absent in-process state is authoritative). The claimer's OWN completion path
releases; the guard never expires a claim on its own (a leak here is a bug in the caller's
`finally`, not a TTL problem).
"""

from __future__ import annotations

import uuid

_mid_reply: set[uuid.UUID] = set()


class ConversationBusyError(Exception):
    """A reply is already being generated for this conversation (typed 409 upstream)."""


def claim_conversation(conversation_id: uuid.UUID) -> None:
    """Claim the conversation for one reply; raises `ConversationBusyError` if taken."""
    if conversation_id in _mid_reply:
        raise ConversationBusyError
    _mid_reply.add(conversation_id)


def release_conversation(conversation_id: uuid.UUID) -> None:
    """Release the claim (idempotent — safe from every `finally`)."""
    _mid_reply.discard(conversation_id)
