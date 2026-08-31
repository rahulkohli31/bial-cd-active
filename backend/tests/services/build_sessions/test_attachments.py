"""R3 — `resolve_build_attachments` over the NATIVE store (U4): the ref-collection rule,
the temporal boundary, and the fail-first error paths that keep a build from silently
ignoring a user's file.

Binary attachments live in payloads as `bial-attachment-ref` markers; inline text/office
content needs no materialization here (it is inlined into the prompt content at persist —
U5/U7), so this suite covers what the module still owns: marker collection since the last
build's START, owner-scoped rehydration, and the deck/mismatch/missing refusals.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind
from src.services.build_sessions.attachments import (
    BuildAttachmentError,
    ConversationNotFoundError,
    resolve_build_attachments,
)
from src.services.extract.office import PPTX_MEDIA_TYPE
from src.services.messages.store import dump_for_row
from src.services.storage import attachment_key
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory

PNG_BYTES = bytes([0x89, 0x50, 0x4E, 0x47]) + b"\x00 pretend png"
PDF_BYTES = bytes([0x25, 0x50, 0x44, 0x46]) + b"-1.7 pretend pdf"


async def _owner(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    conv = await ConversationFactory.create(
        db, user.id, project_id=project.id, kind=ChatKind.BUILD
    )
    return user, project, conv


async def _store(
    db: AsyncSession, storage: Any, user_id: uuid.UUID, attachment_id: str, media: str, data: bytes
) -> str:
    """Persist an attachment as the upload route does: bytes in the store, row in the table."""
    key = attachment_key(user_id, uuid.uuid7())
    await storage.put(key, data, content_type=media)
    db.add(
        Attachment(
            user_id=user_id,
            attachment_id=attachment_id,
            media_type=media,
            name="f",
            size=len(data),
            storage_key=key,
        )
    )
    await db.flush()
    return key


def _turn_with_refs(*attachment_ids: str, text: str = "here you go") -> list[Any]:
    """A native user turn carrying binary ref markers (the U4 externalized shape)."""
    content: list[str | BinaryContent] = [text]
    for attachment_id in attachment_ids:
        content.append(
            BinaryContent(
                data=b"\x89PNGplaceholder", media_type="image/png", identifier=attachment_id
            )
        )
    return dump_for_row([ModelRequest(parts=[UserPromptPart(content=content)])])


def _plain_turn(text: str) -> list[Any]:
    return dump_for_row([ModelRequest(parts=[UserPromptPart(content=text)])])


async def _outcome_row(db: AsyncSession, user, conv, *, seq: int, started_seq: int | None) -> None:
    """A build-outcome system row as `write_build_outcome` shapes it (meta-keyed)."""
    meta: dict[str, Any] = {
        "kind": "build_outcome",
        "status": "ended",
        "sessionId": str(uuid.uuid4()),
    }
    if started_seq is not None:
        meta["startedSeq"] = started_seq
    await MessageFactory.create(
        db,
        user.id,
        conv.id,
        seq=seq,
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        payload=dump_for_row([ModelResponse(parts=[TextPart(content="Build finished.")])]),
        meta=meta,
    )


# --- the happy paths ----------------------------------------------------------


async def test_binary_refs_reach_the_prompt(db_session: AsyncSession, fake_storage) -> None:
    user, project, conv = await _owner(db_session, "ba1@rvaiglobal.com")
    await _store(db_session, fake_storage, user.id, "a-img", "image/png", PNG_BYTES)
    await _store(db_session, fake_storage, user.id, "a-pdf", "application/pdf", PDF_BYTES)
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("a-img", "a-pdf")
    )

    content = await resolve_build_attachments(db_session, user.id, project.id, conv.id)
    assert [type(item) for item in content] == [BinaryContent, BinaryContent]
    img, pdf = content
    assert isinstance(img, BinaryContent) and img.data == PNG_BYTES
    assert img.media_type == "image/png"
    assert isinstance(pdf, BinaryContent) and pdf.data == PDF_BYTES
    assert pdf.media_type == "application/pdf"


async def test_no_refs_yields_empty_content(db_session: AsyncSession, fake_storage) -> None:
    user, project, conv = await _owner(db_session, "ba2@rvaiglobal.com")
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_plain_turn("just words")
    )
    assert await resolve_build_attachments(db_session, user.id, project.id, conv.id) == []


async def test_ref_survives_interposed_interview_turns(
    db_session: AsyncSession, fake_storage
) -> None:
    # The file arrives at seq 0; interview Q/A turns follow. Latest-message semantics would
    # drop the file — the boundary rule must not.
    user, project, conv = await _owner(db_session, "ba3@rvaiglobal.com")
    await _store(db_session, fake_storage, user.id, "a-img", "image/png", PNG_BYTES)
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("a-img")
    )
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=1, payload=_plain_turn("Which columns matter?")
    )
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=2, payload=_plain_turn("revenue")
    )

    content = await resolve_build_attachments(db_session, user.id, project.id, conv.id)
    assert len(content) == 1


async def test_collection_starts_after_the_last_build_outcome(
    db_session: AsyncSession, fake_storage
) -> None:
    # The first build consumed `old`; only `new` (appended after) reaches the next build.
    user, project, conv = await _owner(db_session, "ba4@rvaiglobal.com")
    await _store(db_session, fake_storage, user.id, "old", "image/png", PNG_BYTES)
    await _store(db_session, fake_storage, user.id, "new", "image/png", PNG_BYTES)
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("old")
    )
    await _outcome_row(db_session, user, conv, seq=1, started_seq=0)
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=2, payload=_turn_with_refs("new")
    )

    content = await resolve_build_attachments(db_session, user.id, project.id, conv.id)
    assert len(content) == 1
    only = content[0]
    assert isinstance(only, BinaryContent) and only.identifier == "new"


async def test_a_file_attached_while_the_build_ran_reaches_the_next_build(
    db_session: AsyncSession, fake_storage
) -> None:
    # THE TEMPORAL-BOUNDARY CASE: the outcome row (seq 2) lands AFTER a turn the user sent
    # mid-build (seq 1). Its `startedSeq` (0) is the honest bound — `during` must survive.
    user, project, conv = await _owner(db_session, "ba5@rvaiglobal.com")
    await _store(db_session, fake_storage, user.id, "before", "image/png", PNG_BYTES)
    await _store(db_session, fake_storage, user.id, "during", "image/png", PNG_BYTES)
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("before")
    )
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=1, payload=_turn_with_refs("during")
    )
    await _outcome_row(db_session, user, conv, seq=2, started_seq=0)

    content = await resolve_build_attachments(db_session, user.id, project.id, conv.id)
    identifiers = [item.identifier for item in content if isinstance(item, BinaryContent)]
    assert identifiers == ["during"]


async def test_same_attachment_across_turns_is_deduped(
    db_session: AsyncSession, fake_storage
) -> None:
    user, project, conv = await _owner(db_session, "ba6@rvaiglobal.com")
    await _store(db_session, fake_storage, user.id, "dupe", "image/png", PNG_BYTES)
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("dupe")
    )
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=1, payload=_turn_with_refs("dupe")
    )
    content = await resolve_build_attachments(db_session, user.id, project.id, conv.id)
    assert len(content) == 1


async def test_step_and_system_rows_are_not_scanned_for_refs(
    db_session: AsyncSession, fake_storage
) -> None:
    # Only TURN rows carry user files; a ref-shaped marker inside an agent step's payload
    # (e.g. a tool echoing one) must not smuggle a file into the next build.
    user, project, conv = await _owner(db_session, "ba7@rvaiglobal.com")
    await _store(db_session, fake_storage, user.id, "a-img", "image/png", PNG_BYTES)
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        entry_kind=MessageEntryKind.STEP,
        payload=_turn_with_refs("a-img"),
    )
    assert await resolve_build_attachments(db_session, user.id, project.id, conv.id) == []


# --- fail-first error paths ---------------------------------------------------


async def test_cross_user_conversation_is_not_found(db_session: AsyncSession) -> None:
    user, project, _ = await _owner(db_session, "ba8@rvaiglobal.com")
    _other_user, _, other_conv = await _owner(db_session, "ba8-other@rvaiglobal.com")
    with pytest.raises(ConversationNotFoundError):
        await resolve_build_attachments(db_session, user.id, project.id, other_conv.id)


async def test_cross_project_conversation_is_not_found(db_session: AsyncSession) -> None:
    user, _, conv = await _owner(db_session, "ba9@rvaiglobal.com")
    other_project = await ProjectFactory.create(db_session, user.id)
    with pytest.raises(ConversationNotFoundError):
        await resolve_build_attachments(db_session, user.id, other_project.id, conv.id)


async def test_missing_attachment_row_raises(db_session: AsyncSession, fake_storage) -> None:
    user, project, conv = await _owner(db_session, "ba10@rvaiglobal.com")
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("never-uploaded")
    )
    with pytest.raises(BuildAttachmentError):
        await resolve_build_attachments(db_session, user.id, project.id, conv.id)


async def test_bytes_that_no_longer_match_their_type_raise(
    db_session: AsyncSession, fake_storage
) -> None:
    user, project, conv = await _owner(db_session, "ba11@rvaiglobal.com")
    key = await _store(db_session, fake_storage, user.id, "a-img", "image/png", PNG_BYTES)
    fake_storage.objects[key] = b"not a png at all"  # swapped blob under a stale row
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("a-img")
    )
    with pytest.raises(BuildAttachmentError):
        await resolve_build_attachments(db_session, user.id, project.id, conv.id)


async def test_deck_attachment_is_rejected(db_session: AsyncSession, fake_storage) -> None:
    user, project, conv = await _owner(db_session, "ba12@rvaiglobal.com")
    await _store(db_session, fake_storage, user.id, "a-deck", PPTX_MEDIA_TYPE, b"PK\x03\x04deck")
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("a-deck")
    )
    with pytest.raises(BuildAttachmentError, match="PowerPoint"):
        await resolve_build_attachments(db_session, user.id, project.id, conv.id)


async def test_missing_blob_raises(db_session: AsyncSession, fake_storage) -> None:
    user, project, conv = await _owner(db_session, "ba13@rvaiglobal.com")
    key = await _store(db_session, fake_storage, user.id, "a-img", "image/png", PNG_BYTES)
    del fake_storage.objects[key]
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_turn_with_refs("a-img")
    )
    with pytest.raises(BuildAttachmentError, match="no longer available"):
        await resolve_build_attachments(db_session, user.id, project.id, conv.id)
