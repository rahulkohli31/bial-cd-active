"""The server-written build outcome (003-U5).

The SERVER records a finished build in its thread, because the portal is not reliably there to do
it: builds take minutes, users close tabs, and a session is evicted `_ENDED_RETENTION_SECONDS`
after its terminal — so a portal-only record would be missing for exactly the users a permanent
record serves.

These test `write_build_outcome` directly against a real session. The seq tests are the ones with
teeth: `append_message` treats a seq collision as an idempotent replay and answers 201 WITHOUT
writing, so a seq this writer picks wrongly does not surface as an error — it silently swallows
somebody's message.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.message import Message, MessageRole
from src.services.build_sessions.outcome import build_outcome_parts, write_build_outcome
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory

_SESSION = uuid.UUID("01931f7a-0000-7000-8000-000000000001")


async def _thread(db_session, *, turns: int = 0):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    for seq in range(turns):
        await MessageFactory.create(db_session, user.id, conv.id, seq=seq)
    return user, conv


async def _messages(db_session, conversation_id):
    return list(
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
        )
    )


async def _write(db_session, user, conv, **over):
    kwargs = {
        "user_id": user.id,
        "conversation_id": conv.id,
        "session_id": _SESSION,
        "status": BuildSessionStatus.ENDED,
        "preview_url": "https://app.westeurope.azurecontainerapps.io/",
        "snapshot_committed": True,
        "reason": "completed",
    }
    kwargs.update(over)
    return await write_build_outcome(db_session, **kwargs)


# --- the record ---------------------------------------------------------------


async def test_writes_the_outcome_into_the_thread(db_session) -> None:
    user, conv = await _thread(db_session, turns=2)

    assert await _write(db_session, user, conv) is True

    messages = await _messages(db_session, conv.id)
    assert len(messages) == 3
    outcome = messages[-1]
    assert outcome.role is MessageRole.ASSISTANT
    build = next(p for p in outcome.parts if p["type"] == "build")
    assert build["status"] == "ended"
    assert build["sessionId"] == str(_SESSION)
    assert build["snapshotCommitted"] is True
    assert build["reason"] == "completed"


async def test_carries_a_summary_text_part(db_session) -> None:
    """A build-part-only message would replay to the model as an EMPTY assistant turn on the
    user's next send: the relay assembles a turn from its text parts."""
    user, conv = await _thread(db_session)

    await _write(db_session, user, conv)

    outcome = (await _messages(db_session, conv.id))[-1]
    text = next(p for p in outcome.parts if p["type"] == "text")
    assert "Build finished" in text["text"]


async def test_failed_build_says_why(db_session) -> None:
    user, conv = await _thread(db_session)

    await _write(
        db_session,
        user,
        conv,
        status=BuildSessionStatus.FAILED,
        reason="escalated",
        preview_url=None,
    )

    outcome = (await _messages(db_session, conv.id))[-1]
    assert "escalated" in next(p for p in outcome.parts if p["type"] == "text")["text"]
    assert next(p for p in outcome.parts if p["type"] == "build")["status"] == "failed"


async def test_quota_end_reads_as_a_limit_not_a_failure(db_session) -> None:
    """A quota breach ends GRACEFULLY (C7 §8) — telling the user their app "failed" would be
    both wrong and alarming."""
    user, conv = await _thread(db_session)

    await _write(db_session, user, conv, reason="quota_exceeded")

    text = next(
        p for p in (await _messages(db_session, conv.id))[-1].parts if p["type"] == "text"
    )["text"]
    assert "daily limit" in text
    assert "failed" not in text.lower()


# --- seq allocation (the silent-loss surface) ---------------------------------


async def test_seq_continues_the_transcript(db_session) -> None:
    user, conv = await _thread(db_session, turns=3)  # seqs 0,1,2

    await _write(db_session, user, conv)

    assert [m.seq for m in await _messages(db_session, conv.id)] == [0, 1, 2, 3]


async def test_seq_starts_at_zero_in_an_empty_thread(db_session) -> None:
    user, conv = await _thread(db_session)

    await _write(db_session, user, conv)

    assert [m.seq for m in await _messages(db_session, conv.id)] == [0]


async def test_seq_follows_the_highest_seq_not_the_row_count(db_session) -> None:
    """A gap (a failed append, a pruned turn) makes count != next seq. Allocating from the count
    would collide with an existing turn, and the collision surfaces as a cheerful 201 that writes
    nothing — the portal reserves `max+1` too, so both sides must agree on `max`."""
    user, conv = await _thread(db_session)
    await MessageFactory.create(db_session, user.id, conv.id, seq=0)
    await MessageFactory.create(db_session, user.id, conv.id, seq=7)

    await _write(db_session, user, conv)

    assert [m.seq for m in await _messages(db_session, conv.id)] == [0, 7, 8]


# --- idempotency + scoping ----------------------------------------------------


async def test_a_build_gets_exactly_one_outcome(db_session) -> None:
    user, conv = await _thread(db_session)
    assert await _write(db_session, user, conv) is True

    # A re-run of the end sequence must not stack a second record for the same build.
    assert await _write(db_session, user, conv) is False

    assert len(await _messages(db_session, conv.id)) == 1


async def test_a_second_build_gets_its_own_outcome(db_session) -> None:
    user, conv = await _thread(db_session)
    await _write(db_session, user, conv)

    other = uuid.UUID("01931f7a-0000-7000-8000-000000000002")
    assert await _write(db_session, user, conv, session_id=other) is True

    messages = await _messages(db_session, conv.id)
    assert [next(p for p in m.parts if p["type"] == "build")["sessionId"] for m in messages] == [
        str(_SESSION),
        str(other),
    ]


async def test_a_foreign_conversation_is_never_written_to(db_session) -> None:
    """Owner-scoped (ADR-0004): a build must not be able to write into someone else's thread."""
    _, conv = await _thread(db_session)
    intruder = await UserFactory.create(db_session)

    assert await _write(db_session, intruder, conv) is False

    assert await _messages(db_session, conv.id) == []


async def test_a_deleted_thread_is_a_no_op_not_a_crash(db_session) -> None:
    """The thread can vanish mid-build (the user deletes it while it runs). The end sequence must
    survive that: a raise here would skip the terminal frame and hang every SSE feed."""
    user, _ = await _thread(db_session)

    assert (
        await write_build_outcome(
            db_session,
            user_id=user.id,
            conversation_id=uuid.uuid4(),
            session_id=_SESSION,
            status=BuildSessionStatus.ENDED,
            preview_url=None,
            snapshot_committed=True,
            reason="completed",
        )
        is False
    )


# --- the shape the append API accepts -----------------------------------------


def test_parts_match_the_shape_the_append_route_validates() -> None:
    """The portal appends this same shape through `_validate_parts`. If the two drift, one writer
    produces rows the other's validator would reject — and the portal's render reads both."""
    from src.api.v1.conversations.router import _validate_parts

    parts = build_outcome_parts(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url="https://app.example/",
        snapshot_committed=True,
        reason="completed",
    )

    assert _validate_parts(parts) is None


def test_parts_stay_valid_with_every_optional_field_null() -> None:
    parts = build_outcome_parts(
        status=BuildSessionStatus.FAILED,
        session_id=_SESSION,
        preview_url=None,
        snapshot_committed=False,
        reason=None,
    )

    assert _validate_parts_ok(parts)


def _validate_parts_ok(parts: list[dict[str, object]]) -> bool:
    from src.api.v1.conversations.router import _validate_parts

    return _validate_parts(parts) is None
