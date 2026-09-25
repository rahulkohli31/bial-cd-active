"""`GET /v1/build-sessions/projects/{id}/preview-state` — three states, and a 503 for a read that
decided nothing."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog.testing
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

import src.services.build_sessions.manager as manager_mod
from src.api.v1.build_sessions.schemas import (
    STARTING_MARKER_TTL_SECONDS,
    PreviewLifeState,
)
from src.services.build_sessions.alarms import (
    APP_FIRST_SERVE_NOT_OBSERVED_EVENT,
    APP_FIRST_SERVED_EVENT,
    PREVIEW_STATE_REPORTED_UNKNOWN_EVENT,
)
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import write_starting_marker
from src.services.build_sessions.manager import SessionManager, app_name_for
from src.services.redis import (
    BUILD_COORDINATION_UNAVAILABLE_MSG,
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    starting_key,
)
from src.services.sandbox import DevStatus, SandboxError, SandboxHandle, SandboxNotReadyError
from src.services.storage import StorageError, snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ProjectFactory, UserFactory


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _built(db: AsyncSession, user, project) -> uuid.UUID:
    """Give the project an app row without staging any bundle."""
    app_id = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    return app_id


#: A stamp that reads as PROVEN — an instant something watched this container's app answer a
#: request. Any non-empty ISO-8601 value does; a fixed one keeps the assertions readable.
SERVED = "2026-09-10T09:41:04+00:00"

#: The create-time sentinel `_write_registry` seeds: the container exists and has NEVER served.
NEVER_SERVED = ""


async def _register_container(
    redis, user_id: uuid.UUID, app_name: str, *, state: str, serving_since: str
) -> None:
    """Write the registry hash by hand rather than through a relaunch: this route reads the
    registry and nothing else, so a provisioning path in the setup would test the path
    instead of the read.

    `serving_since` IS REQUIRED, AND THAT KEYWORD IS THE POINT OF THIS FIXTURE. It used to write
    five fields and no stamp, and an ABSENT stamp is the PRE-CUTOVER reading, which the rollout
    grandfathers as PROVEN — so every `alive` assertion in this file passed through the
    grandfather arm and would have gone on passing with the new STARTING arm deleted or
    inverted. A fix landing while the guard meant to prove it is blind is this repo's own
    recorded failure shape. Naming the reading at every call site is what stops it: a test that
    wants ALIVE says `serving_since=SERVED` and means it, and a test that wants the pre-cutover
    arm calls `_register_a_pre_cutover_container` and says THAT out loud."""
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example.azurecontainerapps.io",
            REGISTRY_FIELD_TOKEN_REF: f"ref-{app_name}",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: state,
            REGISTRY_FIELD_SERVING_SINCE: serving_since,
        },
    )


async def _register_a_pre_cutover_container(
    redis, user_id: uuid.UUID, app_name: str, *, state: str
) -> None:
    """The hash as it was written BEFORE `serving_since` existed — the fleet that is live at the
    deploy instant. Field for field the same as its sibling minus the stamp, deliberately spelled
    out rather than expressed as `_register_container(..., serving_since=None)`: the sibling's
    whole job is to refuse to let a caller leave the reading unstated, and an `Optional` would
    hand that hole straight back."""
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


def _state_and_instant(body: dict[str, Any]) -> tuple[str, object]:
    """The two fields that must never disagree, as one tuple — so a failure names both."""
    return (body["state"], body["servingSince"])


async def _probe(client: AsyncClient, user, project) -> dict[str, Any]:
    resp = await client.get(
        f"/v1/build-sessions/projects/{project.id}/preview-state", headers=auth_headers(user)
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["alive"] is (body["state"] == "alive")
    return body


@pytest.fixture
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the waiting in `head_presence`'s retry backoff, never the ladder itself: the
    ladder is what makes an unreachable store answer `null` instead of `false`."""
    from src.services.build_sessions import manager as manager_module

    async def no_waiting(_seconds: float) -> None:
        return None

    monkeypatch.setattr(manager_module, "_asleep", no_waiting)


# --------------------------------------------------------------------------------------
# The states
# --------------------------------------------------------------------------------------


async def test_a_project_nobody_ever_built_is_asleep_with_nothing_to_restore(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """`restorable` is what tells this apart from a project with work to bring back, and the
    client reads its `false` as "describe what to build"."""
    user, project = await _user_project(db_session, "ps-new@rvaiglobal.com")

    body = await _probe(client, user, project)

    assert body["state"] == "asleep"
    assert body["restorable"] is False
    assert body["previewUrl"] is None


async def test_a_reclaimed_workspace_is_asleep_and_offers_the_work_back(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # The reap wrote this container's tree back on its way out, which is the only reason there is
    # anything here to offer. Mutation-check: answer `restorable` from the registry instead and
    # this goes red.
    user, project = await _user_project(db_session, "ps-asleep@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    body = await _probe(client, user, project)

    assert body["state"] == "asleep"
    assert body["restorable"] is True
    assert body["alive"] is False


async def test_a_saved_build_with_no_recovery_copy_is_also_restorable(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-saved@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    body = await _probe(client, user, project)

    assert (body["state"], body["restorable"]) == ("asleep", True)


async def test_a_built_project_with_nothing_stored_is_confirmed_unrestorable(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
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
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
    )

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert body["alive"] is True
    assert body["previewUrl"] == (f"https://citizenapps.bialairport.com/a/{app_name_for(app_id)}")
    assert "azurecontainerapps.io" not in body["previewUrl"]
    assert set(body) == {
        "state",
        "alive",
        "previewUrl",
        "servingSince",
        "startingSince",
        "restorable",
    }
    # ALIVE NOW MEANS SERVED. This container carries a real stamp, so the answer comes off the
    # proven arm rather than the pre-cutover grandfather — see the serving-proof section below.
    assert body["servingSince"] is not None


async def test_whoever_holds_the_workspace_this_project_reads_asleep_and_restorable(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Pressing start takes the one workspace back from its holder, so the answer does not
    depend on who that is: another of this user's projects, or a container nobody can
    attribute. The restore question is still asked, because this is where it is rendered.

    Mutation-check: answer `restorable=None` on this arm and the first assertion goes red."""
    user, mine = await _user_project(db_session, "ps-taken@rvaiglobal.com")
    theirs = await ProjectFactory.create(db_session, user.id, name="Baggage Reconciliation")
    my_app = await _built(db_session, user, mine)
    await fake_storage.put(snapshot_key(my_app), b"SAVED-BUNDLE")
    other_app = await _built(db_session, user, theirs)

    readings = []
    for holder in (app_name_for(other_app), "sbx-somebodyelses"):
        await _register_container(
            fake_redis, user.id, holder, state=REGISTRY_STATE_READY, serving_since=SERVED
        )
        body = await _probe(client, user, mine)
        readings.append((body["state"], body["restorable"], body["previewUrl"]))

    assert readings == [("asleep", True, None), ("asleep", True, None)]


async def test_a_container_of_ours_mid_teardown_reads_as_asleep_with_no_url(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-ending@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(app_id),
        state=REGISTRY_STATE_ENDING,
        serving_since=SERVED,
    )

    body = await _probe(client, user, project)

    assert body["state"] == "asleep"
    assert body["previewUrl"] is None, "never hand back a URL for a container being destroyed"


# --------------------------------------------------------------------------------------
# The serving proof — ALIVE means SERVED, not SCHEDULED
#
# `state=ready` on the registry hash says an ACA container was CREATED. Until the stamp
# existed, the platform reported that as "your app is running", handed out a framable URL, and
# on 2026-09-10 a citizen watched nginx's "This app isn't running right now" page inside their
# own healthy build for eight seconds while the live region announced the preview was live.
#
# THE THREE READINGS OF `serving_since` ARE THE WHOLE CONTRACT and each gets its own test
# below, because the middle one is the fix, the first one is the rollout, and getting either
# wrong is a fleet-scale outage in one direction or the shipped bug in the other.
# --------------------------------------------------------------------------------------


async def test_a_container_that_has_never_answered_a_request_is_a_wait_not_a_running_app(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """★ THE FIX, and the reading in the middle: `serving_since == ""` means the container
    exists and has never served. That is the measured 48s→56s window, in which every byte of
    this hash already said `ready`.

    NO URL IS THE HALF THAT MATTERS. `starting` is in the client's frame veto, so withholding
    the address is what stops an iframe mounting on nginx's app-gone page — the state name alone
    would not.

    Mutation-check: delete the `if not _stamp_is_proven(reg)` arm from `project_preview_state`
    (or invert it to `if _stamp_is_proven(reg)`) and this goes red on `state`."""
    user, project = await _user_project(db_session, "ps-scheduled@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(app_id),
        state=REGISTRY_STATE_READY,
        serving_since=NEVER_SERVED,
    )

    body = await _probe(client, user, project)

    assert body["state"] == "starting"
    assert body["alive"] is False
    assert body["previewUrl"] is None, (
        "a URL here is an iframe mounted on an app that has never answered anything"
    )
    assert body["servingSince"] is None


async def test_a_hash_written_before_the_stamp_existed_still_reads_as_running(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The reading at the top, and it is the entire rollout: an ABSENT `serving_since` is a
    record written before this change, and it is grandfathered as PROVEN.

    Read absence as unproven instead and every container live at the deploy instant flips to
    `starting` — which the frame veto withholds the iframe on — unframing the whole serving
    fleet at once. That is a false negative at fleet scale, strictly worse than the eight-second
    window this change closes, and it is the failure `PreviewLifeState` was written to kill.

    THE GRANDFATHER ARM IS DELETABLE ONE STAY WINDOW AFTER DEPLOY (nothing writes a hash without
    the field any more — not the create-time seed, not the legacy adoption). This test is what
    makes that deletion a one-line change against a red assertion instead of an archaeology
    exercise, so DELETE IT DELIBERATELY when the time comes rather than discovering it."""
    user, project = await _user_project(db_session, "ps-precutover@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_a_pre_cutover_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY
    )

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert body["previewUrl"] == f"https://citizenapps.bialairport.com/a/{app_name_for(app_id)}"
    assert body["servingSince"] is None, (
        "proven, but there is no instant to name — a pre-cutover record has no first serve to "
        "report, and inventing one would put a fabricated timestamp in front of an operator"
    )


async def test_a_stamped_container_is_running_and_names_the_instant_it_first_served(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The reading at the bottom: a real ISO-8601 instant is PROVEN, and it reaches the wire as
    the diagnostic `servingSince` so an operator can join a screenshot to the `app_first_served`
    log line rather than taking "alive" on faith."""
    user, project = await _user_project(db_session, "ps-proven@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
    )

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert body["previewUrl"] == f"https://citizenapps.bialairport.com/a/{app_name_for(app_id)}"
    assert datetime.fromisoformat(body["servingSince"]) == datetime.fromisoformat(SERVED)


async def test_a_stamp_nobody_can_parse_still_keeps_the_app_running(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """A CORRUPT STAMP COSTS THE DIAGNOSTIC, NEVER THE FRAME. Whatever put an unparseable value
    in this field, it was not the empty sentinel — so the container HAS served, and the honest
    answer is `alive` with nothing to report about when.

    The other way round is the tempting one and it is wrong twice over: it would unframe a
    working app over a formatting defect, and it would do it on the strength of a field whose
    docstring says no logic may branch on it."""
    user, project = await _user_project(db_session, "ps-corrupt-stamp@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(app_id),
        state=REGISTRY_STATE_READY,
        serving_since="whenever, honestly",
    )

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert body["previewUrl"] is not None
    assert body["servingSince"] is None, "only the diagnostic goes quiet"


async def test_a_container_that_never_served_is_still_a_wait_once_its_marker_has_gone(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """WHY THE NEW ARM SITS ABOVE THE `starting` MARKER CHECK, which is a placement and not a
    preference. The marker carries a 300s TTL; a cold build that outruns it would otherwise fall
    through to the registry arms below and offer this citizen a Launch button — or, with the
    slot held by an app row it cannot resolve, `slot_taken` — in the middle of their own build.

    No marker is written here at all, which is exactly the state a lapsed TTL leaves behind.

    Mutation-check: move the unproven arm below `if starting is not None:` and this goes red."""
    user, project = await _user_project(db_session, "ps-marker-gone@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")  # a Launch button to offer
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(app_id),
        state=REGISTRY_STATE_READY,
        serving_since=NEVER_SERVED,
    )
    assert await fake_redis.exists(starting_key(user.id)) == 0, "the marker's TTL has lapsed"

    body = await _probe(client, user, project)

    assert body["state"] == "starting"
    assert body["restorable"] is None, "no restore is offered in the middle of a citizen's build"


async def test_the_unproven_arm_spends_nothing_on_the_object_store_either(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget the ALIVE arm has always kept, now held across the WHOLE pre-serve window —
    which is the interval the client polls every 3 seconds. BUILDING used to be a few seconds of
    marker; it now spans every second from container-create to first serve, so an arm placed
    below `snapshot_presence` would have moved a cold build's entire wait onto a Blob HEAD per
    poll without anyone noticing.

    A saved bundle EXISTS, so the empty `heads` proves the question was SKIPPED rather than
    that it had nothing to find.

    Mutation-check: move the unproven arm below `snapshot_presence` and `heads` comes back with
    the saved key in it."""
    user, project = await _user_project(db_session, "ps-unproven-budget@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(app_id),
        state=REGISTRY_STATE_READY,
        serving_since=NEVER_SERVED,
    )

    heads: list[str] = []
    read_head = fake_storage.head

    async def record_a_head(key: str):
        heads.append(key)
        return await read_head(key)

    monkeypatch.setattr(fake_storage, "head", record_a_head)

    body = await _probe(client, user, project)

    assert body["state"] == "starting"
    assert heads == [], "the whole pre-serve window must not touch the object store"


async def test_a_reading_only_moves_from_wait_to_running_and_never_back_the_other_way(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """THE MONOTONICITY INVARIANT, which is what licenses shipping this backend alone: `alive`
    is only ever emitted LATER than it used to be, never earlier. One container, one hash, read
    twice — the only thing that changes between the reads is the stamp landing.

    This is the assertion that catches a future editor who "optimises" the ALIVE arm by relaxing
    the stamp check, and it is also why absence is grandfathered rather than age-boxed: an age
    box can emit alive→starting for a pre-cutover container that is serving perfectly, which
    retires a working frame."""
    user, project = await _user_project(db_session, "ps-monotone@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(app_id),
        state=REGISTRY_STATE_READY,
        serving_since=NEVER_SERVED,
    )

    before = await _probe(client, user, project)

    # The one write an observer makes when it watches the app answer — nothing else changes.
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE, SERVED)

    after = await _probe(client, user, project)

    assert (before["state"], after["state"]) == ("starting", "alive")
    assert before["previewUrl"] is None and after["previewUrl"] is not None


async def test_no_state_but_running_ever_names_a_serving_instant(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """`servingSince` is DIAGNOSTIC ONLY and non-null strictly when `state == alive`. Pinned
    because it is the same fact as `state` spelled a second time from one read: let it leak onto
    another arm and a client computing liveness as `servingSince !== null` — which the field's
    own docstring forbids, and which somebody will write anyway — would disagree with `state`."""
    user, mine = await _user_project(db_session, "ps-diagnostic@rvaiglobal.com")
    named: list[tuple[str, object]] = []

    named.append(_state_and_instant(await _probe(client, user, mine)))  # never built

    app_id = await _built(db_session, user, mine)
    named.append(_state_and_instant(await _probe(client, user, mine)))  # asleep

    await write_starting_marker(fake_redis, user.id, mine.id)
    named.append(_state_and_instant(await _probe(client, user, mine)))  # starting (the marker)
    await fake_redis.delete(starting_key(user.id))

    await _register_container(
        fake_redis,
        user.id,
        app_name_for(app_id),
        state=REGISTRY_STATE_READY,
        serving_since=NEVER_SERVED,
    )
    named.append(_state_and_instant(await _probe(client, user, mine)))  # starting (unproven)

    theirs = await ProjectFactory.create(db_session, user.id, name="Gate Rostering")
    other_app = await _built(db_session, user, theirs)
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(other_app),
        state=REGISTRY_STATE_READY,
        serving_since=SERVED,
    )
    named.append(_state_and_instant(await _probe(client, user, mine)))  # held elsewhere

    assert named == [
        ("asleep", None),
        ("asleep", None),
        ("starting", None),
        ("starting", None),
        ("asleep", None),
    ]
    # …and the positive control, so the list above proves an omission rather than a field that
    # is simply never populated at all.
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
    )
    assert _state_and_instant(await _probe(client, user, mine))[1] is not None


async def test_a_registry_read_failure_is_a_503_not_a_state(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that decided nothing answers no state at all, so no state can be the reassuring
    one. Both polls already read a failed request as "we could not check".

    Mutation-check: fold the `except RedisError` arm into `_at_rest` and this goes red on the
    status."""
    user, project = await _user_project(db_session, "ps-blip@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    async def the_store_will_not_answer(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("connection refused (pipeline)")

    # The route reads through `read_registry_and_starting_marker`, not a bare `read_registry`:
    # patching the latter would leave the route perfectly able to answer.
    monkeypatch.setattr(
        manager_mod, "read_registry_and_starting_marker", the_store_will_not_answer
    )

    resp = await client.get(
        f"/v1/build-sessions/projects/{project.id}/preview-state", headers=auth_headers(user)
    )

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"error": {"message": BUILD_COORDINATION_UNAVAILABLE_MSG}}


async def test_a_failed_read_is_logged_once_per_user_however_often_the_tab_asks(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning is the only record of an outage the pane deliberately does not draw, and the
    caller is a browser timer, so it is said once per silence window rather than once per poll.

    Mutation-check: drop the `_say_the_preview_read_failed` call and `said` is empty; drop its
    early return and `said` has three lines."""
    user, project = await _user_project(db_session, "ps-blip-log@rvaiglobal.com")

    async def the_store_will_not_answer(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("connection refused (pipeline)")

    monkeypatch.setattr(
        manager_mod, "read_registry_and_starting_marker", the_store_will_not_answer
    )

    url = f"/v1/build-sessions/projects/{project.id}/preview-state"
    with structlog.testing.capture_logs() as logs:
        statuses = [
            (await client.get(url, headers=auth_headers(user))).status_code for _ in range(3)
        ]

    said = [e for e in logs if e.get("event") == PREVIEW_STATE_REPORTED_UNKNOWN_EVENT]
    assert statuses == [503, 503, 503]
    assert len(said) == 1
    assert said[0]["project_id"] == str(project.id)


async def test_with_no_coordination_store_configured_a_project_is_asleep_not_a_503(
    client: AsyncClient, db_session: AsyncSession, fake_storage, wire
) -> None:
    """No `fake_redis`: with the singleton unset, `get_redis()` raises `RedisNotConfiguredError`,
    which is a CERTAIN answer — no sandbox subsystem, so nothing can be serving or starting.

    Mutation-check: let `RedisNotConfiguredError` reach the `RedisError` arm and both probes
    below leave 200."""
    user, never = await _user_project(db_session, "ps-noredis-new@rvaiglobal.com")
    saved = await ProjectFactory.create(db_session, user.id)
    app_id = await _built(db_session, user, saved)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    first = await _probe(client, user, never)
    second = await _probe(client, user, saved)

    assert (first["state"], first["restorable"]) == ("asleep", False)
    assert (second["state"], second["restorable"]) == ("asleep", True)


async def test_restorable_is_null_when_the_object_store_is_unreachable(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    instant_backoff: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutation-check: make `snapshot_presence` return `False` on an unreadable store and this
    # goes red — which is precisely the coercion the tri-state exists to prevent.
    user, project = await _user_project(db_session, "ps-storeblip@rvaiglobal.com")
    await _built(db_session, user, project)

    async def the_store_will_not_answer(_key: str) -> None:
        raise StorageError("azure said no", provider="fake")

    monkeypatch.setattr(fake_storage, "head", the_store_will_not_answer)

    body = await _probe(client, user, project)

    assert body["restorable"] is None
    assert body["state"] == "asleep"


# --------------------------------------------------------------------------------------
# `starting`
# --------------------------------------------------------------------------------------


async def test_a_start_in_flight_reads_as_starting_from_a_different_request_and_after_reload(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-starting@rvaiglobal.com")
    await write_starting_marker(fake_redis, user.id, project.id)

    first = await _probe(client, user, project)  # a different session's request
    second = await _probe(client, user, project)  # the simulated reload, moments later

    assert first["state"] == "starting"
    assert second["state"] == "starting"
    assert first["previewUrl"] is None
    assert first["restorable"] is None


async def test_the_wait_is_dated_from_the_start_and_not_from_the_read(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """★ THE CLOCK THAT USED TO LIE. The pane counted elapsed time from its own mount, so a
    reload two minutes into a start told the citizen — and told us — that the wait was one
    second old. Six reloads looked identical to one.

    The marker is written once per start and never renewed, so what is left of its TTL dates
    the wait, and it answers the same however many times the page is reloaded over it.
    Ninety seconds spent is simulated by shortening the key's expiry, which is what a TTL
    decaying looks like from this route's side.

    Mutation check: date the wait from `datetime.now(UTC)` and the span below collapses to
    zero."""
    user, project = await _user_project(db_session, "ps-start-clock@rvaiglobal.com")
    await write_starting_marker(fake_redis, user.id, project.id)
    await fake_redis.expire(starting_key(user.id), STARTING_MARKER_TTL_SECONDS - 90)

    body = await _probe(client, user, project)

    assert body["state"] == "starting"
    began = datetime.fromisoformat(body["startingSince"])
    assert 85 < (datetime.now(UTC) - began).total_seconds() < 95


async def test_a_wait_for_somebody_elses_project_is_not_this_panes_to_count(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """THE MARKER IS PER USER, NOT PER PROJECT, and this arm is where that bites: this
    project's container is up and has never served, so the pane reads `starting` — while a
    start for ANOTHER of this citizen's projects is in flight and holds the only marker.
    Counting from that marker would tell this citizen their app has been starting for a minute
    and a half when it was created seconds ago.

    Mutation check: drop the `starting == project_id` guard and this goes red — the instant
    slides back to the other project's start."""
    user, project = await _user_project(db_session, "ps-other-clock@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    other = await ProjectFactory.create(db_session, user.id, name="Stand Allocation")
    await db_session.commit()
    # Ours, up, and never yet a serve — the registry arm that reads `starting`. `created_at` is
    # written as now, which is what this pane's clock must report.
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=""
    )
    # …and the other project's start, ninety seconds old, holding the only marker there is.
    await write_starting_marker(fake_redis, user.id, other.id)
    await fake_redis.expire(starting_key(user.id), STARTING_MARKER_TTL_SECONDS - 90)

    body = await _probe(client, user, project)

    assert body["state"] == "starting"
    began = datetime.fromisoformat(body["startingSince"])
    assert (datetime.now(UTC) - began).total_seconds() < 5, (
        "this pane was handed another project's clock"
    )


async def test_a_container_outliving_its_marker_is_dated_from_the_registry(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """A start slower than the marker's own 300 seconds still reads as a wait — that arm is
    what takes the marker TTL off the screen — and past the marker the registry's `created_at`
    is the only thing left to date it by. Silence there would restart the citizen's clock at
    exactly the moment the wait got interesting."""
    user, project = await _user_project(db_session, "ps-outlived-clock@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=""
    )

    body = await _probe(client, user, project)

    assert body["state"] == "starting"
    assert body["startingSince"] is not None


async def test_a_completed_start_clears_the_marker_and_the_next_read_answers_alive(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-start-done@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await write_starting_marker(fake_redis, user.id, project.id)

    assert (await _probe(client, user, project))["state"] == "starting"

    await fake_redis.delete(starting_key(user.id))
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
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
    """Driven through the REAL start path rather than by hand-clearing the marker: the
    compensation that clears it is the thing under test."""
    from src.services.sandbox import SandboxError

    user, project = await _user_project(db_session, "ps-start-failed@rvaiglobal.com")

    async def provisioning_blows_up(*_args: object, **_kwargs: object) -> SandboxHandle:
        raise SandboxError("the container never came up")

    monkeypatch.setattr(wire.sbx, "provision_new", provisioning_blows_up)

    with pytest.raises(SandboxError):
        await wire.manager.ensure_sandbox(
            db_session, user, project.id, sandbox_client=wire.sbx, may_write=True
        )

    assert await fake_redis.exists(starting_key(user.id)) == 0

    body = await _probe(client, user, project)
    assert body["state"] == "asleep"


async def test_an_abandoned_marker_expires_and_the_next_read_falls_back_to_the_registry(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-abandoned@rvaiglobal.com")
    await _built(db_session, user, project)
    await write_starting_marker(fake_redis, user.id, project.id)
    assert (await _probe(client, user, project))["state"] == "starting"

    # The TTL lapsing: fakeredis has no fast-forward clock, so the key is deleted outright —
    # from a reader's side that is indistinguishable from the TTL having done it.
    await fake_redis.delete(starting_key(user.id))

    body = await _probe(client, user, project)
    assert body["state"] == "asleep"


async def test_a_marker_naming_another_project_leaves_this_one_asleep(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The marker is per user: a start in flight for another project is not this pane's wait.

    Mutation-check: answer `starting` for any marker and this goes red on `state`."""
    user, mine = await _user_project(db_session, "ps-marker-taken@rvaiglobal.com")
    theirs = await ProjectFactory.create(db_session, user.id, name="Runway Allocation")
    await write_starting_marker(fake_redis, user.id, theirs.id)

    body = await _probe(client, user, mine)

    assert (body["state"], body["restorable"], body["startingSince"]) == ("asleep", False, None)


async def test_a_marker_naming_this_project_while_the_registry_already_serves_it_is_alive(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "ps-stale-marker@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
    )
    await write_starting_marker(fake_redis, user.id, project.id)

    body = await _probe(client, user, project)

    assert body["state"] == "alive"
    assert body["alive"] is True


# --------------------------------------------------------------------------------------
# The cost budget
# --------------------------------------------------------------------------------------


async def test_a_poll_runs_no_command_in_the_container_and_never_attaches(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutation-check: have `project_preview_state` call `_attach_for_read` and this goes red on
    # `attaches`.
    user, project = await _user_project(db_session, "ps-cheap@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
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
    assert body["state"] == "alive"

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
    # Mutation-check: hoist `snapshot_presence` back above the registry read and `heads` comes
    # back with the saved key in it — this goes red immediately.
    user, project = await _user_project(db_session, "ps-hotpath@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    # A saved bundle EXISTS, so the empty `heads` below proves the question was skipped rather
    # than that it had no answer to find.
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
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

    await fake_redis.delete(registry_key(user.id))
    body = await _probe(client, user, project)

    assert (body["state"], body["restorable"]) == ("asleep", True)
    assert heads == [snapshot_key(app_id)], "one HEAD, and only where the answer is rendered"


async def test_every_state_is_reachable_and_there_are_three(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, mine = await _user_project(db_session, "ps-ladder@rvaiglobal.com")

    seen = [(await _probe(client, user, mine))["state"]]

    app_id = await _built(db_session, user, mine)
    seen.append((await _probe(client, user, mine))["state"])

    await write_starting_marker(fake_redis, user.id, mine.id)
    seen.append((await _probe(client, user, mine))["state"])
    await fake_redis.delete(starting_key(user.id))

    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
    )
    seen.append((await _probe(client, user, mine))["state"])

    theirs = await ProjectFactory.create(db_session, user.id, name="Stand Allocation")
    other_app = await _built(db_session, user, theirs)
    await _register_container(
        fake_redis,
        user.id,
        app_name_for(other_app),
        state=REGISTRY_STATE_READY,
        serving_since=SERVED,
    )
    seen.append((await _probe(client, user, mine))["state"])

    assert seen == ["asleep", "asleep", "starting", "alive", "asleep"]
    assert set(seen) == {member.value for member in PreviewLifeState}


# --------------------------------------------------------------------------------------
# One round trip, two pipelined commands
# --------------------------------------------------------------------------------------


async def test_the_registry_and_marker_are_read_in_one_pipelined_round_trip(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutation-check: replace `read_registry_and_starting_marker`'s pipeline with two sequential
    # `redis.hgetall` / `redis.get` calls and `pipelines` goes to `[]` while `bare_reads` goes to
    # `2` — this test catches exactly that regression.
    user, project = await _user_project(db_session, "ps-pipeline@rvaiglobal.com")
    app_id = await _built(db_session, user, project)
    await _register_container(
        fake_redis, user.id, app_name_for(app_id), state=REGISTRY_STATE_READY, serving_since=SERVED
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


async def test_a_readiness_timeout_on_the_attach_arm_is_non_destructive_and_the_triple_holds(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake's `etag` is always `None`, so the snapshot's write time stands in for it: a
    `put` bumps the mtime on every write, so an unchanged mtime IS the unchanged-etag claim."""
    user, project = await _user_project(db_session, "ps-timeout-confirm@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    # A cold relaunch first, so a real container is up and registered for this app. The attach
    # handle is set BEFORE either baseline is read: set it after, and "before" and "after"
    # would differ in what the fake can answer rather than in the timeout under test.
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

    async def the_dev_server_never_answers(handle: SandboxHandle, *, timeout_s: float = 120.0):
        raise SandboxNotReadyError("the app root never served")

    monkeypatch.setattr(wire.sbx, "wait_ready", the_dev_server_never_answers)

    timed_out = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert timed_out.status_code == 200
    assert timed_out.json()["ready"] is False
    reg = await fake_redis.hgetall(registry_key(user.id))
    assert reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_ENDING

    assert wire.sbx.provisioned == provisioned_before
    assert wire.sbx.restored == restored_before
    assert wire.sbx.torn_down == torn_down_before
    after_save_state = (await client.get(save_state_url, headers=auth_headers(user))).json()
    assert after_save_state == before_save_state
    assert fake_storage.objects[snapshot_key(app_id)] == snapshot_before
    assert fake_storage.mtimes[snapshot_key(app_id)] == mtime_before


async def test_a_launch_that_proves_the_app_serves_stamps_it_and_the_pane_says_running(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
) -> None:
    """THE RELAUNCH OBSERVER, end to end and through the real route. `wait_ready` returning
    without `SandboxNotReadyError` IS the proof — it means a request to the app root actually
    succeeded — so the very poll that follows the press already answers `alive`, with no second
    round trip and no watcher needing to catch up.

    Driven from the container's create-time sentinel rather than a hand-written stamp: the
    registry hash here is written by the provisioning path, so this asserts the whole chain
    (`_write_registry` seeds `""` → the relaunch stamps it → the poll reads it), which is the
    one thing three separate unit tests cannot say between them."""
    user, project = await _user_project(db_session, "ps-launch-proves@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    launched = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert launched.status_code == 200
    assert launched.json()["ready"] is True
    stamped = await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE)
    assert stamped, "the relaunch watched the app answer and recorded nothing"
    assert (await _probe(client, user, project))["state"] == "alive"


async def test_a_readiness_timeout_on_the_attach_arm_takes_the_serving_proof_back(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE ONE PLACE OUTSIDE A LIVE TURN THAT CAN RETRACT. This arm has just watched a root
    GET fail against a container it attached to — real evidence the app is not answering — and
    that container HAS served, so it carries an instant nothing else would ever clear: the only
    other clearer, the turn watcher's crash edge, exists only while a turn is streaming.

    Without this the pane goes on reporting RUNNING and frames nginx's "This app isn't running
    right now" page: the measured 2026-09-10 defect, one door down.

    IT MARKS NOTHING `ending` AND TEARS NOTHING DOWN, and that is not timidity — condemning a
    container for a slow root GET once cost a citizen their unsaved work. Retracting a claim
    costs them a card. The sibling test above pins the non-destruction in full; what is added
    here is that the claim itself comes off, and that the pane follows."""
    user, project = await _user_project(db_session, "ps-timeout-retracts@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    cold = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert cold.status_code == 200
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE), (
        "guard the premise: there has to be a standing proof for the retraction to take back"
    )
    assert (await _probe(client, user, project))["state"] == "alive"

    wire.sbx.attach_handle = SandboxHandle(
        fqdn="live.example",
        token="tok",  # noqa: S106 - a fake, never a real bearer
        app_name=app_name_for(app_id),
        preview_url="https://live.example",
        ready=True,
    )
    torn_down_before = list(wire.sbx.torn_down)

    async def the_dev_server_stopped_answering(handle: SandboxHandle, *, timeout_s: float = 120.0):
        raise SandboxNotReadyError("the app root never served")

    monkeypatch.setattr(wire.sbx, "wait_ready", the_dev_server_stopped_answering)
    # AND THE ROOT HAS TO AGREE WITH THE WAIT, or this test contradicts itself. The continuation
    # this arm spawns reads `dev_status` for itself and never borrows the wait's verdict, so a
    # fake whose `wait_ready` refuses while its `/dev/status` still reports a page is scripting
    # a container that IS serving — and the continuation correctly re-takes the proof one tick
    # later, exactly as it should. 404 is what the app whose root never served actually says.
    wire.sbx.root_status = 404

    degraded = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert degraded.status_code == 200
    assert degraded.json()["ready"] is False
    # BACK TO THE SENTINEL, NEVER DELETED: an absent field is the pre-cutover reading and is
    # grandfathered as PROVEN, so a delete here would report the dead app as running again.
    assert await fake_redis.hexists(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == 1
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == ""
    assert wire.sbx.torn_down == torn_down_before, "a claim was retracted by destroying something"

    settled = await _probe(client, user, project)
    assert settled["state"] == "starting", "the pane went on framing an app that stopped serving"
    assert settled["previewUrl"] is None


async def test_an_attach_that_cannot_confirm_anything_refuses_rather_than_restoring(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status code alone cannot carry this: the restore arm answers a perfectly good 200
    with a working preview URL, having thrown away whatever was in the container it replaced.
    What is asserted instead is that nothing was torn down, provisioned or restored."""
    user, project = await _user_project(db_session, "ps-unknown-attach@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    # A cold relaunch first, so a real container is up and registered for this app: without it
    # the attach below would be the certain-absent case rather than the unknown one.
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

    # `SandboxNotReadyError` is a `SandboxError` and NOT a `SandboxGoneError`: raise the latter
    # here and `_attach_for_read` reports certain absence, which is the other arm entirely.
    async def the_attach_cannot_confirm_anything(user_id: str) -> SandboxHandle:
        raise SandboxNotReadyError("the supervisor did not answer")

    monkeypatch.setattr(wire.sbx, "attach_existing", the_attach_cannot_confirm_anything)

    refused = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert refused.status_code == 503

    assert wire.sbx.torn_down == torn_down_before, "the live container was destroyed"
    assert wire.sbx.provisioned == provisioned_before, "a replacement container was created"
    assert wire.sbx.restored == restored_before, "the saved bundle was pulled over live work"
    # The patch comes off BEFORE the second save-state read, and it has to: `save-state` attaches
    # too, so leaving the always-raising double in place would measure the double rather than
    # the container and report a difference that has nothing to do with the relaunch.
    monkeypatch.undo()
    after_save_state = (await client.get(save_state_url, headers=auth_headers(user))).json()
    assert after_save_state == before_save_state
    assert fake_storage.objects[snapshot_key(app_id)] == snapshot_before
    assert fake_storage.mtimes[snapshot_key(app_id)] == mtime_before

    reg = await fake_redis.hgetall(registry_key(user.id))
    assert reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_ENDING


async def test_a_confirmed_absent_container_still_restores(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
) -> None:
    user, project = await _user_project(db_session, "ps-cold-start-still-works@rvaiglobal.com")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"SAVED-BUNDLE")

    # `attach_handle` is deliberately left `None`: that is what makes the fake raise
    # `SandboxGoneError`, its certain-absence refusal.
    assert wire.sbx.attach_handle is None

    restored = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert restored.status_code == 200
    assert restored.json()["previewUrl"]
    assert wire.sbx.restored, "the cold path must still restore, or the app never comes back"


# --- a root that answers WITHOUT A PAGE, which is not the same as a root that never answers ----
#
# ★ THE READING NO TEST DOUBLE IN THIS REPO COULD PRODUCE UNTIL NOW, and the reason a fix could
# tear a restored container down with a green suite. Every fake's `dev_status` returned
# `root_status=None`, which `shows_a_page` reads as the GRANDFATHER arm and answers True — so
# `if not something_watched_it_paint` had never once been True in a test, on any path. The whole
# page proof was inert across the suite. `FakeSandboxClient.root_status` is what ends that; every
# test below sets it and says which reading it is scripting.
#
# THE DISTINCTION THESE PIN, in the words the manager uses: `/dev/status.ready` is fail-open by
# the supervisor's own design (ANY answer counts, 404 and 500 included, so a compile error cannot
# wedge it False and mislead the model), while the FRAME has to ask the narrower question — would
# a citizen opening this preview right now see a page. A build spends its first seconds answering
# 404s, genuinely ready with nothing to show. Measured on 2026-09-10: the platform framed exactly
# that, and the citizen got a blank white pane with no words on it.
#
# AND THE REMEDY IS NEVER DESTRUCTION. A root with nothing to show is a container that is up
# and holds the citizen's tree; answering it with a raise escapes the lock scope before
# `scope.spare()` and lets compensation tear that container down. These tests keep that out.


@pytest.fixture
def instant_first_serve_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the detached continuation's clock so a test can drive it to its own end.

    THE REAL BUDGET IS 120 SECONDS AND A TEST MUST NOT WAIT IT OUT — but neither may a test
    quietly stop the continuation from running, because the continuation IS half the behaviour
    here: the request hands the wait over and returns, and everything the citizen sees next comes
    from the task. So the budget goes to zero (one poll, then the give-up line) and the poll
    interval with it. A test that needs MORE than one poll re-patches the budget itself and says
    why."""
    monkeypatch.setattr(manager_mod, "_COLD_READY_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(manager_mod, "READINESS_POLL_S", 0)


async def _drain_the_watchers(manager: SessionManager) -> None:
    """Run every detached first-serve continuation this relaunch spawned to completion.

    Not tidiness: `_retract_the_proof_and_keep_watching` hands its wait to a task that OUTLIVES
    the request, so a test asserting only on the response has asserted on the half that finished
    first — and the stamp the citizen's pane reads is written by the other half."""
    for task in list(manager._tasks):
        with contextlib.suppress(Exception):
            await task


async def _relaunch(client: AsyncClient, user, project):
    return await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )


async def _a_saved_project(db: AsyncSession, store, email: str):
    user, project = await _user_project(db, email)
    app_id = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    await store.put(snapshot_key(app_id), b"SAVED-BUNDLE")
    return user, project, app_id


def _the_live_container(app_id: uuid.UUID) -> SandboxHandle:
    """What `attach_existing` hands back for a container that is already up — the handle that
    makes the next relaunch take the ATTACH arm instead of restoring."""
    return SandboxHandle(
        fqdn="live.example",
        token="tok",  # noqa: S106 - a fake, never a real bearer
        app_name=app_name_for(app_id),
        preview_url="https://live.example",
        ready=True,
    )


async def test_a_cold_relaunch_whose_root_shows_no_page_keeps_the_container_it_just_restored(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    instant_first_serve_watch,
) -> None:
    """★ THE REGRESSION THAT SHIPPED. A cold relaunch restores the citizen's tree into a fresh
    container, the dev server comes up, and the app root answers 404 because the agent has not
    written `app/page.tsx` yet. That container is up, holds the work, and becomes framable the
    moment a page exists — so the page check answers it by retracting the serving proof and
    watching, never by raising: a raise here escapes `_holding_user_lock` before `scope.spare()`,
    and compensation tears down the container `_restore_or_bust` has just built.

    Mutation-check: revert the gate to `something_watched_it_paint = dev.ready` and this goes red
    on `ready` — the fail-open readiness flag counts the 404 as a serve."""
    user, project, app_id = await _a_saved_project(
        db_session, fake_storage, "ps-cold-no-page@rvaiglobal.com"
    )
    # Set BEFORE the press, unlike every attach-arm test below: this is the shape of a container
    # whose app has never had a page at all, which is what a first build looks like.
    wire.sbx.root_status = 404

    restored = await _relaunch(client, user, project)

    assert restored.status_code == 200, "the citizen got an error over a container that is up"
    assert restored.json()["ready"] is False, "a 404 root was reported as a running app"
    # AND THE WORD ON THE WIRE, NOT ONLY THE FLAG. `ready` and `status` are two fields the router
    # derives from one value (`READY if relaunched.ready else PROVISIONING`), and every other test
    # here reads the flag alone — so hardcoding that mapping back to READY left the whole suite
    # green. Issue #49's acceptance criterion is written in terms of this field, not of `ready`.
    assert restored.json()["status"] == "provisioning", "a page-less container was called READY"
    assert restored.json()["previewUrl"], "the URL is framable the moment a page exists"
    assert wire.sbx.torn_down == [], "the restored container was destroyed for answering 404"
    assert wire.sbx.restored == [app_name_for(app_id)], "guard the premise: this was the cold arm"
    # NOTHING WATCHED IT PAINT, so nothing may claim it did.
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == ""

    await _drain_the_watchers(wire.manager)
    settled = await _probe(client, user, project)
    assert settled["state"] == "starting", "the pane was told to frame a page-less container"
    assert settled["previewUrl"] is None


async def test_an_attached_container_whose_root_shows_no_page_loses_its_proof_not_its_life(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    instant_first_serve_watch,
) -> None:
    """★ THE SAME READING ON THE OTHER ARM, and it must reach the same outcome. A container that
    served yesterday carries an ISO stamp nothing else would ever clear — the only other clearer,
    the turn watcher's crash edge, exists only while a turn is streaming — so an attach that finds
    the root answering 404 has to take the claim back, or the poll goes on reporting RUNNING and
    the pane frames nginx's "This app isn't running right now" page.

    RETRACTED TO THE SENTINEL, NEVER DELETED: an absent `serving_since` is the pre-cutover reading
    and is grandfathered as PROVEN, so a delete here would report the dead app as running again.

    Mutation-check: revert the gate to `something_watched_it_paint = dev.ready` and this goes red
    on the stamp, which never comes off."""
    user, project, app_id = await _a_saved_project(
        db_session, fake_storage, "ps-attach-no-page@rvaiglobal.com"
    )

    cold = await _relaunch(client, user, project)
    assert cold.status_code == 200
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE), (
        "guard the premise: there has to be a standing proof for the retraction to take back"
    )
    wire.sbx.attach_handle = _the_live_container(app_id)
    torn_down_before = list(wire.sbx.torn_down)
    # The agent deleted the page, or the route it is mid-edit stopped compiling. The container is
    # the same one that was serving a moment ago — nothing about it is gone.
    wire.sbx.root_status = 404

    degraded = await _relaunch(client, user, project)

    assert degraded.status_code == 200
    assert degraded.json()["ready"] is False
    assert degraded.json()["previewUrl"], "the citizen keeps the URL; only the claim comes off"
    assert await fake_redis.hexists(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == 1
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == ""
    assert wire.sbx.torn_down == torn_down_before, "a claim was retracted by destroying something"

    await _drain_the_watchers(wire.manager)
    settled = await _probe(client, user, project)
    assert settled["state"] == "starting", "the pane went on framing an app with nothing to show"


async def test_both_ways_into_the_page_wait_go_through_one_retraction(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
    instant_first_serve_watch,
) -> None:
    """★ THE DRIFT GUARD, and it is the whole defence against this defect coming back. TWO
    readings end in the same place — the readiness wait lapsing, and the root answering without a
    page — and they want the identical outcome: keep the container, hand back the URL with
    `ready=False`, retract any standing proof, keep watching. While those were two hand-written
    copies, the second one reached its outcome by RAISING into the first one's handler, and that
    handler re-raises on a cold relaunch. One shared method is what makes that impossible to
    write again.

    Asserted on the CALL, not on the outcome, deliberately: two copies that agree today would
    pass every outcome assertion in this file and still be two copies. `cold` rides along because
    it is the parameter that separates the two entry points' log lines, and nothing else asserts
    it.

    Mutation-check: inline either arm's remedy back into its own three lines and this goes red on
    the call count while every sibling above stays green."""
    user, project, app_id = await _a_saved_project(
        db_session, fake_storage, "ps-one-retraction@rvaiglobal.com"
    )
    went_through: list[bool] = []
    the_shared_remedy = wire.manager._retract_the_proof_and_keep_watching

    async def _record(*args: object, **kwargs: object) -> None:
        went_through.append(bool(kwargs["cold"]))
        await the_shared_remedy(*args, **kwargs)

    monkeypatch.setattr(wire.manager, "_retract_the_proof_and_keep_watching", _record)

    # ARM ONE: the cold arm, root answering without a page.
    wire.sbx.root_status = 404
    assert (await _relaunch(client, user, project)).status_code == 200
    assert went_through == [True], "the page-less arm found its own way out"

    # ARM TWO: an attach whose readiness wait lapses. A different reading, a different handler,
    # and the same remedy — that is the invariant.
    wire.sbx.attach_handle = _the_live_container(app_id)

    async def the_dev_server_never_answers(handle: SandboxHandle, *, timeout_s: float = 120.0):
        raise SandboxNotReadyError("the app root never served")

    monkeypatch.setattr(wire.sbx, "wait_ready", the_dev_server_never_answers)

    assert (await _relaunch(client, user, project)).status_code == 200

    assert went_through == [True, False], "the readiness-timeout arm found its own way out"
    await _drain_the_watchers(wire.manager)


async def test_a_supervisor_that_cannot_be_asked_mints_no_proof_on_a_cold_relaunch(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
    instant_first_serve_watch,
) -> None:
    """★ THE THIRD ANSWER: WE COULD NOT ASK. It is not a quiet vote for either neighbour, and it
    used to be collapsed into the wrong one. Declining to DEMOTE is right — a transport error is
    no evidence about the app, so `ready` stays exactly as the wait left it and a preview that is
    painting keeps its frame. But the same fail-open flag also gated the serving stamp, so one
    blip minted a proof for a container NOTHING HAS EVER WATCHED SERVE, which is the claim this
    whole branch exists to make honest.

    Mutation-check: gate the stamp on `ready` again — or answer the blip with
    `something_watched_it_paint = True` — and this goes red on the stamp."""
    user, project, _ = await _a_saved_project(
        db_session, fake_storage, "ps-blip-cold@rvaiglobal.com"
    )

    async def the_supervisor_did_not_answer(handle: SandboxHandle) -> DevStatus:
        raise SandboxError("the supervisor did not answer")

    monkeypatch.setattr(wire.sbx, "dev_status", the_supervisor_did_not_answer)

    launched = await _relaunch(client, user, project)

    assert launched.status_code == 200
    assert launched.json()["ready"] is True, "a blip demoted a preview that may be painting fine"
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == "", (
        "a transport error was read as a sighting"
    )
    await _drain_the_watchers(wire.manager)


async def test_a_supervisor_blip_never_retracts_a_proof_the_container_already_earned(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
    instant_first_serve_watch,
) -> None:
    """★ THE OTHER HALF OF THE BLIP, and it is the arm that must NOT share the remedy above. A
    container that has been serving its citizen for an hour answers one unreachable `/dev/status`
    on the next press of the control. Retracting there would take a good app's preview away over
    a wedged ingress — the same asymmetry the reconciler's probe is built on, and the reason a
    fleet-wide ARM outage cannot unframe the fleet. The blip keeps watching; it never clears.

    Mutation-check: point the blip arm at `_retract_the_proof_and_keep_watching` instead of
    `_keep_watching_for_a_first_serve` and this goes red on the surviving stamp."""
    user, project, app_id = await _a_saved_project(
        db_session, fake_storage, "ps-blip-attached@rvaiglobal.com"
    )

    assert (await _relaunch(client, user, project)).status_code == 200
    standing = await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE)
    assert standing, "guard the premise: the container earned a proof on the way up"
    wire.sbx.attach_handle = _the_live_container(app_id)

    async def the_supervisor_did_not_answer(handle: SandboxHandle) -> DevStatus:
        raise SandboxError("the supervisor did not answer")

    monkeypatch.setattr(wire.sbx, "dev_status", the_supervisor_did_not_answer)

    pressed_again = await _relaunch(client, user, project)

    assert pressed_again.status_code == 200
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == standing
    await _drain_the_watchers(wire.manager)
    assert (await _probe(client, user, project))["state"] == "alive"


async def test_the_continuation_reads_the_root_itself_and_never_borrows_the_readiness_verdict(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
    instant_first_serve_watch,
) -> None:
    """★ THE CONTINUATION'S OWN DEFECT, and it undid the fix one second later. The task borrowed
    `wait_ready` to decide whether the app had come up — and `wait_ready` returns on
    `/dev/status.ready`, the fail-open flag that counts a 404. So the page check refused to call
    the app ready, cleared the standing proof on the way out, and the first iteration of the
    watcher it spawned wrote that proof straight back over the same 404: the poll flipped to
    RUNNING and the pane framed the blank container the refusal had just saved the citizen from.

    The script is the discriminator: `wait_ready` REFUSES ONCE (which is what puts the request on
    this arm) and would succeed on any later call, while the root goes on answering 404 for the
    whole budget. A continuation reading `wait_ready` stamps; one reading `shows_a_page` does not.

    Mutation-check: swap the continuation's `dev_status` reading back to
    `await sandbox_client.wait_ready(handle, timeout_s=1.0)` and this goes red."""
    user, project, app_id = await _a_saved_project(
        db_session, fake_storage, "ps-continuation-reads@rvaiglobal.com"
    )

    assert (await _relaunch(client, user, project)).status_code == 200
    wire.sbx.attach_handle = _the_live_container(app_id)
    wire.sbx.root_status = 404
    refusals = {"left": 1}

    async def it_refuses_once_then_answers(handle: SandboxHandle, *, timeout_s: float = 120.0):
        if refusals["left"]:
            refusals["left"] -= 1
            raise SandboxNotReadyError("the app root did not answer inside the budget")
        return handle

    monkeypatch.setattr(wire.sbx, "wait_ready", it_refuses_once_then_answers)

    degraded = await _relaunch(client, user, project)
    assert degraded.status_code == 200
    assert degraded.json()["ready"] is False
    await _drain_the_watchers(wire.manager)

    assert refusals["left"] == 0, "guard the premise: the refusal really did put us on this arm"
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == "", (
        "the continuation re-minted the proof the page check had just taken back"
    )
    assert (await _probe(client, user, project))["state"] == "starting"


async def test_a_continuation_that_never_sees_a_page_gives_up_out_loud_and_stamps_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    instant_first_serve_watch,
) -> None:
    """The give-up line, which is NOT a claim that the app is dead — the five-minute reconciler
    may still stamp this container. What it says is that for this long, nothing the platform runs
    watched the app show a page, and for two of the three callers that means a citizen sat in
    front of a wait card for the whole of it. Without it, a build that never painted leaves an
    operator nothing but silence to read.

    Mutation-check: move the `app_first_serve_not_observed` warning up into
    `_retract_the_proof_and_keep_watching` (where the wait is delegated, not over) and this goes
    red on the count — it would fire once for the hand-over and once for the give-up."""
    user, project, _ = await _a_saved_project(
        db_session, fake_storage, "ps-continuation-gives-up@rvaiglobal.com"
    )
    wire.sbx.root_status = 404

    with structlog.testing.capture_logs() as logs:
        assert (await _relaunch(client, user, project)).status_code == 200
        await _drain_the_watchers(wire.manager)

    gave_up = [e for e in logs if e.get("event") == APP_FIRST_SERVE_NOT_OBSERVED_EVENT]
    assert len(gave_up) == 1, "the citizen's whole wait went unrecorded"
    assert gave_up[0]["arm"] == "relaunch_continuation"
    assert [e for e in logs if e.get("event") == APP_FIRST_SERVED_EVENT] == []
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE) == ""


async def test_a_continuation_that_watches_the_page_arrive_stamps_it_once_and_names_its_arm(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE POSITIVE ARM, and the only thing that pins the `cold` parameter at all. The
    continuation is not a formality: a heavy dashboard route compiling under 1.0 vCPU shows no
    page for far longer than the press's own budget, and this task is what turns that into a
    preview seconds later instead of a wait until the five-minute reconciler notices.

    `cold=True` IS THE CLAIM UNDER TEST. It rides from the caller rather than being assumed
    False, because the page check can now hand over a RESTORED container too — and a cold start
    whose first page arrived late is exactly what an operator reading that field is looking for.

    Mutation-check: hard-code `cold=False` at the continuation's `_record_the_first_serve` call
    and this goes red; nothing else in the tree asserts it."""
    user, project, _ = await _a_saved_project(
        db_session, fake_storage, "ps-continuation-stamps@rvaiglobal.com"
    )
    # NOT the shared fixture: this one needs the loop to keep going past its first poll, so the
    # budget stays generous and only the sleep is collapsed. The script ends the loop, not the
    # clock — a page that never arrives would hang here, which is what the sibling above covers.
    monkeypatch.setattr(manager_mod, "READINESS_POLL_S", 0)
    polls = {"taken": 0}

    async def the_page_arrives_on_the_fourth_look(handle: SandboxHandle) -> DevStatus:
        polls["taken"] += 1
        return DevStatus(
            running=True, ready=True, port=3000, root_status=404 if polls["taken"] < 4 else 200
        )

    monkeypatch.setattr(wire.sbx, "dev_status", the_page_arrives_on_the_fourth_look)

    with structlog.testing.capture_logs() as logs:
        launched = await _relaunch(client, user, project)
        assert launched.status_code == 200
        assert launched.json()["ready"] is False, (
            "the press itself saw no page — that is the setup"
        )
        await _drain_the_watchers(wire.manager)

    served = [e for e in logs if e.get("event") == APP_FIRST_SERVED_EVENT]
    assert len(served) == 1, "`app_first_served` means FIRST — a second line makes it meaningless"
    assert served[0]["observer"] == "relaunch_continuation"
    assert served[0]["cold"] is True, "a restored container's late first page read as a warm one"
    assert polls["taken"] > 1, "guard the premise: the continuation really did look again"
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE)
    assert (await _probe(client, user, project))["state"] == "alive"


# --- a first-time project ---------------------------------------------------------------------


async def test_a_first_time_project_reads_the_same_whoever_holds_the_workspace(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No app row means no bundle key can exist, so `restorable` is a confirmed `false` that
    costs no object-store call — held workspace or free.

    Mutation-check: answer `None` for a missing app row in `_at_rest` and this goes red on
    `restorable`."""
    user, project = await _user_project(db_session, "ps-first@rvaiglobal.com")
    heads: list[str] = []
    read_head = fake_storage.head

    async def record_a_head(key: str):
        heads.append(key)
        return await read_head(key)

    monkeypatch.setattr(fake_storage, "head", record_a_head)

    free = await _probe(client, user, project)
    await _register_container(
        fake_redis, user.id, "sbx-somebodyelses", state=REGISTRY_STATE_READY, serving_since=""
    )
    held = await _probe(client, user, project)

    for body in (free, held):
        assert (body["state"], body["restorable"], body["previewUrl"]) == ("asleep", False, None)
    assert heads == []
