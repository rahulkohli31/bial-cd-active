"""`SessionManager.launch_shared_preview` / `revoke_shared_preview` (#198 slice 3) — a
colleague's own door into the one-per-user slot `relaunch_preview` uses for a builder's own
project. NOT "read-only": Key Decision 3 grants "Can use", and the recipient really can
create, update and delete the owner's records through the app's own UI.

Driven by FakeSandboxClient + fakeredis + fake storage + the `:5432` test DB, the same harness
`test_manager.py::test_relaunch_*` uses for the method this one mirrors — go there for the
attach/cold-restore/readiness-failure state machine's own exhaustive coverage; these tests are
about what is DIFFERENT for a shared view: whose slot it occupies, which snapshot it may ever
restore from, and how it tags the container it mints.
"""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.project import Project
from src.db.models.user import User
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import lock_is_held, read_registry
from src.services.build_sessions.manager import (
    NoSnapshotToRelaunchError,
    SandboxReclaimBlockedError,
    SessionManager,
    shr_name_for,
)
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_SHARED_OWNER_ID,
    REGISTRY_FIELD_SHARED_PROJECT_ID,
    REGISTRY_FIELD_SHARED_SERVED_COUNT,
)
from src.services.sandbox import SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.storage import recovery_key, snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every peer service-level `build_sessions` test file defines this — omitting it here was
    the bug: `build_app_env`/`_provision_container` read `settings.sandbox` and raise
    `SandboxNotConfiguredError` without it, which silently failed 9 of this file's 11 tests on
    plumbing rather than on the property each one exists to prove."""
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


async def _owner_with_saved_app(
    db: AsyncSession, store: FakeStorage, *, email: str
) -> tuple[User, Project, uuid.UUID]:
    owner = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, owner.id, description="A shared project")
    app_id = await resolve_app_for_project(db, owner.id, project.id)
    await db.commit()
    await db.refresh(project)
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return owner, project, app_id


async def test_launch_cold_restores_and_returns_the_owners_app_id(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner1@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient1@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    preview = await manager.launch_shared_preview(db_session, recipient, project, client)

    shared_name = shr_name_for(app_id, recipient.id)
    assert preview.app_id == app_id  # the OWNER's app id, never the recipient's
    assert client.restored == [shared_name]
    assert client.provisioned == []  # never a blank template
    assert preview.ready is True
    assert await lock_is_held(fake_redis, recipient.id) is False  # lock released, slot free


async def test_launch_never_restores_the_owners_recovery_bundle(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Requirement 21, pinned directly: a shared view is restored from the owner's last
    deliberate Save, never their crash-recovery bundle — even when one exists and is newer."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner2@example.com"
    )
    await fake_storage.put(recovery_key(app_id), b"NEWER, BUT NEVER THE ANSWER")
    recipient = await UserFactory.create(db_session, email="recipient2@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    await manager.launch_shared_preview(db_session, recipient, project, client)

    assert client.restored_from == [snapshot_key(app_id)]


async def test_the_restored_container_is_tagged_as_a_shared_sandbox(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner3@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient3@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    await manager.launch_shared_preview(db_session, recipient, project, client)

    assert client.restored_as_kind == ["shared_sandbox"]


async def test_launch_stamps_the_registry_with_the_shared_projects_identity(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The registry-hash half of requirement 24 (#198 slice 5): Launch must stamp WHICH
    project this slot is a shared view of, and WHOSE, so a later occupancy check can
    recognize it without reverse-parsing `shr_name_for`'s hash. See
    `test_a_live_shared_view_earns_the_hand_over_dialog_instead_of_silent_reclaim` for the
    behavior this stamp exists to enable."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner3b@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient3b@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    await manager.launch_shared_preview(db_session, recipient, project, client)

    reg = await read_registry(fake_redis, recipient.id)
    assert reg is not None
    assert reg[REGISTRY_FIELD_SHARED_PROJECT_ID] == str(project.id)
    assert reg[REGISTRY_FIELD_SHARED_OWNER_ID] == str(owner.id)


async def test_refresh_disowns_the_prior_containers_served_count(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A high-water mark left on the hash by the SWEEP (`reaper.py::_renew_shared_view_from_
    traffic`) must not survive into the container that replaces it — the fresh container's
    first `/served` reading would otherwise compare against the old one's total, read as "no
    new traffic", and never earn a liveness renewal at all."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner3d@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient3d@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, client)
    # Simulate a sweep having recorded traffic against the FIRST container.
    await fake_redis.hset(registry_key(recipient.id), REGISTRY_FIELD_SHARED_SERVED_COUNT, "9")

    await manager.launch_shared_preview(db_session, recipient, project, client, force_refresh=True)

    reg = await read_registry(fake_redis, recipient.id)
    assert reg is not None
    assert REGISTRY_FIELD_SHARED_SERVED_COUNT not in reg


async def test_launch_attaches_to_an_already_live_shared_view(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner4@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient4@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.launch_shared_preview(db_session, recipient, project, client)

    # Re-attach: point the fake at itself as an already-live container under the same name.
    shared_name = shr_name_for(app_id, recipient.id)
    client.attach_handle = SandboxHandle(
        fqdn=f"{shared_name}.example",
        token="tok",  # noqa: S106 - a fake, never a real bearer
        app_name=shared_name,
        preview_url=first.preview_url,
        ready=True,
    )
    second = await manager.launch_shared_preview(db_session, recipient, project, client)

    assert client.restored == [shared_name]  # only ONE restore — the second call attached
    assert second.app_id == app_id


async def test_refresh_always_restores_even_when_already_live(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Requirement 22: Refresh moves the served snapshot forward even when a live view is
    already up — unlike Launch, it never treats an already-attached container as good enough."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner5@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient5@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, client)

    await manager.launch_shared_preview(db_session, recipient, project, client, force_refresh=True)

    shared_name = shr_name_for(app_id, recipient.id)
    assert client.restored == [shared_name, shared_name]  # restored TWICE, not attached once


async def test_launch_with_no_saved_snapshot_is_a_dead_end_404(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    owner = await UserFactory.create(db_session, email="owner6@example.com")
    project = await ProjectFactory.create(db_session, owner.id, description="Never saved")
    await resolve_app_for_project(db_session, owner.id, project.id)
    await db_session.commit()
    await db_session.refresh(project)
    recipient = await UserFactory.create(db_session, email="recipient6@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    with pytest.raises(NoSnapshotToRelaunchError):
        await manager.launch_shared_preview(db_session, recipient, project, client)

    assert client.provisioned == []
    assert client.restored == []
    assert await lock_is_held(fake_redis, recipient.id) is False


async def test_launch_refuses_while_the_recipient_is_mid_build_on_their_own_project(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`_slot_conflict_for` (not `BuildSessionConflictError` outright) is what actually answers
    here, and correctly so: the recipient's build and the shared project they are trying to open
    are two DIFFERENT projects, so this earns the richer `SandboxReclaimBlockedError` — the same
    hand-over-dialog opportunity any other different-project slot conflict gets — rather than the
    bare "already building, nothing you can do" refusal a same-project double-send would."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner7@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient7@example.com")
    recipient_project = await ProjectFactory.create(
        db_session, recipient.id, description="Recipient's own project"
    )
    manager = SessionManager()
    build_client = FakeSandboxClient()
    await manager.ensure_sandbox(
        db_session, recipient, recipient_project.id, sandbox_client=build_client, may_write=True
    )

    with pytest.raises(SandboxReclaimBlockedError) as caught:
        await manager.launch_shared_preview(db_session, recipient, project, FakeSandboxClient())

    assert caught.value.project_id == recipient_project.id
    assert caught.value.building is True


async def test_revoke_tears_down_a_live_shared_view(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner8@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient8@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, client)
    shared_name = shr_name_for(app_id, recipient.id)

    revoked = await manager.revoke_shared_preview(recipient.id, app_id, sandbox_client=client)

    assert revoked is True
    assert shared_name in client.torn_down
    assert await lock_is_held(fake_redis, recipient.id) is False


async def test_revoke_is_a_noop_when_nothing_is_there(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner9@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient9@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    revoked = await manager.revoke_shared_preview(recipient.id, app_id, sandbox_client=client)

    assert revoked is False
    assert client.torn_down == []


async def test_a_live_shared_view_earns_the_hand_over_dialog_instead_of_silent_reclaim(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Requirement 24 / #161's own mechanism, extended to a `shr-` occupant.

    Before the two registry fields this slice adds (`REGISTRY_FIELD_SHARED_PROJECT_ID`/
    `REGISTRY_FIELD_SHARED_OWNER_ID`, stamped at Launch), a `shr-` name matched no app the
    recipient owns, so `_occupying_project` returned `None` and `_refuse_if_reclaim_would_
    destroy_work` fell through its ghost exit — the recipient's still-open shared view was
    torn down with no dialog at all. This pins the fix: starting a build in a DIFFERENT
    project of the recipient's own must raise `SandboxReclaimBlockedError` naming the SHARED
    project, not silently reclaim the slot."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner11@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient11@example.com")
    recipient_project = await ProjectFactory.create(
        db_session, recipient.id, description="Recipient's own, different project"
    )
    manager = SessionManager()
    shared_client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, shared_client)

    build_client = FakeSandboxClient()
    with pytest.raises(SandboxReclaimBlockedError) as caught:
        await manager.ensure_sandbox(
            db_session,
            recipient,
            recipient_project.id,
            sandbox_client=build_client,
            may_write=True,
        )

    assert caught.value.project_id == project.id  # the SHARED project, not the recipient's own
    assert caught.value.project_name == project.name
    assert caught.value.dirty is False  # a clean stop — nothing of the recipient's own to lose
    assert caught.value.building is False
    assert caught.value.agent_working is False
    # THE CLIENT'S ONE SIGNAL to route to `give_up_shared_view` rather than `stopActiveBuild`/
    # `release` — both of which gate on `owned_project_or_404`, and `project_id` above names
    # the SHARED project's owner, whom the recipient never owns.
    assert caught.value.is_shared_view is True
    assert build_client.provisioned == []  # refused before anything was destroyed
    # The teardown that WOULD have run is `build_client`'s (whatever client `ensure_sandbox`
    # was passed reaps the incumbent on the way in) — `shared_client` never sees a teardown
    # call either way, so it is `build_client.torn_down` that actually pins the fix.
    shared_name = shr_name_for(app_id, recipient.id)
    assert shared_name not in build_client.torn_down


async def test_an_ordinary_build_disowns_a_prior_occupants_shared_stamp(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The MERGE half of the same fix: `hset(mapping=...)` only ADDS fields, so once a shared
    view is revoked and the recipient's OWN build takes the freed slot, the new registry
    record must not still carry the PRIOR occupant's `shared_project_id`/`shared_owner_id` —
    a leftover stamp would make `_occupying_shared_project` misidentify an ordinary build
    sandbox as somebody else's shared view."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner3c@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient3c@example.com")
    recipient_project = await ProjectFactory.create(
        db_session, recipient.id, description="Recipient's own project"
    )
    manager = SessionManager()
    shared_client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, shared_client)
    await manager.revoke_shared_preview(recipient.id, app_id, sandbox_client=shared_client)

    build_client = FakeSandboxClient()
    await manager.ensure_sandbox(
        db_session, recipient, recipient_project.id, sandbox_client=build_client, may_write=True
    )

    reg = await read_registry(fake_redis, recipient.id)
    assert reg is not None
    assert REGISTRY_FIELD_SHARED_PROJECT_ID not in reg
    assert REGISTRY_FIELD_SHARED_OWNER_ID not in reg


async def test_revoke_never_touches_the_recipients_own_build(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The container-identity check is the whole safety property here: revoking access to
    project A must never tear down a container the recipient is using for project B, whether
    that is their own build or a DIFFERENT colleague's shared view."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner10@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient10@example.com")
    recipient_project = await ProjectFactory.create(
        db_session, recipient.id, description="Recipient's own, unrelated project"
    )
    manager = SessionManager()
    build_client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, recipient, recipient_project.id, sandbox_client=build_client, may_write=True
    )
    await manager._finalize(session, "completed", build_client)  # noqa: SLF001 - pardons it

    revoked = await manager.revoke_shared_preview(
        recipient.id, app_id, sandbox_client=build_client
    )

    assert revoked is False
    assert build_client.torn_down == []


# --- SessionManager.give_up_shared_view — the recipient's own self-service exit (#198 R24) ----


async def test_give_up_shared_view_tears_down_the_recipients_own_slot(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The door `SandboxReclaimBlockedError.is_shared_view` points a recipient at: no
    `project_id`, because the shared occupant's own id names its OWNER, which the recipient
    does not own and `owned_project_or_404` would refuse."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner12@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient12@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, client)
    shared_name = shr_name_for(app_id, recipient.id)

    gave_up = await manager.give_up_shared_view(recipient.id, sandbox_client=client)

    assert gave_up is True
    assert shared_name in client.torn_down
    assert await lock_is_held(fake_redis, recipient.id) is False


async def test_give_up_shared_view_is_a_noop_when_nothing_is_there(
    fake_redis: aioredis.Redis,
) -> None:
    manager = SessionManager()
    client = FakeSandboxClient()

    gave_up = await manager.give_up_shared_view(uuid.uuid4(), sandbox_client=client)

    assert gave_up is False
    assert client.torn_down == []


async def test_give_up_shared_view_never_touches_the_recipients_own_build(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The slot holding the recipient's OWN `sbx-` build is not this action's business —
    `release_project_sandbox` is the door for that, and this one must not reach for it."""
    recipient = await UserFactory.create(db_session, email="recipient13@example.com")
    recipient_project = await ProjectFactory.create(
        db_session, recipient.id, description="Recipient's own project"
    )
    manager = SessionManager()
    build_client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, recipient, recipient_project.id, sandbox_client=build_client, may_write=True
    )
    await manager._finalize(session, "completed", build_client)  # noqa: SLF001 - pardons it

    gave_up = await manager.give_up_shared_view(recipient.id, sandbox_client=build_client)

    assert gave_up is False
    assert build_client.torn_down == []
