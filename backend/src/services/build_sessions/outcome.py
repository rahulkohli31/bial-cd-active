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
from typing import Annotated, Any, Final

import sqlalchemy as sa
import structlog
from pydantic import AnyUrl, TypeAdapter, UrlConstraints, ValidationError
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

# The graceful end reasons whose prose differs from a natural finish. `manager.py` imports the
# first two for its `stop`/`force_end` defaults: the token and the sentence it produces must move
# together, because a drifted token does not fail loudly — it falls straight back through to
# "Build finished.", which is the bug these arms exist to fix.
STOPPED_BY_USER: Final = "stopped_by_user"
FORCE_ENDED: Final = "force_ended"
# The idle reaper's reason — part of C3's documented terminal set (`build_sessions/schemas.py`).
IDLE_TEARDOWN: Final = "idle_teardown"

# An empty thread's high-water seq. Not a sentinel for "unknown": seq starts at 0, so -1 is the
# honest EXCLUSIVE lower bound for a thread with nothing in it — `seq > -1` collects everything,
# which is exactly what a first build should see.
EMPTY_TRANSCRIPT: Final = -1

# The preview link is PARSED, not pattern-checked, and https-only — the same parse the deployed URL
# gets at the admin boundary (`api/v1/admin/schemas.py::HttpsUrl`). `javascript:` and `data:` fall
# out of that parse, which is the point: this string is rendered straight into an `<a href>` in the
# portal's outcome card, same-origin with the user's session.
#
# Nothing reaching here should ever fail it — the only writer is this module and the only value is
# the ACA FQDN the platform itself minted onto the sandbox handle. That is precisely why the check
# is cheap to keep: it is the fail-closed floor under "we only write URLs we minted", so that claim
# stays true by validation rather than by every future producer of `handle.preview_url` being
# careful.
_PREVIEW_URL_MAX_CHARS: Final = 2048
_PREVIEW_URL: Final[TypeAdapter[AnyUrl]] = TypeAdapter(
    Annotated[AnyUrl, UrlConstraints(max_length=_PREVIEW_URL_MAX_CHARS, allowed_schemes=["https"])]
)


def _safe_preview_url(preview_url: str | None) -> str | None:
    """The preview link when it parses as https, else None — never the raw string.

    Fails closed to None, which is the part's own "no preview" state, rather than raising: this
    runs inside the end sequence, where a raise would cost the user their entire outcome record
    over a cosmetic link. The ORIGINAL string is returned rather than the parse's output — pydantic
    normalizes (a path-less URL gains a trailing `/`), and the recorded link should be the address
    the sandbox actually served, not a rewrite of it.
    """
    if preview_url is None:
        return None
    try:
        _PREVIEW_URL.validate_python(preview_url)
    except ValidationError:
        _log.warning("build preview url failed the https parse; recording the outcome without it")
        return None
    return preview_url


def _summary(status: BuildSessionStatus, reason: str | None) -> str:
    """The outcome's prose. This is the message's TEXT, so it is both what a reader sees and what
    the model is replayed as history on the user's next turn — hence plain, factual wording.

    Every arm under the FAILED one keys on the REASON, because the STATUS cannot tell these apart:
    `_terminal_status` maps a natural finish, a Stop, a force-end and an idle reap ALL onto ENDED.
    Reading the status alone is what recorded a build stopped at minute two as "Build finished." —
    permanently, and then replayed that back to the model as history on the user's next turn.
    """
    if status is BuildSessionStatus.FAILED:
        return f"The build failed: {reason}" if reason else "The build failed."
    if reason == "quota_exceeded":
        return "The build stopped: you reached your daily limit."
    if reason == STOPPED_BY_USER:
        return "You stopped this build before it finished."
    if reason == FORCE_ENDED:
        # The one graceful end that DISCARDS its work: `_do_finalize` skips the snapshot when
        # `force_ended` is set, so any summary implying otherwise is a lie about the user's code.
        return "This build was force-stopped before it finished, and its work was discarded."
    if reason == IDLE_TEARDOWN:
        return "This build was stopped because it sat idle."
    return "Build finished."


def build_outcome_parts(
    *,
    status: BuildSessionStatus,
    session_id: uuid.UUID,
    preview_url: str | None,
    snapshot_committed: bool,
    reason: str | None,
    started_seq: int | None,
) -> list[dict[str, Any]]:
    """The outcome message's parts: a summary text part + the `build` part.

    The text part is not decoration. `buildContent` (portal) and this table's readers assemble a
    turn from its TEXT parts, so a build-part-only message would replay to the model as an empty
    assistant turn.

    `startedSeq` is the build's START marker — the transcript's high-water seq at the moment the
    build began — and it is what makes the attachment boundary TEMPORAL rather than positional.
    This row is allocated at build END, so it lands AFTER any turn the user sent while the build
    ran (the composer stays live during a build, deliberately). A reader that started collecting
    after this row's POSITION therefore skipped those turns permanently and silently — the files
    a user attached mid-build were dropped from every later build. `_collect_parts`
    (`attachments.py`) reads this field instead. Omitted when the start recorded no marker, which
    is the state every row written before this field existed is in.
    """
    part: dict[str, Any] = {
        "type": "build",
        "status": status.value,
        "sessionId": str(session_id),
        "previewUrl": _safe_preview_url(preview_url),
        "endedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "snapshotCommitted": snapshot_committed,
        "reason": reason,
    }
    if started_seq is not None:
        part["startedSeq"] = started_seq
    return [{"type": "text", "text": _summary(status, reason)}, part]


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
    started_seq: int | None = None,
) -> bool:
    """Append the build-outcome message to its thread. Returns True if a row was written.

    Owner-scoped (ADR-0004): the conversation must be the caller's, else this is a no-op rather
    than a cross-user write. Idempotent on `session_id` — a build has exactly one outcome, so a
    re-run of the end sequence must not add a second.

    `started_seq` is the transcript's high-water mark at build START, captured by the caller then
    (`SessionManager.start`) because it is unrecoverable now: by the time this runs, a turn the
    user sent DURING the build is already indistinguishable from one they sent before it.
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
        started_seq=started_seq,
    )
    for _ in range(_SEQ_RETRIES + 1):
        if await _already_recorded(db, conversation_id, session_id):
            return False
        seq = await _next_seq(db, conversation_id)
        db.add(
            Message(
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


async def transcript_head_seq(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    """A thread's highest seq right now, or `EMPTY_TRANSCRIPT` (-1) when it holds no messages.

    Scoped by conversation ALONE, deliberately: seq's uniqueness is `uq_messages_conversation_seq`,
    so the conversation IS the seq space. Adding a `user_id` predicate would narrow the scan to a
    different axis than the constraint it answers to — and every row in a conversation belongs to
    its owner anyway (the callers establish that before asking). Ownership is the CALLER'S to check
    first; this is arithmetic over an already-authorized thread.

    Two callers need this exact number for opposite reasons: `_next_seq` allocates the slot after
    it, and `SessionManager.start` records it as the build's START marker (`startedSeq`) so the
    NEXT build knows which turns arrived while this one was running.
    """
    highest = await db.scalar(
        sa.select(sa.func.max(Message.seq)).where(Message.conversation_id == conversation_id)
    )
    return EMPTY_TRANSCRIPT if highest is None else int(highest)


async def _next_seq(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    return await transcript_head_seq(db, conversation_id) + 1
