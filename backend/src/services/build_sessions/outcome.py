"""Record a finished build in its thread — the durable half of 003-U5.

WHY THE SERVER WRITES THIS. The plan had the PORTAL append the outcome at its terminal, with a
reconciliation pass for the closed-tab case. That pass turned out to be unimplementable: sessions
live only in `SessionManager._sessions`, are evicted `_ENDED_RETENTION_SECONDS` (5 min) after the
terminal, and do not survive a restart — so "the project's latest build session" is not a question
anything can answer once the tab is gone. Since builds take minutes and users close tabs, the
portal-only design would have missed exactly the users the record exists for. The thing that
always knows a build finished is the thing that finished it, so it writes.

This does NOT weaken the stateless-relay rule (issue #28). That rule governs `/v1/claude` — the
browser assembles the prompt and the server forwards it. The BUILD path already reads this same
table (`attachments.py` materializes persisted file parts); writing its own outcome to it is the
same boundary, in the other direction.

WHY IT WRITES BEFORE THE TERMINAL FRAME. `_do_finalize` calls this immediately before emitting
`ended`, so by the time any client learns the build is over, the row is already there. The reverse
order would race every reader.

SEQ IS THE SERVER'S TO ALLOCATE, and this is the second writer that does it. The first is
`append_message`, which takes the client's `seq` as a HINT and reallocates to `max(seq) + 1` when
that slot is taken. This writer allocates the same way, so the two can race but never disagree
about the OUTCOME: whoever loses the unique constraint re-picks (here) or is told to retry with a
`message_seq_conflict` 409 (there).

That design is load-bearing, and it replaced one that was not. This module originally assumed the
portal could RESERVE the slot it was about to take, so the two sides would "agree by construction".
They could not: the portal counts slots it has reserved but not yet persisted, the server counts
rows, and only the tab that started the build reserved anything at all — so a reloaded tab's next
message landed on the outcome's slot. The append route answered `201` and wrote nothing, which made
the loss invisible on both sides. Allocation moved server-side precisely so no writer has to guess.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.conversation import Conversation
from src.db.models.message import Message, MessageRole

_log = structlog.get_logger()

# How many times to re-pick a seq when something else appended concurrently. The portal reserves
# the same slot we do, so a collision means a genuinely concurrent turn (a user sending as the
# build lands). Two retries covers that; more would mean a caller in a tight loop.
_SEQ_RETRIES = 2


def _summary(status: BuildSessionStatus, reason: str | None) -> str:
    """The outcome's prose. This is the message's TEXT, so it is both what a reader sees and what
    the model is replayed as history on the user's next turn — hence plain, factual wording."""
    if status is BuildSessionStatus.FAILED:
        return f"The build failed: {reason}" if reason else "The build failed."
    if reason == "quota_exceeded":
        return "The build stopped: you reached your daily limit."
    return "Build finished."


def build_outcome_parts(
    *,
    status: BuildSessionStatus,
    session_id: uuid.UUID,
    preview_url: str | None,
    snapshot_committed: bool,
    reason: str | None,
) -> list[dict[str, Any]]:
    """The outcome message's parts: a summary text part + the `build` part.

    The text part is not decoration. `buildContent` (portal) and this table's readers assemble a
    turn from its TEXT parts, so a build-part-only message would replay to the model as an empty
    assistant turn. The shape must satisfy `_validate_parts`'s `build` kind — the portal appends
    the same shape through the HTTP boundary, and both must round-trip identically.
    """
    return [
        {"type": "text", "text": _summary(status, reason)},
        {
            "type": "build",
            "status": status.value,
            "sessionId": str(session_id),
            "previewUrl": preview_url,
            "endedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "snapshotCommitted": snapshot_committed,
            "reason": reason,
        },
    ]


async def write_build_outcome(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session_id: uuid.UUID,
    status: BuildSessionStatus,
    preview_url: str | None,
    snapshot_committed: bool,
    reason: str | None,
) -> bool:
    """Append the build-outcome message to its thread. Returns True if a row was written.

    Owner-scoped (ADR-0004): the conversation must be the caller's, else this is a no-op rather
    than a cross-user write. Idempotent on `session_id` — a build has exactly one outcome, so a
    re-run of the end sequence must not add a second.
    """
    conversation = await db.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:
        return False  # deleted mid-build, or never ours — nothing to record it in

    parts = build_outcome_parts(
        status=status,
        session_id=session_id,
        preview_url=preview_url,
        snapshot_committed=snapshot_committed,
        reason=reason,
    )
    for _ in range(_SEQ_RETRIES + 1):
        if await _already_recorded(db, conversation_id, session_id):
            return False
        seq = await _next_seq(db, conversation_id)
        db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                user_id=user_id,
                role=MessageRole.ASSISTANT,
                seq=seq,
                schema_version=1,
                parts=parts,
                created_at=datetime.now(UTC),
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # Someone appended while we were choosing a seq. Roll back and re-pick: dropping the
            # outcome here would leave the thread of a user who closed their tab with no record
            # at all — the exact gap this writer exists to close.
            await db.rollback()
            continue
        return True
    _log.warning(
        "build outcome not recorded after seq retries",
        session_id=str(session_id),
        conversation_id=str(conversation_id),
    )
    return False


async def _already_recorded(
    db: AsyncSession, conversation_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    """True if this session's outcome is already in the thread — keyed on `sessionId`, the only
    field that identifies the BUILD (a fresh `_id`/seq says nothing about which build it was)."""
    rows = await db.scalars(
        sa.select(Message.parts).where(Message.conversation_id == conversation_id)
    )
    target = str(session_id)
    return any(
        isinstance(part, dict) and part.get("type") == "build" and part.get("sessionId") == target
        for parts in rows
        if isinstance(parts, list)
        for part in parts
    )


async def _next_seq(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    highest = await db.scalar(
        sa.select(sa.func.max(Message.seq)).where(Message.conversation_id == conversation_id)
    )
    return 0 if highest is None else int(highest) + 1
