"""DELETE /v1/conversations/{id} — delete-with-cleanup over the NATIVE message store (U4).

Carried forward from the retired append/delete suite: the delete flow survives the reset,
but attachment discovery now walks native payloads for `bial-attachment-ref` markers (the
externalized-binary shape) instead of the legacy SPA `file` parts.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart
from sqlalchemy import select

from src.config import settings
from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, MessageFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_PNG = bytes([0x89, 0x50, 0x4E, 0x47]) + b"body"


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    return {"Cookie": f"session={jwt}"}, user


async def test_delete_sweeps_attachments(client, db_session, fake_storage) -> None:
    from src.services.messages.store import dump_for_row

    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    key = f"att/{user.id}/att_swept"
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id="att_swept",
            media_type="image/png",
            name="pic.png",
            size=len(_PNG),
            storage_key=key,
        )
    )
    fake_storage.objects[key] = _PNG
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        payload=dump_for_row(
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content=[
                                "use this",
                                BinaryContent(
                                    data=_PNG, media_type="image/png", identifier="att_swept"
                                ),
                            ]
                        )
                    ]
                )
            ]
        ),
    )

    resp = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Conversation + its messages gone (cascade), attachment row + object swept.
    assert await db_session.scalar(select(Conversation).where(Conversation.id == conv.id)) is None
    assert (
        await db_session.execute(select(Message).where(Message.conversation_id == conv.id))
    ).scalars().all() == []
    assert (
        await db_session.scalar(select(Attachment).where(Attachment.attachment_id == "att_swept"))
        is None
    )
    assert fake_storage.objects == {}


async def test_delete_cross_user_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id)
    resp = await client.delete(f"/v1/conversations/{theirs.id}", headers=headers)
    assert resp.status_code == 404
    # Untouched.
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == theirs.id))
        is not None
    )


@pytest.mark.filterwarnings("ignore:transaction already deassociated:sqlalchemy.exc.SAWarning")
async def test_patch_losing_race_to_delete_is_404_not_500(client, db_session, monkeypatch) -> None:
    # (the filtered SAWarning is a test-harness artifact: the endpoint's failed commit
    # plus the fixture's outer rollback double-clean the same connection)
    # Builder auto-save PATCH vs a concurrent delete: the deleted row makes the flush
    # UPDATE match zero rows (StaleDataError) — the loser must get the same non-leaking
    # 404 a one-second-later PATCH would, never a 500.
    import src.api.v1.conversations.router as conv_router

    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)

    real_load = conv_router._load_owned

    async def _load_then_lose_race(db, user_id, conversation_id):
        owned = await real_load(db, user_id, conversation_id)
        # The concurrent DELETE commits between our load and our flush.
        # synchronize_session=False: a REAL concurrent delete happens in another
        # session, so THIS session's identity map must not learn about it — the
        # flush then emits the zero-row UPDATE exactly as in production.
        await db.execute(
            sa.delete(Conversation).where(Conversation.id == owned.id),
            execution_options={"synchronize_session": False},
        )
        return owned

    monkeypatch.setattr(conv_router, "_load_owned", _load_then_lose_race)
    resp = await client.patch(f"/v1/conversations/{conv.id}", json={"title": "T"}, headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}


async def test_delete_invalid_id_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.delete("/v1/conversations/bad!id", headers=headers)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid conversation id."}}
