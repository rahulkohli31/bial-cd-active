"""What happens to a pardoned preview after its turn ends.

The pardon itself (no teardown, registry kept, stay granted, lock released) is asserted on the
happy path in `test_manager.py`; this module covers what happens next: nothing renews liveness
(deliberate — the lease is the owner, exactly like a relaunched preview), the background sweep
honors an unexpired lease and reaps through it once it lapses, and reconcile-on-start reaps
through even an unexpired one (covered in
`test_manager.py::test_clean_end_then_start_restores_from_snapshot_not_fresh`).

HOW THE SESSIONS GET HERE. A session is allocated by `ensure_sandbox` and ended by
`finish_turn_sandbox` — the pair production uses, and the only pair left. `touched=True` is the
arm that earns the full stay, which is what the lease assertions below are about.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.config import settings
from src.db.models.user import User
from src.services.build_sessions.locks import (
    lock_is_held,
    read_registry,
    stay_of_execution_is_current,
)
from src.services.build_sessions.manager import SessionManager, app_name_for
from src.services.build_sessions.reaper import sweep_all
from src.services.redis import heartbeat_key, registry_key
from src.services.redis.keys import REGISTRY_FIELD_PREVIEW_STAY_UNTIL
from src.services.sandbox.config import SandboxConfig
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirrors `test_manager.py`: `ensure_sandbox` builds the app env, which needs a configured
    # sandbox block even though every container here is a FakeSandboxClient.
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


async def _completed_build(
    db: AsyncSession, email: str, client: FakeSandboxClient
) -> tuple[User, SessionManager, uuid.UUID]:
    """Take one session all the way to the end of a write turn, and hand back the pardoned
    state. The assertion is the fixture's own liveness check: a session that never reached its
    terminal would make every lease assertion below vacuous."""
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    manager = SessionManager()
    session = await manager.ensure_sandbox(
        db, user, project.id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=True)
    assert session.status is BuildSessionStatus.ENDED
    return user, manager, session.app_id


async def test_sweep_spares_a_pardoned_preview_inside_its_lease(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The heartbeat is deleted FIRST so the lease alone spares the container — otherwise its
    # ≤90 s residue would mask a broken stay.
    client = FakeSandboxClient()
    user, manager, app_id = await _completed_build(db_session, "pardon1@rvaiglobal.com", client)
    await fake_redis.delete(heartbeat_key(user.id))

    reaped = (await sweep_all(fake_redis, client, live_users=manager.live_user_ids())).reaped

    assert reaped == 0  # spared: the lease is current
    assert app_name_for(app_id) not in client.torn_down
    assert await read_registry(fake_redis, user.id) is not None
    assert await stay_of_execution_is_current(fake_redis, user.id) is True


async def test_sweep_reaps_a_pardoned_preview_once_its_lease_lapses(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # Idle expiry: overwrite the granted stay with a lapsed stamp (the reaper-suite technique)
    # and the next sweep executes the pardon — teardown, registry gone. This is the server half
    # of the portal's "placeholder + Relaunch" journey.
    client = FakeSandboxClient()
    user, manager, app_id = await _completed_build(db_session, "pardon2@rvaiglobal.com", client)
    await fake_redis.delete(heartbeat_key(user.id))
    lapsed = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, lapsed)

    reaped = (await sweep_all(fake_redis, client, live_users=manager.live_user_ids())).reaped

    assert reaped == 1
    assert app_name_for(app_id) in client.torn_down
    assert await read_registry(fake_redis, user.id) is None
    assert await lock_is_held(fake_redis, user.id) is False


async def test_pardon_survives_a_stay_grant_failure(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Best-effort per the end-sequence policy: a Redis blip on the grant must not hang the feed
    # or strand the lock.
    # Degraded mode is the pre-stay lifetime — the registry stays for the sweep to find at
    # heartbeat lapse, so nothing is orphaned.
    async def boom_grant(*_a: object, **_k: object) -> datetime:
        raise RuntimeError("redis blip on the stay grant")

    monkeypatch.setattr("src.services.build_sessions.manager.grant_stay_of_execution", boom_grant)
    client = FakeSandboxClient()
    user, manager, app_id = await _completed_build(db_session, "pardon4@rvaiglobal.com", client)

    # The turn's ending completed: lock released, session popped.
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None
    # No lease landed — but the container is discoverable (registry kept), so the next
    # sweep reaps it at heartbeat lapse instead of leaking it.
    assert await stay_of_execution_is_current(fake_redis, user.id) is False
    assert await read_registry(fake_redis, user.id) is not None
    await fake_redis.delete(heartbeat_key(user.id))
    assert (await sweep_all(fake_redis, client, live_users=manager.live_user_ids())).reaped == 1
    assert app_name_for(app_id) in client.torn_down
