"""Router-level tests for `POST /projects/{project_id}/discard`: scoping, CSRF, the refusals and
the notice for the conversation the request came from. The discard itself is covered in
`tests/services/build_sessions/test_discard.py`, so the manager is replaced here."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
from src.services.build_sessions import (
    BuildSessionConflictError,
    DiscardOutcome,
    NoLiveSandboxError,
    NothingSavedToGoBackToError,
    SaveState,
)
from src.services.sandbox import SandboxError
from src.services.storage import StorageError
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

SAVED_AT = datetime(2026, 9, 13, 14, 32, tzinfo=UTC)
SAVED = "5" * 40


def _url(project_id: uuid.UUID) -> str:
    return f"/v1/build-sessions/projects/{project_id}/discard"


def _discarded(notes: dict[uuid.UUID, int]) -> DiscardOutcome:
    return DiscardOutcome(
        state=SaveState(app_id=uuid.uuid4(), dirty=False, container_head=SAVED, saved_head=SAVED),
        saved_at=SAVED_AT,
        notes=notes,
    )


async def test_a_discard_answers_the_save_state_and_the_notice_for_its_conversation(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user = await UserFactory.create(db_session, email="disc1@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    other = uuid.uuid4()
    seen: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]] = []

    async def _fake(db, user, project_id, *, sandbox_client, conversation_id) -> DiscardOutcome:
        seen.append((user.id, project_id, conversation_id))
        return _discarded({conversation.id: 7, other: 3})

    wire.manager.discard_unsaved_changes = _fake

    resp = await client.post(
        _url(project.id),
        headers=auth_headers(user),
        json={"conversationId": str(conversation.id)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["dirty"] is False
    assert body["containerHead"] == SAVED
    assert body["savedHead"] == SAVED
    assert body["notice"]["seq"] == 7
    assert datetime.fromisoformat(body["notice"]["savedAt"]) == SAVED_AT
    assert seen == [(user.id, project.id, conversation.id)]


async def test_a_discard_from_outside_a_conversation_carries_no_notice(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user = await UserFactory.create(db_session, email="disc2@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    seen: list[uuid.UUID | None] = []

    async def _fake(db, user, project_id, *, sandbox_client, conversation_id) -> DiscardOutcome:
        seen.append(conversation_id)
        return _discarded({uuid.uuid4(): 3})

    wire.manager.discard_unsaved_changes = _fake

    resp = await client.post(_url(project.id), headers=auth_headers(user), json={})

    assert resp.status_code == 200
    assert resp.json()["notice"] is None
    assert seen == [None]


@pytest.mark.parametrize(
    ("refusal", "message"),
    [
        (
            NoLiveSandboxError(uuid.uuid4()),
            "Your workspace is not running, so there is nothing to discard. "
            "Your saved version is intact.",
        ),
        (BuildSessionConflictError(None), "Wait for the reply to finish, then discard."),
        (
            NothingSavedToGoBackToError(uuid.uuid4()),
            "There is no saved version to go back to yet.",
        ),
    ],
    ids=["no workspace", "reply running", "nothing saved"],
)
async def test_a_refused_discard_is_409_in_words_the_page_shows(
    client: AsyncClient, db_session: AsyncSession, wire, refusal: Exception, message: str
) -> None:
    user = await UserFactory.create(
        db_session, email=f"disc3-{uuid.uuid4().hex[:6]}@rvaiglobal.com"
    )
    project = await ProjectFactory.create(db_session, user.id)

    async def _fake(db, user, project_id, *, sandbox_client, conversation_id) -> DiscardOutcome:
        raise refusal

    wire.manager.discard_unsaved_changes = _fake

    resp = await client.post(_url(project.id), headers=auth_headers(user), json={})

    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == message


@pytest.mark.parametrize(
    "failure",
    [StorageError("blob is having a day"), SandboxError("exec timed out")],
    ids=["store", "sandbox"],
)
async def test_a_discard_that_could_not_finish_is_503(
    client: AsyncClient, db_session: AsyncSession, wire, failure: Exception
) -> None:
    user = await UserFactory.create(
        db_session, email=f"disc4-{uuid.uuid4().hex[:6]}@rvaiglobal.com"
    )
    project = await ProjectFactory.create(db_session, user.id)

    async def _fake(db, user, project_id, *, sandbox_client, conversation_id) -> DiscardOutcome:
        raise failure

    wire.manager.discard_unsaved_changes = _fake

    resp = await client.post(_url(project.id), headers=auth_headers(user), json={})

    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == (
        "Your changes could not be discarded just now. Try again in a moment."
    )


async def test_another_users_project_is_404_before_anything_is_discarded(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    owner = await UserFactory.create(db_session, email="disc5-owner@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, owner.id)
    intruder = await UserFactory.create(db_session, email="disc5-intruder@rvaiglobal.com")
    seen: list[uuid.UUID] = []

    async def _fake(db, user, project_id, *, sandbox_client, conversation_id) -> DiscardOutcome:
        seen.append(project_id)
        return _discarded({})

    wire.manager.discard_unsaved_changes = _fake

    resp = await client.post(_url(project.id), headers=auth_headers(intruder), json={})

    assert resp.status_code == 404
    assert seen == []


@pytest.mark.parametrize("whose", ["another project", "another user"])
async def test_a_conversation_that_is_not_this_projects_is_404_before_anything_is_discarded(
    client: AsyncClient, db_session: AsyncSession, wire, whose: str
) -> None:
    user = await UserFactory.create(db_session, email=f"disc6-{whose[-4:]}@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    if whose == "another project":
        conversation = await ConversationFactory.create(db_session, user.id)
    else:
        stranger = await UserFactory.create(
            db_session, email=f"disc6-stranger-{whose[-4:]}@rvaiglobal.com"
        )
        conversation = await ConversationFactory.create(db_session, stranger.id)
    seen: list[uuid.UUID] = []

    async def _fake(db, user, project_id, *, sandbox_client, conversation_id) -> DiscardOutcome:
        seen.append(project_id)
        return _discarded({})

    wire.manager.discard_unsaved_changes = _fake

    resp = await client.post(
        _url(project.id),
        headers=auth_headers(user),
        json={"conversationId": str(conversation.id)},
    )

    assert resp.status_code == 404
    assert seen == []


async def test_a_discard_without_csrf_is_403(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user = await UserFactory.create(db_session, email="disc7@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(
        _url(project.id), headers=auth_headers(user, with_csrf=False), json={}
    )

    assert resp.status_code == 403


async def test_a_discard_with_no_sandbox_configured_is_503(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user = await UserFactory.create(db_session, email="disc8@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: None

    resp = await client.post(_url(project.id), headers=auth_headers(user), json={})

    assert resp.status_code == 503
