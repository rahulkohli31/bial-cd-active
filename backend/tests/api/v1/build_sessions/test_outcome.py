"""The server-written build outcome (003-U5).

The SERVER records a finished build in its thread, because the portal is not reliably there to do
it: builds take minutes, users close tabs, and a session is evicted `_ENDED_RETENTION_SECONDS`
after its terminal — so a portal-only record would be missing for exactly the users a permanent
record serves.

These test `write_build_outcome` directly against a real session. The seq tests are the ones with
teeth: this writer is the SECOND allocator on a transcript, so a seq it picks wrongly collides with
a user's turn. `uq_messages_conversation_seq` turns that into an IntegrityError rather than a
silent overwrite, and this writer answers it by re-picking (bounded by `_SEQ_RETRIES`) — the append
route answers the same collision with a `message_seq_conflict` 409 instead, because it has a client
that can retry. Both arms have to hold or a build outcome goes missing from the thread.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
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
    kwargs: dict[str, Any] = {
        "user_id": user.id,
        "conversation_id": conv.id,
        "session_id": _SESSION,
        "status": BuildSessionStatus.ENDED,
        "preview_url": "https://app.westeurope.azurecontainerapps.io/",
        "snapshot_committed": True,
        "reason": "completed",
        "started_seq": 0,
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


# --- the prose tells the truth about how the build ended -----------------------
#
# `_terminal_status` maps a natural finish, a Stop, a force-end and an idle reap ALL onto ENDED, so
# a summary keyed on status alone cannot tell them apart — and this text is not just chrome: the
# thread IS the model's history, so whatever it says is replayed to the model on the user's next
# turn. "Build finished." on a build the user stopped at minute two is a lie told twice.


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("completed", "Build finished."),
        ("stopped_by_user", "You stopped this build before it finished."),
        # force_end is the one graceful end that DISCARDS its work (`_do_finalize` skips the
        # snapshot when `force_ended` is set), so this is the summary that must say so.
        (
            "force_ended",
            "This build was force-stopped before it finished, and its work was discarded.",
        ),
        ("idle_teardown", "This build was stopped because it sat idle."),
        ("quota_exceeded", "The build stopped: you reached your daily limit."),
    ],
)
def test_a_graceful_end_says_how_it_ended(reason: str, expected: str) -> None:
    parts = build_outcome_parts(
        status=BuildSessionStatus.ENDED,  # every one of these is ENDED — that is the whole problem
        session_id=_SESSION,
        preview_url=None,
        snapshot_committed=True,
        reason=reason,
        started_seq=0,
    )

    assert parts[0]["text"] == expected


@pytest.mark.parametrize("reason", ["stopped_by_user", "force_ended", "idle_teardown"])
def test_no_stopped_build_is_recorded_as_finished(reason: str) -> None:
    # The regression itself, stated once: whatever the copy says, it may not be the finish line.
    parts = build_outcome_parts(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url=None,
        snapshot_committed=True,
        reason=reason,
        started_seq=0,
    )

    assert parts[0]["text"] != "Build finished."
    # ...and it never leaks the internal token at the user (or the model).
    assert reason not in parts[0]["text"]


def test_a_failure_still_leads_with_the_failure() -> None:
    parts = build_outcome_parts(
        status=BuildSessionStatus.FAILED,
        session_id=_SESSION,
        preview_url=None,
        snapshot_committed=False,
        reason="tsc failed",
        started_seq=0,
    )

    assert parts[0]["text"] == "The build failed: tsc failed"


# --- the shape this writer OWNS -----------------------------------------------
#
# There is no second writer to agree with any more: the append route refuses a client-written
# `build` part outright (422), so this module is the only producer of the shape and these tests are
# the only thing pinning it. Its READERS are what the assertions below are really about — the
# portal's outcome card, and `attachments.py::_last_build_boundary`.


def _build_part(parts: list[dict[str, object]]) -> dict[str, object]:
    return next(p for p in parts if p.get("type") == "build")


def test_parts_carry_the_summary_text_and_the_build_part() -> None:
    """The text part is not decoration: readers assemble a turn from TEXT parts, so a
    build-part-only message replays to the model as an empty assistant turn."""
    parts = build_outcome_parts(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url="https://app.example/",
        snapshot_committed=True,
        reason="completed",
        started_seq=4,
    )

    assert [p["type"] for p in parts] == ["text", "build"]
    assert parts[0]["text"] == "Build finished."
    assert _build_part(parts) == {
        "type": "build",
        "status": "ended",
        "sessionId": str(_SESSION),
        "previewUrl": "https://app.example/",
        "endedAt": parts[1]["endedAt"],
        "snapshotCommitted": True,
        "reason": "completed",
        "startedSeq": 4,
    }


def test_parts_stay_well_formed_with_every_optional_field_null() -> None:
    parts = build_outcome_parts(
        status=BuildSessionStatus.FAILED,
        session_id=_SESSION,
        preview_url=None,
        snapshot_committed=False,
        reason=None,
        started_seq=None,
    )

    build = _build_part(parts)
    assert build["previewUrl"] is None
    assert build["reason"] is None
    # No marker recorded → the field is ABSENT, not null. That is the one state
    # `_last_build_boundary` reads as "fall back to this row's own position", which is exactly
    # what every row written before the marker existed needs.
    assert "startedSeq" not in build


# --- the preview link is https-only (a stored XSS sink otherwise) --------------


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "http://app.example/",  # plaintext — the same parse the deployed URL gets refuses it
        "not a url at all",
    ],
)
def test_a_non_https_preview_url_is_dropped_not_recorded(hostile: str) -> None:
    """The card renders this straight into an `<a href>`, same-origin with the portal session.

    Nothing should ever reach here with one — the only value is the ACA FQDN the platform minted —
    which is the point: this is the fail-closed floor under that claim. It drops the LINK and keeps
    the record, because a raise inside the end sequence would cost the user their whole outcome.
    """
    parts = build_outcome_parts(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url=hostile,
        snapshot_committed=True,
        reason="completed",
        started_seq=0,
    )

    assert _build_part(parts)["previewUrl"] is None
    assert _build_part(parts)["status"] == "ended"  # the outcome itself still lands


def test_a_real_preview_url_survives_verbatim() -> None:
    # Returned as GIVEN, not as pydantic re-serializes it: the recorded link should be the address
    # the sandbox actually served, and a normalizing parse would append a trailing slash.
    url = "https://app-xyz.westeurope.azurecontainerapps.io/preview?x=1"
    parts = build_outcome_parts(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url=url,
        snapshot_committed=True,
        reason="completed",
        started_seq=0,
    )

    assert _build_part(parts)["previewUrl"] == url
