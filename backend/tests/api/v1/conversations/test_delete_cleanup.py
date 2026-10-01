"""DELETE /v1/conversations/{id} — delete-with-cleanup over the NATIVE message store.

Attachment discovery walks native payloads for `bial-attachment-ref` markers (the
externalized-binary shape), not the legacy SPA `file` parts.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import httpx
import pytest
import sqlalchemy as sa
from pydantic_ai.messages import BinaryContent, ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from src.config import settings
from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from src.services.conversations import gather_and_delete_conversation
from src.services.messages.store import load_history
from src.services.turns.engine import _TurnState
from src.services.turns.guard import (
    claim_conversation,
    conversation_is_mid_reply,
    release_conversation,
)
from tests.api.v1.conversations.conftest import _headers
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    MessageFactory,
    ProjectFactory,
    UserFactory,
)

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
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == theirs.id))
        is not None
    )


async def test_patch_losing_race_to_delete_is_404_not_500(client, db_session) -> None:
    # Builder auto-save PATCH vs a concurrent delete: the write is one owner-scoped UPDATE, so a
    # delete that lands first leaves it matching zero rows. Either body shape must get the same
    # non-leaking 404 a one-second-later PATCH would, never a 500.
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    await db_session.execute(sa.delete(Conversation).where(Conversation.id == conv.id))

    for body in ({"title": "T"}, {"context": {"step": 1}}):
        resp = await client.patch(f"/v1/conversations/{conv.id}", json=body, headers=headers)
        assert resp.status_code == 404, body
        assert resp.json() == {"error": {"message": "Conversation not found."}}, body


async def test_delete_invalid_id_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.delete("/v1/conversations/bad!id", headers=headers)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid conversation id."}}


async def _delete(client, headers, conversation_id: uuid.UUID) -> int:
    resp = await client.delete(f"/v1/conversations/{conversation_id}", headers=headers)
    return resp.status_code


async def _exists(db_session, conversation_id: uuid.UUID) -> bool:
    found = await db_session.scalar(
        sa.select(Conversation.id).where(Conversation.id == conversation_id)
    )
    return found is not None


async def _message_count(db_session, conversation_id: uuid.UUID) -> int:
    return int(
        await db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        or 0
    )


async def test_an_idle_chat_is_deleted_with_its_messages(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    await MessageFactory.create(db_session, user.id, conv.id, seq=0)

    resp = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)

    assert resp.status_code == 200
    assert not await _exists(db_session, conv.id)
    assert await _message_count(db_session, conv.id) == 0


async def test_deleting_an_unknown_or_already_deleted_chat_is_404(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)

    assert await _delete(client, headers, conv.id) == 200
    again = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)
    unknown = await client.delete(f"/v1/conversations/{uuid.uuid4()}", headers=headers)

    assert again.status_code == 404
    assert again.json() == {"error": {"message": "Conversation not found."}}
    assert unknown.status_code == 404


async def test_another_users_delete_is_404_and_the_owners_succeeds(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id)
    await MessageFactory.create(db_session, other.id, theirs.id, seq=0)

    refused = await client.delete(f"/v1/conversations/{theirs.id}", headers=headers)

    assert refused.status_code == 404
    assert await _exists(db_session, theirs.id)
    assert await _message_count(db_session, theirs.id) == 1
    owner = _headers(other, with_csrf=False)
    assert await _delete(client, owner, theirs.id) == 200
    assert not await _exists(db_session, theirs.id)


async def test_a_generic_chat_deletes_like_a_project_chat(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)
    await MessageFactory.create(db_session, user.id, conv.id, seq=0, kind=ChatKind.GENERIC)

    resp = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)

    assert resp.status_code == 200
    assert not await _exists(db_session, conv.id)
    assert await _message_count(db_session, conv.id) == 0


# --- a chat that is still running ---------------------------------------------------------

_RUNNING = {
    "error": {
        "message": "This chat is still running. Stop it first, then delete it.",
        "code": "conversation_running",
    }
}


async def _post_turn(client, headers, conv, text: str = "hello"):
    return await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=headers,
        json={"message": {"text": text, "attachmentTexts": [], "attachmentIds": []}},
    )


async def _settle(engine, conversation_id: uuid.UUID) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _until_mid_turn(state: _TurnState) -> None:
    async def _first_block() -> None:
        while not state.text_blocks():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_first_block(), timeout=10)


async def _one_word(messages: list[ModelMessage], info: AgentInfo):
    yield "ok"


@pytest.mark.usefixtures("_override_billing")
async def test_a_running_chat_is_refused_until_it_is_stopped(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "working "
        await gate.wait()
        yield "never"

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(FunctionModel(stream_function=_stall))
    headers = _headers(user)
    turn_id = (await _post_turn(client, headers, conv)).json()["turnId"]
    state = _fresh_engine.peek(conv.id)
    assert state is not None
    # Mid-turn, and past the user message's flush: the app and fixtures share one session.
    await _until_mid_turn(state)

    refused = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)

    assert refused.status_code == 409
    assert refused.json() == _RUNNING
    assert await _exists(db_session, conv.id)
    assert await _message_count(db_session, conv.id) >= 1
    assert _fresh_engine.active_turn_info(conv.id) is not None

    stop = await client.post(f"/v1/conversations/{conv.id}/turns/{turn_id}/stop", headers=headers)
    assert stop.status_code == 200
    await _settle(_fresh_engine, conv.id)

    deleted = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)
    assert deleted.status_code == 200
    assert not await _exists(db_session, conv.id)
    assert await _message_count(db_session, conv.id) == 0


@pytest.mark.usefixtures("_override_billing")
async def test_another_users_delete_of_a_running_chat_is_404_not_409(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The ownership check runs before the running check, so a running chat is no existence
    oracle: the stranger gets the same 404 as for an unknown id, the owner gets the 409."""
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "working "
        await gate.wait()
        yield "never"

    owner = await UserFactory.create(db_session)
    stranger = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, owner.id, kind=ChatKind.PLAN)
    set_chat_model(FunctionModel(stream_function=_stall))
    assert (await _post_turn(client, _headers(owner), conv)).status_code == 202
    state = _fresh_engine.peek(conv.id)
    assert state is not None
    await _until_mid_turn(state)

    refused = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(stranger))
    owners = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(owner))

    assert refused.status_code == 404
    assert refused.json() == {"error": {"message": "Conversation not found."}}
    assert owners.status_code == 409
    assert owners.json() == _RUNNING
    assert await _exists(db_session, conv.id)
    gate.set()
    await _settle(_fresh_engine, conv.id)


async def test_a_chat_claimed_for_a_reply_is_refused(client, db_session, _fresh_engine) -> None:
    """The claim alone, with no turn registered yet: the window between a send claiming the
    chat and its run being recorded."""
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    claim_conversation(conv.id)
    try:
        refused = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)
    finally:
        release_conversation(conv.id)

    assert refused.status_code == 409
    assert refused.json() == _RUNNING
    assert await _exists(db_session, conv.id)
    assert await _delete(client, headers, conv.id) == 200


async def test_a_chat_with_a_running_turn_is_refused_even_without_the_claim(
    client, db_session, _fresh_engine
) -> None:
    """The registry alone, with the claim already let go: the two answers are read separately,
    so either one refuses."""
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    _fresh_engine._by_conversation[conv.id] = _TurnState(
        turn_id=uuid.uuid7(), conversation_id=conv.id, user_id=user.id, kind=ChatKind.BUILD
    )

    refused = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)

    assert refused.status_code == 409
    assert refused.json() == _RUNNING
    assert await _exists(db_session, conv.id)


async def test_a_refused_delete_lets_go_of_the_chat(client, db_session, _fresh_engine) -> None:
    """The delete holds the claim while it works, so a send meanwhile gets the busy answer —
    and lets go on every exit, or the chat would answer busy for the life of the process."""
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    _fresh_engine._by_conversation[conv.id] = _TurnState(
        turn_id=uuid.uuid7(), conversation_id=conv.id, user_id=user.id, kind=ChatKind.BUILD
    )

    assert await _delete(client, headers, conv.id) == 409

    assert not conversation_is_mid_reply(conv.id)


@pytest.mark.usefixtures("_override_billing")
async def test_a_send_during_a_delete_gets_the_busy_answer(
    client, db_session, set_chat_model, monkeypatch, _fresh_engine
) -> None:
    """Characterizes the half of the race the claim closes: while the delete is working, the
    chat is claimed, so a send is refused before anything is written."""
    import src.api.v1.conversations.router as conv_router

    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    set_chat_model(FunctionModel(stream_function=_one_word))
    real_delete = gather_and_delete_conversation
    seen: dict[str, object] = {}

    async def _send_arrives_mid_delete(db, conversation, *, user_id):
        seen["send"] = await _post_turn(client, _headers(user), conv, text="still there?")
        return await real_delete(db, conversation, user_id=user_id)

    monkeypatch.setattr(conv_router, "gather_and_delete_conversation", _send_arrives_mid_delete)

    deleted = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)

    assert deleted.status_code == 200
    send = seen["send"]
    assert isinstance(send, httpx.Response)
    assert send.status_code == 409
    assert send.json() == {
        "error": {"message": "A turn is already running for this conversation."}
    }
    assert not await _exists(db_session, conv.id)
    assert await _message_count(db_session, conv.id) == 0


@pytest.mark.usefixtures("_override_billing")
async def test_a_send_that_loaded_the_chat_before_a_delete_leaves_no_orphan(
    client, db_session, set_chat_model, monkeypatch, _fresh_engine
) -> None:
    """Characterizes the half of the race the claim cannot close: the send read the chat, the
    delete finished, then the send tried to write. Single-replica best effort means this is
    answered at the write, and the answer must be a clean refusal with nothing left behind."""
    import src.api.v1.conversations.turns as turns_module

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(FunctionModel(stream_function=_one_word))
    real_load_history = load_history

    async def _deleted_while_the_send_was_reading(db, **kwargs):
        resp = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(user))
        assert resp.status_code == 200
        return await real_load_history(db, **kwargs)

    monkeypatch.setattr(turns_module, "load_history", _deleted_while_the_send_was_reading)

    send = await _post_turn(client, _headers(user), conv)

    assert send.status_code == 404
    assert send.json() == {"error": {"message": "Conversation not found."}}
    assert not await _exists(db_session, conv.id)
    assert await _message_count(db_session, conv.id) == 0
    assert _fresh_engine.peek(conv.id) is None
    assert not conversation_is_mid_reply(conv.id)


# --- deleting one chat leaves the application and its other chat ---------------------------


async def _application_reads(client, headers, project_id: uuid.UUID) -> tuple[dict, dict]:
    project = await client.get(f"/v1/projects/{project_id}", headers=headers)
    deployment = await client.get(f"/v1/projects/{project_id}/deployment", headers=headers)
    assert project.status_code == 200, project.text
    assert deployment.status_code == 200, deployment.text
    return project.json(), deployment.json()


async def _listed(client, headers, project_id: uuid.UUID) -> list[str]:
    resp = await client.get(f"/v1/conversations?projectId={project_id}", headers=headers)
    assert resp.status_code == 200
    return [c["_id"] for c in resp.json()["conversations"]]


@pytest.fixture
def _deployment_reads_the_fake_store(app, fake_storage) -> None:
    from src.api.deps import storage_or_none_dependency

    app.dependency_overrides[storage_or_none_dependency] = lambda: fake_storage


@pytest.mark.usefixtures("_deployment_reads_the_fake_store")
async def test_deleting_the_plan_chat_then_the_build_chat_leaves_the_application_reading_alike(
    client, db_session
) -> None:
    """The application's soft head pointer names the build chat, and the plan chat holds the
    offer it was built from; nothing links the two chats, so either can go without the other or
    the application noticing."""
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    plan = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )
    build = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.BUILD
    )
    await MessageFactory.create(db_session, user.id, plan.id, seq=0, kind=ChatKind.PLAN)
    await MessageFactory.create(db_session, user.id, build.id, seq=0)
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, conversation_id=build.id
    )
    before = await _application_reads(client, headers, project.id)

    assert await _delete(client, headers, plan.id) == 200

    assert await _listed(client, headers, project.id) == [str(build.id)]
    reopened = await client.get(f"/v1/conversations/{build.id}", headers=headers)
    assert reopened.status_code == 200
    assert reopened.json()["projection"] != []
    assert await _application_reads(client, headers, project.id) == before

    assert await _delete(client, headers, build.id) == 200

    assert await _listed(client, headers, project.id) == []
    assert await _application_reads(client, headers, project.id) == before
