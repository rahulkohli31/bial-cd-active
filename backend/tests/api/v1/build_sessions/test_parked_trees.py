"""The operator surface for the trees this plan sets aside (U25).

U2's quarantine slot and U3's divert slot would otherwise be WRITE-ONLY: no reader, no retention,
no runbook. In a false-`REVERTED` case those objects hold the only copy of a citizen's newest
work, and this plan names exactly that shape as a defect elsewhere — so it must not reproduce it.

THE ONE TO READ FIRST is `test_a_key_from_another_app_is_refused`. The promote route takes a key
out of a request body and writes what it names into an app's recovery slot; without a scoping
check that is one operator typo away from putting one citizen's tree into another's app.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.storage import divert_key, quarantine_key, recovery_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import UserFactory
from tests.fakes import FakeStorage, a_git_bundle

APP = uuid.UUID("0198f2c0-5555-7000-8000-00000000dead")
OTHER_APP = uuid.UUID("0198f2c0-6666-7000-8000-00000000beef")
EARLIER = datetime(2026, 8, 18, 11, 0, 0, 111111, tzinfo=UTC)
LATER = EARLIER + timedelta(minutes=5)

_LIST = "/v1/build-sessions/internal/apps/{app_id}/parked"
_PROMOTE = "/v1/build-sessions/internal/apps/{app_id}/promote"


async def _admin(db: AsyncSession) -> object:
    return await UserFactory.create(db, email="admin@bial.com")


async def _citizen(db: AsyncSession) -> object:
    return await UserFactory.create(db, email="nobody@rvaiglobal.com")


async def _park(store: FakeStorage, key: str, sha: str) -> None:
    await store.put(key, a_git_bundle(sha), metadata={"head_sha": sha})


async def test_a_citizen_cannot_list_another_persons_parked_work(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage
) -> None:
    """★ These objects are somebody's source code. The gate here is opt-in per route, exactly as
    it is in the admin package, so its absence is invisible — the route works perfectly."""
    citizen = await _citizen(db_session)

    resp = await client.post(_LIST.format(app_id=APP), headers=auth_headers(citizen))

    assert resp.status_code == 403
    # LIVENESS: an operator gets a real answer for the same app, so the 403 is the gate.
    admin = await _admin(db_session)
    assert (
        await client.post(_LIST.format(app_id=APP), headers=auth_headers(admin))
    ).status_code == 200


async def test_both_slots_are_listed_newest_first(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage: FakeStorage
) -> None:
    """An operator scrolling to the bottom of a list to find the tree they want is an operator who
    will eventually promote the wrong one."""
    admin = await _admin(db_session)
    await _park(fake_storage, quarantine_key(APP, EARLIER), "a" * 40)
    await _park(fake_storage, divert_key(APP, LATER), "b" * 40)

    body = (await client.post(_LIST.format(app_id=APP), headers=auth_headers(admin))).json()

    assert [tree["headSha"] for tree in body["trees"]] == ["b" * 40, "a" * 40]
    assert {tree["kind"] for tree in body["trees"]} == {"quarantine", "divert"}


async def test_an_app_with_nothing_parked_lists_nothing(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage
) -> None:
    admin = await _admin(db_session)

    body = (await client.post(_LIST.format(app_id=APP), headers=auth_headers(admin))).json()

    assert body["trees"] == []


async def test_promotion_puts_the_named_tree_back(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage: FakeStorage
) -> None:
    admin = await _admin(db_session)
    key = divert_key(APP, LATER)
    await _park(fake_storage, key, "b" * 40)

    resp = await client.post(
        _PROMOTE.format(app_id=APP), headers=auth_headers(admin), json={"key": key}
    )

    assert resp.status_code == 200
    assert resp.json()["promoted"] is True
    meta = await fake_storage.head(recovery_key(APP))
    assert meta is not None
    assert (meta.metadata or {})["head_sha"] == "b" * 40


async def test_a_key_from_another_app_is_refused(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage: FakeStorage
) -> None:
    """★ THE KEY COMES OUT OF A REQUEST BODY. It names an object to READ and an app to write it
    INTO, so without a scoping check this route is one typo away from putting one citizen's tree
    into another citizen's app — through the operator surface built to recover work.

    Mutation check: drop the prefix check in `promote_parked` and this goes red."""
    admin = await _admin(db_session)
    theirs = divert_key(OTHER_APP, LATER)
    await _park(fake_storage, theirs, "c" * 40)

    resp = await client.post(
        _PROMOTE.format(app_id=APP), headers=auth_headers(admin), json={"key": theirs}
    )

    assert resp.status_code >= 400
    assert await fake_storage.head(recovery_key(APP)) is None


async def test_promoting_the_tree_already_in_the_slot_is_a_no_op(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage: FakeStorage
) -> None:
    """`promoted: false` is NOT an error and must not read as one — it means there was nothing to
    do, which is the ordinary answer to an operator clicking twice."""
    admin = await _admin(db_session)
    key = divert_key(APP, LATER)
    await _park(fake_storage, key, "b" * 40)
    await _park(fake_storage, recovery_key(APP), "b" * 40)

    resp = await client.post(
        _PROMOTE.format(app_id=APP), headers=auth_headers(admin), json={"key": key}
    )

    assert resp.status_code == 200
    assert resp.json()["promoted"] is False


@pytest.mark.parametrize("route", [_LIST, _PROMOTE])
async def test_both_operator_routes_are_in_the_csrf_matrix(route: str) -> None:
    """A mutating POST outside the matrix is one nobody is checking. The table is hand-maintained,
    so a new route is only covered because the change that added it added its row."""
    from tests.api.v1.build_sessions.test_csrf import _MUTATING_POSTS

    assert route.replace("{app_id}", "{app_id}") in _MUTATING_POSTS
