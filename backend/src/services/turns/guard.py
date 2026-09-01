"""The ONE per-conversation in-flight guard.

One reply at a time per conversation. This was built when TWO surfaces could generate one —
the relay and the turn engine — so that a conversation could never run one of each at once.
That window is closed: the relay is retired and `claim_conversation` has a single caller
(`turns/engine.py`). The guard is kept because the property it defends is unchanged — two
concurrent turns on one conversation interleave into one transcript. Plain in-process set —
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


def conversation_is_mid_reply(conversation_id: uuid.UUID) -> bool:
    """Is a reply being generated right now?

    One reply at a time per conversation: a second send while the first is still streaming
    would interleave two runs writing the same thread. (It once also gated the mode switch,
    which was legal only between turns; that route is gone, this invariant is not.)"""
    return conversation_id in _mid_reply
