"""The server-written build outcome (003-U5), native-store edition (U4).

The SERVER records a finished build in its thread, because the portal is not reliably there to do
it: builds take minutes, users close tabs, and a session is evicted `_ENDED_RETENTION_SECONDS`
after its terminal — so a portal-only record would be missing for exactly the users a permanent
record serves.

These test `write_build_outcome` directly against a real session. The row is a `system_event` in
the native message store: the PAYLOAD is a synthesized assistant text (replayed to the model as
history) and the build's structured record lives in `meta` (`sessionId` idempotency key,
`startedSeq` boundary marker, https-parsed `previewUrl`). Seq allocation + the two-writer retry
now live in the store's `append_batch` (covered by `tests/services/messages/`); what this suite
pins is the outcome's OWN contract: shape, prose, idempotency, scoping.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.conversation import Conversation, ConversationMode
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.build_sessions.outcome import (
    _summary,
    build_outcome_meta,
    restore_conversation_mode,
    write_build_outcome,
)
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


def _payload_text(row: Message) -> str:
    """The outcome's summary prose out of the native payload (one ModelResponse, one TextPart)."""
    (message,) = row.payload
    (part,) = message["parts"]
    return part["content"]


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
    assert outcome.entry_kind is MessageEntryKind.SYSTEM_EVENT
    assert outcome.visibility is MessageVisibility.VISIBLE
    assert outcome.meta["status"] == "ended"
    assert outcome.meta["sessionId"] == str(_SESSION)
    assert outcome.meta["snapshotCommitted"] is True
    assert outcome.meta["reason"] == "completed"


async def test_carries_a_summary_text_payload(db_session) -> None:
    """A meta-only row would replay to the model as NOTHING on the user's next send: the
    history loader concatenates payloads, so the summary must live there as assistant text."""
    user, conv = await _thread(db_session)

    await _write(db_session, user, conv)

    outcome = (await _messages(db_session, conv.id))[-1]
    assert "Build finished" in _payload_text(outcome)


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
    assert "escalated" in _payload_text(outcome)
    assert outcome.meta["status"] == "failed"


async def test_quota_end_reads_as_a_limit_not_a_failure(db_session) -> None:
    """A quota breach ends GRACEFULLY (C7 §8) — telling the user their app "failed" would be
    both wrong and alarming."""
    user, conv = await _thread(db_session)

    await _write(db_session, user, conv, reason="quota_exceeded")

    text = _payload_text((await _messages(db_session, conv.id))[-1])
    assert "daily limit" in text
    assert "failed" not in text.lower()


# --- seq allocation (now the store's, asserted end-to-end here) ---------------


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
    would collide with an existing turn — `max+1` is the rule the store owns."""
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
    assert [m.meta["sessionId"] for m in messages] == [str(_SESSION), str(other)]


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
    # every one of these is ENDED — that is the whole problem the reason arms solve
    assert _summary(BuildSessionStatus.ENDED, reason) == expected


@pytest.mark.parametrize("reason", ["stopped_by_user", "force_ended", "idle_teardown"])
def test_no_stopped_build_is_recorded_as_finished(reason: str) -> None:
    # The regression itself, stated once: whatever the copy says, it may not be the finish line.
    text = _summary(BuildSessionStatus.ENDED, reason)
    assert text != "Build finished."
    # ...and it never leaks the internal token at the user (or the model).
    assert reason not in text


def test_a_failure_still_leads_with_the_failure() -> None:
    assert _summary(BuildSessionStatus.FAILED, "tsc failed") == "The build failed: tsc failed"


# --- the meta shape this writer OWNS ------------------------------------------
#
# This module is the only producer of the build-outcome record, and these pins are really about
# its READERS: the projection (U6) and `attachments.py::_boundary`.


def test_meta_carries_the_structured_record() -> None:
    meta = build_outcome_meta(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url="https://app.example/",
        snapshot_committed=True,
        reason="completed",
        started_seq=4,
    )

    assert meta == {
        "kind": "build_outcome",
        "status": "ended",
        "sessionId": str(_SESSION),
        "previewUrl": "https://app.example/",
        "snapshotCommitted": True,
        "reason": "completed",
        "startedSeq": 4,
    }


def test_meta_stays_well_formed_with_every_optional_field_null() -> None:
    meta = build_outcome_meta(
        status=BuildSessionStatus.FAILED,
        session_id=_SESSION,
        preview_url=None,
        snapshot_committed=False,
        reason=None,
        started_seq=None,
    )

    assert meta["previewUrl"] is None
    assert meta["reason"] is None
    # No marker recorded → the field is ABSENT, not null. That is the one state `_boundary`
    # reads as "fall back to this row's own position".
    assert "startedSeq" not in meta


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
    meta = build_outcome_meta(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url=hostile,
        snapshot_committed=True,
        reason="completed",
        started_seq=0,
    )

    assert meta["previewUrl"] is None
    assert meta["status"] == "ended"  # the outcome itself still lands


def test_a_real_preview_url_survives_verbatim() -> None:
    # Returned as GIVEN, not as pydantic re-serializes it: the recorded link should be the address
    # the sandbox actually served, and a normalizing parse would append a trailing slash.
    url = "https://app-xyz.westeurope.azurecontainerapps.io/preview?x=1"
    meta = build_outcome_meta(
        status=BuildSessionStatus.ENDED,
        session_id=_SESSION,
        preview_url=url,
        snapshot_committed=True,
        reason="completed",
        started_seq=0,
    )

    assert meta["previewUrl"] == url


# --- giving the thread its mode back -------------------------------------------------------
#
# Write is a dead end for chat: no toolset, and `start_turn` refuses the turn outright. Under the
# one-gate model the composer re-opens the instant a build ends, so the build that took the thread
# INTO Write is what has to give it back — otherwise the citizen gets a live composer whose every
# send 400s, with the mode pill as the only (invisible) way out.


async def _mode(db_session, conversation_id) -> ConversationMode:
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    return conversation.mode


async def test_restore_puts_the_thread_back_in_its_entry_mode(db_session) -> None:
    user, conv = await _thread(db_session)
    conv.mode = ConversationMode.PLAN
    await db_session.flush()
    # What Build-it did.
    conv.mode = ConversationMode.WRITE
    await db_session.flush()

    assert (
        await restore_conversation_mode(
            db_session,
            user_id=user.id,
            conversation_id=conv.id,
            entry_mode=ConversationMode.PLAN,
        )
        is True
    )

    assert await _mode(db_session, conv.id) is ConversationMode.PLAN
    # The MODEL has to learn its build tools are gone at exactly this point in the history —
    # the same hidden marker a manual switch writes, so replayed history reads identically
    # whoever moved the mode.
    (marker,) = await _messages(db_session, conv.id)
    assert marker.entry_kind is MessageEntryKind.MODE_SWITCH
    assert marker.visibility is MessageVisibility.HIDDEN
    assert "write" in str(marker.payload) and "not available" in str(marker.payload)


async def test_restore_returns_to_ask_not_a_hardcoded_plan(db_session) -> None:
    # A citizen who was ASKING questions when they hit Build it must land back in Ask. A
    # hardcoded Plan would silently move them, which is a worse trap than staying in Write.
    user, conv = await _thread(db_session)
    conv.mode = ConversationMode.WRITE
    await db_session.flush()

    assert (
        await restore_conversation_mode(
            db_session, user_id=user.id, conversation_id=conv.id, entry_mode=ConversationMode.ASK
        )
        is True
    )
    assert await _mode(db_session, conv.id) is ConversationMode.ASK


async def test_restore_is_idempotent_across_a_re_run_of_the_end_sequence(db_session) -> None:
    # The end sequence is single-owner by construction, but every step in it is written to
    # survive being run twice. A second restore must not append a second marker.
    user, conv = await _thread(db_session)
    conv.mode = ConversationMode.WRITE
    await db_session.flush()

    first = await restore_conversation_mode(
        db_session, user_id=user.id, conversation_id=conv.id, entry_mode=ConversationMode.PLAN
    )
    second = await restore_conversation_mode(
        db_session, user_id=user.id, conversation_id=conv.id, entry_mode=ConversationMode.PLAN
    )

    assert (first, second) == (True, False)
    assert len(await _messages(db_session, conv.id)) == 1
    assert await _mode(db_session, conv.id) is ConversationMode.PLAN


async def test_restore_never_stomps_a_mode_the_user_moved_themselves(db_session) -> None:
    # The thread is no longer in Write — someone chose that. Overwriting a deliberate choice
    # with a stale entry mode is worse than leaving it alone.
    user, conv = await _thread(db_session)
    conv.mode = ConversationMode.ASK
    await db_session.flush()

    assert (
        await restore_conversation_mode(
            db_session, user_id=user.id, conversation_id=conv.id, entry_mode=ConversationMode.PLAN
        )
        is False
    )
    assert await _mode(db_session, conv.id) is ConversationMode.ASK
    assert await _messages(db_session, conv.id) == []


async def test_a_thread_that_entered_write_deliberately_stays_in_write(db_session) -> None:
    # Build it from an explicit Write thread: there is nothing to give back.
    user, conv = await _thread(db_session)
    conv.mode = ConversationMode.WRITE
    await db_session.flush()

    assert (
        await restore_conversation_mode(
            db_session, user_id=user.id, conversation_id=conv.id, entry_mode=ConversationMode.WRITE
        )
        is False
    )
    assert await _mode(db_session, conv.id) is ConversationMode.WRITE


async def test_restore_is_owner_scoped_and_survives_a_thread_deleted_mid_build(db_session) -> None:
    # ADR-0004: another user's thread is not ours to move, and it must be indistinguishable
    # from one that no longer exists — the end sequence treats both as "nothing to restore".
    user, conv = await _thread(db_session)
    conv.mode = ConversationMode.WRITE
    await db_session.flush()
    intruder = await UserFactory.create(db_session, email="mode-restore-other@rvaiglobal.com")

    assert (
        await restore_conversation_mode(
            db_session,
            user_id=intruder.id,
            conversation_id=conv.id,
            entry_mode=ConversationMode.PLAN,
        )
        is False
    )
    assert await _mode(db_session, conv.id) is ConversationMode.WRITE

    assert (
        await restore_conversation_mode(
            db_session,
            user_id=user.id,
            conversation_id=uuid.uuid4(),
            entry_mode=ConversationMode.PLAN,
        )
        is False
    )
