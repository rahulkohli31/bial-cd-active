"""The build-outcome record: the reason tokens a stored row can carry, and the read that finds
the newest one.

Rows of this shape are PERMANENT in the production transcript, and nothing in `src` appends one
any more — a build runs as an ordinary Write chat turn and records its ending as a
`turn_terminal` row instead. So this module is the reader's half; the writer that produces a
faithful row for a test lives in `tests/fakes.py`.
"""

from __future__ import annotations

import uuid
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.conversation import Conversation
from src.db.models.message import Message, MessageEntryKind

# The graceful end reasons whose prose differs from a natural finish. The token and the sentence
# it produces must move together, because a drifted token does not fail loudly — it falls
# straight back through to "Build finished.".
#
# SPELLED HERE RATHER THAN IMPORTED FROM `turns/copy.END_REASONS`, which is where every other
# producer's reason lives: `src/services/turns/__init__` imports the turn engine, and the engine
# imports this package, so a module-level import of anything under `turns` from here is a cycle.
# `tests/services/turns/test_end_reasons.py` holds these four against that collection instead.
STOPPED_BY_USER: Final = "stopped_by_user"
FORCE_ENDED: Final = "force_ended"
# The idle reaper's reason — part of the documented terminal set (`build_sessions/schemas.py`).
IDLE_TEARDOWN: Final = "idle_teardown"
# A spent daily budget, raised by the turn engine.
QUOTA_EXCEEDED: Final = "quota_exceeded"


async def newest_build_outcome_status(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> BuildSessionStatus | None:
    """The status of the NEWEST recorded build outcome across the project's threads, or None
    when no outcome was ever recorded (or the newest one is unreadable).

    Owner- AND project-scoped. Best-effort by design: the outcome write itself can silently
    fail, so an absent row reads as "nothing known" — None — never an error. Relaunch uses
    this to label a restore whose newest build FAILED as "last saved version": a failed build
    still snapshots, so the newest snapshot may be that build's workspace, and an unqualified
    "ready" would misrepresent what the user is looking at."""
    meta = await db.scalar(
        sa.select(Message.meta)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.user_id == user_id,
            Conversation.project_id == project_id,
            Message.user_id == user_id,
            Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
            # Outcomes only: a `build_started` lifecycle row also carries a sessionId,
            # and picking it up here would read as "status unknown" — regressing the
            # relaunch label for a project whose newest build has merely STARTED.
            Message.meta["kind"].astext == "build_outcome",
            Message.meta["sessionId"].astext.is_not(None),
        )
        # Outcomes land across conversations, so seq (per-conversation) alone cannot order
        # them — newest write first, seq as the same-instant tiebreak within a thread.
        .order_by(Message.created_at.desc(), Message.seq.desc())
        .limit(1)
    )
    if not isinstance(meta, dict):
        return None
    raw = meta.get("status")
    if not isinstance(raw, str):
        return None  # an unreadable status is "nothing known", not a crash
    try:
        return BuildSessionStatus(raw)
    except ValueError:
        return None
