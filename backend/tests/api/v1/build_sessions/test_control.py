"""Build-session control ops: stop / status (cookie auth + CSRF, owner-scoping).

`start` is gone from the title and from this file. The bare `POST /v1/build-sessions` was
deleted along with `SessionManager.start`, and every test whose subject was that route went with
it; the ones below test surfaces that survive it, re-fixtured onto `a_live_session` — the
`ensure_sandbox` door production uses."""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import PreviewReadyEvent, StepEvent
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.manager import StopOutcome
from src.services.build_sessions.snapshot import RecoveryOutcome, RecoveryWrite
from tests.api.v1.build_sessions.conftest import a_live_session, auth_headers
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def test_status_after_completion_carries_preview_and_last_seq(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The status read reports a FINISHED session's terminal state — the three fields the
    portal branches on, and the reason the route survived the start route's deletion.

    Re-fixtured onto `a_live_session` + the real end sequence. The two progress frames are
    pushed straight through `manager.on_progress` (which documents that it must derive state
    from envelopes handed to it directly) instead of coming out of a brain, and the terminal is
    the one `manager.stop` synthesizes — the same seq-3 `ended` a natural completion produced,
    from the same emitter."""
    user, project = await _user_project(db_session, "ctl5@rvaiglobal.com")
    session = await a_live_session(wire, db_session, user, project.id)
    await wire.manager.on_progress(
        session, StepEvent(seq=1, name="scaffold", label="Scaffolding the app", state="started")
    )
    await wire.manager.on_progress(
        session, PreviewReadyEvent(seq=2, preview_url="https://preview.example/")
    )
    await wire.manager.stop(session, wire.sbx, reason="completed")

    sid = session.session_id
    s = await client.get(f"/v1/build-sessions/{sid}", headers=auth_headers(user))
    assert s.status_code == 200
    body = s.json()
    assert body["status"] == "ended"
    assert body["previewUrl"] == "https://preview.example/"
    assert body["lastSeq"] == 3


async def test_status_of_another_users_session_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    owner, project = await _user_project(db_session, "ctl6a@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="ctl6b@rvaiglobal.com")
    session = await a_live_session(wire, db_session, owner, project.id)
    s = await client.get(
        f"/v1/build-sessions/{session.session_id}", headers=auth_headers(intruder)
    )
    assert s.status_code == 404  # non-leaking


async def test_stop_is_idempotent(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Two stops on one session both answer `ended` — the second joins the first's shielded
    end sequence (`_await_end_sequence`) rather than starting a second one. The session is a
    live workspace rather than a build now; the route's idempotence is unchanged by that."""
    user, project = await _user_project(db_session, "ctl7@rvaiglobal.com")
    session = await a_live_session(wire, db_session, user, project.id)
    sid = session.session_id
    s1 = await client.post(f"/v1/build-sessions/{sid}/stop", json={}, headers=auth_headers(user))
    assert s1.status_code == 200 and s1.json()["status"] == "ended"
    s2 = await client.post(f"/v1/build-sessions/{sid}/stop", json={}, headers=auth_headers(user))
    assert s2.status_code == 200 and s2.json()["status"] == "ended"  # idempotent


# --- stop-and-switch, over HTTP -------------------------------------------------------


async def _stop_active(client: AsyncClient, user, project, *, csrf: bool = True):
    return await client.post(
        f"/v1/build-sessions/projects/{project.id}/stop-active-build",
        headers=auth_headers(user, with_csrf=csrf),
    )


async def _stop_state(client: AsyncClient, user, project):
    """Deliberately WITHOUT the CSRF header. It is a GET that changes nothing, and sending one
    would hide a route that had quietly started requiring it."""
    return await client.get(
        f"/v1/build-sessions/projects/{project.id}/stop-state",
        headers=auth_headers(user, with_csrf=False),
    )


async def _stopped_state(client: AsyncClient, user, project) -> str:
    """THE COMPLETION BARRIER over HTTP: poll the status read until it stops saying "still
    running", exactly as the browser does, and fail loudly if it never does.

    A bounded poll of the real condition rather than a sleep. A fixed sleep here could only be
    too short — and would then report an absence it had never waited long enough to observe,
    which is the harness failure that once cost this repo an entire misdirected investigation."""
    for _ in range(400):
        body = (await _stop_state(client, user, project)).json()
        if body["state"] != "still_running":
            return str(body["state"])
        await asyncio.sleep(0.01)
    raise AssertionError("the stop never left 'still running'")


async def test_stop_active_build_settles_a_live_build_so_release_can_proceed(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """THE ORDERING, end to end over HTTP: while the agent works, save and release BOTH refuse;
    once the STATUS READ says the work has stopped, the release goes through.

    That is the whole reason this route exists. The switch dialog used to offer "Save and switch"
    to a user whose project was mid-build, and the server declined both halves — so the user
    got a choice, then an error, whichever button they pressed.

    THE BARRIER SITS BELOW THE STATUS READ, NOT BELOW THE ASK. The POST does not wait for the
    work to settle — it asks, and answers `still_running` while the stop is in flight — so an
    assertion on its own response proves nothing about whether the system has finished. Only
    the status read, polled until it stops saying "still running", can.

    WHAT PERFORMS THE UNWIND, re-fixtured. The live work is a TURN now, not a `run_build` task:
    `_stop_the_held_session` asks the turn engine to cancel and then reads whether a session
    still holds the app, and the step that actually frees the slot is `finish_turn_sandbox`, run
    by the turn as it unwinds. With no turn engine in this test there is nothing to cancel, so
    that unwind is invoked directly — which is exactly the event the ask is waiting for, and
    keeps the barrier a poll of the real state rather than a wait on a clock."""
    user, project = await _user_project(db_session, "ctl-stop1@rvaiglobal.com")
    session = await a_live_session(wire, db_session, user, project.id)

    # Mid-turn, both onward steps refuse — this is what makes the stop necessary rather than
    # a nicety, and what keeps the ORDER an invariant instead of a client convention.
    save = await client.post(
        f"/v1/build-sessions/projects/{project.id}/save", headers=auth_headers(user)
    )
    assert save.status_code == 409
    assert "still being built" in save.json()["error"]["message"]
    release = await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )
    assert release.status_code == 409

    # The gate stays SHUT while the session holds the workspace: the ask returns immediately and
    # says so, and the release below must still refuse at this point.
    asked = await _stop_active(client, user, project)

    assert asked.status_code == 200
    assert asked.json()["state"] == "still_running"  # the ask returned; the stop is in flight

    # The turn unwinding — the one step that frees the slot the release is waiting on.
    await wire.manager.finish_turn_sandbox(session, wire.sbx, touched=True)

    # THE BARRIER. Nothing below this line runs until the status read says the work has stopped,
    # and it is a poll of the real state rather than a wait on a clock.
    assert await _stopped_state(client, user, project) == "stopped"

    after = await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )
    assert after.status_code != 409


def test_the_published_api_names_the_stop_states_the_wire_actually_sends(app: FastAPI) -> None:
    """FastAPI publishes a route's docstring as its OpenAPI description, so prose in a route is
    API surface. `CamelModel` camelizes FIELD names only — `StopOutcome` values go out verbatim —
    so a docstring saying `nothingWasRunning` tells a reader to branch on a value the wire never
    sends. A client written from it falls through its own guard and, on the shape the portal
    uses, reads every answer as "still running": a hand-over that can never complete.

    SPELLING-BLIND rather than a list of known wrong spellings: any backticked token shaped
    like a state name with its separators or casing changed is unmatchable, however it gets
    respelled later. Python MEMBER names (`STILL_RUNNING`) are allowed beside the values — that
    prose names the symbol, not the wire value."""
    # Mutation check: put `nothingWasRunning` back in any of the stop docstrings and this goes red.
    published = json.dumps(app.openapi())
    spellings = {outcome.value for outcome in StopOutcome} | {
        outcome.name for outcome in StopOutcome
    }
    flattened = {outcome.value.replace("_", ""): outcome.value for outcome in StopOutcome}
    named = {
        token
        for token in re.findall(r"`([A-Za-z_]+)`", published)
        if token.lower().replace("_", "") in flattened
    }
    # LIVENESS: an empty set satisfies the loop below trivially, and is also what a schema that
    # failed to render its descriptions produces.
    assert named, "no stop state is named anywhere in the published schema"
    for token in sorted(named):
        assert token in spellings, (
            f"the published API tells a client to branch on `{token}`; the wire sends "
            f"`{flattened[token.lower().replace('_', '')]}`"
        )


async def test_stopping_a_settled_project_says_nothing_was_running_not_an_error(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """`nothing_was_running` is the answer, not a 409. The caller wants "settled" and it already is
    — and the common path is exactly this, because the build usually finishes while the user
    is still reading the dialog.

    ITS OWN STATE NOW, where the old `stopped: false` shared a field with a timeout's hardcoded
    `true`. The status read agrees, which is the property the browser depends on: an ask and a
    read of the same untouched project cannot disagree."""
    user, project = await _user_project(db_session, "ctl-stop2@rvaiglobal.com")
    resp = await _stop_active(client, user, project)
    assert resp.status_code == 200
    assert resp.json()["state"] == "nothing_was_running"

    read = await _stop_state(client, user, project)
    assert read.status_code == 200
    assert read.json()["state"] == "nothing_was_running"


async def test_stop_active_build_is_owner_scoped_and_csrf_guarded(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Owner-scoping and CSRF on a route that KILLS WORK IN PROGRESS. Another user's project is a
    non-leaking 404, and a cookie without the CSRF header is refused — a forged cross-site POST
    here would destroy an unfinished build."""
    owner, project = await _user_project(db_session, "ctl-stop3@rvaiglobal.com")
    stranger = await UserFactory.create(db_session, email="ctl-stop4@rvaiglobal.com")

    assert (await _stop_active(client, stranger, project)).status_code == 404
    assert (await _stop_active(client, owner, project, csrf=False)).status_code == 403


async def test_the_stop_state_read_is_owner_scoped_and_needs_no_csrf(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Owner-scoping on the new half of the pair, and the reason it is a GET.

    It changes nothing — no cancel, no teardown, nothing written — so a CSRF header would be
    ceremony, and the browser polls it while it narrates. What it DOES leak if unscoped is
    whether another citizen's project is busy, so a stranger gets the same non-leaking 404 the
    ask gives."""
    owner, project = await _user_project(db_session, "ctl-stop5@rvaiglobal.com")
    stranger = await UserFactory.create(db_session, email="ctl-stop6@rvaiglobal.com")

    assert (await _stop_state(client, stranger, project)).status_code == 404
    mine = await _stop_state(client, owner, project)
    assert mine.status_code == 200  # no CSRF header sent, and none needed
    assert mine.json()["state"] == "nothing_was_running"


async def test_stop_active_build_answers_without_redis(
    client: AsyncClient, db_session: AsyncSession, fake_storage, wire
) -> None:
    """This route ANSWERS with no Redis, where `release` and `save` must refuse — and that
    asymmetry is the reason it carries no `build_coordination_or_503` seam.

    Those two ask the registry what is live, so an absent coordination subsystem leaves them
    deciding nothing. This one asks "is this process running work for this user?", which lives
    in `_active_by_user` and is answerable regardless. Wrapping it in the seam produced a
    trailing `_coordination_is_gone()` that could never execute — a dead arm — and would have
    refused on the one path that matters: a live in-process build during a Redis outage is
    exactly when a user still needs to stop it.

    Deliberately takes no `fake_redis` fixture: with the singleton unset `get_redis()` raises
    `RedisNotConfiguredError`, which is what a deployment with no Redis configured does."""
    user, project = await _user_project(db_session, "ctl-stop-noredis@rvaiglobal.com")
    resp = await _stop_active(client, user, project)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "nothing_was_running"}  # and it could still say so

    # The status read carries the same asymmetry, and needs it more: this is what the browser
    # polls, so a Redis outage that silenced it would strand a hand-over mid-narration.
    read = await _stop_state(client, user, project)
    assert read.status_code == 200, read.text
    assert read.json() == {"state": "nothing_was_running"}


# --- release answers for ONE project, not for the person ----------------------------


async def test_releasing_an_idle_project_is_not_refused_because_another_one_is_live(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Over the wire, with one workspace and two projects: only the project actually holding
    the container may refuse its release.

    Refusing for every project a person owns leaves them no way to free the slot but to wait,
    since giving up an idle project is the way out of the reclaim refusal.

    Mutation-check: refuse on any entry in `_active_by_user` and the first release below is a
    409 for a project that holds nothing."""
    user, live_project = await _user_project(db_session, "ctl-rel1@rvaiglobal.com")
    idle_project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(db_session, user_id=user.id, project_id=idle_project.id)
    await a_live_session(wire, db_session, user, live_project.id)

    idle = await client.post(
        f"/v1/build-sessions/projects/{idle_project.id}/release", headers=auth_headers(user)
    )

    assert idle.status_code == 200, idle.text
    assert idle.json()["released"] is False  # nothing of this project's was up to give up
    # ...and the project that IS holding the workspace still refuses, which is what keeps this
    # from admitting a second container for one person.
    held = await client.post(
        f"/v1/build-sessions/projects/{live_project.id}/release", headers=auth_headers(user)
    )
    assert held.status_code == 409, held.text
    assert wire.sbx.torn_down == []  # neither container was taken


async def test_the_release_refusal_names_the_project_holding_the_workspace(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """★ A citizen who asked to close a workspace from a project list is told which project is
    still working, so "finish or stop it" points somewhere.

    Turn red by putting the bare "a build session is already active" back: it names no project,
    and the code beside it is for the client rather than the person reading."""
    user, project = await _user_project(db_session, "ctl-rel2@rvaiglobal.com")
    project.name = "Visitor Log"
    await db_session.flush()
    await a_live_session(wire, db_session, user, project.id)

    refused = await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "build_session_already_active", "the client still branches on this"
    assert "“Visitor Log”" in error["message"]


# --- the window between a turn's terminal and its release ------------------------------


async def test_a_finished_turn_keeps_the_workspace_until_its_recovery_copy_is_written(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire, monkeypatch
) -> None:
    """The two halves of the turn seam, over the wire and at once: the release still refuses
    while the finished turn writes its recovery copy, and the next message waits for that write
    instead of being refused.

    The ordering is the point. Admitting the next message by freeing the slot ahead of the
    recovery copy would buy the same green test and cost the citizen the one copy standing
    between them and a lost session — the release would be admitted mid-write and tear the
    container down underneath it.

    Mutation check: move the `_active_by_user` pop above the recovery write in
    `finish_turn_sandbox` and the release below answers 200."""
    user, project = await _user_project(db_session, "ctl-finish1@rvaiglobal.com")
    session = await a_live_session(wire, db_session, user, project.id)
    wire.sbx.attach_handle = session.handle  # the pardoned container answers the next message

    entered, gate = asyncio.Event(), asyncio.Event()

    async def gated_recovery_copy(*_args: object, **_kwargs: object) -> RecoveryWrite:
        entered.set()
        await gate.wait()
        return RecoveryWrite(outcome=RecoveryOutcome.WRITTEN, reason="written")

    monkeypatch.setattr(manager_module, "write_recovery_copy", gated_recovery_copy)
    finishing = asyncio.create_task(
        wire.manager.finish_turn_sandbox(session, wire.sbx, touched=True)
    )
    await entered.wait()

    held = await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )
    assert held.status_code == 409, held.text
    assert wire.sbx.torn_down == []  # nothing was taken out from under the write

    # ...and the next message, arriving in that same window, waits rather than bouncing.
    next_message = asyncio.create_task(a_live_session(wire, db_session, user, project.id))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not next_message.done()

    gate.set()
    await finishing
    assert (await next_message).session_id != session.session_id
