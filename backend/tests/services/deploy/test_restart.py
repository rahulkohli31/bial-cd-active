"""The restart pipeline, against fakes: what it runs, and what it refuses to do when the
recycled revision does not come up.

No Azure, no registry, no snapshot. The sharpest assertion here is the one the sandbox's
destructive-restore incident left behind: a readiness timeout means "not ready yet", never
"dead, put the saved version back". A restart extracts no snapshot, builds no image and
removes no container, so a slow-but-healthy application cannot be rolled back to its last
save by an owner pressing the button again.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy import service as service_module
from src.services.deploy.aca_publish import RevisionState, _state_of
from src.services.deploy.config import DeployConfig
from src.services.deploy.images import BuiltImage
from src.services.deploy.names import published_app_name
from src.services.deploy.service import (
    FAIL_RESTART,
    FAIL_RESTART_NOT_READY,
    STEP_RESTARTING,
    DeployNotPossibleError,
    DeployService,
    LiveRevision,
)
from src.services.sandbox.aca import AcaError
from src.services.storage import snapshot_key
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage, a_git_bundle

_LIVE_DIGEST = "sha256:" + "cd" * 32
_LIVE_HEAD = "a" * 40
# The commit the citizen has saved SINCE the live one — the version a restart must never run.
_SAVED_SINCE = "b" * 40


def _config() -> DeployConfig:
    values: dict[str, Any] = {
        "acr_server": "bialgenaicr.azurecr.io",
        "acr_name": "bialgenaicr",
        "acr_resource_group": "rg-acr",
        "acr_subscription_id": "sub-acr",
        "acr_username": "bialgenaicr",
        "acr_password": SecretStr("pw"),
        "subscription_id": "sub",
        "resource_group": "rg",
        "region": "centralindia",
        "managed_environment_name": "env",
        # One second, the floor `PositiveInt` allows: the never-ready case has to wait it out.
        "ready_timeout_s": 1,
    }
    return DeployConfig(**values)


@dataclass
class FakeImages:
    """Records every build. A restart must leave this empty — there is nothing to build."""

    contexts: list[bytes] = field(default_factory=list)

    async def build(self, *, app_id: uuid.UUID, deployment_id: uuid.UUID, context: bytes):
        self.contexts.append(context)
        return BuiltImage(digest=_LIVE_DIGEST, tag="citizen-apps/x:y", run_id="run1")

    async def aclose(self) -> None:
        return None


class FakeAca:
    """Records every provision and reports whatever revision state it is told to.

    `healthy=False, failed=False` is the case the whole file turns on: ARM keeps answering
    "still provisioning", which is an UNKNOWN and must never be read as a death certificate."""

    def __init__(self, *, healthy: bool = True, failed: bool = False) -> None:
        self.config = _config()
        self.created: list[dict[str, Any]] = []
        self.raise_on_create: Exception | None = None
        self.healthy = healthy
        self.failed = failed

    async def create_or_update(self, *, app_id, deployment_id, image, env, container_url) -> str:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        self.created.append({"app_id": app_id, "image": image, "env": env})
        return f"pub-{app_id.hex[:28]}.example.azurecontainerapps.io"

    async def get_revision(self, *, app_id, deployment_id) -> RevisionState:
        raw = "Failed" if self.failed else ("Provisioned" if self.healthy else "Provisioning")
        return RevisionState(
            name="rev", provisioning_state=_state_of(raw), running_state=_state_of("Running")
        )


@pytest.fixture
def wire(db_session, monkeypatch, tmp_path):
    """A service whose every outward edge is a fake, plus a spy on the snapshot read.

    `extractions` is the load-bearing one: it is how a test proves the saved bundle was
    never even looked at, which is what separates recycling a revision from republishing."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "package.json").write_text("{}")
    extractions: list[uuid.UUID] = []

    async def _extract(app_id, *, cache_root=None):
        extractions.append(app_id)
        from src.services.storage.snapshot_read import ExtractedSnapshot

        return ExtractedSnapshot(app_id=app_id, head_sha=_SAVED_SINCE, root=tree)

    monkeypatch.setattr(service_module, "extract_snapshot", _extract)
    monkeypatch.setattr(
        service_module,
        "build_published_env",
        lambda db, *, app_id, project_id, user_id: _immediate(
            ({"BIAL_APP_ID": str(app_id)}, None)
        ),
    )
    # A 20-second heartbeat would never fire inside a test; make the absence explicit.
    monkeypatch.setattr(service_module, "_HEARTBEAT_S", 3600.0)
    monkeypatch.setattr(service_module, "_REVISION_POLL_S", 0.01)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    store = FakeStorage()
    monkeypatch.setattr(service_module, "get_storage", lambda: store)

    images = FakeImages()
    aca = FakeAca()
    return SimpleNamespace(
        service=DeployService(
            session_factory=lambda: _session(), image_builder=images, published_apps=aca
        ),
        images=images,
        aca=aca,
        store=store,
        extractions=extractions,
    )


async def _immediate(value):
    return value


async def _owned_app(db):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    return user, app


async def _restart(wire, db, user, app) -> Deployment:
    """Run one restart to completion and hand back its settled row."""
    started = await wire.service.restart(
        db,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        live=LiveRevision(image_digest=_LIVE_DIGEST, head_sha=_LIVE_HEAD),
    )
    await wire.service.drain()
    row = await db.get(Deployment, started.deployment_id)
    assert row is not None
    await db.refresh(row)
    return row


async def test_a_restart_runs_the_live_image_and_builds_nothing(wire, db_session) -> None:
    """The product rule, at the site that decides it: the image comes from the digest the
    live deployment recorded, so the version that comes back is the version that went down.

    Mutation receipt: point `_restart` at `_deploy` (drop `_run`'s restart arm) and this goes
    red three ways — a build is recorded, the snapshot is extracted, and the row settles at
    the commit saved since."""
    user, app = await _owned_app(db_session)
    wire.store.objects[snapshot_key(app.id)] = a_git_bundle(_SAVED_SINCE)
    wire.store.meta[snapshot_key(app.id)] = {"head_sha": _SAVED_SINCE}

    row = await _restart(wire, db_session, user, app)

    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.head_sha == _LIVE_HEAD
    assert row.image_digest == _LIVE_DIGEST
    # Nothing was built and nothing was read out of the citizen's saved bundle.
    assert wire.images.contexts == []
    assert wire.extractions == []
    # The digest-pinned reference ARM was handed names the live image, not a mutable tag.
    assert wire.aca.created[0]["image"].endswith(f"@{_LIVE_DIGEST}")
    # Same address as before: the container name is a pure function of the app id.
    assert row.url is not None
    assert published_app_name(app.id) in row.url


async def test_a_readiness_timeout_is_a_failed_restart_that_restores_nothing(
    wire, db_session
) -> None:
    """The scar this endpoint is built around. A revision that has not reported healthy inside
    the budget says nothing about the container that is still serving — so the attempt settles
    as retryable and the saved bundle is neither read nor put back.

    Mutation receipt: collapse the deadline branch into the positively-failed branch (one
    `FAIL_RESTART` for both) and the code and sentence below go red; drop `_run`'s restart arm
    and the untouched-snapshot assertions go red instead."""
    user, app = await _owned_app(db_session)
    wire.aca.healthy = False
    key = snapshot_key(app.id)
    wire.store.objects[key] = a_git_bundle(_SAVED_SINCE)
    wire.store.meta[key] = {"head_sha": _SAVED_SINCE}
    saved_bytes = wire.store.objects[key]

    row = await _restart(wire, db_session, user, app)

    # THE SAVED WORK FIRST, because it is what the incident cost. The citizen's bundle is
    # exactly where it was — same bytes, same key, never read. A rollback would have READ
    # this blob; a restore would have replaced it; a republish would have built from it.
    assert wire.store.objects[key] is saved_bytes
    assert wire.store.meta[key] == {"head_sha": _SAVED_SINCE}
    assert wire.extractions == []
    assert wire.images.contexts == []
    # And the container that was already serving was recycled, never removed.
    assert wire.aca.created[0]["image"].endswith(f"@{_LIVE_DIGEST}")

    assert row.status is DeploymentStatus.FAILED
    # NOT the positively-failed code: ARM never said the revision failed, it just never
    # answered, and "unknown" must reach the citizen as "try again", not "it is broken".
    assert row.failure_code == FAIL_RESTART_NOT_READY
    assert "still running" in (row.failure_detail or "")


async def test_a_revision_the_platform_was_told_failed_is_not_one_that_never_answered(
    wire, db_session
) -> None:
    """Two different sentences for two different facts. Only ARM's own verdict is a verdict;
    everything else is an unknown that retrying settles."""
    user, app = await _owned_app(db_session)
    wire.aca.failed = True

    row = await _restart(wire, db_session, user, app)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == FAIL_RESTART


async def test_a_container_service_that_refuses_leaves_the_app_alone(wire, db_session) -> None:
    """ARM refusing the spec is a failed restart, not a reason to take anything down."""
    user, app = await _owned_app(db_session)
    wire.aca.raise_on_create = AcaError("ARM said no")

    row = await _restart(wire, db_session, user, app)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == FAIL_RESTART
    assert wire.aca.created == []


async def test_the_restarting_phase_is_recorded_before_the_container_is_touched(
    wire, db_session
) -> None:
    """What the client polls while it waits. The phase is written first so a restart never
    shows the generic claimed step while ARM is being called."""
    user, app = await _owned_app(db_session)
    seen: list[str | None] = []

    original = wire.aca.create_or_update

    async def _spy(**kwargs):
        row = await db_session.get(Deployment, kwargs["deployment_id"])
        seen.append(None if row is None else row.step)
        return await original(**kwargs)

    wire.aca.create_or_update = _spy

    await _restart(wire, db_session, user, app)

    assert seen == [STEP_RESTARTING]


async def test_a_second_restart_cannot_claim_the_slot_a_first_one_holds(wire, db_session) -> None:
    """The one-in-flight index is the whole concurrency story — a second press is refused,
    never a second operation against the same container."""
    user, app = await _owned_app(db_session)
    live = LiveRevision(image_digest=_LIVE_DIGEST, head_sha=_LIVE_HEAD)
    wire.aca.healthy = False

    first = await wire.service.restart(
        db_session, user_id=user.id, app_id=app.id, project_id=app.project_id, live=live
    )

    with pytest.raises(DeployNotPossibleError) as refused:
        await wire.service.restart(
            db_session, user_id=user.id, app_id=app.id, project_id=app.project_id, live=live
        )

    assert refused.value.code == "deploy_in_flight"
    await wire.service.drain()
    assert len(wire.aca.created) == 1
    row = await db_session.get(Deployment, first.deployment_id)
    assert row is not None
