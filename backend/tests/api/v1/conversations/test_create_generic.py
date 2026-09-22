"""POST /v1/conversations for the kind that has no project, and what the header says about it.

The parentage rule is a BICONDITIONAL and both directions are refused here, above the database
constraint that carries the same rule — two guards, two failures, and this file drives the
validator half. Its companion `tests/db/test_chat_kind_generic.py` drives the database half.

THE HEADER ASSERTIONS READ THE SERIALIZED BODY, never the model attribute. The attribute is
already correct in the exact case that ships the defect: an absent project stringified into the
five-character `"None"`, which the response model accepts and the SPA resolves into a breadcrumb
for a project that does not exist.
"""

from __future__ import annotations

import uuid

import pytest

from src.config import settings
from src.db.models.conversation import ChatKind, Conversation
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _headers(user) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


def _generic_body(conversation_id: str | None = None, **extra) -> dict:
    return {"id": conversation_id or str(uuid.uuid4()), "kind": "generic", **extra}


async def test_a_generic_conversation_is_created_with_no_project_key_at_all(
    client, db_session
) -> None:
    user = await UserFactory.create(db_session)
    conversation_id = str(uuid.uuid4())

    resp = await client.post(
        "/v1/conversations", headers=_headers(user), json=_generic_body(conversation_id)
    )

    assert resp.status_code == 201, resp.text
    header = resp.json()["conversation"]
    assert header["_id"] == conversation_id
    assert header["kind"] == "generic"
    # A REAL ABSENCE on the wire, asserted on the serialized body: `"None"` is what a
    # stringified `None` looks like, and it would pass every assertion phrased about the model.
    assert header["projectId"] is None


async def test_the_detail_read_carries_the_same_real_absence(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conversation_id = str(uuid.uuid4())
    await client.post(
        "/v1/conversations", headers=_headers(user), json=_generic_body(conversation_id)
    )

    resp = await client.get(f"/v1/conversations/{conversation_id}", headers=_headers(user))

    assert resp.status_code == 200, resp.text
    header = resp.json()["conversation"]
    assert header["projectId"] is None
    assert header["kind"] == "generic"
    # Paired with a liveness assertion: a body that failed to build would satisfy the absence
    # above by being absent altogether.
    assert resp.json()["projection"] == []


async def test_the_created_row_reloads_as_generic_with_no_parent(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conversation_id = uuid.uuid4()

    resp = await client.post(
        "/v1/conversations", headers=_headers(user), json=_generic_body(str(conversation_id))
    )
    assert resp.status_code == 201, resp.text

    row = await db_session.get(Conversation, conversation_id)
    assert row is not None
    assert row.kind is ChatKind.GENERIC
    assert row.project_id is None


@pytest.mark.parametrize("kind", ["plan", "build"])
async def test_a_project_bearing_kind_with_no_project_is_refused(
    client, db_session, kind: str
) -> None:
    user = await UserFactory.create(db_session)

    resp = await client.post(
        "/v1/conversations",
        headers=_headers(user),
        json={"id": str(uuid.uuid4()), "kind": kind},
    )

    assert resp.status_code == 422, resp.text


async def test_a_generic_conversation_that_names_a_project_is_refused(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(
        "/v1/conversations",
        headers=_headers(user),
        json=_generic_body(projectId=str(project.id)),
    )

    assert resp.status_code == 422, resp.text


async def test_re_posting_the_same_generic_id_is_idempotent(client, db_session) -> None:
    """Two absences compare equal, so the existing parentage check needs nothing added for the
    kind that has none."""
    user = await UserFactory.create(db_session)
    conversation_id = str(uuid.uuid4())
    body = _generic_body(conversation_id, title="Visitor pass query")

    first = await client.post("/v1/conversations", headers=_headers(user), json=body)
    second = await client.post("/v1/conversations", headers=_headers(user), json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["conversation"]["_id"] == conversation_id
    assert second.json()["conversation"]["projectId"] is None


async def test_re_posting_a_generic_id_as_a_plan_chat_is_a_conflict(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation_id = str(uuid.uuid4())
    await client.post(
        "/v1/conversations", headers=_headers(user), json=_generic_body(conversation_id)
    )

    resp = await client.post(
        "/v1/conversations",
        headers=_headers(user),
        json={"id": conversation_id, "projectId": str(project.id), "kind": "plan"},
    )

    assert resp.status_code == 409, resp.text


async def test_another_citizen_is_told_the_generic_conversation_does_not_exist(
    client, db_session
) -> None:
    """★ A cross-user id resolves as ABSENT, never as forbidden — the same single non-leaking
    answer every other conversation read gives (ADR-0004)."""
    owner = await UserFactory.create(db_session)
    stranger = await UserFactory.create(db_session)
    conversation_id = str(uuid.uuid4())
    created = await client.post(
        "/v1/conversations", headers=_headers(owner), json=_generic_body(conversation_id)
    )
    assert created.status_code == 201, created.text

    resp = await client.get(f"/v1/conversations/{conversation_id}", headers=_headers(stranger))

    assert resp.status_code == 404, resp.text
    # Paired with a liveness assertion: the owner can still read it, so the 404 above is
    # scoping rather than a conversation that failed to be created at all.
    still_there = await client.get(f"/v1/conversations/{conversation_id}", headers=_headers(owner))
    assert still_there.status_code == 200, still_there.text


async def test_a_generic_conversation_is_not_in_a_project_scoped_list(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    generic_id = str(uuid.uuid4())
    await client.post("/v1/conversations", headers=_headers(user), json=_generic_body(generic_id))
    await client.post(
        "/v1/conversations",
        headers=_headers(user),
        json={"id": str(uuid.uuid4()), "projectId": str(project.id), "kind": "plan"},
    )

    scoped = await client.get(f"/v1/conversations?projectId={project.id}", headers=_headers(user))
    unscoped = await client.get("/v1/conversations?kind=generic", headers=_headers(user))

    assert generic_id not in [c["_id"] for c in scoped.json()["conversations"]]
    # The liveness half: the project scope is not simply empty, and the generic chat is
    # reachable by its own kind filter.
    assert len(scoped.json()["conversations"]) == 1
    assert [c["_id"] for c in unscoped.json()["conversations"]] == [generic_id]
