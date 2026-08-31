"""R3 at the HTTP boundary — `conversationId` on start over the NATIVE store (U4): the
404/422 matrix, the back-compat text-only path, and the invariant that a rejected attachment
costs the user NOTHING (no sandbox, no held lock)."""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import run_build_dependency
from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind
from src.services.messages.store import dump_for_row
from src.services.storage import attachment_key
from tests.api.v1.build_sessions.conftest import auth_headers, drain
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory
from tests.fakes import FakeBrain

PNG_BYTES = bytes([0x89, 0x50, 0x4E, 0x47]) + b"\x00 pretend png"
PPTX_MEDIA = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


async def _owner(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    conv = await ConversationFactory.create(
        db, user.id, project_id=project.id, kind=ChatKind.BUILD
    )
    return user, project, conv


def _ref_turn(*attachment_ids: str) -> list[Any]:
    """A native user turn whose binaries are ref markers (the stored U4 shape)."""
    content: list[str | BinaryContent] = ["build from these"]
    for attachment_id in attachment_ids:
        content.append(
            BinaryContent(data=b"\x89PNGx", media_type="image/png", identifier=attachment_id)
        )
    return dump_for_row([ModelRequest(parts=[UserPromptPart(content=content)])])


async def _upload(
    db: AsyncSession,
    storage: Any,
    user_id: uuid.UUID,
    attachment_id: str,
    *,
    media: str = "image/png",
    data: bytes = PNG_BYTES,
    name: str = "chart.png",
) -> str:
    key = attachment_key(user_id, uuid.uuid7())
    await storage.put(key, data, content_type=media)
    db.add(
        Attachment(
            user_id=user_id,
            attachment_id=attachment_id,
            media_type=media,
            name=name,
            size=len(data),
            storage_key=key,
        )
    )
    await db.flush()
    return key


async def _start(client: AsyncClient, user, project, **extra: Any):
    body: dict[str, Any] = {"projectId": str(project.id), "prompt": "build me an app"}
    body.update(extra)
    return await client.post("/v1/build-sessions", json=body, headers=auth_headers(user))


# --- happy paths ------------------------------------------------------------


async def test_start_with_conversation_materializes_attachments(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project, conv = await _owner(db_session, "sa1@rvaiglobal.com")
    await _upload(db_session, fake_storage, user.id, "a-img")
    await MessageFactory.create(db_session, user.id, conv.id, seq=0, payload=_ref_turn("a-img"))

    resp = await _start(client, user, project, conversationId=str(conv.id))

    assert resp.status_code == 201
    session = wire.manager.get(uuid.UUID(resp.json()["sessionId"]))
    assert session is not None
    assert len(session.attachments) == 1
    binary = session.attachments[0]
    assert isinstance(binary, BinaryContent) and binary.data == PNG_BYTES
    await drain(wire.manager, resp.json()["sessionId"])


async def test_start_without_conversation_is_unchanged_and_text_only(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Back-compat: the field is optional; omitting it must behave exactly as before R3."""
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project, _ = await _owner(db_session, "sa2@rvaiglobal.com")

    resp = await _start(client, user, project)

    assert resp.status_code == 201
    session = wire.manager.get(uuid.UUID(resp.json()["sessionId"]))
    assert session is not None
    assert session.attachments == []
    await drain(wire.manager, resp.json()["sessionId"])


async def test_thread_with_no_refs_starts_text_only(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project, conv = await _owner(db_session, "sa3@rvaiglobal.com")
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        payload=dump_for_row([ModelRequest(parts=[UserPromptPart(content="no files here")])]),
    )

    resp = await _start(client, user, project, conversationId=str(conv.id))

    assert resp.status_code == 201
    session = wire.manager.get(uuid.UUID(resp.json()["sessionId"]))
    assert session is not None and session.attachments == []
    await drain(wire.manager, resp.json()["sessionId"])


# --- 404 arms (scoping) -----------------------------------------------------


async def test_foreign_users_conversation_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    _, _, victim_conv = await _owner(db_session, "victim@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="intruder2@rvaiglobal.com")
    intruder_project = await ProjectFactory.create(db_session, intruder.id)

    resp = await _start(client, intruder, intruder_project, conversationId=str(victim_conv.id))

    assert resp.status_code == 404
    assert wire.sbx.provisioned == []


async def test_conversation_from_another_project_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Same user, wrong project — still a non-leaking 404: a build must never be grounded in
    another project's files."""
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, _, conv = await _owner(db_session, "sa4@rvaiglobal.com")
    other_project = await ProjectFactory.create(db_session, user.id)

    resp = await _start(client, user, other_project, conversationId=str(conv.id))

    assert resp.status_code == 404
    assert wire.sbx.provisioned == []


# --- 422 arms (fail-first) --------------------------------------------------


async def test_missing_attachment_row_is_422_with_no_sandbox(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The invariant that makes fail-first cheap: the rejection lands BEFORE the lock and the
    container, so nothing is provisioned and nothing needs compensating."""
    from src.services.build_sessions.locks import lock_is_held

    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project, conv = await _owner(db_session, "sa5@rvaiglobal.com")
    # A ref whose attachment row was never uploaded (or was reclaimed).
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, payload=_ref_turn("never-uploaded")
    )

    resp = await _start(client, user, project, conversationId=str(conv.id))

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "build_attachment_unusable"
    assert wire.sbx.provisioned == []  # no sandbox
    assert wire.sbx.torn_down == []  # and none to tear down
    assert not await lock_is_held(fake_redis, user.id)  # no lock left behind
    assert wire.manager.active_session_for(user.id) is None


async def test_magic_byte_mismatch_is_422(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project, conv = await _owner(db_session, "sa6@rvaiglobal.com")
    await _upload(
        db_session, fake_storage, user.id, "a-img", data=b"definitely not a png", name="chart.png"
    )
    await MessageFactory.create(db_session, user.id, conv.id, seq=0, payload=_ref_turn("a-img"))

    resp = await _start(client, user, project, conversationId=str(conv.id))

    assert resp.status_code == 422
    assert "chart.png" in resp.json()["error"]["message"]
    assert wire.sbx.provisioned == []


async def test_deck_attachment_is_422_decks_not_supported(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project, conv = await _owner(db_session, "sa7@rvaiglobal.com")
    await _upload(
        db_session,
        fake_storage,
        user.id,
        "a-ppt",
        media=PPTX_MEDIA,
        data=b"PK\x03\x04deck",
        name="pitch.pptx",
    )
    await MessageFactory.create(db_session, user.id, conv.id, seq=0, payload=_ref_turn("a-ppt"))

    resp = await _start(client, user, project, conversationId=str(conv.id))

    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert "pitch.pptx" in message and "not supported" in message
    assert wire.sbx.provisioned == []
