"""The two-writer scenarios that motivated server-side seq allocation (003-U5).

The SPA and a build's end sequence both append to one transcript, and they cannot coordinate: the
server writes while the tab may be mid-stream, reloading, backgrounded, or closed. These reproduce
the two interleavings a code review traced, at the level where they actually happen — a real append
request against a real outcome row — and assert the user's turn SURVIVES.

Both were silent before: `append_message` answered 201 and wrote nothing, so a lost message looked
exactly like a stored one until it was missing after a reload.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.config import settings
from src.db.models.conversation import ConversationKind
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.outcome import write_build_outcome
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_SESSION = uuid.UUID("01931f7a-0000-7000-8000-00000000beef")


async def _thread(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.BUILDER
    )
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    return {"Cookie": f"session={jwt}"}, user, project, conv


async def _send(client, headers, conv, project, *, seq, text):
    """A turn the SPA sends, with the seq its local counter believes is next."""
    return await client.post(
        f"/v1/conversations/{conv.id}/messages",
        headers=headers,
        json={
            "message": {
                "_id": str(uuid.uuid4()),
                "role": "user",
                "seq": seq,
                "parts": [{"type": "text", "text": text}],
            },
            "header": {"kind": "builder", "projectId": str(project.id)},
        },
    )


async def _transcript(db_session, conv) -> list[str]:
    rows = await db_session.scalars(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
    )
    return [p["text"] for m in rows for p in m.parts if p["type"] == "text"]


async def _record(db_session, user, conv):
    return await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        session_id=_SESSION,
        status=BuildSessionStatus.ENDED,
        preview_url=None,
        snapshot_committed=True,
        reason="completed",
    )


async def test_a_reloaded_tab_that_missed_the_outcome_does_not_lose_its_next_message(
    client, db_session
) -> None:
    """DETERMINISTIC — no race. The tab reloads mid-build, so it holds no session and never learns
    the outcome landed; its counter still points at the slot the server took. Reloading during a
    multi-minute build is ordinary, and serving exactly that user is why the server writes at all.

    Before: the send collided, was answered 201, and vanished — while the assistant's reply to it
    persisted, leaving the thread holding an answer to a question that was not there.
    """
    headers, user, project, conv = await _thread(db_session)
    await MessageFactory.create(db_session, user.id, conv.id, seq=0)  # "build me an app"
    # The tab reloads here: it reads seqs [0] and sets its counter to 1.
    assert await _record(db_session, user, conv) is True  # the build ends; the server writes at 1

    resp = await _send(client, headers, conv, project, seq=1, text="also add a chart")

    assert resp.status_code == 201
    assert "also add a chart" in await _transcript(db_session, conv)
    assert resp.json()["message"]["seq"] == 2  # the server moved it past its own row


async def test_an_outcome_landing_mid_stream_does_not_steal_the_assistant_turn(
    client, db_session
) -> None:
    """RACY. The SPA reserves the assistant's seq when the stream STARTS and persists it when the
    stream ENDS — seconds later, on a composer left deliberately usable during a build. A terminal
    landing in that window takes the reserved slot.

    The turn at stake is often the one carrying the build brief, so losing it loses the build.
    """
    headers, user, project, conv = await _thread(db_session)
    await MessageFactory.create(db_session, user.id, conv.id, seq=0)  # the user's question
    # The SPA reserved seq 1 for the assistant reply and is streaming it. Nothing is persisted yet,
    # so the server sees max=0 and takes 1 for the outcome.
    await _record(db_session, user, conv)

    # The stream finishes and the SPA persists the assistant turn at the seq it reserved.
    resp = await _send(client, headers, conv, project, seq=1, text="here is your brief")

    assert resp.status_code == 201
    assert "here is your brief" in await _transcript(db_session, conv)


async def test_the_outcome_and_the_turn_both_survive_in_order(client, db_session) -> None:
    """Neither writer overwrites the other: the transcript keeps every turn, and seq still orders
    them by when they were stored."""
    headers, user, project, conv = await _thread(db_session)
    await MessageFactory.create(
        db_session, user.id, conv.id, seq=0, parts=[{"type": "text", "text": "build it"}]
    )
    await _record(db_session, user, conv)

    await _send(client, headers, conv, project, seq=1, text="now add a chart")

    assert await _transcript(db_session, conv) == [
        "build it",
        "Build finished.",
        "now add a chart",
    ]
