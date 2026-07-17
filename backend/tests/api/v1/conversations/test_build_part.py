"""The `build` message part is the SERVER'S to write — the append route refuses it (003-U5).

A build turn's outcome (status / preview link / session / start marker) is persisted so reopening a
finished thread shows the whole story: prompt, questions, brief, outcome. But the writer is
`services/build_sessions/outcome.py`, which constructs the row through the ORM model directly — the
thing that always knows a build finished is the thing that finished it, and it is still there when
the user's tab is not. Nothing legitimate posts one through here: the portal's live outcome card
renders from memory and is deliberately never persisted.

So this file pins the REFUSAL, and the two reads that make it matter rather than cosmetic:
`_last_build_boundary` trusts `startedSeq` to decide which of a user's files the next build sees,
and `_already_recorded` trusts `sessionId` to decide the outcome is already written. A forged part
reaches only the forger's own thread — and inside it, silently drops their attachments and
suppresses the real outcome of their own build.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from src.config import settings
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


async def _setup(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    return {"Cookie": f"session={jwt}"}, user, project


def _build_part(**over) -> dict:
    part = {
        "type": "build",
        "status": "ended",
        "sessionId": "01931f7a-0000-7000-8000-000000000001",
        "previewUrl": "https://app-xyz.westeurope.azurecontainerapps.io/",
        "endedAt": "2026-07-17T10:00:00.000Z",
        "snapshotCommitted": True,
        "reason": "completed",
    }
    part.update(over)
    return part


async def _append(client, headers, conversation_id, project_id, parts, seq=0, role="assistant"):
    return await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={
            "message": {"_id": str(uuid.uuid4()), "role": role, "seq": seq, "parts": parts},
            "header": {"kind": "builder", "projectId": str(project_id)},
        },
    )


# --- the route is closed to it ------------------------------------------------


async def test_a_client_written_build_part_is_refused(client, db_session) -> None:
    headers, _, project = await _setup(db_session)
    conversation_id = uuid.uuid4()

    resp = await _append(client, headers, conversation_id, project.id, [_build_part()])

    assert resp.status_code == 422
    # ...and NOTHING lands: the whole append (header included) rolls back, so a refused part can
    # never leave a half-written conversation behind.
    assert (
        await db_session.scalar(select(Message).where(Message.conversation_id == conversation_id))
    ) is None


async def test_a_perfectly_shaped_build_part_is_still_refused(client, db_session) -> None:
    """The gate is about the WRITER, not the shape. The old validator checked the shape and let
    anything well-formed through — which is how a caller with their own session could forge one."""
    headers, _, project = await _setup(db_session)

    resp = await _append(
        client,
        headers,
        uuid.uuid4(),
        project.id,
        [{"type": "text", "text": "Build finished."}, _build_part()],
    )

    assert resp.status_code == 422


async def test_a_user_role_build_part_is_refused_too(client, db_session) -> None:
    # Role is not the discriminator either: no role may write this part.
    headers, _, project = await _setup(db_session)

    resp = await _append(client, headers, uuid.uuid4(), project.id, [_build_part()], role="user")

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "over",
    [
        # The forgeries that actually bite, refused for being build parts at all rather than for
        # any of it: `startedSeq` moves the attachment boundary (a huge one strands every future
        # file the user attaches), `sessionId` suppresses the real outcome writer, and a
        # `javascript:` previewUrl is a stored XSS sink rendered into an `<a href>`.
        {"startedSeq": 2**31 - 1},
        {"sessionId": "01931f7a-0000-7000-8000-000000000009"},
        {"previewUrl": "javascript:alert(document.cookie)"},
        # ...and the malformed ones, which used to be the only thing refused here.
        {"status": "building"},
        {"sessionId": "../../etc/passwd"},
    ],
)
async def test_every_build_part_forgery_gets_the_same_answer(client, db_session, over) -> None:
    headers, _, project = await _setup(db_session)

    resp = await _append(client, headers, uuid.uuid4(), project.id, [_build_part(**over)])

    assert resp.status_code == 422


# --- the parts a client MAY write are untouched -------------------------------


async def test_text_parts_still_append(client, db_session) -> None:
    """The gate is surgical: it closes one part type, not the route."""
    headers, _, project = await _setup(db_session)
    conversation_id = uuid.uuid4()

    resp = await _append(
        client, headers, conversation_id, project.id, [{"type": "text", "text": "hello"}]
    )

    assert resp.status_code == 201
    stored = await db_session.scalar(
        select(Message).where(Message.conversation_id == conversation_id)
    )
    assert stored is not None
    assert stored.parts == [{"type": "text", "text": "hello"}]


async def test_unknown_part_type_still_rejected(client, db_session) -> None:
    """The part set stays closed — an unknown type is still a 400, not a 422."""
    headers, _, project = await _setup(db_session)

    resp = await _append(client, headers, uuid.uuid4(), project.id, [{"type": "outcome"}])

    assert resp.status_code == 400
    assert "unsupported part type" in resp.json()["error"]["message"]
