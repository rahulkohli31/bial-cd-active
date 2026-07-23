"""POST /v1/conversations — create the row BEFORE the first turn (U7).

The stateless relay 404s unknown conversations, so the SPA creates the row it minted, then
streams. The contract under test: 201 on create, idempotent 200 on the same mint, one
non-leaking 409 for any id that exists under different ownership/parentage, 404 for a
project the caller does not own, CSRF enforced.
"""

from __future__ import annotations

import uuid

from src.config import settings
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _headers(user, *, with_csrf: bool = True) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


def _body(project, conversation_id: str | None = None, **extra) -> dict:
    return {
        "id": conversation_id or str(uuid.uuid4()),
        "projectId": str(project.id),
        "kind": "planning",
        **extra,
    }


async def test_create_returns_201_with_the_header(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation_id = str(uuid.uuid4())

    resp = await client.post(
        "/v1/conversations",
        headers=_headers(user),
        json=_body(project, conversation_id, title="Gate tracker", context={"theme": "dark"}),
    )
    assert resp.status_code == 201, resp.text
    header = resp.json()["conversation"]
    assert header["_id"] == conversation_id
    assert header["projectId"] == str(project.id)
    assert header["kind"] == "planning"
    assert header["mode"]  # the server-owned sticky mode rides the header from birth
    assert header["title"] == "Gate tracker"
    assert header["context"] == {"theme": "dark"}


async def test_create_honors_the_requested_mode_and_defaults_to_plan(client, db_session) -> None:
    """U13: the root box mints Ask/Plan/Write chats — `mode` on create sticks; omitted
    keeps the server default ('plan'), so older callers are untouched."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    asked = await client.post(
        "/v1/conversations", headers=_headers(user), json=_body(project, mode="ask")
    )
    assert asked.status_code == 201, asked.text
    assert asked.json()["conversation"]["mode"] == "ask"

    defaulted = await client.post("/v1/conversations", headers=_headers(user), json=_body(project))
    assert defaulted.status_code == 201
    assert defaulted.json()["conversation"]["mode"] == "plan"


async def test_create_is_idempotent_per_owner(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation_id = str(uuid.uuid4())

    first = await client.post(
        "/v1/conversations", headers=_headers(user), json=_body(project, conversation_id)
    )
    assert first.status_code == 201
    again = await client.post(
        "/v1/conversations", headers=_headers(user), json=_body(project, conversation_id)
    )
    assert again.status_code == 200  # a retry / second tab, not an error
    assert again.json()["conversation"]["_id"] == conversation_id


async def test_create_with_anothers_id_is_a_non_leaking_409(client, db_session) -> None:
    user_a = await UserFactory.create(db_session)
    project_a = await ProjectFactory.create(db_session, user_a.id)
    taken = str(uuid.uuid4())
    assert (
        await client.post(
            "/v1/conversations", headers=_headers(user_a), json=_body(project_a, taken)
        )
    ).status_code == 201

    user_b = await UserFactory.create(db_session, email="other@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user_b.id)
    resp = await client.post(
        "/v1/conversations", headers=_headers(user_b), json=_body(project_b, taken)
    )
    assert resp.status_code == 409
    # Same message as the owned-but-different-parentage arm — existence under another
    # owner is not distinguishable (ADR-0004).
    assert resp.json() == {"error": {"message": "This conversation id is already in use."}}


async def test_create_same_id_different_project_is_the_same_409(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project_one = await ProjectFactory.create(db_session, user.id)
    project_two = await ProjectFactory.create(db_session, user.id)
    conversation_id = str(uuid.uuid4())
    assert (
        await client.post(
            "/v1/conversations", headers=_headers(user), json=_body(project_one, conversation_id)
        )
    ).status_code == 201

    resp = await client.post(
        "/v1/conversations", headers=_headers(user), json=_body(project_two, conversation_id)
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": {"message": "This conversation id is already in use."}}


async def test_create_under_an_unowned_project_is_404(client, db_session) -> None:
    user_a = await UserFactory.create(db_session)
    project_a = await ProjectFactory.create(db_session, user_a.id)
    user_b = await UserFactory.create(db_session, email="intruder@rvaiglobal.com")

    resp = await client.post("/v1/conversations", headers=_headers(user_b), json=_body(project_a))
    assert resp.status_code == 404  # non-leaking: same as a project that does not exist


async def test_create_requires_csrf(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    resp = await client.post(
        "/v1/conversations", headers=_headers(user, with_csrf=False), json=_body(project)
    )
    assert resp.status_code == 403


async def test_create_requires_auth(client, db_session) -> None:
    resp = await client.post(
        "/v1/conversations",
        json={"id": str(uuid.uuid4()), "projectId": str(uuid.uuid4()), "kind": "planning"},
    )
    assert resp.status_code == 401
