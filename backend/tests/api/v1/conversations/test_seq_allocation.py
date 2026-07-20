"""`seq` is allocated by the SERVER — the client's value is a hint, not a claim.

WHY THIS EXISTS. `seq` used to be whatever the client said, and a collision was read as an
idempotent replay: 201, nothing written. That is sound only while the client is the ONLY writer.
It stopped being true when the build's end sequence began recording outcomes
(`services/build_sessions/outcome.py`), and the failure had no symptom — the caller is TOLD 201,
so a lost turn looks identical to a stored one until it is missing after a reload.

THE ASSERTIONS HERE READ THE DATABASE, NOT THE STATUS CODE. A test that checks for 201 passes
whether or not the row exists, which is exactly how this class of bug survives a green suite.
"""

from __future__ import annotations

import uuid

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


async def _append(client, headers, conversation_id, project_id, *, seq, text, message_id=None):
    return await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={
            "message": {
                "_id": message_id or str(uuid.uuid4()),
                "role": "user",
                "seq": seq,
                "parts": [{"type": "text", "text": text}],
            },
            "header": {"kind": "builder", "projectId": str(project_id)},
        },
    )


async def _texts(db_session, conversation_id) -> list[str]:
    rows = await db_session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return [p["text"] for m in rows for p in m.parts if p["type"] == "text"]


async def test_a_distinct_turn_on_a_taken_seq_is_stored_not_swallowed(client, db_session) -> None:
    """THE REGRESSION. Two writers over one transcript WILL disagree about the next seq — the
    portal counts slots it has reserved but not yet persisted, the server counts rows. When they
    land on the same number the second turn is a DIFFERENT message, not a replay, and dropping it
    loses a real user message with no error anywhere."""
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    await _append(client, headers, conversation_id, project.id, seq=0, text="first")

    # A different message claims a seq that is already taken.
    resp = await _append(client, headers, conversation_id, project.id, seq=0, text="second")

    assert resp.status_code == 201
    # It was STORED — at a seq the server chose, not the one the client guessed.
    assert await _texts(db_session, conversation_id) == ["first", "second"]
    assert resp.json()["message"]["seq"] == 1


async def test_the_response_reports_the_seq_the_server_actually_used(client, db_session) -> None:
    """The client re-seeds its counter from this, so a wrong number here re-collides forever."""
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    for seq in range(3):
        await _append(client, headers, conversation_id, project.id, seq=seq, text=f"turn {seq}")

    resp = await _append(client, headers, conversation_id, project.id, seq=0, text="late")

    assert resp.json()["message"]["seq"] == 3
    rows = await db_session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    assert [m.seq for m in rows] == [0, 1, 2, 3]


async def test_a_true_replay_is_still_idempotent(client, db_session) -> None:
    """The behaviour that must NOT change: the same turn re-sent (same `_id`) after a flaky
    response is one turn, not two. That is what the collision branch was written for, and it is
    identified by the message id — never by the seq."""
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    message_id = str(uuid.uuid4())

    first = await _append(
        client, headers, conversation_id, project.id, seq=0, text="once", message_id=message_id
    )
    second = await _append(
        client, headers, conversation_id, project.id, seq=0, text="once", message_id=message_id
    )

    assert first.status_code == second.status_code == 201
    assert await _texts(db_session, conversation_id) == ["once"]  # not duplicated
    assert second.json()["message"]["seq"] == 0  # and it reports the ORIGINAL seq


async def test_an_uncontested_seq_is_honoured_exactly(client, db_session) -> None:
    """The client's seq is a hint the server takes when it is free — this keeps every existing
    caller's ordering intact, and keeps the common path a single INSERT."""
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()

    resp = await _append(client, headers, conversation_id, project.id, seq=0, text="hi")

    assert resp.json()["message"]["seq"] == 0
    resp = await _append(client, headers, conversation_id, project.id, seq=1, text="there")
    assert resp.json()["message"]["seq"] == 1


async def test_a_real_seq_collision_answers_409_with_the_transient_code(
    client, db_session, monkeypatch
) -> None:
    """The route has TWO 409s that mean opposite things, and only the `code` tells them apart.

    The permanent one ("already in use") means retrying is pointless; this one means nothing was
    written and the caller should retry. The portal branches on this exact string
    (`chatErrors.ts::isSeqConflict`) to choose between "already saved, reload" and "send it
    again" — read the wrong one and we tell a user their message landed when it did not, then
    send them to reload and destroy the text the retry needed.

    THIS DRIVES THE COLLISION, it does not assert the constant. Pinning `_SEQ_CONFLICT_CODE == "…"`
    is what this used to do, and it proves nothing: the arm that ATTACHES the code could stop doing
    so and the constant would still be there, green, while the portal silently misread the 409
    again. So everything below the interposition is real — a real INSERT, a real
    `uq_messages_conversation_seq` violation, the real IntegrityError, the real arm that reads it.

    The one simulated thing is the only thing a single connection cannot produce: `_free_seq`'s
    answer going stale between its SELECT and the INSERT. That is exactly what a build's end
    sequence committing at that instant does to it (`build_sessions/outcome.py` is the second
    writer on this transcript), and the route cannot tell the two apart — by the time it has an
    IntegrityError, the cause is already history.
    """
    from src.api.v1.conversations import router

    headers, _, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    await _append(client, headers, conversation_id, project.id, seq=0, text="the turn that landed")

    async def _stale_read(*_args, **_kwargs) -> int:
        return 0  # "free" — and taken by the time the caller acts on it

    monkeypatch.setattr(router, "_free_seq", _stale_read)

    resp = await _append(client, headers, conversation_id, project.id, seq=0, text="the loser")

    assert resp.status_code == 409
    # The whole contract: not the status, the CODE. A 409 without it means "already saved".
    assert resp.json()["error"]["code"] == "message_seq_conflict"


async def test_the_permanent_409_carries_no_transient_code(client, db_session) -> None:
    """The `_id`-already-taken 409 must NOT look transient — retrying it can never succeed."""
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    message_id = str(uuid.uuid4())
    await _append(
        client, headers, conversation_id, project.id, seq=0, text="mine", message_id=message_id
    )

    # The same `_id` in a DIFFERENT conversation — a genuine permanent collision.
    resp = await _append(
        client, headers, uuid.uuid4(), project.id, seq=0, text="theirs", message_id=message_id
    )

    assert resp.status_code == 409
    assert resp.json()["error"].get("code") != "message_seq_conflict"


async def test_a_reallocation_past_int32_is_the_routes_own_error_not_a_500(
    client, db_session
) -> None:
    """One boundary for both seqs — the one the client proposes AND the one the server picks.

    `seq` is an int32 column, and an out-of-range value makes asyncpg raise a DataError, NOT an
    IntegrityError — so it sails past the collision handler and escapes as a bare 500. The route
    already refuses a client seq that cannot fit (`_fits_int32`); the reallocation path was added
    later and answered to nobody. A client can reach it in one move: seq 2**31-1 is IN range, so
    it is accepted, and every append after it reallocates to max+1 = 2**31.
    """
    headers, _, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    await _append(client, headers, conversation_id, project.id, seq=2**31 - 1, text="the ceiling")

    # Same seq → taken → reallocate to 2**31, which no int32 column can hold.
    resp = await _append(client, headers, conversation_id, project.id, seq=2**31 - 1, text="next")

    assert resp.status_code == 409
    # Not the "already saved" 409: nothing WAS saved, and telling the user otherwise would send
    # them to reload and bin the text.
    assert resp.json()["error"]["code"] == "message_seq_conflict"


async def test_a_gap_in_the_transcript_is_preserved_not_backfilled(client, db_session) -> None:
    """Reallocation continues the transcript (max+1); it must not hunt for a hole and reorder
    history — seq is the ordering, so backfilling a gap would move a turn in time."""
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    await _append(client, headers, conversation_id, project.id, seq=0, text="a")
    await _append(client, headers, conversation_id, project.id, seq=7, text="b")

    resp = await _append(client, headers, conversation_id, project.id, seq=7, text="c")

    assert resp.json()["message"]["seq"] == 8
    assert await _texts(db_session, conversation_id) == ["a", "b", "c"]
