"""`GET /v1/build-sessions/projects/{id}/preview-state` — four states, not one boolean.

C3 §8.3 / R16–R18. The route used to answer `alive: false` identically for *never built*,
*another project took the slot*, *asleep*, and *the registry read threw* — and the portal
rendered all four as "your preview is gone", including the one that was an ERROR rather than a
fact about anything.

Two invariants are load-bearing here and each has its own test:

* **An unknown is its own answer.** A registry read that failed decided nothing, so it is
  `unknown` — never `asleep`, never `never_built`, and above all never an error the citizen has
  to read. Same rule for `restorable`, which is tri-state for the same reason `dirty` is.
* **The poll stays cheap.** The caller is a browser tab on a 45-second timer. C3 §8.3 freezes
  the budget: one registry hash read, at most two user-scoped rows, at most two object-store
  HEADs, and NO container command or attach — which R14 forbids outright, because a poll that
  touched the container would make every framed preview look busy forever and nothing would
  ever be reclaimed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.build_sessions.appdata import resolve_app_for_project
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
)
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
    assert body["previewUrl"] == (f"https://citizenapps.bialairport.com/a/{app_name_for(app_id)}/")
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
        raise RedisConnectionError("connection refused (hgetall)")

    monkeypatch.setattr(manager_module, "read_registry", the_store_will_not_answer)

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
    values are four distinct answers. A regression that collapses two of them back into one
    boolean fails here even if every single-state test above were somehow still green."""
    user, mine = await _user_project(db_session, "ps-ladder@rvaiglobal.com")

    seen = [(await _probe(client, user, mine))["state"]]  # never_built

    app_id = await _built(db_session, user, mine)
    seen.append((await _probe(client, user, mine))["state"])  # asleep

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

    assert seen == ["never_built", "asleep", "alive", "slot_taken"]
    assert len(set(seen)) == 4, "four states, not one boolean"
