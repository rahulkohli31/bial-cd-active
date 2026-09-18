"""The GET-SSE progress feed: framing (`id:` line + snake_case `data:` + terminal
`[DONE]`), `Last-Event-ID` resume, cookie auth (no CSRF), and subscriber cleanup."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions import sse as sse_module
from src.api.v1.build_sessions.schemas import (
    BuildSessionStatus,
    EndedEvent,
    PreviewReadyEvent,
    StepEvent,
)
from src.api.v1.build_sessions.sse import build_sse_response
from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions import BuildSession
from src.services.sandbox.base import SandboxHandle
from tests.api.v1.build_sessions.conftest import a_live_session
from tests.factories import ProjectFactory, UserFactory

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
    """A session that ran to its terminal, with the exact three-frame story the feed replays.

    Re-fixtured off the deleted start route and then off the deleted end sequence. What produced
    these frames was a `run_build` task emitting seq 1-2 and the manager synthesizing the
    terminal seq 3; both are gone, so all three are handed to `manager.on_progress` directly —
    the generic progress sink, which documents that it must derive correct state from envelopes
    pushed by tests. `_bare_session()` below is the same technique with no manager at all; this
    one keeps the manager because the feed is reached over HTTP."""
    user, project = await _user_project(db, email)
    session = await a_live_session(wire, db, user, project.id)
    await wire.manager.on_progress(
        session, StepEvent(seq=1, name="scaffold", label="Scaffolding the app", state="started")
    )
    await wire.manager.on_progress(
        session, PreviewReadyEvent(seq=2, preview_url="https://preview.example/")
    )
    await wire.manager.on_progress(
        session,
        EndedEvent(
            seq=3,
            status=BuildSessionStatus.ENDED,
            preview_url="https://preview.example/",
            snapshot_committed=True,
            reason="completed",
        ),
    )
    return user, str(session.session_id)


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
    assert [f["id"] for f in frames if "id" in f] == ["1", "2", "3"]
    assert frames[-1]["data"] == "[DONE]"  # terminal sentinel
    # Envelope fidelity: the preview_ready frame (seq 2) is the compact snake_case shape.
    ev2 = json.loads(next(f["data"] for f in frames if f.get("id") == "2"))
    assert ev2["type"] == "preview_ready"
    assert ev2["preview_url"] == "https://preview.example/"


async def test_replay_of_a_finished_session_has_exactly_one_truthful_terminal(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # End to end over the real feed, with no manager internals stubbed: a finished build
    # replays ONE `ended`, it is last, it reports the snapshot that actually committed, and the
    # stream closes on it. A second terminal would fight the `[DONE]` close semantics — the
    # generator returns at the FIRST one, so any extra frame would silently vanish here.
    user, sid = await _completed_session(client, db_session, wire, "sse-r7@rvaiglobal.com")
    resp = await client.get(
        f"/v1/build-sessions/{sid}/events", headers={**_cookie(user), "Last-Event-ID": "0"}
    )
    assert resp.status_code == 200
    frames = _frames(resp.text)
    events = [json.loads(f["data"]) for f in frames if f.get("data", "") != "[DONE]"]

    terminals = [e for e in events if e["type"] == "ended"]
    assert len(terminals) == 1
    assert events[-1] is terminals[0]  # terminal is last
    assert terminals[0]["snapshot_committed"] is True
    assert terminals[0]["reason"] == "completed"
    assert terminals[0]["status"] == "ended"
    assert terminals[0]["seq"] == 3  # continues the run's stream — no gap at the handoff
    assert frames[-1]["data"] == "[DONE]"  # …and the feed closed on it


async def test_resume_replays_only_after_last_event_id(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid = await _completed_session(client, db_session, wire, "sse2@rvaiglobal.com")
    resp = await client.get(
        f"/v1/build-sessions/{sid}/events", headers={**_cookie(user), "Last-Event-ID": "1"}
    )
    assert resp.status_code == 200
    frames = _frames(resp.text)
    assert [f["id"] for f in frames if "id" in f] == ["2", "3"]
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
    assert [f["id"] for f in frames if "id" in f] == ["1", "2", "3"]


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
    # Direct drive (no DB / HTTP): a client disconnect drops the subscriber; the session and
    # the task behind it are untouched (the SessionManager owns them).
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
    first = await gen.__anext__()
    assert b"id: 1" in first
    assert len(session.subscribers) == 1  # registered
    await gen.aclose()  # simulate disconnect mid-stream
    assert len(session.subscribers) == 0  # dropped, no leak


def _bare_session() -> BuildSession:
    return BuildSession(
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


async def test_live_session_without_cursor_does_not_replay_the_backlog() -> None:
    # No Last-Event-ID on a STILL-LIVE session -> replay_after = session.last_seq, so a fresh
    # connect is live-from-now: already-buffered frames are NOT re-sent, only frames with
    # seq > last_seq. (An already-ended session takes the full-story branch instead.)
    session = _bare_session()
    session.envelopes.append(StepEvent(seq=1, name="s", label="l", state="started"))
    session.envelopes.append(StepEvent(seq=2, name="s", label="l", state="started"))
    session.last_seq = 2  # replay_after is captured as 2 at build time (session is live)

    resp = build_sse_response(session, None)  # no cursor + terminal_emitted=False -> live
    # `replay_after` is captured when the response is BUILT, so the terminal below has to be
    # appended after this line: append it before, and it reads as backlog nobody asked for.
    session.envelopes.append(
        EndedEvent(
            seq=3,
            status=BuildSessionStatus.ENDED,
            preview_url=None,
            snapshot_committed=True,
            reason="completed",
        )
    )
    session.last_seq = 3
    gen = cast(AsyncGenerator[bytes], resp.body_iterator)
    text = b"".join([chunk async for chunk in gen]).decode()

    assert "id: 1" not in text and "id: 2" not in text  # backlog NOT replayed (idx starts > 2)
    assert "id: 3" in text  # only the future frame
    assert text.rstrip().endswith("[DONE]")
    assert len(session.subscribers) == 0


async def test_sse_recovers_a_dropped_terminal_from_the_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The append-only buffer is authoritative. This drives the RESCAN path: the terminal
    # `ended` never reaches the queue (dropped for a slow client) and only lands in the buffer
    # after the generator has caught up and parked on the rescan timeout. It must still be
    # recovered from the buffer on the next scan and emit [DONE] — never hang.
    monkeypatch.setattr(sse_module, "_BUFFER_RESCAN_SECONDS", 0.01)
    session = _bare_session()
    session.envelopes.append(StepEvent(seq=1, name="s", label="l", state="started"))
    session.last_seq = 1  # NOT terminal yet — the terminal is appended later, never queued

    resp = build_sse_response(session, last_event_id=0)
    gen = cast(AsyncGenerator[bytes], resp.body_iterator)
    collected: list[bytes] = []

    async def drain_gen() -> None:
        async for chunk in gen:
            collected.append(chunk)

    drainer = asyncio.create_task(drain_gen())
    # The terminal is appended to the buffer ONLY — never onto the subscriber queue — and only
    # once the generator has emitted seq 1 and parked on the rescan wait. Queue it, or append
    # it before the park, and the ordinary path carries it instead of the rescan under test.
    await asyncio.sleep(0.05)
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
    await asyncio.wait_for(drainer, timeout=2.0)

    text = b"".join(collected).decode()
    assert "id: 1" in text and "id: 2" in text  # terminal recovered from the buffer via rescan
    assert text.rstrip().endswith("[DONE]")  # closed, no hang
    assert len(session.subscribers) == 0  # subscriber cleaned up
