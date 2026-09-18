"""Record a finished build in its thread — the durable counterpart to the live `ended` frame,
native-store edition.

WHY THIS EXISTS. The plan had the portal append the outcome at its terminal, with a
reconciliation pass for the closed-tab case — unimplementable: sessions live only in
`SessionManager._sessions`, are evicted 5 minutes after the terminal
(`_ENDED_RETENTION_SECONDS`), and do not survive a restart, so "the project's latest build
session" is unanswerable once the tab is gone. Builds take minutes and users close tabs, so a
portal-only design would miss exactly the users the record exists for. The thing that always
knows a build finished is the thing that finished it, so the server writes.

THE WRITE HALF IS PARKED, NOT LIVE. `write_build_started` — the hidden `build_started` marker —
went with `SessionManager.start`, its only caller, when the start route was deleted; so did
`transcript_head_seq`, which existed to capture that marker's `startedSeq`. `write_build_outcome`
STAYS, and nothing in `src/` calls it any more: the end sequence that did was retired with the
session-scoped stop route. It is kept rather than deleted because the `{sessionId}` routes it
belongs to are kept — they read `build_outcome` rows already in the production database — and it
is how those readers are tested against a faithful row rather than a hand-built dict. Treat it as
parked: a writer for tests and for a future re-home, not a path production takes.

Two consequences to know rather than rediscover. (1) No new `build_started`
rows are written, so `projection._closed_sessions()` has nothing to close and the projection's
`BuildInProgressItem` arm now serves only rows already in the database. (2) A build that runs as
an ordinary Write chat turn records its ending as a `turn_terminal` row instead, which is the
projection arm a citizen actually sees today.

SHAPE. A `system_event` row: PAYLOAD is synthesized assistant text
(`ModelResponse(TextPart(summary))`) — plain factual prose, because it replays to the model as
history on the user's next turn; build METADATA (`sessionId`/`startedSeq`/`previewUrl`/`status`/
`reason`/`snapshotCommitted`) lives in `meta`, OUTSIDE the payload, so the payload stays pure
native. Idempotency keys on
`meta->>'sessionId'`; `startedSeq` was the attachment-consumption boundary the now-deleted
`attachments.py` read. Seq allocation and the two-writer retry live in the store's
`append_batch`.

TODO: once BRAIN persists its full transcript per step, this row becomes that stream's terminal
lifecycle entry (provisioned/quota/stopped/reaped entries join it); re-home the writer then.
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
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageEntryKind
from src.services.messages.store import (
    SeqContentionError,
    append_batch,
)

_log = structlog.get_logger()

# The graceful end reasons whose prose differs from a natural finish. The token and the sentence
# it produces must move together, because a drifted token does not fail loudly — it falls
# straight back through to "Build finished.", which is the bug these arms exist to fix.
#
# SPELLED HERE RATHER THAN IMPORTED FROM `turns/copy.END_REASONS`, which is where every other
# producer's reason lives: `src/services/turns/__init__` imports the turn engine, and the engine
# imports this package, so a module-level import of anything under `turns` from here is a cycle.
# `tests/services/turns/test_end_reasons.py` holds these four against that collection instead.
STOPPED_BY_USER: Final = "stopped_by_user"
FORCE_ENDED: Final = "force_ended"
# The idle reaper's reason — part of the documented terminal set (`build_sessions/schemas.py`).
# Read by `_summary` below, which keys its arms on the REASON rather than the status precisely so
# that a row carrying this one reads as what it was instead of "Build finished.".
IDLE_TEARDOWN: Final = "idle_teardown"
QUOTA_EXCEEDED: Final = "quota_exceeded"
"""The fourth token this module keys an arm on — a spent daily budget, raised by the turn engine
and read here."""

# The preview link is PARSED, not pattern-checked, and https-only — the same parse the deployed URL
# gets at the admin boundary (`api/v1/admin/schemas.py::HttpsUrl`). `javascript:` and `data:` fall
# out of that parse, which is the point: this string is rendered straight into an `<a href>` in the
# portal's outcome card, same-origin with the user's session.
#
# Nothing reaching here should ever fail it — the only writer is this module, and the only value is
# the public app address the platform itself composed onto the sandbox handle. That address is now
# `https://<apps-host>/a/<app-name>/` rather than the ACA FQDN it used to be; it CARRIES A PATH,
# and the constraint above still passes it, because the constraint is scheme plus length and never
# host or shape. That is precisely why the check is cheap to keep: it is the fail-closed floor
# under "we only write URLs we minted", so that claim stays true by validation rather than by every
# future producer of `handle.preview_url` being careful.
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

    Every arm under the FAILED one keys on the REASON, because the STATUS cannot tell these
    apart: a natural finish, a Stop, a force-end and an idle reap all carry ENDED. Reading the
    status alone is what recorded a build stopped at minute two as "Build finished." —
    permanently, and then replayed that back to the model as history on the user's next turn.
    """
    if status is BuildSessionStatus.FAILED:
        return f"The build failed: {reason}" if reason else "The build failed."
    if reason == QUOTA_EXCEEDED:
        return "The build stopped: you reached your daily limit."
    if reason == STOPPED_BY_USER:
        return "You stopped this build before it finished."
    if reason == FORCE_ENDED:
        # The one graceful end that DISCARDED its work — the kill switch skipped the snapshot —
        # so any summary implying otherwise is a lie about the user's code.
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

    `startedSeq` was the build's START marker — the transcript's high-water seq at the moment the
    build began — and it is what made the attachment boundary TEMPORAL rather than positional. Its
    reader (`attachments._boundary`) is deleted, and so is the only writer that could stamp it
    (`SessionManager.start`), so in practice this key is now absent from every new row: the
    parameter stays because a caller may still pass one and because the rows already in the
    database carry it. The reasoning is preserved rather than trimmed, since the trap it names is
    the one any future re-introduction has to avoid — a row allocated at build END can land AFTER
    a turn recorded while the build ran, so a reader keying on this row's POSITION drops those
    turns permanently and silently.
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
    """Append the build-outcome `system_event` row. Returns True if written.

    Owner-scoped: the conversation must be the caller's, else this is a no-op, never a
    cross-user write. Idempotent on `session_id` — one outcome per build; an exhausted seq
    retry budget (`append_batch`) logs rather than raises, since raising here would hang
    every SSE feed. `started_seq` is the build's START high-water mark, captured by the
    caller then — unrecoverable later, once a mid-build turn looks no different from one
    sent before it."""
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
            kind=ChatKind.BUILD,
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
            # `kind` disambiguates: the `build_started` lifecycle row carries this
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
