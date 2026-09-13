"""The seam between a build starting and a window's files reaching Redis — and its gate.

WHAT THIS FILE IS FOR. `copy_window_for_project` answers six questions for itself, and five of the
six answers mean "do nothing": no lake configured, no Redis configured, the connector was never
switched on for this project, the switch is down, the owner is not approved. Each of those is a
supported state — a developer machine, a deployment without a lake, a citizen who never asked —
and each must end in a build that provisions normally and a lake that was never contacted.

THE ONE ASSERTION THAT IS NOT ABOUT REDIS. Every off-state is asserted on the LAKE, not on the key
count: the point is that an unapproved project's window is never LISTED, not merely that nothing
was written. A version that listed first and refused later would pass a key-count assertion while
reading a container the citizen has no right to.

The on-ness conjunction is read off `resolve_window`, never spelled again here. A second place
that decides whether a connector reads is a second place that can disagree with the rail the
citizen is looking at — which is why the approved/enabled matrix below is parametrised over the
states rather than written as separate tests that could each drift.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.lake import client as lake_client
from src.services.lake import copy as lake_copy
from src.services.lake.client import LakeClient, LakeEntry
from src.services.lake.config import LakeConfig
from src.services.lake.errors import LakeError
from src.services.lake.transfer import _digest
from src.services.redis import client as redis_client
from src.services.redis.keys import lake_file_key
from src.services.usage import ist_today
from tests.api.v1.connectors.conftest import KEY, seed_decision
from tests.factories import ProjectFactory, UserFactory

_ROOT = "AOS/tb_flight_fact_report/"


def _config() -> LakeConfig:
    return LakeConfig(
        url=f"https://alake.blob.core.windows.net/acontainer/{_ROOT}",
        identity_client_id="52b74947-0621-46e2-a523-a6b466f47c33",
        identity_resource_id="/subscriptions/x/resourcegroups/y/providers/z/id/an-identity",
    )


class _RecordingLake:
    """A lake that records whether it was reached at all — which is the assertion here, not the
    payload it hands back."""

    def __init__(self) -> None:
        self.config = _config()
        self.listed = 0
        self.downloaded: list[str] = []
        # Ten consecutive days ending at the connector's ceiling, so any live window overlaps.
        ceiling = ist_today() - timedelta(days=1)
        self._entries = tuple(
            LakeEntry(
                f"{_ROOT}{day:%Y}/{day:%B}".upper()
                + f"/tb_flight_fact_report_{day:%Y%m%d}.parquet",
                4_000,
            )
            for day in (ceiling - timedelta(days=offset) for offset in range(10))
        )

    async def list_files(self) -> tuple[LakeEntry, ...]:
        self.listed += 1
        return self._entries

    async def download(self, name: str) -> bytes:
        self.downloaded.append(name)
        return b"PAR1" + name.encode()


@pytest.fixture
async def lake() -> AsyncIterator[_RecordingLake]:
    """Bind the recording lake as the app-level singleton, exactly as `get_lake()` would."""
    from typing import cast

    recording = _RecordingLake()
    lake_client._lake_singleton = cast("LakeClient", recording)
    yield recording
    lake_client._lake_singleton = None


@pytest.fixture
async def redis_bytes() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    binary = fakeredis.aioredis.FakeRedis(decode_responses=False)
    redis_client._redis_bytes_singleton = binary
    yield binary
    await binary.flushall()
    await binary.aclose()
    redis_client._redis_bytes_singleton = None


async def _project_with(
    db,
    *,
    access: ConnectorRequestStatus | None,
    enabled: bool = True,
    window_days: int = 7,
) -> tuple[uuid.UUID, uuid.UUID]:
    user = await UserFactory.create(db)
    project = await ProjectFactory.create(db, user_id=user.id)
    if access is not None:
        await seed_decision(db, user.id, access, None)
    db.add(
        ProjectConnector(
            project_id=project.id,
            connector_key=KEY,
            enabled=enabled,
            window_kind=ConnectorWindowKind.RELATIVE,
            window_days=window_days,
        )
    )
    await db.flush()
    return user.id, project.id


# --- the happy path -----------------------------------------------------------------------------


async def test_an_approved_switched_on_project_copies_its_window(db_session, lake, redis_bytes):
    """★ The one state in which anything happens at all."""
    user_id, project_id = await _project_with(db_session, access=ConnectorRequestStatus.APPROVED)

    report = await lake_copy.copy_window_for_project(
        db_session, user_id=user_id, project_id=project_id
    )

    assert report is not None
    assert report.copied == 7, "seven days requested, seven days present in the listing"
    assert lake.listed == 1
    for name in lake.downloaded:
        assert await redis_bytes.exists(lake_file_key(_digest(name)))


async def test_a_second_copy_of_the_same_window_downloads_nothing(db_session, lake, redis_bytes):
    """Fired on every birth, so the skip is what stops a relaunch re-downloading the window."""
    user_id, project_id = await _project_with(db_session, access=ConnectorRequestStatus.APPROVED)
    await lake_copy.copy_window_for_project(db_session, user_id=user_id, project_id=project_id)
    lake.downloaded.clear()

    report = await lake_copy.copy_window_for_project(
        db_session, user_id=user_id, project_id=project_id
    )

    assert report is not None and report.already_held is True
    assert lake.downloaded == []


# --- the gate -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("access", "enabled"),
    [
        (None, True),
        (ConnectorRequestStatus.PENDING, True),
        (ConnectorRequestStatus.DECLINED, True),
        (ConnectorRequestStatus.CANCELLED, True),
        (ConnectorRequestStatus.APPROVED, False),
    ],
    ids=["never-asked", "pending", "declined", "cancelled", "switched-off"],
)
async def test_a_connector_that_is_not_effectively_on_never_reaches_the_lake(
    db_session, lake, redis_bytes, access, enabled
):
    """★ ASSERTED ON THE LAKE, NOT ON THE KEY COUNT. The claim is that an unapproved project's
    container is never LISTED — a version that listed first and refused afterwards would pass a
    "nothing was written" assertion while reading data the citizen has no right to.

    Five states, one branch: the switch AND the approval, read off `resolve_window` as a single
    `effectively_on`. Written as one parametrised test because they ARE one branch reached five
    ways; separate tests would read as five behaviours and drift into five rules."""
    user_id, project_id = await _project_with(db_session, access=access, enabled=enabled)

    report = await lake_copy.copy_window_for_project(
        db_session, user_id=user_id, project_id=project_id
    )

    assert report is None
    assert lake.listed == 0
    assert lake.downloaded == []


async def test_a_project_that_never_switched_it_on_has_no_row_and_reaches_nothing(
    db_session, lake, redis_bytes
):
    """No row is a different fact from `enabled = false`, and the resolver answers `None` for it.
    Reached by every project on the platform that has never touched this feature."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await seed_decision(db_session, user.id, ConnectorRequestStatus.APPROVED, None)

    report = await lake_copy.copy_window_for_project(
        db_session, user_id=user.id, project_id=project.id
    )

    assert report is None
    assert lake.listed == 0


async def test_another_persons_project_row_is_not_reachable(db_session, lake, redis_bytes):
    """★ THE ISOLATION PREDICATE. `project_connectors` carries no user column of its own, so the
    ownership claim rides a join on `projects` in the same WHERE clause. Drop it and an approved
    citizen's build would copy somebody else's project's window."""
    owner_id, project_id = await _project_with(db_session, access=ConnectorRequestStatus.APPROVED)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")
    await seed_decision(db_session, stranger.id, ConnectorRequestStatus.APPROVED, None)

    report = await lake_copy.copy_window_for_project(
        db_session, user_id=stranger.id, project_id=project_id
    )

    assert report is None, f"the row belongs to {owner_id}, not the caller"
    assert lake.listed == 0


# --- the off postures ---------------------------------------------------------------------------


async def test_no_lake_configured_is_a_quiet_no_op(db_session, redis_bytes):
    """The developer-machine posture, and the deployment that has not been given a lake. Binds no
    lake fixture on purpose: with one bound this branch is unreachable by construction."""
    user_id, project_id = await _project_with(db_session, access=ConnectorRequestStatus.APPROVED)

    assert lake_client._lake_singleton is None
    assert (
        await lake_copy.copy_window_for_project(db_session, user_id=user_id, project_id=project_id)
        is None
    )


async def test_no_redis_configured_is_a_quiet_no_op(db_session, lake):
    """Binds no Redis fixture, so `get_redis_bytes()` raises `RedisNotConfiguredError` — which is
    an ANSWER here, not a failure, and a build still provisions."""
    user_id, project_id = await _project_with(db_session, access=ConnectorRequestStatus.APPROVED)

    assert (
        await lake_copy.copy_window_for_project(db_session, user_id=user_id, project_id=project_id)
        is None
    )
    assert lake.listed == 0


# --- failure never reaches the caller ------------------------------------------------------------


async def test_a_lake_failure_never_escapes_the_detached_copy(db_session, lake, redis_bytes):
    """★ Nothing reads this copy, so a citizen must never lose a build to it. The guarded entry
    point swallows the lake's and Redis's failures; the detached wrapper catches everything else.
    Asserted through the wrapper, because that is the one a build path actually calls."""
    user_id, project_id = await _project_with(db_session, access=ConnectorRequestStatus.APPROVED)

    async def _explode() -> tuple[LakeEntry, ...]:
        raise LakeError("the lake refused this identity")

    lake.list_files = _explode

    await lake_copy._copy_in_its_own_session(user_id, project_id)


# --- the pool the copy must not sit on ---------------------------------------------------------


async def test_the_database_session_is_closed_before_a_single_byte_is_downloaded(
    db_session, lake, redis_bytes, monkeypatch
):
    """★ THE COPY MUST NOT HOLD A POOLED CONNECTION ACROSS ITS NETWORK WORK. The transfer is a
    listing plus up to a full window of blob downloads — measured at ~6.7 s for one window from
    outside the region, and unbounded in the general case. The control plane's pool is twenty
    wide, so a background copy nobody reads, sitting idle-in-transaction for that long on every
    container birth, is a request-path outage waiting for enough concurrent builds.

    ASSERTED AT THE HAND-OFF, NOT BY READING THE INDENTATION. `in_transaction()` is precisely
    "this session is holding a connection out of the pool", and it is sampled at the moment the
    network half is invoked. Moving `run_window_copies` back inside the `async with` turns this
    red, which is the only reason the test is worth having.

    Deliberately independent of what the database contains: this asserts WHERE the two halves
    are called from, and it must not quietly pass because the plan came back empty."""
    holding_a_connection: list[bool] = []
    planned_first: list[bool] = []

    from src.db import base as db_base

    made: list[AsyncSession] = []
    original_factory = db_base.async_session_factory

    def _recording_factory():
        session = original_factory()
        made.append(session)
        return session

    monkeypatch.setattr(db_base, "async_session_factory", _recording_factory)

    real_plan = lake_copy.plan_window_copies

    async def _plan(db, **kwargs):
        planned_first.append(True)
        return await real_plan(db, **kwargs)

    async def _run(plans, *, project_id):
        holding_a_connection.append(any(s.in_transaction() for s in made))
        return None

    monkeypatch.setattr(lake_copy, "plan_window_copies", _plan)
    monkeypatch.setattr(lake_copy, "run_window_copies", _run)

    await lake_copy._copy_in_its_own_session(uuid.uuid4(), uuid.uuid4())

    # Liveness first: both halves must actually have been reached, in order, or the assertion
    # underneath is about a hand-off that never happened.
    assert planned_first == [True]
    assert made, "the detached copy did not open a session at all"
    assert holding_a_connection == [False]
