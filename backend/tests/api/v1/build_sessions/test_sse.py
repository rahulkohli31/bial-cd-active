"""U7 — the GET-SSE progress feed: copied framing (`id:` line + snake_case `data:` +
terminal `[DONE]`), `Last-Event-ID` resume, cookie auth (no CSRF), and subscriber
cleanup on disconnect."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import cast

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import run_build_dependency
from src.api.v1.build_sessions.schemas import StepEvent
from src.api.v1.build_sessions.sse import build_sse_response
from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions import BuildSession
from src.services.sandbox.base import SandboxHandle
from tests.api.v1.build_sessions.conftest import auth_headers, drain
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain

_TTL = settings.auth.access_ttl_seconds


def _cookie(user) -> dict[str, str]:
    return {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}


def _frames(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                frame["id"] = line[4:]
            elif line.startswith("data: "):
                frame["data"] = line[6:]
        out.append(frame)
    return out


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _completed_session(client, db, wire, email):
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db, email)
    r = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    sid = r.json()["sessionId"]
    await drain(wire.manager, sid)  # run the fast brain to the terminal ended
    return user, sid


async def test_full_replay_carries_id_lines_snake_case_and_done(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid = await _completed_session(client, db_session, wire, "sse1@rvaiglobal.com")
    resp = await client.get(
        f"/v1/build-sessions/{sid}/events", headers={**_cookie(user), "Last-Event-ID": "0"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _frames(resp.text)
    assert [f["id"] for f in frames if "id" in f] == ["1", "2", "3", "4"]  # gap-free
    assert frames[-1]["data"] == "[DONE]"  # terminal sentinel
    # Envelope fidelity: the preview_ready frame (seq 3) is the compact snake_case C7 shape.
    ev3 = json.loads(next(f["data"] for f in frames if f.get("id") == "3"))
    assert ev3["type"] == "preview_ready"
    assert ev3["preview_url"] == "https://preview.example/"


async def test_resume_replays_only_after_last_event_id(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid = await _completed_session(client, db_session, wire, "sse2@rvaiglobal.com")
    resp = await client.get(
        f"/v1/build-sessions/{sid}/events", headers={**_cookie(user), "Last-Event-ID": "2"}
    )
    assert resp.status_code == 200
    frames = _frames(resp.text)
    assert [f["id"] for f in frames if "id" in f] == ["3", "4"]  # only seq > 2
    assert frames[-1]["data"] == "[DONE]"


async def test_already_ended_session_without_cursor_gets_story_and_done(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid = await _completed_session(client, db_session, wire, "sse3@rvaiglobal.com")
    # No Last-Event-ID: an already-ended session still gets the full story + [DONE], not a hang.
    resp = await client.get(f"/v1/build-sessions/{sid}/events", headers=_cookie(user))
    assert resp.status_code == 200
    frames = _frames(resp.text)
    assert frames[-1]["data"] == "[DONE]"
    assert [f["id"] for f in frames if "id" in f] == ["1", "2", "3", "4"]


async def test_sse_auth_no_cookie_401_other_user_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid = await _completed_session(client, db_session, wire, "sse4@rvaiglobal.com")
    no_cookie = await client.get(f"/v1/build-sessions/{sid}/events")
    assert no_cookie.status_code == 401
    intruder = await UserFactory.create(db_session, email="sse4b@rvaiglobal.com")
    other = await client.get(f"/v1/build-sessions/{sid}/events", headers=_cookie(intruder))
    assert other.status_code == 404  # another user's session, non-leaking


async def test_sse_generator_drops_subscriber_on_close() -> None:
    # Direct drive (no DB / HTTP): a client disconnect drops the subscriber; the session +
    # its run_build task are untouched (the SessionManager owns them).
    session = BuildSession(
        session_id=uuid.uuid7(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="p",
        lock_token="tok",
        handle=SandboxHandle(
            fqdn="x.example",
            token="t",
            app_name="sbx-x",
            preview_url="https://x.example/",
            ready=False,
        ),
    )
    session.envelopes.append(StepEvent(seq=1, name="s", label="l", state="started"))
    session.last_seq = 1
    resp = build_sse_response(session, last_event_id=0)
    gen = cast(AsyncGenerator[bytes], resp.body_iterator)
    first = await gen.__anext__()  # the seq-1 replay frame
    assert b"id: 1" in first
    assert len(session.subscribers) == 1  # registered
    await gen.aclose()  # simulate disconnect mid-stream
    assert len(session.subscribers) == 0  # dropped, no leak


async def test_sse_recovers_a_dropped_terminal_from_the_buffer() -> None:
    # FIX-4: the append-only buffer is authoritative. Even if the queue never receives the
    # terminal `ended` (dropped on a full queue for a slow client), the generator recovers
    # it from the buffer and emits [DONE] — it never hangs.
    from src.api.v1.build_sessions.schemas import BuildSessionStatus, EndedEvent

    session = BuildSession(
        session_id=uuid.uuid7(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="p",
        lock_token="tok",
        handle=SandboxHandle(
            fqdn="x.example",
            token="t",
            app_name="sbx-x",
            preview_url="https://x.example/",
            ready=False,
        ),
    )
    # The buffer holds the whole story incl. the terminal; the subscriber queue stays EMPTY
    # (as if on_progress dropped every push to this slow client).
    session.envelopes.append(StepEvent(seq=1, name="s", label="l", state="started"))
    session.envelopes.append(
        EndedEvent(
            seq=2,
            status=BuildSessionStatus.ENDED,
            preview_url=None,
            snapshot_committed=True,
            reason="completed",
        )
    )
    session.last_seq = 2
    session.terminal_emitted = True
    session.terminal_committed = True

    resp = build_sse_response(session, last_event_id=0)
    gen = cast(AsyncGenerator[bytes, None], resp.body_iterator)
    body = b"".join([chunk async for chunk in gen])
    text = body.decode()
    assert "id: 1" in text and "id: 2" in text  # both frames recovered from the buffer
    assert text.rstrip().endswith("[DONE]")  # closed, no hang
    assert len(session.subscribers) == 0  # subscriber cleaned up
