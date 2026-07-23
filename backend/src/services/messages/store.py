"""The native message store (U4, plan 2026-07-22-002) — append, load, repair, mark.

The `messages` table holds NATIVE pydantic-ai batches (one row per persisted batch). Files
are the ONLY transcript transformation: everything else round-trips byte-faithfully between
JSONB and `list[ModelMessage]` via `ModelMessagesTypeAdapter`. The two seams:

* PERSIST (`append_batch`): dump → externalize every `BinaryContent` to an attachment
  REFERENCE marker (bytes never land in a row; Azure Foundry has no Files API, so the bytes
  re-enter as base64 at send time) → `redact_secrets` over every string in the tree → insert
  with a server-owned, gap-free `seq` under the two-writer retry discipline.
* LOAD (`load_history`): rows → concatenate payloads in seq order → swap reference markers
  back to binary dicts (rehydrated from the attachments table + object store) → validate
  through the TypeAdapter → repair dangling `ToolCallPart`s (a crash mid-step leaves a call
  with no result; Anthropic refuses such a replay, so a synthesized "interrupted" result is
  stitched in).

WHY THE MARKER SWAP MUST BE EXHAUSTIVE AND PRE-VALIDATION (pinned by test against pydantic-ai
2.5.0): the user-content union contains `CachePoint`, whose fields all have defaults — so ANY
unknown dict inside content validates *silently* as `CachePoint(kind='cache-point')`. A
reference marker that survived to validation would not raise; it would quietly become a cache
hint and the attachment would vanish. `_swap_refs` therefore walks every dict in the tree, and
`load_history` fail-firsts if a marker key is still present after the swap.

Redaction here is NOT length-capped, deliberately: truncating a transcript would corrupt the
durable record, every pattern in `core/redaction.py` is linear by construction, and every
producer already bounds its own output (run_command caps, attachment ceilings). The ReDoS
learning's "cap before scanning" applies to synchronous relay paths, not the persistence seam.
"""

from __future__ import annotations

import base64
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeGuard

import sqlalchemy as sa
import structlog
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.redaction import redact_secrets
from src.db.models.attachment import Attachment
from src.db.models.conversation import ConversationMode
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.media.magic import bytes_match_declared
from src.services.storage import ObjectStorage, StorageError, assert_owned

_log = structlog.get_logger()

# The payload serialization contract this code writes (pydantic-ai 2.5.0 native batch +
# attachment-ref externalization). Readers of a row with a HIGHER version than they know
# must refuse rather than guess.
SCHEMA_VERSION: Final = 1

# The attachment reference marker's discriminator value. The serialized `BinaryContent` uses
# `kind: "binary"`; the externalized reference uses this kind so the two can never be confused.
ATTACHMENT_REF_KIND: Final = "bial-attachment-ref"

# How many times to re-pick a seq when a concurrent writer took the slot (the established
# two-writer discipline — see `build_sessions/outcome.py`'s original). Two retries covers a
# genuinely concurrent turn; more would mean a caller in a tight loop.
_SEQ_RETRIES: Final = 2

# An empty conversation's high-water seq: seq starts at 0, so -1 is the honest EXCLUSIVE
# lower bound (`max + 1` → 0 for the first row).
_EMPTY: Final = -1

# The synthesized result stitched under a dangling tool call at load (see
# `repair_dangling_tool_calls`). Plain factual prose — the model reads this as history.
_INTERRUPTED_RESULT: Final = (
    "This tool call was interrupted before a result was recorded (the run was cut short). "
    "Treat it as not executed."
)


class TranscriptStoreError(Exception):
    """Base for the store's own failures (never used for plain DB errors)."""


class UnattributedBinaryError(TranscriptStoreError):
    """A `BinaryContent` reached the persist seam without an `identifier` — a PRODUCER bug.
    Every binary in this platform originates from an uploaded attachment row; persisting raw
    bytes into JSONB (silent fallback) is exactly what the externalization exists to prevent,
    so this fails first."""


class AttachmentRehydrationError(TranscriptStoreError):
    """A stored attachment reference could not be rehydrated (row gone, blob gone, or bytes
    no longer match their declared type). Carries a user-safe message only."""


class SeqContentionError(TranscriptStoreError):
    """The seq retry budget ran out — something is appending to this conversation in a tight
    loop. Nothing was written (the failed insert rolled back); the caller may retry the turn."""


class MarkerSwapIncompleteError(TranscriptStoreError):
    """An attachment reference marker survived `_swap_refs` — a walk bug. Failing here beats
    the alternative: pydantic-ai 2.5.0 silently coerces unknown content dicts to `CachePoint`,
    so a leaked marker would VANISH into a cache hint, not raise."""


@dataclass(frozen=True)
class StoredBatch:
    """What `append_batch` durably wrote — post-commit values via `.returning()` (never a
    `refresh` across the commit: the MissingGreenlet learning)."""

    id: uuid.UUID
    seq: int


# A rehydrator resolves an attachment reference to (base64 data, media_type). Injectable so
# tests (and non-storage callers) can supply their own; production uses
# `attachment_rehydrator(db, storage, user_id)`.
Rehydrator = Callable[[str], Awaitable[tuple[str, str]]]


# --- persist seam -------------------------------------------------------------


def _assert_binaries_attributed(node: Any) -> None:
    """Fail-first walk over the LIVE dataclasses: every `BinaryContent` must carry a
    producer-set identifier (the attachment id). Checked BEFORE the dump because pydantic-ai
    2.5.0 auto-generates a content-hash identifier at serialization time when none was set —
    which matches no attachment row and would surface as a confusing rehydration failure
    turns later instead of a producer bug now. `_identifier` is the declared dataclass field
    behind the auto-generating `identifier` property (pinned version)."""
    if isinstance(node, BinaryContent):
        if node._identifier is None:  # noqa: SLF001 — the property auto-generates; the field is the truth
            raise UnattributedBinaryError(
                "a BinaryContent without an attachment identifier reached the store"
            )
        return
    if isinstance(node, (list, tuple)):  # fmt: skip  # ruff py314 strips parens
        for item in node:
            _assert_binaries_attributed(item)
        return
    if isinstance(node, dict):
        for value in node.values():
            _assert_binaries_attributed(value)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for field in dataclasses.fields(node):
            _assert_binaries_attributed(getattr(node, field.name))


def _redact_tree(node: Any) -> Any:
    """`redact_secrets` over every string VALUE in the dumped tree (text, tool args — dict or
    JSON-string — tool returns, thinking, error details: one uniform rule instead of a
    per-part allowlist that drifts). Keys are structural, never redacted."""
    if isinstance(node, str):
        return redact_secrets(node)
    if isinstance(node, list):
        return [_redact_tree(item) for item in node]
    if isinstance(node, dict):
        return {key: _redact_tree(value) for key, value in node.items()}
    return node


def _externalize_binaries(node: Any) -> Any:
    """Replace every serialized `BinaryContent` (`kind: "binary"`) with an attachment
    reference marker. The `identifier` (producer-set, verified by
    `_assert_binaries_attributed` before the dump) is the reference; bytes never land in the
    row."""
    if isinstance(node, list):
        return [_externalize_binaries(item) for item in node]
    if isinstance(node, dict):
        if node.get("kind") == "binary":
            identifier = node.get("identifier")
            if not isinstance(identifier, str) or not identifier:
                raise UnattributedBinaryError(
                    "a BinaryContent without an attachment identifier reached the store"
                )
            return {"kind": ATTACHMENT_REF_KIND, "attachment_id": identifier}
        return {key: _externalize_binaries(value) for key, value in node.items()}
    return node


def dump_for_row(messages: Sequence[ModelMessage]) -> list[Any]:
    """A native batch → the JSONB payload: verify binaries are attributed (live-object walk,
    fail-first) → dump (json mode → base64 bytes, ISO datetimes) → strip instructions →
    externalize binaries → redact. Externalize FIRST so the redactor never scans base64 blobs.

    Instructions are stripped at THIS seam (U9/D4): pydantic-ai stamps the run's composed
    `instructions` onto every `ModelRequest` it returns, but the D4 contract is that prompts
    are per-run and NEVER persisted — history loads from the DB and each new run re-injects
    its own composition, so a stored copy could only bloat rows and fossilize stale prompt
    text. Payload-level normalization (not object mutation): the live run's in-memory
    history keeps whatever upstream put there."""
    _assert_binaries_attributed(list(messages))
    dumped = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
    for message in dumped:
        if isinstance(message, dict) and "instructions" in message:
            message["instructions"] = None
    return [_redact_tree(_externalize_binaries(message)) for message in dumped]


# --- load seam ----------------------------------------------------------------


async def _swap_refs(node: Any, rehydrate: Rehydrator) -> Any:
    """Replace every attachment reference marker with a serialized binary dict (base64 data +
    media type from the AUTHORITATIVE attachment row — the payload's word is never trusted for
    bytes). The walk is exhaustive over dicts/lists; `load_history` verifies completeness."""
    if isinstance(node, list):
        return [await _swap_refs(item, rehydrate) for item in node]
    if isinstance(node, dict):
        if node.get("kind") == ATTACHMENT_REF_KIND:
            attachment_id = node.get("attachment_id")
            if not isinstance(attachment_id, str) or not attachment_id:
                raise AttachmentRehydrationError("a stored attachment reference is malformed")
            data_b64, media_type = await rehydrate(attachment_id)
            return {
                "kind": "binary",
                "data": data_b64,
                "media_type": media_type,
                "identifier": attachment_id,
                "vendor_metadata": None,
            }
        return {key: await _swap_refs(value, rehydrate) for key, value in node.items()}
    return node


def _assert_no_marker_left(node: Any) -> None:
    """Fail-first backstop: no reference marker may survive the swap (see the module
    docstring's CachePoint hazard)."""
    if isinstance(node, list):
        for item in node:
            _assert_no_marker_left(item)
    elif isinstance(node, dict):
        if node.get("kind") == ATTACHMENT_REF_KIND:
            raise MarkerSwapIncompleteError("an attachment reference survived the swap walk")
        for value in node.values():
            _assert_no_marker_left(value)


def attachment_rehydrator(
    db: AsyncSession, storage: ObjectStorage, user_id: uuid.UUID
) -> Rehydrator:
    """The production rehydrator: owner-scoped attachment row (ADR-0004) → object store →
    magic-byte re-check (the upload path's gate, re-asserted so a swapped blob can't ride a
    stale row) → base64. The row is authoritative for both the key and the media type."""

    async def rehydrate(attachment_id: str) -> tuple[str, str]:
        row = await db.scalar(
            sa.select(Attachment).where(
                Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
            )
        )
        if row is None:
            raise AttachmentRehydrationError(
                "an attached file is no longer available; remove it and attach it again"
            )
        assert_owned(row.storage_key, user_id)
        try:
            data = await storage.get(row.storage_key)
        except StorageError as exc:
            raise AttachmentRehydrationError(
                "an attached file could not be read right now; please try again"
            ) from exc
        if not bytes_match_declared(row.media_type, data):
            raise AttachmentRehydrationError(
                "an attached file no longer matches its declared type; attach it again"
            )
        return base64.b64encode(data).decode("ascii"), row.media_type

    return rehydrate


def _is_tool_answer(part: Any) -> TypeGuard[ToolReturnPart | RetryPromptPart]:
    """A part that answers a tool call on the wire (`ToolReturnPart` or a tool-scoped
    `RetryPromptPart`). `RetryPromptPart` always carries a `tool_call_id` (auto-generated when
    unset), so the caller gates on call-id membership, not `is None`."""
    return isinstance(part, (ToolReturnPart, RetryPromptPart))  # fmt: skip


def repair_dangling_tool_calls(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Make the loaded history wire-valid for Anthropic: every `ToolCallPart` is answered by
    EXACTLY ONE `ToolReturnPart`/`RetryPromptPart` sitting in the request IMMEDIATELY after its
    response. This is the ONE choke point where history is assembled, so it closes every way the
    U11/U12 plan-options lifecycle (a resolution appended as its own later row, not inline) can
    otherwise produce a replay Anthropic rejects — which wedges every later turn:

    * NO answer anywhere → a synthesized "interrupted" result is stitched in (a crash between a
      step's call and its result).
    * A NON-ADJACENT answer (a `build`/`refine` return appended after intervening rows — a
      mode-switch marker, a re-armed `build_failed` card, a later turn) → RELOCATED to sit right
      after its call and removed from its original position, so no `tool_result` rides the wire
      without an adjacent `tool_use`.
    * MORE THAN ONE answer for one call (the Build-it vs turn-start refine race writing two
      returns for the same card) → DEDUPED to a single answer.

    Answers whose id matches no call are left untouched (never this code's to move)."""
    call_ids: set[str] = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }
    # Canonical answer per CALLED id — first occurrence wins, which deduplicates a raced
    # double-resolve to one return.
    answer_by_id: dict[str, ToolReturnPart | RetryPromptPart] = {}
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if _is_tool_answer(part) and part.tool_call_id in call_ids:
                    answer_by_id.setdefault(part.tool_call_id, part)

    # Called ids already answered ADJACENTLY (in the immediately-following request) stay in
    # place; every other copy is stripped and the canonical answer relocated next to its call.
    adjacent_ids: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, ModelResponse):
            continue
        here = {p.tool_call_id for p in message.parts if isinstance(p, ToolCallPart)}
        following = messages[index + 1] if index + 1 < len(messages) else None
        if isinstance(following, ModelRequest):
            for part in following.parts:
                if _is_tool_answer(part) and part.tool_call_id in here:
                    adjacent_ids.add(part.tool_call_id)

    repaired: list[ModelMessage] = []
    for index, message in enumerate(messages):
        if isinstance(message, ModelRequest):
            previous = messages[index - 1] if index >= 1 else None
            prev_calls = (
                {p.tool_call_id for p in previous.parts if isinstance(p, ToolCallPart)}
                if isinstance(previous, ModelResponse)
                else set()
            )
            kept = [
                part
                for part in message.parts
                if not (
                    _is_tool_answer(part)
                    and part.tool_call_id in call_ids
                    # Keep only the ONE adjacent placement (this request follows its call);
                    # strip duplicates and non-adjacent late resolutions for relocation.
                    and not (part.tool_call_id in adjacent_ids and part.tool_call_id in prev_calls)
                )
            ]
            if kept:
                repaired.append(
                    message
                    if len(kept) == len(message.parts)
                    else dataclasses.replace(message, parts=kept)
                )
            continue
        repaired.append(message)
        if not isinstance(message, ModelResponse):
            continue
        calls = [part for part in message.parts if isinstance(part, ToolCallPart)]
        stitched: list[ToolReturnPart | RetryPromptPart] = []
        for call in calls:
            if call.tool_call_id in adjacent_ids:
                continue  # answered in place by the following request
            answer = answer_by_id.get(call.tool_call_id)
            stitched.append(
                answer
                if answer is not None
                else ToolReturnPart(
                    tool_name=call.tool_name,
                    content=_INTERRUPTED_RESULT,
                    tool_call_id=call.tool_call_id,
                )
            )
        if stitched:
            repaired.append(ModelRequest(parts=stitched))
    return repaired


async def load_history(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    rehydrate: Rehydrator,
) -> list[ModelMessage]:
    """The conversation's full native history, ready for `message_history`: every row's
    payload (hidden marker rows INCLUDED — the model must see where the mode changed) in seq
    order, references rehydrated, validated, dangling calls repaired. Owner-scoped
    (ADR-0004)."""
    payloads = (
        (
            await db.execute(
                sa.select(Message.payload)
                .where(Message.conversation_id == conversation_id, Message.user_id == user_id)
                .order_by(Message.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    combined: list[Any] = [message for payload in payloads for message in payload]
    if not combined:
        return []
    swapped = await _swap_refs(combined, rehydrate)
    _assert_no_marker_left(swapped)
    history = ModelMessagesTypeAdapter.validate_python(swapped)
    return repair_dangling_tool_calls(history)


async def load_rows(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    include_hidden: bool = False,
) -> Sequence[Message]:
    """The conversation's rows in seq order — the projection/audit read (U6 builds on this).
    Hidden rows (mode-switch markers) are excluded unless asked for: hiddenness is this SQL
    predicate, never a payload property."""
    query = (
        sa.select(Message)
        .where(Message.conversation_id == conversation_id, Message.user_id == user_id)
        .order_by(Message.seq.asc())
    )
    if not include_hidden:
        query = query.where(Message.visibility == MessageVisibility.VISIBLE)
    return (await db.execute(query)).scalars().all()


# --- append (the two-writer discipline) ---------------------------------------


async def _head_seq(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    """The conversation's highest seq, or -1 when empty. Scoped by conversation alone — the
    unique constraint (`uq_messages_conversation_seq`) defines the seq space, and ownership
    is the caller's to have established (every caller here has)."""
    highest = await db.scalar(
        sa.select(sa.func.max(Message.seq)).where(Message.conversation_id == conversation_id)
    )
    return _EMPTY if highest is None else int(highest)


async def append_batch(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    messages: Sequence[ModelMessage],
    entry_kind: MessageEntryKind,
    mode: ConversationMode,
    visibility: MessageVisibility = MessageVisibility.VISIBLE,
    meta: dict[str, Any] | None = None,
) -> StoredBatch:
    """Durably append one batch with a server-owned gap-free seq. OWNS its commit: every
    caller sits at a durability seam (a turn boundary, a BRAIN step, a lifecycle event) where
    "returned" must mean "on disk". Two-writer discipline: pick `max+1`, insert, and when a
    concurrent writer took the slot (IntegrityError on the unique constraint) roll back and
    re-pick — bounded, then `SeqContentionError` with nothing written. Post-commit values come
    back via `.returning()` (never a refresh across the commit).

    `meta` is for system entries (redacted here too — same egress discipline as the payload).
    """
    payload = dump_for_row(messages)
    safe_meta = _redact_tree(meta) if meta is not None else None
    for _ in range(_SEQ_RETRIES + 1):
        seq = await _head_seq(db, conversation_id) + 1
        statement = (
            sa.insert(Message)
            .values(
                user_id=user_id,
                conversation_id=conversation_id,
                seq=seq,
                schema_version=SCHEMA_VERSION,
                entry_kind=entry_kind,
                visibility=visibility,
                mode=mode,
                payload=payload,
                meta=safe_meta,
            )
            .returning(Message.id, Message.seq)
        )
        try:
            # SAVEPOINT per attempt: a losing insert aborts only its own savepoint, so the
            # session's transaction — and everything already flushed in it — survives the
            # retry. A bare session.rollback() here would unwind the CALLER'S work too.
            async with db.begin_nested():
                row = (await db.execute(statement)).one()
        except IntegrityError as exc:
            if "uq_messages_conversation_seq" in str(exc.orig):
                continue  # a concurrent writer took the slot — re-pick and retry
            raise  # FK violation (conversation deleted mid-append) etc. — the caller's problem
        await db.commit()
        return StoredBatch(id=row.id, seq=row.seq)
    raise SeqContentionError(
        f"could not allocate a seq for conversation {conversation_id} "
        f"after {_SEQ_RETRIES + 1} attempts"
    )


# --- mode-switch markers ------------------------------------------------------


def mode_switch_marker_text(old_mode: ConversationMode, new_mode: ConversationMode) -> str:
    """The direction-aware marker prose (U4 decision). Upgrades stay minimal; a downgrade OUT
    of Write adds the capability clarification — because post-Write history contains
    successful write/run tool calls that contradict the current toolset, and that contradiction
    exists exactly (and only) at this point in the history. The static mode prompts stay free
    of absent-tool prose (R13); this marker is the one sanctioned exception."""
    if old_mode is ConversationMode.WRITE and new_mode is not ConversationMode.WRITE:
        follow_up = (
            "read the app's files to answer questions."
            if new_mode is ConversationMode.ASK
            else "read the app's files and discuss what to change."
        )
        return (
            f"[mode changed: {old_mode.value} → {new_mode.value}. The build tools used "
            f"earlier in this conversation are not available in {new_mode.value.capitalize()} "
            f"mode; {follow_up} The user can switch back to Write mode to make changes.]"
        )
    return f"[mode changed: {old_mode.value} → {new_mode.value}]"


async def append_mode_switch_marker(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    old_mode: ConversationMode,
    new_mode: ConversationMode,
) -> StoredBatch:
    """Persist the hidden mode-switch marker row: a pure native user message (payload) whose
    hiddenness lives on the ROW (`entry_kind`/`visibility` — never in the payload). Written
    directly by the switch endpoint, never through an agent run (`new_messages()` structurally
    excludes injected history, so a run could never save it for us).

    GOTCHA (pinned by test): never start a run with NO user prompt while a marker is the last
    history message — pydantic-ai adopts the trailing request as the prompt, and the model
    would be asked to answer the marker."""
    marker = ModelRequest(
        parts=[UserPromptPart(content=mode_switch_marker_text(old_mode, new_mode))]
    )
    return await append_batch(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        messages=[marker],
        entry_kind=MessageEntryKind.MODE_SWITCH,
        mode=new_mode,
        visibility=MessageVisibility.HIDDEN,
    )
