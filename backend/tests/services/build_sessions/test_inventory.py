"""The Azure-side sandbox inventory — the fleet view the Redis sweep cannot
produce. A fake lister, the real registry namespace, and what holds a container no registry
names: the pool's ledger and the owed teardowns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.pending_teardown import PendingTeardown
from src.db.models.sandbox_start import SandboxProjectType
from src.services.build_sessions.inventory import FleetLister, take_sandbox_inventory
from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE
from src.services.sandbox import SandboxError, pool
from src.services.sandbox.base import FleetMember, a_fresh_sandbox_name, app_name_for
from tests.factories import UserFactory
from tests.fakes import a_fleet_member, a_ready_pool_row


class _Fleet:
    """A control plane that lists whatever it is told to — or refuses."""

    def __init__(self, names: list[str], *, error: Exception | None = None) -> None:
        self.names = names
        self.error = error

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        if self.error is not None:
            raise self.error
        return [a_fleet_member(n) for n in self.names]


async def _register(redis: aioredis.Redis, user_id: uuid.UUID, app_name: str) -> None:
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )


async def test_a_container_no_registry_entry_tracks_is_reported_as_unregistered(
    db_session: AsyncSession, fake_redis: aioredis.Redis
) -> None:
    """THE LEAK, and the whole reason this module exists. `sweep_all` walks the registry, so a
    container with no entry there is one it will never reach — it keeps billing forever."""
    tracked_user, tracked_app = uuid.uuid4(), uuid.uuid7()
    await _register(fake_redis, tracked_user, app_name_for(tracked_app))
    orphan = "sbx-019f74300c9f747db10b73b6dcdd"

    inv = await take_sandbox_inventory(
        db_session, fake_redis, _Fleet([app_name_for(tracked_app), orphan])
    )

    assert inv.unregistered == (orphan,)  # the one nothing is tracking
    assert inv.registered_missing == ()
    assert len(inv.live) == 2


async def test_a_registry_entry_whose_container_is_gone_is_reported_separately(
    db_session: AsyncSession, fake_redis: aioredis.Redis
) -> None:
    """The opposite gap, and far less urgent — nothing is billing for it, and the next
    `reconcile_user` clears the entry on its own. Reported apart from the leak so an operator
    is never asked to act on the harmless half."""
    user, app_id = uuid.uuid4(), uuid.uuid7()
    await _register(fake_redis, user, app_name_for(app_id))

    inv = await take_sandbox_inventory(db_session, fake_redis, _Fleet([]))

    assert inv.registered_missing == (app_name_for(app_id),)
    assert inv.unregistered == ()


async def test_a_fully_tracked_fleet_reports_no_gaps(
    db_session: AsyncSession, fake_redis: aioredis.Redis
) -> None:
    user, app_id = uuid.uuid4(), uuid.uuid7()
    await _register(fake_redis, user, app_name_for(app_id))

    inv = await take_sandbox_inventory(db_session, fake_redis, _Fleet([app_name_for(app_id)]))

    assert inv.unregistered == ()
    assert inv.registered_missing == ()


async def test_a_listing_failure_propagates_rather_than_reporting_a_clean_fleet(
    db_session: AsyncSession, fake_redis: aioredis.Redis
) -> None:
    """A partial inventory is indistinguishable from a clean one, and "clean" is the answer
    that lets a billing container go unnoticed. The route maps this to a 503; what it must
    never do is return an empty `unregistered` list."""
    with pytest.raises(SandboxError):
        await take_sandbox_inventory(
            db_session, fake_redis, _Fleet([], error=SandboxError("ARM said no"))
        )


@pytest.mark.usefixtures("empty_sandbox_pool")
async def test_a_ready_container_the_pool_holds_is_not_reported_as_unregistered(
    db_session: AsyncSession, fake_redis: aioredis.Redis
) -> None:
    """No registry names a pool container until a start claims it, and the runbook tells an
    operator to delete an unregistered one by hand."""
    member, orphan = a_fresh_sandbox_name(), a_fresh_sandbox_name()
    await a_ready_pool_row(
        member,
        fqdn=f"{member}.example",
        image_ref="acr/img:v1",
        project_type=SandboxProjectType.PLAIN,
    )

    inv = await take_sandbox_inventory(db_session, fake_redis, _Fleet([member, orphan]))

    assert inv.unregistered == (orphan,)
    assert inv.registered == ()


async def test_a_container_whose_teardown_is_owed_is_not_reported_as_unregistered(
    db_session: AsyncSession, fake_redis: aioredis.Redis
) -> None:
    """An owed container's record is cleared before it goes, and its work may still be being
    written back: deleting it by hand as an orphan would lose that work."""
    user = await UserFactory.create(db_session, email="inv-owed@rvaiglobal.com")
    owed, orphan = a_fresh_sandbox_name(), a_fresh_sandbox_name()
    db_session.add(
        PendingTeardown(
            user_id=user.id,
            app_id=uuid.uuid4(),
            app_name=owed,
            write_back=True,
            project_id=uuid.uuid4(),
            instance_ref=datetime.now(UTC),
            claimed_until=datetime.now(UTC),
        )
    )
    await db_session.flush()

    inv = await take_sandbox_inventory(db_session, fake_redis, _Fleet([owed, orphan]))

    assert inv.unregistered == (orphan,)


@pytest.mark.usefixtures("empty_sandbox_pool")
async def test_a_container_a_start_is_still_creating_is_not_reported_as_unregistered(
    db_session: AsyncSession, fake_redis: aioredis.Redis
) -> None:
    """Until the registry records it, the ledger row its start wrote is the only thing naming it,
    and a create in flight is exactly what an operator deleting orphans by hand must not reach."""
    being_born, orphan = a_fresh_sandbox_name(), a_fresh_sandbox_name()
    await pool.hold_a_create(being_born, "acr/img:v1")

    inv = await take_sandbox_inventory(db_session, fake_redis, _Fleet([being_born, orphan]))

    assert inv.unregistered == (orphan,)
    assert inv.registered == ()


def test_the_concrete_client_satisfies_the_protocol_by_shape() -> None:
    """The capability deliberately lives on `AcaSandboxClient`, not the frozen `SandboxClient`
    ABC. This is what keeps that decision honest — drop the method and the admin route's
    `isinstance` check starts answering 503 on a healthy deployment."""
    from src.services.sandbox.client import AcaSandboxClient

    assert issubclass(AcaSandboxClient, FleetLister)
