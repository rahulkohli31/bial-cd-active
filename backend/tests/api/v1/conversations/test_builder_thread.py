"""POST /v1/conversations/builder-thread — the project's ONE canonical build thread (003-U1).

Newest-wins get-or-create, owner-scoped, CSRF-gated. The concurrency test at the bottom is the
only one in this file that does not use the rolled-back shared session — see its docstring.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.conversations.router import builder_thread
from src.api.v1.conversations.schemas import BuilderThreadRequest
from src.config import settings
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.project import Project
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_UTC = datetime.UTC


def _auth_headers(user, *, with_csrf: bool = True) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


async def _resolve(client, headers, project_id):
    return await client.post(
        "/v1/conversations/builder-thread",
        headers=headers,
        json={"projectId": str(project_id)},
    )


# --- get-or-create ------------------------------------------------------------


async def test_creates_thread_when_project_has_none(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id, name="Gate Ops")
    headers = _auth_headers(user)

    resp = await _resolve(client, headers, project.id)

    assert resp.status_code == 200
    conv = resp.json()["conversation"]
    assert conv["kind"] == "builder"
    assert conv["projectId"] == str(project.id)
    # The thread is named for its project: it IS the project's build conversation, and a
    # nameless row would render as "Untitled" in recents forever (nothing else titles it
    # until the first send).
    assert conv["title"] == "Gate Ops"
    stored = (
        await db_session.scalars(select(Conversation).where(Conversation.project_id == project.id))
    ).all()
    assert [str(c.id) for c in stored] == [conv["_id"]]


async def test_second_call_returns_the_same_thread(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    headers = _auth_headers(user)

    first = await _resolve(client, headers, project.id)
    second = await _resolve(client, headers, project.id)

    assert first.status_code == second.status_code == 200
    assert first.json()["conversation"]["_id"] == second.json()["conversation"]["_id"]
    stored = (
        await db_session.scalars(select(Conversation).where(Conversation.project_id == project.id))
    ).all()
    assert len(stored) == 1  # the second call resolved, it did not create


async def test_newest_builder_thread_wins_and_older_stay_listed(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    now = datetime.datetime.now(_UTC)
    older = [
        await ConversationFactory.create(
            db_session,
            user.id,
            project_id=project.id,
            kind=ConversationKind.BUILDER,
            title=f"build {n}",
            created_at=now - datetime.timedelta(hours=3 - n),
        )
        for n in range(2)
    ]
    newest = await ConversationFactory.create(
        db_session,
        user.id,
        project_id=project.id,
        kind=ConversationKind.BUILDER,
        title="newest",
        created_at=now,
    )
    headers = _auth_headers(user)

    resolved = await _resolve(client, headers, project.id)
    assert resolved.json()["conversation"]["_id"] == str(newest.id)
    # Consistently — a second call must not hop to a different row.
    again = await _resolve(client, headers, project.id)
    assert again.json()["conversation"]["_id"] == str(newest.id)

    # The older builds remain reachable history; only the canonical thread takes new work.
    listed = await client.get(f"/v1/conversations?projectId={project.id}", headers=headers)
    assert {c["_id"] for c in listed.json()["conversations"]} == {
        str(newest.id),
        *(str(c.id) for c in older),
    }


async def test_planning_chats_are_not_canonical_threads(client, db_session) -> None:
    """A project whose only conversation is a planning chat still gets a builder thread
    created — the kind filter is load-bearing, not decoration."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    planning = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.PLANNING
    )
    headers = _auth_headers(user)

    resp = await _resolve(client, headers, project.id)

    assert resp.status_code == 200
    assert resp.json()["conversation"]["_id"] != str(planning.id)
    assert resp.json()["conversation"]["kind"] == "builder"


# --- scoping + gates ----------------------------------------------------------


async def test_foreign_project_404(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ProjectFactory.create(db_session, other.id)

    resp = await _resolve(client, _auth_headers(user), theirs.id)

    assert resp.status_code == 404
    # Non-leaking: indistinguishable from a project that never existed (ADR-0004).
    missing = await _resolve(client, _auth_headers(user), uuid.uuid4())
    assert missing.status_code == 404
    assert resp.json() == missing.json()
    # …and nothing was created under the other user's project.
    assert (
        await db_session.scalars(select(Conversation).where(Conversation.project_id == theirs.id))
    ).all() == []


async def test_missing_csrf_token_rejected(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await _resolve(client, _auth_headers(user, with_csrf=False), project.id)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"
    assert (
        await db_session.scalars(select(Conversation).where(Conversation.project_id == project.id))
    ).all() == []


async def test_unauthenticated_401(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(
        "/v1/conversations/builder-thread", json={"projectId": str(project.id)}
    )

    assert resp.status_code == 401


async def test_malformed_project_id_422(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await client.post(
        "/v1/conversations/builder-thread",
        headers=_auth_headers(user),
        json={"projectId": "not-a-uuid"},
    )

    assert resp.status_code == 422


# --- the race -----------------------------------------------------------------


async def test_concurrent_first_open_creates_exactly_one_thread(test_engine) -> None:
    """Two tabs opening a fresh project at once must land on ONE thread.

    This is the ONLY test here that commits for real. The shared `db_session` fixture binds
    every request to a single rolled-back connection, so requests through it SERIALIZE on that
    connection — a race test written against it would pass whether or not the advisory lock
    exists, i.e. it would assert nothing. Two independent sessions on two real connections are
    what actually contend for `pg_advisory_xact_lock`. The rows are cleaned up in `finally`;
    the project's FK cascade takes the conversations with it.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as setup:
        user = await UserFactory.create(setup, azure_oid=f"oid-{uuid.uuid4()}")
        project = await ProjectFactory.create(setup, user.id)
        await setup.commit()
        user_id, project_id = user.id, project.id

    body = BuilderThreadRequest(project_id=project_id)

    async def open_thread() -> str:
        # A fresh session per caller = a fresh connection = a real, separate transaction.
        async with AsyncSession(test_engine, expire_on_commit=False) as db:
            caller = await db.get(type(user), user_id)
            assert caller is not None
            resp = await builder_thread(body, caller, db)
            import json

            return json.loads(bytes(resp.body))["conversation"]["_id"]

    try:
        first, second = await asyncio.gather(open_thread(), open_thread())
        # Both callers agree on one thread, and only one row exists — the loser of the race
        # resolved the winner's row instead of creating its own. Without the lock the second
        # caller's SELECT would miss the first's uncommitted INSERT and create a duplicate,
        # which newest-wins would then promote over a thread that may already hold a transcript.
        assert first == second
        async with AsyncSession(test_engine, expire_on_commit=False) as check:
            rows = (
                await check.scalars(
                    select(Conversation).where(Conversation.project_id == project_id)
                )
            ).all()
            assert [str(r.id) for r in rows] == [first]
    finally:
        async with AsyncSession(test_engine, expire_on_commit=False) as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(
                delete(type(user)).where(type(user).id == user_id)  # fmt: skip
            )
            await cleanup.commit()
