"""The Azure-side sandbox inventory (#83 follow-up) — the fleet view the Redis sweep cannot
produce. No DB: a fake lister plus the real registry namespace."""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis

from src.services.build_sessions.inventory import FleetLister, take_sandbox_inventory
from src.services.build_sessions.manager import app_name_for
from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE
from src.services.sandbox import SandboxError
from src.services.sandbox.base import FleetMember
from tests.fakes import a_fleet_member


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
    fake_redis: aioredis.Redis,
) -> None:
    """THE LEAK, and the whole reason this module exists. `sweep_all` walks the registry, so a
    container with no entry there is one it will never reach — it just bills. One did, for
    twelve days, and nothing in the platform could have told anyone."""
    tracked_user, tracked_app = uuid.uuid4(), uuid.uuid7()
    await _register(fake_redis, tracked_user, app_name_for(tracked_app))
    orphan = "sbx-019f74300c9f747db10b73b6dcdd"

    inv = await take_sandbox_inventory(fake_redis, _Fleet([app_name_for(tracked_app), orphan]))

    assert inv.unregistered == (orphan,)  # the one nothing is tracking
    assert inv.registered_missing == ()
    assert len(inv.live) == 2


async def test_a_registry_entry_whose_container_is_gone_is_reported_separately(
    fake_redis: aioredis.Redis,
) -> None:
    """The opposite gap, and far less urgent — nothing is billing for it, and the next
    `reconcile_user` clears the entry on its own. Reported apart from the leak so an operator
    is never asked to act on the harmless half."""
    user, app_id = uuid.uuid4(), uuid.uuid7()
    await _register(fake_redis, user, app_name_for(app_id))

    inv = await take_sandbox_inventory(fake_redis, _Fleet([]))

    assert inv.registered_missing == (app_name_for(app_id),)
    assert inv.unregistered == ()


async def test_a_fully_tracked_fleet_reports_no_gaps(fake_redis: aioredis.Redis) -> None:
    user, app_id = uuid.uuid4(), uuid.uuid7()
    await _register(fake_redis, user, app_name_for(app_id))

    inv = await take_sandbox_inventory(fake_redis, _Fleet([app_name_for(app_id)]))

    assert inv.unregistered == ()
    assert inv.registered_missing == ()


async def test_a_listing_failure_propagates_rather_than_reporting_a_clean_fleet(
    fake_redis: aioredis.Redis,
) -> None:
    """A partial inventory is indistinguishable from a clean one, and "clean" is the answer
    that gets a billing container forgotten for another twelve days. The route maps this to a
    503; what it must never do is return an empty `unregistered` list."""
    with pytest.raises(SandboxError):
        await take_sandbox_inventory(fake_redis, _Fleet([], error=SandboxError("ARM said no")))


def test_the_concrete_client_satisfies_the_protocol_by_shape() -> None:
    """The capability deliberately lives on `AcaSandboxClient`, not the frozen `SandboxClient`
    ABC (C2). This is what keeps that decision honest — drop the method and the admin route's
    `isinstance` check starts answering 503 on a healthy deployment."""
    from src.services.sandbox.client import AcaSandboxClient

    assert issubclass(AcaSandboxClient, FleetLister)
