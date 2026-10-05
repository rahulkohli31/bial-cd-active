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
from src.db.models.pending_teardown import PendingTeardownKind
from src.db.models.project import Project
from src.db.models.user import User
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import SharedViewStamp, lock_is_held, read_registry
from src.services.build_sessions.manager import (
    NoSnapshotToRelaunchError,
    SandboxReclaimBlockedError,
    SessionManager,
)
from src.services.build_sessions.shutdown import OwedTeardown, ShutdownReason
from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_ID,
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_SHARED_OWNER_ID,
    REGISTRY_FIELD_SHARED_PROJECT_ID,
    REGISTRY_FIELD_SHARED_SERVED_COUNT,
    REGISTRY_FIELD_STATE,
)
from src.services.sandbox import SandboxHandle, SandboxNotReadyError
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import (
    AttachesWhatTheRecordNames,
    FakeSandboxClient,
    FakeStorage,
    a_manager_whose_ledger_is,
    a_name_unrelated_to_its_app,
    detached_work_done,
)


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


@pytest.fixture
def handed_over(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, PendingTeardownKind, bool]]:
    """Every container a start hands to the shutdown routine, recorded rather than run: what the
    routine does with a shared view is `test_shutdown.py`'s subject."""
    recorded: list[tuple[str, PendingTeardownKind, bool]] = []

    def _record(owed: OwedTeardown, *, reason: ShutdownReason, **_aimed_at: object) -> None:
        recorded.append((owed.app_name, owed.kind, owed.write_back))

    monkeypatch.setattr(manager_module, "shut_it_down_in_the_background", _record)
    return recorded


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


def _the_view(owner: User, project: Project) -> SharedViewStamp:
    return SharedViewStamp(owner_id=owner.id, project_id=project.id)


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

    assert preview.app_id == app_id  # the OWNER's app id, never the recipient's
    assert len(client.restored) == 1
    assert client.provisioned == []  # never a blank template
    assert preview.ready is True
    assert await lock_is_held(fake_redis, recipient.id) is False  # lock released, slot free


async def test_launch_keeps_a_restored_container_whose_dev_server_never_readies(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The readiness arm is the one piece of the state machine this door does NOT share with
    `relaunch_preview` — it is a second copy of the same handler — so it gets its own pin here
    rather than in `test_manager.py`. A readiness timeout is a statement about the owner's app,
    and the heaviest apps in the estate are exactly the ones a colleague is sent a link to.

    Mutation check: restore `if not attached: raise` on the shared arm and this goes red."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner-slow@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient-slow@example.com")
    manager = SessionManager()

    class DevNeverReadies(FakeSandboxClient):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxNotReadyError("dev server not ready within 120s")

    client = DevNeverReadies()

    preview = await manager.launch_shared_preview(db_session, recipient, project, client)

    assert preview.ready is False, "an app that never served must not be reported as ready"
    assert preview.preview_url, "…but the URL still ships — the pane owns the labelled wait"
    assert len(client.restored) == 1  # it WAS created...
    assert client.torn_down == []  # ...and it survives the owner's app being slow
    assert await lock_is_held(fake_redis, recipient.id) is False


async def test_launch_restores_the_owners_saved_bundle_and_nothing_beside_it(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Requirement 21, pinned directly: a shared view is restored from the owner's last
    deliberate Save, never another object the store happens to hold for that app."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner2@example.com"
    )
    await fake_storage.put(
        f"quarantine/{app_id}/20260826T110500000000Z.bundle", b"NEWER, BUT NEVER THE ANSWER"
    )
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
    recognize it, which the container's name cannot say. See
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
    shared_name = client.restored[0]
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

    # Restored TWICE, each into a container of its own, not attached once.
    assert len(set(client.restored)) == 2


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
    shared_name = client.restored[0]

    revoked = await manager.revoke_shared_preview(
        recipient.id, app_id, _the_view(owner, project), sandbox_client=client
    )

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

    revoked = await manager.revoke_shared_preview(
        recipient.id, app_id, _the_view(owner, project), sandbox_client=client
    )

    assert revoked is False
    assert client.torn_down == []


async def test_a_first_message_puts_a_live_shared_view_away_and_starts(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[tuple[str, PendingTeardownKind, bool]],
) -> None:
    """★ A colleague's shared view in the recipient's slot never blocks their own project: it
    holds no work of its own, so it is handed over as a shared view, deleted with nothing written
    back, and the recipient's workspace starts. The OWNER's saved copy is untouched."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner11@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient11@example.com")
    recipient_project = await ProjectFactory.create(
        db_session, recipient.id, description="Recipient's own, different project"
    )
    manager = a_manager_whose_ledger_is(db_session)
    viewer = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, viewer)

    build_client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, recipient, recipient_project.id, sandbox_client=build_client, may_write=True
    )

    assert session.project_id == recipient_project.id
    assert handed_over == [(viewer.restored[0], PendingTeardownKind.SHARED, False)]
    assert build_client.torn_down == []
    assert build_client.provisioned, "the recipient's own workspace was started"
    assert await fake_storage.get(snapshot_key(app_id)) == b"BUNDLE"


async def test_opening_their_own_app_puts_a_live_shared_view_away_and_starts(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    handed_over: list[tuple[str, PendingTeardownKind, bool]],
) -> None:
    """★ The same for the start control: relaunching a saved app of the recipient's own
    replaces the shared view in their slot instead of refusing."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner14@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient14@example.com")
    recipient_project = await ProjectFactory.create(
        db_session, recipient.id, description="Recipient's own saved app"
    )
    own_app_id = await resolve_app_for_project(db_session, recipient.id, recipient_project.id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(own_app_id), b"OWN-BUNDLE")
    manager = a_manager_whose_ledger_is(db_session)
    viewer = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, viewer)

    client = FakeSandboxClient()
    started = await manager.relaunch_preview(db_session, recipient, recipient_project.id, client)
    await detached_work_done(manager)

    assert started.app_id == own_app_id
    assert handed_over == [(viewer.restored[0], PendingTeardownKind.SHARED, False)]
    assert client.torn_down == []
    assert len(client.restored) == 1
    assert await fake_storage.get(snapshot_key(app_id)) == b"BUNDLE"


async def test_an_ordinary_build_disowns_a_prior_occupants_shared_stamp(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The MERGE half of the same fix: `hset(mapping=...)` only ADDS fields, so once a shared
    view is revoked and the recipient's OWN build takes the freed slot, the new registry
    record must not still carry the PRIOR occupant's `shared_project_id`/`shared_owner_id` —
    a leftover stamp would misidentify an ordinary build sandbox as somebody else's shared
    view."""
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
    await manager.revoke_shared_preview(
        recipient.id, app_id, _the_view(owner, project), sandbox_client=shared_client
    )

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
    await manager.finish_turn_sandbox(session)  # pardons it

    revoked = await manager.revoke_shared_preview(
        recipient.id, app_id, _the_view(owner, project), sandbox_client=build_client
    )

    assert revoked is False
    assert build_client.torn_down == []


# --- a container whose name says nothing about its app -------------------------------------


async def _renamed(redis: aioredis.Redis, user_id: uuid.UUID) -> str:
    """Give the view in `user_id`'s slot a name unrelated to its app, as any container may carry
    now that every lookup reads the app and the stamp from the record."""
    name = a_name_unrelated_to_its_app()
    await redis.hset(registry_key(user_id), REGISTRY_FIELD_APP_NAME, name)
    return name


async def test_a_second_launch_reattaches_a_view_named_like_a_build_sandbox(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Mutation check: look the view up by the name derived from the app and recipient, and the
    second launch restores over the view the colleague already has open."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner-r1@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient-r1@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, client)
    name = await _renamed(fake_redis, recipient.id)
    client.attach_handle = SandboxHandle(
        fqdn=f"{name}.example", token="tok", app_name=name, preview_url="/a/x", ready=True
    )

    await manager.launch_shared_preview(db_session, recipient, project, client)

    assert len(client.restored) == 1, "the standing view was attached, not restored again"
    assert client.torn_down == []
    reg = await read_registry(fake_redis, recipient.id)
    assert reg is not None and reg[REGISTRY_FIELD_APP_ID] == str(app_id)


async def test_revoking_a_share_tears_down_the_view_named_like_a_build_sandbox(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ A missed revoke leaves a removed colleague a running copy of the owner's app.

    Mutation check: look the view up by the derived name and the revoke finds nothing."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner-r2@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient-r2@example.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    await manager.launch_shared_preview(db_session, recipient, project, client)
    name = await _renamed(fake_redis, recipient.id)

    revoked = await manager.revoke_shared_preview(
        recipient.id, app_id, _the_view(owner, project), sandbox_client=client
    )

    assert revoked is True
    assert client.torn_down == [name]
    assert await read_registry(fake_redis, recipient.id) is None


async def test_a_viewer_releases_their_view_named_like_a_build_sandbox_and_nothing_is_saved(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ Releasing is the viewer's way back to their own workspace. The view is torn down and
    written nowhere: not over the owner's copy, and not into the viewer's own app either. The
    client attaches whatever the record names and answers the write-back, so a write-back that
    ran would show up below.

    Mutation check: decide "is this a view" from the name in the release and it refuses, leaving
    the slot held by a colleague's app; decide it from the name in the reap and the view's tree
    is bundled into the viewer's own app."""
    owner, project, app_id = await _owner_with_saved_app(
        db_session, fake_storage, email="owner-r3@example.com"
    )
    recipient = await UserFactory.create(db_session, email="recipient-r3@example.com")
    own_project = await ProjectFactory.create(db_session, recipient.id, description="Their own")
    own_app_id = await resolve_app_for_project(db_session, recipient.id, own_project.id)
    await db_session.commit()
    manager = SessionManager()
    client = AttachesWhatTheRecordNames()
    await manager.launch_shared_preview(db_session, recipient, project, client)
    name = await _renamed(fake_redis, recipient.id)

    released = await manager.release_project_sandbox(
        db_session, recipient, own_project.id, sandbox_client=client
    )

    assert released is True
    assert client.torn_down == [name]
    assert client.bundled_from == []
    assert await fake_storage.get(snapshot_key(app_id)) == b"BUNDLE"
    assert await fake_storage.head(snapshot_key(own_app_id)) is None


async def test_releasing_a_project_saves_and_ends_its_container_whatever_its_name(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The project's own build sandbox, under a name unrelated to its app: released, its tree
    written to that app's saved copy first.

    Mutation check: recognise the container by the name derived from its app and the release
    finds nothing to give up."""
    user = await UserFactory.create(db_session, email="releaser@example.com")
    project = await ProjectFactory.create(db_session, user.id, description="Their own")
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    name = a_name_unrelated_to_its_app()
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: name,
            REGISTRY_FIELD_APP_ID: str(app_id),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_FQDN: f"{name}.example",
            REGISTRY_FIELD_CREATED_AT: "2026-10-05T00:00:00+00:00",
        },
    )
    client = AttachesWhatTheRecordNames()

    released = await SessionManager().release_project_sandbox(
        db_session, user, project.id, sandbox_client=client
    )

    assert released is True
    assert client.bundled_from == [name]
    assert client.torn_down == [name]
    meta = await fake_storage.head(snapshot_key(app_id))
    assert meta is not None and (meta.metadata or {})["head_sha"] == "c" * 40
