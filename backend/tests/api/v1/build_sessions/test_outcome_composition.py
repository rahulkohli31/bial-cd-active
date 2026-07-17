"""The thread→build→record seam, composed (003-U5/U6).

`test_outcome.py` calls `write_build_outcome` directly, which proves the writer. It cannot prove
the SEAM: that a real `POST /v1/build-sessions` carrying a `conversationId` actually threads that
id through the session and into the end sequence. Every link there is a plain assignment, and a
plain assignment is exactly the kind of thing that gets dropped in a refactor while every unit test
stays green (`mocks-mask-composition-seams` learning).

So this drives the REAL router → REAL SessionManager → REAL end sequence, with only the sandbox and
BRAIN faked (they need a container and a model), and asserts the outcome lands in the real thread.
Includes the scripted-failure half: a build that FAILS must still leave a record saying so.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import run_build_dependency
from src.api.v1.build_sessions.schemas import BuildResult, BuildSessionStatus, StepEvent
from src.db.models.conversation import ConversationKind
from src.db.models.message import Message
from tests.api.v1.build_sessions.conftest import auth_headers, drain
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory

PREVIEW = "https://sbx-abc.westeurope.azurecontainerapps.io/"


class ScriptedBrain:
    """Runs to a scripted verdict. Emits no terminal — that frame is SESSION-API's alone (R7)."""

    def __init__(self, result: BuildResult) -> None:
        self._result = result

    async def __call__(self, session_id, user_id, sandbox_client, on_progress) -> BuildResult:
        await on_progress(StepEvent(seq=1, name="scaffold", label="Scaffolding", state="started"))
        return self._result


async def _thread(db_session: AsyncSession):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.BUILDER
    )
    # The turn that asked for the build — the outcome must land AFTER it.
    await MessageFactory.create(db_session, user.id, conv.id, seq=0)
    return user, project, conv


def _verdict(
    # BRAIN's verdict can only be one of the two ABSORBING terminals — the same narrowing
    # `BuildResult` itself carries.
    status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED],
    reason: str,
    *,
    preview_url: str | None = PREVIEW,
    snapshot_committed: bool = True,
) -> BuildResult:
    return BuildResult(
        status=status,
        reason=reason,
        app_id=uuid.uuid4(),
        preview_url=preview_url,
        last_seq=1,
        snapshot_committed=snapshot_committed,
    )


async def _start(client, wire, user, project, conv, verdict: BuildResult) -> str:
    wire.app.dependency_overrides[run_build_dependency] = lambda: ScriptedBrain(verdict)
    resp = await client.post(
        "/v1/build-sessions",
        headers=auth_headers(user),
        json={"projectId": str(project.id), "prompt": "build it", "conversationId": str(conv.id)},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["sessionId"]
    await drain(wire.manager, session_id)
    return session_id


async def _build_parts(db_session: AsyncSession, conversation_id) -> list[dict]:
    rows = await db_session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return [p for m in rows for p in m.parts if p.get("type") == "build"]


async def test_a_finished_build_records_itself_in_its_thread(
    client, db_session, wire, fake_redis, fake_storage
) -> None:
    user, project, conv = await _thread(db_session)

    session_id = await _start(
        client, wire, user, project, conv, _verdict(BuildSessionStatus.ENDED, "completed")
    )

    parts = await _build_parts(db_session, conv.id)
    assert len(parts) == 1
    # The seam: the conversationId from the START body reached the END sequence. Every link is a
    # plain assignment — this is the only test that would notice one going missing.
    assert parts[0]["sessionId"] == session_id
    assert parts[0]["status"] == "ended"
    assert parts[0]["previewUrl"] == PREVIEW


async def test_a_failed_build_still_records_what_happened(
    client, db_session, wire, fake_redis, fake_storage
) -> None:
    """The scripted-failure half: a failure is exactly when a user most needs the record, so it
    must not be the path that quietly writes nothing."""
    user, project, conv = await _thread(db_session)

    await _start(
        client,
        wire,
        user,
        project,
        conv,
        _verdict(BuildSessionStatus.FAILED, "escalated", preview_url=None),
    )

    parts = await _build_parts(db_session, conv.id)
    assert len(parts) == 1
    assert parts[0]["status"] == "failed"
    assert parts[0]["reason"] == "escalated"
    assert parts[0]["previewUrl"] is None


async def test_the_record_reports_the_session_apis_snapshot_verdict_not_brains_claim(
    client, db_session, wire, fake_redis, fake_storage
) -> None:
    """R7, carried into the record: `snapshotCommitted` must be the value SESSION-API settled
    AFTER its own snapshot step, never BRAIN's — anything BRAIN says necessarily predates that
    snapshot and could only ever report `false`. Recording BRAIN's claim would tell a user their
    work was lost when it was saved a moment later."""
    user, project, conv = await _thread(db_session)

    # BRAIN claims the snapshot did NOT commit; the session's own snapshot then succeeds.
    await _start(
        client,
        wire,
        user,
        project,
        conv,
        _verdict(BuildSessionStatus.ENDED, "completed", snapshot_committed=False),
    )

    parts = await _build_parts(db_session, conv.id)
    assert parts[0]["snapshotCommitted"] is True


async def test_the_outcome_lands_after_the_turn_that_asked_for_it(
    client, db_session, wire, fake_redis, fake_storage
) -> None:
    user, project, conv = await _thread(db_session)

    await _start(
        client, wire, user, project, conv, _verdict(BuildSessionStatus.ENDED, "completed")
    )

    rows = list(
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
        )
    )
    assert [m.seq for m in rows] == [0, 1]  # the asking turn, then its outcome


async def test_a_build_with_no_thread_records_nothing_and_still_ends(
    client, db_session, wire, fake_redis, fake_storage
) -> None:
    """An API-only start names no conversation. It has no transcript to write to — that is a
    no-op, not an error, and the session must still reach its terminal."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    wire.app.dependency_overrides[run_build_dependency] = lambda: ScriptedBrain(
        _verdict(BuildSessionStatus.ENDED, "completed")
    )

    resp = await client.post(
        "/v1/build-sessions",
        headers=auth_headers(user),
        json={"projectId": str(project.id), "prompt": "build it"},
    )
    assert resp.status_code == 201
    session_id = resp.json()["sessionId"]
    await drain(wire.manager, session_id)

    session = wire.manager.get(uuid.UUID(session_id))
    assert session is not None
    assert session.status is BuildSessionStatus.ENDED
