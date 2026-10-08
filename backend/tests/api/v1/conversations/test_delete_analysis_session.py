"""Deleting a chat takes its analysis session out of Azure at once, best-effort."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
import structlog.testing

from src.db.models.conversation import ChatKind, Conversation
from src.services.analysis import placement
from src.services.analysis import runtime as analysis_runtime
from src.services.turns.guard import claim_conversation, release_conversation
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeAnalysisRuntime


def _record(conversation_id: uuid.UUID) -> None:
    placement._records[conversation_id] = placement._Record(files={}, touched=1e12)


async def test_deleting_a_chat_deletes_its_session_once_after_the_commit(
    client, db_session, fake_analysis: FakeAnalysisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)
    _record(conv.id)
    row_when_deleted: list[int] = []
    delete_session = fake_analysis.delete_session

    async def _delete_session(session_id: str) -> None:
        row_when_deleted.append(
            await db_session.scalar(sa.select(sa.func.count()).where(Conversation.id == conv.id))
        )
        await delete_session(session_id)

    monkeypatch.setattr(fake_analysis, "delete_session", _delete_session)

    deleted = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(user))

    assert deleted.status_code == 200
    assert fake_analysis.operations("delete_session") == [conv.id.hex]
    assert row_when_deleted == [0]
    assert conv.id not in placement._records


async def test_deleting_one_chat_deletes_only_its_own_session(
    client, db_session, fake_analysis: FakeAnalysisRuntime
) -> None:
    user = await UserFactory.create(db_session)
    kept = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)
    gone = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)
    _record(kept.id)

    await client.delete(f"/v1/conversations/{gone.id}", headers=_headers(user))

    assert fake_analysis.operations("delete_session") == [gone.id.hex]
    assert kept.id in placement._records
    placement.forget(kept.id)


async def test_a_failed_session_delete_still_deletes_the_chat_and_warns(
    client, db_session, fake_analysis: FakeAnalysisRuntime
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)
    fake_analysis.unavailable = True

    with structlog.testing.capture_logs() as logs:
        deleted = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(user))

    assert deleted.status_code == 200
    warned = [log for log in logs if log["event"] == "analysis_session_delete_failed"]
    assert [log["conversation_id"] for log in warned] == [str(conv.id)]
    assert warned[0]["error"] == "AnalysisUnavailableError"


async def test_a_session_delete_that_never_answers_is_abandoned_at_its_deadline(
    client, db_session, fake_analysis: FakeAnalysisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.v1.conversations import router

    monkeypatch.setattr(router, "ANALYSIS_DELETE_DEADLINE_S", 0.05)
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)

    async def _hang(_session_id: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_analysis, "delete_session", _hang)

    with structlog.testing.capture_logs() as logs:
        deleted = await asyncio.wait_for(
            client.delete(f"/v1/conversations/{conv.id}", headers=_headers(user)), timeout=5
        )

    assert deleted.status_code == 200
    assert [log["error"] for log in logs if log["event"] == "analysis_session_delete_failed"] == [
        "TimeoutError"
    ]


async def test_a_chat_still_running_is_refused_without_touching_its_session(
    client, db_session, fake_analysis: FakeAnalysisRuntime
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)
    claim_conversation(conv.id)
    try:
        refused = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(user))
    finally:
        release_conversation(conv.id)

    assert refused.status_code == 409
    assert fake_analysis.calls == []


async def test_a_stranger_cannot_delete_a_chats_session(
    client, db_session, fake_analysis: FakeAnalysisRuntime
) -> None:
    owner = await UserFactory.create(db_session)
    stranger = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, owner.id, kind=ChatKind.GENERIC)

    refused = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(stranger))

    assert refused.status_code == 404
    assert fake_analysis.calls == []


async def test_with_no_analysis_configured_a_delete_builds_no_runtime(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)

    deleted = await client.delete(f"/v1/conversations/{conv.id}", headers=_headers(user))

    assert deleted.status_code == 200
    assert analysis_runtime._runtime_singleton is None
