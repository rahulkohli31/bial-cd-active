"""Record a finished build in its thread — the durable half of 003-U5, native-store edition.

WHY THE SERVER WRITES THIS. The plan had the PORTAL append the outcome at its terminal, with a
reconciliation pass for the closed-tab case. That pass turned out to be unimplementable: sessions
live only in `SessionManager._sessions`, are evicted `_ENDED_RETENTION_SECONDS` (5 min) after the
terminal, and do not survive a restart — so "the project's latest build session" is not a question
anything can answer once the tab is gone. Since builds take minutes and users close tabs, the
portal-only design would have missed exactly the users the record exists for. The thing that
always knows a build finished is the thing that finished it, so it writes.

SHAPE (U4). The outcome is a `system_event` row in the native message store: the PAYLOAD is a
synthesized assistant text (`ModelResponse(TextPart(summary))`) — plain factual prose, because
it replays to the model as history on the user's next turn — and the build METADATA
(`sessionId` / `startedSeq` / `previewUrl` / `status` / `reason` / `snapshotCommitted`) lives in
the row's `meta` column, OUTSIDE the payload, so the payload stays pure native. Idempotency keys
on `meta->>'sessionId'`; the attachment-consumption boundary reads `meta->>'startedSeq'`
(`attachments.py`). Seq allocation and the two-writer retry now live in ONE place — the store's
`append_batch` — instead of being reimplemented here.

TODO(U5): when BRAIN persists its full transcript per step, this summary row becomes the
terminal lifecycle entry of that stream (provisioned/quota/stopped/reaped entries join it) —
re-home the writer accordingly.

WHY IT WRITES BEFORE THE TERMINAL FRAME. `_do_finalize` calls this immediately before emitting
`ended`, so by the time any client learns the build is over, the row is already there. The
reverse order would race every reader.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

import sqlalchemy as sa
import structlog
from pydantic import AnyUrl, TypeAdapter, UrlConstraints, ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.conversation import Conversation, ConversationMode
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.messages.store import (
    SeqContentionError,
    append_batch,
)

_log = structlog.get_logger()

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

    Fails closed to None, which is the record's own "no preview" state, rather than raising: this
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
    """The outcome's prose. This is the payload's TEXT, so it is both what a reader sees and what
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


def build_outcome_meta(
    *,
    status: BuildSessionStatus,
    session_id: uuid.UUID,
    preview_url: str | None,
    snapshot_committed: bool,
    reason: str | None,
    started_seq: int | None,
) -> dict[str, Any]:
    """The outcome row's `meta` — the build's structured record, OUTSIDE the native payload.

    `startedSeq` is the build's START marker — the transcript's high-water seq at the moment the
    build began — and it is what makes the attachment boundary TEMPORAL rather than positional.
    This row is allocated at build END, so it can land AFTER a turn that was recorded while the
    build ran. The composer is now gated shut for the whole of a build (one "the agent is
    working" gate), but the server may not assume that: a stale or reloaded client, the
    conversation-less `POST /build-sessions` start, and a crashed build that never writes an
    outcome at all can each put a turn inside the window. A reader that started collecting after
    this row's POSITION would skip those turns permanently and silently — the files a user
    attached would drop from every later build. `_boundary` (`attachments.py`) reads this field
    instead. Omitted when the start recorded no marker.
    """
    meta: dict[str, Any] = {
        "kind": "build_outcome",
        "status": status.value,
        "sessionId": str(session_id),
        "previewUrl": _safe_preview_url(preview_url),
        "snapshotCommitted": snapshot_committed,
        "reason": reason,
    }
    if started_seq is not None:
        meta["startedSeq"] = started_seq
    return meta


async def write_build_started(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session_id: uuid.UUID,
    started_seq: int,
) -> bool:
    """Append the `build_started` lifecycle row (U5): a HIDDEN `system_event` marking the
    moment a build began in this thread. Payload is an EMPTY native batch — the record is the
    row itself (`meta.kind = 'build_started'`), it replays nothing to the model and renders
    nothing to the user; U6's projection reads it to anchor "a build ran here" even when the
    build never reached its outcome (crash, kill -9). Returns True if written.

    Best-effort by contract (the caller sits between lock adoption and task launch, where a
    raise would leak the adopted container): a failure is the caller's to log-and-continue.
    """
    try:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            mode=ConversationMode.WRITE,
            visibility=MessageVisibility.HIDDEN,
            meta={
                "kind": "build_started",
                "sessionId": str(session_id),
                "startedSeq": started_seq,
            },
        )
    except SeqContentionError:
        _log.warning(
            "build_started marker not recorded after seq retries",
            session_id=str(session_id),
            conversation_id=str(conversation_id),
        )
        return False
    return True


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
    """Append the build-outcome `system_event` row to its thread. Returns True if written.

    Owner-scoped (ADR-0004): the conversation must be the caller's, else this is a no-op rather
    than a cross-user write. Idempotent on `session_id` — a build has exactly one outcome, so a
    re-run of the end sequence must not add a second. Seq allocation + the two-writer retry live
    in the store's `append_batch`; a retry budget exhausted there is logged, not raised (this
    runs inside the end sequence, where a raise would hang every SSE feed).

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

    if await _already_recorded(db, conversation_id, session_id):
        return False

    payload = ModelResponse(parts=[TextPart(content=_summary(status, reason))])
    meta = build_outcome_meta(
        status=status,
        session_id=session_id,
        preview_url=preview_url,
        snapshot_committed=snapshot_committed,
        reason=reason,
        started_seq=started_seq,
    )
    try:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[payload],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            mode=ConversationMode.WRITE,
            meta=meta,
        )
    except SeqContentionError:
        # Someone is appending in a tight loop. Dropping the outcome leaves the thread of a
        # user who closed their tab with no record at all — log loudly; the caller's
        # best-effort wrapper owns the give-up.
        _log.warning(
            "build outcome not recorded after seq retries",
            session_id=str(session_id),
            conversation_id=str(conversation_id),
        )
        return False
    return True


async def _already_recorded(
    db: AsyncSession, conversation_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    """True if this session's outcome is already in the thread — keyed on `meta->>'sessionId'`,
    the only field that identifies the BUILD (a fresh row id/seq says nothing about which build
    it was)."""
    row = await db.scalar(
        sa.select(Message.id).where(
            Message.conversation_id == conversation_id,
            Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
            # `kind` disambiguates: the U5 `build_started` lifecycle row carries this
            # session's id too, and without this predicate it would satisfy the idempotency
            # probe and silently suppress the real outcome.
            Message.meta["kind"].astext == "build_outcome",
            Message.meta["sessionId"].astext == str(session_id),
        )
    )
    return row is not None


async def newest_build_outcome_status(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> BuildSessionStatus | None:
    """The status of the NEWEST recorded build outcome across the project's threads, or None
    when no outcome was ever recorded (or the newest one is unreadable).

    Owner- AND project-scoped (ADR-0004). Best-effort by design: the outcome write itself is
    best-effort (`_record_outcome` swallows failures rather than hang the terminal), so an
    absent row must read as "nothing known" — None — never an error. Relaunch (#43/U6) uses
    this to label a restore whose newest build FAILED as "last saved version": `_do_finalize`
    snapshots pass and fail alike, so the newest snapshot may well be that failed build's
    workspace, and an unqualified "ready" would misrepresent what the user is looking at.
    """
    meta = await db.scalar(
        sa.select(Message.meta)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.user_id == user_id,
            Conversation.project_id == project_id,
            Message.user_id == user_id,
            Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
            # Outcomes only: a `build_started` lifecycle row (U5) also carries a sessionId,
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


async def transcript_head_seq(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    """A thread's highest seq right now, or `EMPTY_TRANSCRIPT` (-1) when it holds no messages.

    Scoped by conversation ALONE, deliberately: seq's uniqueness is `uq_messages_conversation_seq`,
    so the conversation IS the seq space. Adding a `user_id` predicate would narrow the scan to a
    different axis than the constraint it answers to — and every row in a conversation belongs to
    its owner anyway (the callers establish that before asking). Ownership is the CALLER'S to check
    first; this is arithmetic over an already-authorized thread.

    Two callers need this exact number for opposite reasons: the store allocates the slot after
    it, and `SessionManager.start` records it as the build's START marker (`startedSeq`) so the
    NEXT build knows which turns arrived while this one was running.
    """
    highest = await db.scalar(
        sa.select(sa.func.max(Message.seq)).where(Message.conversation_id == conversation_id)
    )
    return EMPTY_TRANSCRIPT if highest is None else int(highest)
