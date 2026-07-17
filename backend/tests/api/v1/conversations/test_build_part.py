"""The `build` message part — the persisted build outcome (003-U5).

A build turn's outcome (status / preview link / session / reason) is persisted by the PORTAL as a
message part, so reopening a finished thread shows the whole story: prompt, questions, brief,
outcome. The relay stays stateless about builds; the server's only job is validating the shape.

The shape is not cosmetic. `services/build_sessions/attachments.py::_is_build_outcome` keys the
attachment-materialization boundary off `type == "build"`, so a part that slips through malformed
can silently move that boundary and drop a user's file from their next build.
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


async def _append(client, headers, conversation_id, project_id, parts, seq=0):
    return await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={
            "message": {"_id": str(uuid.uuid4()), "role": "assistant", "seq": seq, "parts": parts},
            "header": {"kind": "builder", "projectId": str(project_id)},
        },
    )


# --- the happy shape ----------------------------------------------------------


async def test_build_part_persists_verbatim(client, db_session) -> None:
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    part = _build_part()

    resp = await _append(client, headers, conversation_id, project.id, [part])

    assert resp.status_code == 201
    stored = await db_session.scalar(
        select(Message).where(Message.conversation_id == conversation_id)
    )
    assert stored is not None
    # Round-trips unchanged: the portal reads this back to render the outcome card, and the
    # build path reads it to find the attachment boundary.
    assert stored.parts == [part]


async def test_build_part_alongside_its_summary_text(client, db_session) -> None:
    """The outcome message carries prose too — the transcript needs something to say, and the
    relay assembles a message from its text parts, so a build-part-only message would replay to
    the model as an empty assistant turn."""
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()
    parts = [{"type": "text", "text": "Build finished — your preview is live."}, _build_part()]

    resp = await _append(client, headers, conversation_id, project.id, parts)

    assert resp.status_code == 201


async def test_failed_build_carries_its_reason(client, db_session) -> None:
    headers, user, project = await _setup(db_session)

    resp = await _append(
        client,
        headers,
        uuid.uuid4(),
        project.id,
        [
            _build_part(
                status="failed", previewUrl=None, snapshotCommitted=False, reason="tsc failed"
            )
        ],
    )

    assert resp.status_code == 201


@pytest.mark.parametrize("field", ["previewUrl", "endedAt", "snapshotCommitted", "reason"])
async def test_optional_fields_may_be_absent(client, db_session, field) -> None:
    headers, user, project = await _setup(db_session)
    part = _build_part()
    del part[field]

    resp = await _append(client, headers, uuid.uuid4(), project.id, [part])

    assert resp.status_code == 201


# --- the shape is closed ------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        # Only the two ABSORBING terminals: an outcome exists only once a build is over, so
        # recording a live build as finished must not be possible.
        ({"status": "building"}, "status"),
        ({"status": "ready"}, "status"),
        ({"status": "provisioning"}, "status"),
        ({"status": "cancelled"}, "status"),
        ({"status": None}, "status"),
        # sessionId is the DEDUPE key (003-U5) — a malformed one would let the same terminal
        # append twice, so the transcript would show a build that ran once as two builds.
        ({"sessionId": "../../etc/passwd"}, "sessionId"),
        ({"sessionId": ""}, "sessionId"),
        ({"sessionId": 12345}, "sessionId"),
        ({"sessionId": None}, "sessionId"),
        ({"previewUrl": 12345}, "previewUrl"),
        ({"previewUrl": "https://x/" + "a" * 2048}, "previewUrl"),
        ({"endedAt": 12345}, "endedAt"),
        ({"snapshotCommitted": "yes"}, "snapshotCommitted"),
        ({"reason": 12345}, "reason"),
        ({"reason": "x" * 2001}, "reason"),
    ],
)
async def test_malformed_build_part_400(client, db_session, over, expected) -> None:
    headers, user, project = await _setup(db_session)

    resp = await _append(client, headers, uuid.uuid4(), project.id, [_build_part(**over)])

    assert resp.status_code == 400
    assert expected in resp.json()["error"]["message"]


async def test_unknown_part_type_still_rejected(client, db_session) -> None:
    """The `build` kind is an addition, not an opening: everything else stays closed."""
    headers, user, project = await _setup(db_session)

    resp = await _append(client, headers, uuid.uuid4(), project.id, [{"type": "outcome"}])

    assert resp.status_code == 400
    assert "unsupported part type" in resp.json()["error"]["message"]


async def test_nothing_is_persisted_when_the_build_part_is_rejected(client, db_session) -> None:
    headers, user, project = await _setup(db_session)
    conversation_id = uuid.uuid4()

    resp = await _append(
        client, headers, conversation_id, project.id, [_build_part(status="building")]
    )

    assert resp.status_code == 400
    # The whole append (header included) rolls back — a rejected part must not leave a
    # half-written conversation behind.
    assert (
        await db_session.scalar(select(Message).where(Message.conversation_id == conversation_id))
    ) is None
