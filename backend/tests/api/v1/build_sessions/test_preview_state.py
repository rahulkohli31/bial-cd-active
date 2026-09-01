"""`GET /v1/build-sessions/projects/{id}/preview-state` — five states, not one boolean.

C3 §8.3 / R16–R18. The route used to answer `alive: false` identically for *never built*,
*another project took the slot*, *asleep*, and *the registry read threw* — and the portal
rendered all four as "your preview is gone", including the one that was an ERROR rather than a
fact about anything. U13 adds a fifth: *a start is in flight right now*, which used to be
indistinguishable from `asleep` and invited a second press to provision a second container.

Invariants load-bearing here, each with its own test:

* **An unknown is its own answer.** A registry read that failed decided nothing, so it is
  `unknown` — never `asleep`, never `never_built`, and above all never an error the citizen has
  to read. Same rule for `restorable`, which is tri-state for the same reason `dirty` is.
* **The poll stays cheap.** The caller is a browser tab on a 45-second timer. C3 §8.3 freezes
  the budget: one ROUND TRIP to Redis (two commands, pipelined: the registry hash and the U13
  starting marker), at most two user-scoped rows, at most two object-store HEADs, and NO
  container command or attach — which R14 forbids outright, because a poll that touched the
  container would make every framed preview look busy forever and nothing would ever be
  reclaimed.
* **R5 — starting is never destructive.** The signal→action mapping (`PREVIEW_STATE_ACTION`)
  is total over the enum and no ambiguous or timed-out signal may map to the one action that can
  destroy or restore — proved directly against the recorded incident this rule exists because of
  (`docs/solutions/logic-errors/readiness-timeout-triggers-destructive-sandbox-restore-2026-08-02.md`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    PREVIEW_STATE_ACTION,
    PreviewLifeState,
    PreviewStateAction,
)
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import write_starting_marker
from src.services.build_sessions.manager import app_name_for
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    starting_key,
)
from src.services.sandbox import SandboxHandle, SandboxNotReadyError
from src.services.storage import StorageError, recovery_key, snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ProjectFactory, UserFactory


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _built(db: AsyncSession, user, project) -> uuid.UUID:
    """Give the project an app row — i.e. it has been built at least once — without staging
    any bundle. `_existing_app_id` is what separates NEVER_BUILT from every other state."""
    app_id = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    return app_id


async def _register_container(redis, user_id: uuid.UUID, app_name: str, *, state: str) -> None:
    """Put a live container in the one-per-user registry hash, by hand.

    Deliberately not via a relaunch: the whole point of this route is that it reads the
    registry and nothing else, so the test should be able to state the registry's contents
    outright rather than arriving at them through a provisioning path."""
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example.azurecontainerapps.io",
            REGISTRY_FIELD_TOKEN_REF: f"ref-{app_name}",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: state,
        },
    )


async def _probe(client: AsyncClient, user, project) -> dict[str, Any]:
    resp = await client.get(
        f"/v1/build-sessions/projects/{project.id}/preview-state", headers=auth_headers(user)
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    # THE INVARIANT the retained field exists to keep: `alive` is strictly `state == "alive"`,
    # so a browser tab that predates this reshape and still reads `alive` cannot be told
    # anything this response does not mean. Asserted on EVERY probe rather than once.
    assert body["alive"] is (body["state"] == "alive")
    return body


@pytest.fixture
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse `head_presence`'s retry backoff. The retry ladder is the reason an unreachable
    store answers `null` instead of `false`; the sleeping is not the part under test."""
    from src.services.build_sessions import manager as manager_module

    async def no_waiting(_seconds: float) -> None:
        return None

    monkeypatch.setattr(manager_module, "_asleep", no_waiting)


# --------------------------------------------------------------------------------------
# The four states, plus the unknown that used to masquerade as one of them
# --------------------------------------------------------------------------------------


async def test_a_project_nobody_ever_built_says_so(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """NEVER_BUILT — and `restorable` is a CONFIRMED false, not an unknown: with no app row
    there is no key a bundle could live under, so declining to call the store is an answer
    rather than an omission."""
    user, project = await _user_project(db_session, "ps-new@rvaiglobal.com")

    body = await _probe(client, user, project)

    assert body["state"] == "never_built"
    assert body["restorable"] is False
    assert body["previewUrl"] is None


async def test_a_reclaimed_workspace_is_asleep_and_offers_the_work_back(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """AE9 + AE10, and the reason `restorable` exists at all.

    The builder worked across several turns and NEVER PRESSED SAVE, so there is no saved
    bundle — only the platform's turn-boundary recovery copy. Their container was then
    reclaimed, so there is no live sandbox either, which is exactly the case `recoveryAt`
    cannot serve: it is written inside `_save_state_of`, after a successful attach AND a
    container read, so it is null here.

    The honest answer is "asleep, and yes we can bring it back" — not "gone", and emphatically
    not "this project has no saved build".

    Mutation-check: point `restorable_presence` at `snapshot_key` alone (its shipped
    predecessor) and `restorable` comes back False, which is the sentence this test exists to
    stop the product saying."""
    user, project = await _user_project(db_session, "ps-asleep@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(recovery_key(app_id), b"RECOVERY-BUNDLE")
    assert snapshot_key(app_id) not in fake_storage.objects, "the user never pressed Save"

    body = await _probe(client, user, project)

    assert body["state"] == "asleep"
    assert body["restorable"] is True
    assert body["alive"] is False


async def test_a_saved_build_with_no_recovery_copy_is_also_restorable(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The other arm of the OR. `restorable` is `recovery_key` OR `snapshot_key` — the same
    pair `newest_restore_source` consults — so a user who pressed Save and nothing else is
    offered their app back exactly as readily."""
    user, project = await _user_project(db_session, "ps-saved@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    body = await _probe(client, user, project)

    assert (body["state"], body["restorable"]) == ("asleep", True)


async def test_a_built_project_with_nothing_stored_is_confirmed_unrestorable(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Both keys confirmed absent → a real `false`. This is the state that legitimately says
    "there is nothing to relaunch yet"; it has to stay reachable, or the UI copy for it is
    dead code."""
    user, project = await _user_project(db_session, "ps-empty@rvaiglobal.com")
    await _built(db_session, user, project)

    body = await _probe(client, user, project)

    assert (body["state"], body["restorable"]) == ("asleep", False)


async def test_a_live_container_for_this_project_is_alive_with_a_framable_url(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-alive@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert body["alive"] is True
    # THE PUBLIC address, not the container's own. This site composes the URL without ever
    # building a `SandboxHandle`, so it is invisible to anything that follows the handle's field
    # — and it is what the cockpit frames, so getting it wrong shows a blank preview over a
    # perfectly healthy container.
    assert body["previewUrl"] == (f"https://citizenapps.bialairport.com/a/{app_name_for(app_id)}")
    assert "azurecontainerapps.io" not in body["previewUrl"]
    assert body["occupyingProjectName"] is None  # nobody is standing in the way


async def test_another_project_holding_the_slot_is_named(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """SLOT_TAKEN, WITH the name. The registry is one-per-user, so "somebody else's container
    is up" IS the shape of "yours is not" — but the builder is being told about their own two
    projects, and a nameless "your preview is gone" made that look like a platform failure
    instead of the ordinary consequence of having switched projects."""
    user, mine = await _user_project(db_session, "ps-taken@rvaiglobal.com")
    theirs = await ProjectFactory.create(db_session, user.id, name="Baggage Reconciliation")
    await _built(db_session, user, mine)
    other_app = await _built(db_session, user, theirs)
    await _register_container(
        fake_redis, user.id, app_name_for(other_app), state=REGISTRY_STATE_READY
    )

    body = await _probe(client, user, mine)

    assert body["state"] == "slot_taken"
    assert body["occupyingProjectName"] == "Baggage Reconciliation"
    assert body["occupyingProjectId"] == str(theirs.id)
    assert body["previewUrl"] is None, "the other project's URL is not this project's preview"


async def test_an_unattributable_container_takes_the_slot_without_naming_anyone(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """A ghost — a container the registry names that matches no app this user owns. The slot is
    genuinely taken, and we decline to guess whose work is in it: naming the wrong project in a
    sentence about somebody's unsaved work is worse than naming none."""
    user, project = await _user_project(db_session, "ps-ghost@rvaiglobal.com")
    await _built(db_session, user, project)
    await _register_container(fake_redis, user.id, "sbx-somebodyelses", state=REGISTRY_STATE_READY)

    body = await _probe(client, user, project)

    assert body["state"] == "slot_taken"
    assert body["occupyingProjectName"] is None
    assert body["occupyingProjectId"] is None


async def test_a_container_of_ours_mid_teardown_reads_as_asleep_not_taken(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """`ending` is the reaper's durable mark: the container is ours and on its way out. From
    the builder's side that is a workspace going to sleep, not one somebody stole — and the
    next prompt brings it back, which is what the copy for `asleep` promises."""
    user, project = await _user_project(db_session, "ps-ending@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_ENDING
    )

    body = await _probe(client, user, project)

    assert body["state"] == "asleep"
    assert body["previewUrl"] is None, "never hand back a URL for a container being destroyed"


# --------------------------------------------------------------------------------------
# The unknowns — the two answers that must never be coerced into a neighbour
# --------------------------------------------------------------------------------------


async def test_a_registry_read_failure_is_unknown_not_gone(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE defect this unit was written for. A store that would not answer decided nothing, and
    a thing that decided nothing is not evidence that a container is gone. The old code
    returned `alive=false` here, indistinguishable from a real teardown, and the portal pulled
    a live preview off the screen because Redis blinked.

    Not a 503 either: the caller is a background timer, and 503ing it would turn a blip into an
    error message the citizen has to read and cannot act on.

    Mutation-check: fold the `except RedisError` arm back into the `asleep` return and this
    goes red on the `state` assertion."""
    user, project = await _user_project(db_session, "ps-blip@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    from src.services.build_sessions import manager as manager_module

    async def the_store_will_not_answer(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("connection refused (pipeline)")

    # U13 — the route reads through `read_registry_and_starting_marker` (the pipelined
    # registry + marker read), not a bare `read_registry`, so that is the seam to fail.
    monkeypatch.setattr(
        manager_module, "read_registry_and_starting_marker", the_store_will_not_answer
    )

    body = await _probe(client, user, project)

    assert body["state"] == "unknown"
    assert body["state"] not in {"asleep", "never_built", "slot_taken"}
    assert body["alive"] is False  # `alive` cannot express this, which is why `state` exists
    # The store question is INDEPENDENT of the registry question, and still answerable.
    assert body["restorable"] is True


async def test_restorable_is_null_when_the_object_store_is_unreachable(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    instant_backoff: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tri-state, per C3 §8.2: `null` is UNKNOWN and the client claims nothing from it. A
    `false` here would render "there is nothing to relaunch yet — this project has no saved
    build" to a user whose entire workspace is sitting on Blob, unread.

    Mutation-check: make `restorable_presence` return `False` on an unreadable store and this
    goes red — which is precisely the coercion the tri-state exists to prevent."""
    user, project = await _user_project(db_session, "ps-storeblip@rvaiglobal.com")
    await _built(db_session, user, project)

    async def the_store_will_not_answer(_key: str) -> None:
        raise StorageError("azure said no", provider="fake")

    monkeypatch.setattr(fake_storage, "head", the_store_will_not_answer)

    body = await _probe(client, user, project)

    assert body["restorable"] is None
    # The registry was perfectly readable; only the store was not. Two questions, two answers.
    assert body["state"] == "asleep"


async def test_one_readable_key_is_enough_to_answer_even_when_the_other_is_not(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    instant_backoff: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kleene, not "any failure poisons the answer": a CONFIRMED bundle is a fact, and an
    unreadable second key cannot unmake it. Offering a restore we can actually perform beats
    declining one out of tidiness."""
    user, project = await _user_project(db_session, "ps-halfblind@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(recovery_key(app_id), b"RECOVERY-BUNDLE")

    readable = fake_storage.head

    async def only_the_saved_key_is_unreadable(key: str):
        if key == snapshot_key(app_id):
            raise StorageError("azure said no", provider="fake")
        return await readable(key)

    monkeypatch.setattr(fake_storage, "head", only_the_saved_key_is_unreadable)

    body = await _probe(client, user, project)

    assert body["restorable"] is True


# --------------------------------------------------------------------------------------
# `starting` — a start in flight is a fact the platform holds (U13)
# --------------------------------------------------------------------------------------


async def test_a_start_in_flight_reads_as_starting_from_a_different_request_and_after_reload(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Covers AE54a. The marker is a fact in Redis, not a tab's own memory: a fresh HTTP
    request (standing in for a different browser tab) and a repeated probe of the same request
    (standing in for a reload thirty seconds later) both read `starting`, because neither reads
    anything a browser ever held."""
    user, project = await _user_project(db_session, "ps-starting@rvaiglobal.com")
    await write_starting_marker(fake_redis, user.id, project.id)

    first = await _probe(client, user, project)  # a different session's request
    second = await _probe(client, user, project)  # the simulated reload, moments later

    assert first["state"] == "starting"
    assert second["state"] == "starting"
    assert first["previewUrl"] is None
    assert first["restorable"] is None  # no claim — a start offers no restore affordance


async def test_a_completed_start_clears_the_marker_and_the_next_read_answers_alive(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-start-done@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await write_starting_marker(fake_redis, user.id, project.id)

    assert (await _probe(client, user, project))["state"] == "starting"

    # The start completes: `_holding_user_lock`'s clean exit clears the marker and the
    # container is now registered and serving — exactly what a real completion leaves behind.
    await fake_redis.delete(starting_key(user.id))
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )

    body = await _probe(client, user, project)
    assert body["state"] == "alive"


async def test_a_failed_start_clears_the_marker_through_compensation_and_reads_asleep(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case, driven through the REAL start path rather than by hand-clearing the marker:
    a provisioning failure inside `ensure_sandbox` (the turn's door into `_holding_user_lock`,
    same skeleton as a build start or a relaunch) must leave no marker behind, or a citizen
    whose start genuinely failed would see `starting` forever instead of the truthful `asleep`
    that invites a retry."""
    from src.services.sandbox import SandboxError

    user, project = await _user_project(db_session, "ps-start-failed@rvaiglobal.com")

    async def provisioning_blows_up(*_args: object, **_kwargs: object) -> SandboxHandle:
        raise SandboxError("the container never came up")

    monkeypatch.setattr(wire.sbx, "provision_new", provisioning_blows_up)

    with pytest.raises(SandboxError):
        await wire.manager.ensure_sandbox(
            db_session, user, project.id, sandbox_client=wire.sbx, may_write=True
        )

    assert await fake_redis.exists(starting_key(user.id)) == 0  # compensation cleared it

    body = await _probe(client, user, project)
    assert body["state"] == "asleep"  # not "starting forever"


async def test_an_abandoned_marker_expires_and_the_next_read_falls_back_to_the_registry(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Edge case: a marker with no writer left to clear it (the process died) is a BOUNDED
    claim, not a pardon — past its TTL the read falls straight through to whatever the
    registry says, exactly as if the marker had never existed. Simulated by deleting the key
    directly (fakeredis has no fast-forward clock); from a reader's side that is indistinguishable
    from the TTL having done it."""
    user, project = await _user_project(db_session, "ps-abandoned@rvaiglobal.com")
    await _built(db_session, user, project)
    await write_starting_marker(fake_redis, user.id, project.id)
    assert (await _probe(client, user, project))["state"] == "starting"

    await fake_redis.delete(starting_key(user.id))  # the TTL lapsing

    body = await _probe(client, user, project)
    assert body["state"] == "asleep"  # the registry's own answer, nothing left claiming a start


async def test_a_marker_naming_another_project_is_slot_taken_and_names_it(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Edge case. Better than today's ghost: the marker already carries the occupying
    project's id, so naming it needs no inversion of a container name and no registry entry to
    exist yet — this is reachable BEFORE the starting build has provisioned anything."""
    user, mine = await _user_project(db_session, "ps-marker-taken@rvaiglobal.com")
    theirs = await ProjectFactory.create(db_session, user.id, name="Runway Allocation")
    await write_starting_marker(fake_redis, user.id, theirs.id)

    body = await _probe(client, user, mine)

    assert body["state"] == "slot_taken"
    assert body["occupyingProjectId"] == str(theirs.id)
    assert body["occupyingProjectName"] == "Runway Allocation"


async def test_a_marker_naming_this_project_while_the_registry_already_serves_it_is_alive(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Edge case, and the precedence rule this pins directly: a STALE marker must never hide a
    running app. If a completion's marker-clear were lost (a Redis blip in the compensation
    arm) but the container came up anyway, the citizen must still see their live app, not a
    spinner for a start that already finished."""
    user, project = await _user_project(db_session, "ps-stale-marker@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )
    await write_starting_marker(fake_redis, user.id, project.id)

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert body["alive"] is True


# --------------------------------------------------------------------------------------
# The cost budget (C3 §8.3) — this is what keeps R14 honest
# --------------------------------------------------------------------------------------


async def test_a_poll_runs_no_command_in_the_container_and_never_attaches(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen budget, asserted on the fake's call log rather than trusted to a comment.

    Two independent reasons, either sufficient. (1) Reusing the reclaim guard would drag in
    `_attach_for_read` and `_save_state_of` — a container round trip — and would let a
    `RedisError` turn a poll into a 503. (2) R14: a poll that touched the container would be a
    manufactured activity signal, so every framed preview would look busy forever and the
    reclaimer would never take one back. A 45-second heartbeat against a container nobody is
    using is the exact fiction this whole plan exists to remove.

    Mutation-check: have `project_preview_state` call `_attach_for_read` and this goes red on
    `attaches`."""
    user, project = await _user_project(db_session, "ps-cheap@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(recovery_key(app_id), b"RECOVERY-BUNDLE")
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )

    commands: list[list[str]] = []
    attaches: list[str] = []
    ran, attached = wire.sbx.exec, wire.sbx.attach_existing

    async def record_a_command(handle, cmd, **kwargs):
        commands.append(cmd)
        return await ran(handle, cmd, **kwargs)

    async def record_an_attach(user_id: str):
        attaches.append(user_id)
        return await attached(user_id)

    monkeypatch.setattr(wire.sbx, "exec", record_a_command)
    monkeypatch.setattr(wire.sbx, "attach_existing", record_an_attach)

    body = await _probe(client, user, project)
    assert body["state"] == "alive"  # the expensive-looking state, and still nothing was spent

    assert commands == [], "a browser-timer poll must never run a command in the container"
    assert attaches == [], "…nor attach to it (R14: that is a manufactured activity signal)"
    assert (wire.sbx.provisioned, wire.sbx.restored, wire.sbx.torn_down) == ([], [], [])
    assert wire.sbx.warmed == []


async def test_the_alive_path_spends_nothing_on_the_object_store(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE HOT PATH PAYS FOR NOTHING IT DOES NOT USE (C3 §8.3).

    A healthy container is what the overwhelming majority of polls find, and the restore
    question is the one thing on this route that leaves the process: one or two Blob `HEAD`s.
    Asking it unconditionally meant every framed tab spent a Blob round trip every 45 seconds
    to answer "could we put this app back?" about an app that was, at that moment, running —
    and no surface renders that answer while it is (the Relaunch affordance and the "nothing is
    lost" copy exist only on the states where nothing is serving the project).

    So `alive` answers `restorable: null` — NO CLAIM, the same instruction to the client as an
    unreachable store, which its `restorable ?? hasSavedBuild` already falls through. The
    project route (`hasRelaunchableSnapshot`) answers the same question the same way at load,
    so nothing the client can render loses its source.

    Mutation-check: hoist `restorable_presence` back above the registry read (its shipped
    position) and `heads` comes back with the recovery key in it — this goes red immediately."""
    user, project = await _user_project(db_session, "ps-hotpath@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    # A recovery copy EXISTS: the answer is genuinely available, and still not worth asking for.
    await fake_storage.put(recovery_key(app_id), b"RECOVERY-BUNDLE")
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )

    heads: list[str] = []
    read_head = fake_storage.head

    async def record_a_head(key: str):
        heads.append(key)
        return await read_head(key)

    monkeypatch.setattr(fake_storage, "head", record_a_head)

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert heads == [], "an alive poll must not touch the object store at all"
    assert body["restorable"] is None, "no claim — not a `false`, which would deny a real bundle"

    # …and the moment the container is gone, the question earns its round trip again: this is
    # a cost fix, not a quiet retirement of the signal the gone card is built on.
    await fake_redis.delete(registry_key(user.id))
    body = await _probe(client, user, project)

    assert (body["state"], body["restorable"]) == ("asleep", True)
    assert heads == [recovery_key(app_id)], "one HEAD, and only where the answer is rendered"


async def test_every_state_is_reachable_and_they_are_all_different(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The round-trip anchor: drive one user through the whole ladder and assert the wire
    values are five distinct answers. A regression that collapses two of them back into one
    boolean fails here even if every single-state test above were somehow still green."""
    user, mine = await _user_project(db_session, "ps-ladder@rvaiglobal.com")

    seen = [(await _probe(client, user, mine))["state"]]  # never_built

    app_id = await _built(db_session, user, mine)
    seen.append((await _probe(client, user, mine))["state"])  # asleep

    await write_starting_marker(fake_redis, user.id, mine.id)
    seen.append((await _probe(client, user, mine))["state"])  # starting
    await fake_redis.delete(starting_key(user.id))

    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )
    seen.append((await _probe(client, user, mine))["state"])  # alive

    theirs = await ProjectFactory.create(db_session, user.id, name="Stand Allocation")
    other_app = await _built(db_session, user, theirs)
    await _register_container(
        fake_redis, user.id, app_name_for(other_app), state=REGISTRY_STATE_READY
    )
    seen.append((await _probe(client, user, mine))["state"])  # slot_taken

    assert seen == ["never_built", "asleep", "starting", "alive", "slot_taken"]
    assert len(set(seen)) == 5, "five states, not one boolean"


# --------------------------------------------------------------------------------------
# U13's cost budget amendment — one round trip, two pipelined commands
# --------------------------------------------------------------------------------------


async def test_the_registry_and_marker_are_read_in_one_pipelined_round_trip(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The amended budget (C3 §8.3): the poll spends ONE round trip on the coordination store —
    a pipeline carrying two commands (the registry hash and the starting marker) — never two
    sequential awaits. Asserted on the client's own call log, because "pipelined" is a claim
    about *how many round trips*, and only the transport itself can say that.

    Mutation-check: replace `read_registry_and_starting_marker`'s pipeline with two sequential
    `redis.hgetall` / `redis.get` calls and `pipelines` goes to `[]` while `bare_reads` goes to
    `2` — this test catches exactly that regression."""
    user, project = await _user_project(db_session, "ps-pipeline@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )

    pipelines: list[object] = []
    bare_reads: list[str] = []
    real_pipeline = fake_redis.pipeline
    real_hgetall = fake_redis.hgetall
    real_get = fake_redis.get

    def recording_pipeline(*args: object, **kwargs: object):
        pipe = real_pipeline(*args, **kwargs)
        pipelines.append(pipe)
        return pipe

    async def recording_hgetall(*args: object, **kwargs: object):
        bare_reads.append("hgetall")
        return await real_hgetall(*args, **kwargs)

    async def recording_get(*args: object, **kwargs: object):
        bare_reads.append("get")
        return await real_get(*args, **kwargs)

    monkeypatch.setattr(fake_redis, "pipeline", recording_pipeline)
    monkeypatch.setattr(fake_redis, "hgetall", recording_hgetall)
    monkeypatch.setattr(fake_redis, "get", recording_get)

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert len(pipelines) == 1, "one round trip, not two sequential ones"
    assert bare_reads == [], "the registry and marker travel inside the pipeline, not beside it"


# --------------------------------------------------------------------------------------
# R5 — starting is never destructive (U13)
# --------------------------------------------------------------------------------------


def test_every_preview_life_state_maps_to_exactly_one_action() -> None:
    """R5, first half, as a table over the enum rather than a claim in a docstring: a state
    added later with no entry in `PREVIEW_STATE_ACTION` fails this test loudly instead of
    quietly rendering a button whose meaning nobody chose."""
    assert set(PREVIEW_STATE_ACTION) == set(PreviewLifeState)
    for state in PreviewLifeState:
        assert PREVIEW_STATE_ACTION[state] in set(PreviewStateAction)


def test_no_ambiguous_state_maps_to_the_remedy_action() -> None:
    """R5, second half — the one that matters. `REMEDY` is the one bucket that can be
    consequential (releasing ANOTHER project's container), and `UNKNOWN` — the state built
    from a read that decided nothing — must never map to it. `STARTING` is the other state a
    reader might expect to gate something; it maps to `NEITHER`, because nothing may be
    offered on top of a start already in flight."""
    assert PREVIEW_STATE_ACTION[PreviewLifeState.UNKNOWN] is not PreviewStateAction.REMEDY
    assert PREVIEW_STATE_ACTION[PreviewLifeState.UNKNOWN] == PreviewStateAction.RETRY
    assert PREVIEW_STATE_ACTION[PreviewLifeState.STARTING] == PreviewStateAction.NEITHER


async def test_a_readiness_timeout_on_the_attach_arm_is_non_destructive_and_the_triple_holds(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5, second half, asserted against L3's own recorded incident rather than the enum table
    alone — this is the scenario Plan F's start button rests on
    (`docs/solutions/logic-errors/readiness-timeout-triggers-destructive-sandbox-restore-2026-08-02.md`).

    A readiness TIMEOUT is not a death certificate: the attach arm fails OPEN (`ready=False`,
    no registry `ending` mark), and L3's confirmation triple — the thing that tells a rollback
    apart from every innocent explanation — still holds afterwards: nothing was torn down or
    re-provisioned (the fake's own "planted marker" — its call counts survive untouched), the
    save-state answer is byte-for-byte the same, and the saved snapshot's bytes and write time
    are unchanged (this fake's `etag` is always `None`, so the write time is what stands in for
    it — a `put` bumps it on every write, so an unchanged value IS the unchanged-etag claim)."""
    user, project = await _user_project(db_session, "ps-timeout-confirm@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    # A cold relaunch first, to leave a real container up and registered for this app, and
    # ATTACHABLE from here on — set before either save-state baseline so "before" and "after"
    # differ only in whether the second relaunch's wait timed out, never in what the fake is
    # newly capable of answering.
    cold = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert cold.status_code == 200
    wire.sbx.attach_handle = SandboxHandle(
        fqdn="live.example",
        token="tok",
        app_name=app_name_for(app_id),
        preview_url="https://live.example",
        ready=True,
    )

    save_state_url = f"/v1/build-sessions/projects/{project.id}/save-state"
    before_save_state = (await client.get(save_state_url, headers=auth_headers(user))).json()
    provisioned_before = list(wire.sbx.provisioned)
    restored_before = list(wire.sbx.restored)
    torn_down_before = list(wire.sbx.torn_down)
    snapshot_before = fake_storage.objects[snapshot_key(app_id)]
    mtime_before = fake_storage.mtimes[snapshot_key(app_id)]

    # …then relaunch again with a dev server that never comes back ready — the exact shape of
    # the incident: the container is alive, the citizen's own root route is merely slow.
    async def the_dev_server_never_answers(handle: SandboxHandle, *, timeout_s: float = 120.0):
        raise SandboxNotReadyError("the app root never served")

    monkeypatch.setattr(wire.sbx, "wait_ready", the_dev_server_never_answers)

    timed_out = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    # THE NON-DESTRUCTIVE ANSWER: fails open, never a 503, never claims to be serving.
    assert timed_out.status_code == 200
    assert timed_out.json()["ready"] is False
    reg = await fake_redis.hgetall(registry_key(user.id))
    assert reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_ENDING

    # THE CONFIRMATION TRIPLE (L3): nothing that could destroy work actually ran, the platform's
    # belief about unsaved work did not move, and the saved bundle was READ, never rewritten.
    assert wire.sbx.provisioned == provisioned_before
    assert wire.sbx.restored == restored_before
    assert wire.sbx.torn_down == torn_down_before  # "the planted marker is present"
    after_save_state = (await client.get(save_state_url, headers=auth_headers(user))).json()
    assert after_save_state == before_save_state  # dirty (and everything else) unchanged
    assert fake_storage.objects[snapshot_key(app_id)] == snapshot_before
    assert fake_storage.mtimes[snapshot_key(app_id)] == mtime_before  # the etag proxy


async def test_an_attach_that_cannot_confirm_anything_refuses_rather_than_restoring(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 / L3 / L7, proof TWO — the one the merged code did not have, on the exact arm the
    citizen-facing start control enters.

    THE DEFECT THIS PINS. `SandboxUnreachableError` is a SUBCLASS of `NoLiveSandboxError`, and
    `relaunch_preview`'s attach fork caught the PARENT. So the one case meaning *"the registry
    says a container is live, the attach could not confirm anything, and it may well be up
    holding hours of unsaved work"* was swallowed by the handler written for *"certain
    absence"* and fell straight into the RESTORE arm — which `_safe_teardown`s the live
    container before pulling the last saved bundle. The raising site states the intended
    contract in its own comment: *"Callers that only want 'no handle' catch the parent and are
    unaffected; the reclaim guard catches this subclass and refuses rather than guess."* The
    reclaim guard does. This path did not.

    WHY THE RESPONSE SHAPE IS NOT THE ASSERTION. A test checking only the status code passes
    today, while the container is destroyed — the restore arm returns a perfectly good 200 with
    a working preview URL, having thrown away whatever was in the container it replaced. So
    this asserts L3's confirmation triple: nothing was torn down, provisioned or restored; the
    platform's belief about unsaved work did not move; and the saved bundle's bytes and write
    time are unchanged.
    """
    user, project = await _user_project(db_session, "ps-unknown-attach@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    # A cold relaunch first, so a real container is up and registered for this app — that is
    # what makes the attach below the UNKNOWN case rather than the certain-absent one.
    cold = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert cold.status_code == 200
    wire.sbx.attach_handle = SandboxHandle(
        fqdn="live.example",
        token="tok",
        app_name=app_name_for(app_id),
        preview_url="https://live.example",
        ready=True,
    )

    save_state_url = f"/v1/build-sessions/projects/{project.id}/save-state"
    before_save_state = (await client.get(save_state_url, headers=auth_headers(user))).json()
    provisioned_before = list(wire.sbx.provisioned)
    restored_before = list(wire.sbx.restored)
    torn_down_before = list(wire.sbx.torn_down)
    snapshot_before = fake_storage.objects[snapshot_key(app_id)]
    mtime_before = fake_storage.mtimes[snapshot_key(app_id)]

    # THE UNKNOWN: the registry still names this app READY and the attach cannot confirm
    # anything — a cold container, a supervisor timeout, or an ARM blip. `SandboxNotReadyError`
    # is a `SandboxError` and NOT a `SandboxGoneError`, which is exactly the distinction
    # `_attach_for_read` turns into `SandboxUnreachableError`.
    async def the_attach_cannot_confirm_anything(user_id: str) -> SandboxHandle:
        raise SandboxNotReadyError("the supervisor did not answer")

    monkeypatch.setattr(wire.sbx, "attach_existing", the_attach_cannot_confirm_anything)

    refused = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    # A REFUSAL THE CLIENT TURNS INTO "TRY AGAIN" — never a 200 over a container it replaced.
    # The route's own message for this arm already says the saved version is intact and a retry
    # is the way forward, which is the honest answer to a question nobody could answer.
    assert refused.status_code == 503

    # THE CONFIRMATION TRIPLE (L3). Each of the three has an innocent explanation on its own;
    # together they are what tells a rollback apart from every one of them.
    assert wire.sbx.torn_down == torn_down_before, "the live container was destroyed"
    assert wire.sbx.provisioned == provisioned_before, "a replacement container was created"
    assert wire.sbx.restored == restored_before, "the saved bundle was pulled over live work"
    # THE PATCH COMES OFF BEFORE THE SECOND SAVE-STATE READ, and it has to. `save-state` attaches
    # too, so leaving the always-raising double in place would measure the double rather than the
    # container and report a difference that has nothing to do with the relaunch — the exact
    # false positive that makes a confirmation triple worthless.
    monkeypatch.undo()
    after_save_state = (await client.get(save_state_url, headers=auth_headers(user))).json()
    assert after_save_state == before_save_state
    assert fake_storage.objects[snapshot_key(app_id)] == snapshot_before
    assert fake_storage.mtimes[snapshot_key(app_id)] == mtime_before

    # And the registry is left alone: an unreadable answer is not a death certificate.
    reg = await fake_redis.hgetall(registry_key(user.id))
    assert reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_ENDING


async def test_a_confirmed_absent_container_still_restores(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
) -> None:
    """THE REGRESSION THE FIX ABOVE COULD CAUSE, and it is the one that matters commercially:
    the narrowing must not turn an ordinary cold start into a refusal.

    A project whose container is genuinely gone is the COMMON case behind the start control —
    the citizen comes back the next morning and presses it. `attach_existing` raises
    `SandboxGoneError` (certain absence), `_attach_for_read` maps it to the plain parent, and
    the restore arm is exactly right there: there is nothing live to lose."""
    user, project = await _user_project(db_session, "ps-cold-start-still-works@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    # `attach_handle` left as `None`: the fake raises `SandboxGoneError`, its "certain" refusal.
    assert wire.sbx.attach_handle is None

    restored = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert restored.status_code == 200
    assert restored.json()["previewUrl"]
    assert wire.sbx.restored, "the cold path must still restore, or the app never comes back"
