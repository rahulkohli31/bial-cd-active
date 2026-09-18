"""`POST /v1/build-sessions/projects/{id}/renew` — presence renews, silence is departure.

THE WHOLE POINT OF THE ROUTE is that nothing is sent when a citizen leaves. There is no departure
message to lose, no writer that could truncate a deadline, and no transport to fail: a screen that
is open renews a short stay on the poll it is already making, and a screen that is gone stops. So
the tests here are as much about what is NOT written as about what is.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    HIDDEN_SURFACE_PRESENT_STAY_SECONDS,
    SURFACE_PRESENT_STAY_SECONDS,
)
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.manager import app_name_for
from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_STAY_WRITER,
    REGISTRY_FIELD_TOKEN_REF,
)
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ProjectFactory, UserFactory


async def _user_project_app(db_session: AsyncSession, email: str):
    user = await UserFactory.create(db_session, email=email)
    project = await ProjectFactory.create(db_session, user.id)
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    return user, project, app_id


async def _register(redis, user_id: uuid.UUID, app_name: str) -> None:
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example.azurecontainerapps.io",
            REGISTRY_FIELD_TOKEN_REF: f"ref-{app_name}",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_SERVING_SINCE: datetime.now(UTC).isoformat(),
        },
    )


async def _renew(
    client: AsyncClient, user, project, *, presence: str = "visible"
) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/renew",
        headers=auth_headers(user),
        json={"presence": presence},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _stay(redis, user_id: uuid.UUID) -> tuple[datetime | None, str | None]:
    raw = await redis.hmget(
        registry_key(user_id),
        [REGISTRY_FIELD_PREVIEW_STAY_UNTIL, REGISTRY_FIELD_STAY_WRITER],
    )
    stamp, writer = raw[0], raw[1]
    if stamp is None:
        return None, None
    text = stamp.decode() if isinstance(stamp, bytes) else str(stamp)
    name = (writer.decode() if isinstance(writer, bytes) else str(writer)) if writer else None
    return datetime.fromisoformat(text), name


# --- the happy path ----------------------------------------------------------


async def test_a_present_surface_pushes_the_stay_forward(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """The renewal in one sentence: the screen is open, so the container stays."""
    user, project, app_id = await _user_project_app(db_session, "present@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))
    before = datetime.now(UTC)

    body = await _renew(client, user, project)

    assert body["outcome"] == "renewed"
    stay, writer = await _stay(fake_redis, user.id)
    assert stay is not None
    assert writer == "surface_present"
    assert timedelta(seconds=SURFACE_PRESENT_STAY_SECONDS - 5) <= stay - before
    assert stay - before <= timedelta(seconds=SURFACE_PRESENT_STAY_SECONDS + 5)


async def test_a_hidden_surface_asks_for_the_longer_budget(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """A backgrounded tab cannot promise to come back in 45 seconds — browsers throttle, sleep
    and freeze their timers — so it buys the longer budget instead of losing its container for
    failing a promise it was never able to keep."""
    user, project, app_id = await _user_project_app(db_session, "hidden@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))
    before = datetime.now(UTC)

    body = await _renew(client, user, project, presence="hidden")

    assert body["outcome"] == "renewed"
    stay, _ = await _stay(fake_redis, user.id)
    assert stay is not None
    assert stay - before >= timedelta(seconds=HIDDEN_SURFACE_PRESENT_STAY_SECONDS - 5)


async def test_a_visible_renewal_never_shortens_a_hidden_ones_reprieve(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """THE DEADLINE NEVER MOVES BACKWARD, and this is where it would if it could: a tab coming
    back on screen renews on the very next tick, and the visible budget is fifteen minutes
    shorter than the hidden one it would be overwriting."""
    user, project, app_id = await _user_project_app(db_session, "waking@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))

    await _renew(client, user, project, presence="hidden")
    hidden_stay, _ = await _stay(fake_redis, user.id)

    await _renew(client, user, project, presence="visible")
    after_waking, _ = await _stay(fake_redis, user.id)

    assert hidden_stay is not None
    assert after_waking is not None
    assert after_waking == hidden_stay


async def test_the_comparison_that_protects_the_deadline_happens_inside_the_script(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """★ WHERE the monotonic comparison lives, which is the whole of whether it can be raced.

    A surface renews every forty-five seconds and a project can be framed by two of them, so
    "read the standing value, compare, write" is three steps with a gap a second renewal fits
    inside — the shorter write lands last and cuts the longer reprieve. Comparing INSIDE the
    script closes the gap by construction.

    WHAT THIS CAN AND CANNOT SEE, stated plainly. A stay written straight onto the hash, longer
    than anything a presence renewal can buy, must survive a renewal that asks for less — so
    deleting the script's clause and leaving nothing in its place goes red here. It CANNOT
    distinguish a correct caller-side comparison from the in-script one: with nothing racing, the
    two compute the same answer. The interleaving that separates them needs two real clients and
    is not reproducible in this suite; `test_deadline_writers.py` pins the script itself instead,
    which is where the atomicity actually lives.

    Mutation check: delete the `standing >= ARGV[2]` clause from
    `_CAS_GRANT_PRESENCE_STAY_LUA` and this goes red."""
    user, project, app_id = await _user_project_app(db_session, "inscript@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))

    far_off = datetime.now(UTC) + timedelta(seconds=HIDDEN_SURFACE_PRESENT_STAY_SECONDS * 4)
    await fake_redis.hset(
        registry_key(user.id),
        REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
        far_off.isoformat(timespec="microseconds"),
    )

    body = await _renew(client, user, project, presence="visible")

    settled, _ = await _stay(fake_redis, user.id)
    assert settled == far_off, "a shorter renewal overwrote a longer standing deadline"
    assert body["stayUntil"] is not None
    assert datetime.fromisoformat(body["stayUntil"]) == far_off


async def test_a_renewal_reports_the_deadline_actually_in_force(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """The wire value, not the one the caller proposed. A visible renewal arriving behind a
    standing hidden one keeps the longer deadline, and `stayUntil` has to say so — a client shown
    the five minutes it asked for would be told the app closes long before it does."""
    user, project, app_id = await _user_project_app(db_session, "inforce@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))

    hidden = await _renew(client, user, project, presence="hidden")
    visible = await _renew(client, user, project, presence="visible")

    assert visible["outcome"] == "renewed"
    assert visible["stayUntil"] == hidden["stayUntil"]


# --- what must not be written ------------------------------------------------


async def test_a_renewal_aimed_at_another_project_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """The ordinary reading a moment after somebody opens a second project — and the one that
    would be a real defect if it were allowed through. The registry key is per USER and survives
    a container swap, so a tab still holding the first project would otherwise push a stay onto
    the second project's container: sparing, under a reprieve nobody granted it, a container the
    first project's own teardown is about to delete."""
    user, project, _ = await _user_project_app(db_session, "switcher@bial.test")
    other_project = await ProjectFactory.create(db_session, user.id)
    other_app_id = await resolve_app_for_project(db_session, user.id, other_project.id)
    await db_session.commit()
    await _register(fake_redis, user.id, app_name_for(other_app_id))

    body = await _renew(client, user, project)

    assert body["outcome"] == "not_this_container"
    assert body["stayUntil"] is None
    stay, _ = await _stay(fake_redis, user.id)
    assert stay is None, "the other project's container was given a reprieve it did not earn"


async def test_no_registry_hash_is_conjured_for_a_project_with_nothing_running(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """A hash written here would be a container record for a container that does not exist, and
    every sparing arm downstream reads that record as evidence."""
    user, project, _ = await _user_project_app(db_session, "nothing@bial.test")

    body = await _renew(client, user, project)

    assert body["outcome"] == "nothing_running"
    assert await fake_redis.exists(registry_key(user.id)) == 0


async def test_a_project_that_was_never_built_says_nothing_is_running(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """No app row at all. Not a 404 — the PROJECT exists and belongs to this citizen; there is
    simply no container for a surface to be holding open."""
    user = await UserFactory.create(db_session, email="unbuilt@bial.test")
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()

    body = await _renew(client, user, project)

    assert body["outcome"] == "nothing_running"


# --- the doors ----------------------------------------------------------------


async def test_another_users_project_is_a_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """Owned-or-404 like every route in this file: a cross-user project id and a missing one are
    the same non-leaking answer."""
    owner, project, app_id = await _user_project_app(db_session, "owner@bial.test")
    intruder = await UserFactory.create(db_session, email="intruder@bial.test")
    await db_session.commit()
    await _register(fake_redis, owner.id, app_name_for(app_id))

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/renew",
        headers=auth_headers(intruder),
        json={"presence": "visible"},
    )

    assert resp.status_code == 404
    stay, _ = await _stay(fake_redis, owner.id)
    assert stay is None


async def test_a_renewal_without_csrf_is_refused(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """It is not a free read: it pushes a deadline forward on coordination state, and a deadline
    a third-party page could extend from a citizen's browser is a bill an attacker can run up."""
    user, project, app_id = await _user_project_app(db_session, "nocsrf@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/renew",
        headers=auth_headers(user, with_csrf=False),
        json={"presence": "visible"},
    )

    assert resp.status_code == 403
    stay, _ = await _stay(fake_redis, user.id)
    assert stay is None


async def test_an_unreadable_coordination_store_answers_503(
    client: AsyncClient, db_session: AsyncSession, fake_redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that will not answer is a 503, never a quiet `nothing_running` — the client
    re-arms on the difference, and reading an outage as "your container is gone" is exactly the
    mistake `preview-state` was reshaped to stop making.

    PATCHED ON `eval`, WHICH IS THE ONE CALL THIS ROUTE MAKES. Refusing a method the route never
    invokes would leave this green whatever the route did with an outage."""
    user, project, app_id = await _user_project_app(db_session, "outage@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise RedisConnectionError("coordination store is gone")

    monkeypatch.setattr(fake_redis, "eval", _refuse)

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/renew",
        headers=auth_headers(user),
        json={"presence": "visible"},
    )

    assert resp.status_code == 503


async def test_the_body_may_be_omitted_and_reads_as_a_visible_surface(
    client: AsyncClient, db_session: AsyncSession, fake_redis
) -> None:
    """`presence` carries a default so a caller that sends `{}` is answered rather than 422'd —
    and the default is the SHORTER budget, which is the safe direction to be wrong in."""
    user, project, app_id = await _user_project_app(db_session, "emptybody@bial.test")
    await _register(fake_redis, user.id, app_name_for(app_id))
    before = datetime.now(UTC)

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/renew",
        headers=auth_headers(user),
        json={},
    )

    assert resp.status_code == 200, resp.text
    stay, _ = await _stay(fake_redis, user.id)
    assert stay is not None
    assert stay - before <= timedelta(seconds=SURFACE_PRESENT_STAY_SECONDS + 5)
