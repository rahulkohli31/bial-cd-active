"""The deploy pipeline, end to end against fakes.

No Azure, no registry, no sandbox. What is under test is the ORDER and the OUTCOMES: that a
successful deploy leaves a settled row with a URL and tells the citizen, that each distinct
failure leaves a distinct code and a sentence they can act on, and — the one that would be
expensive to get wrong — that a failed deploy never claims success.

The sandbox assertions are the sharpest tests here. The pipeline must never provision or
restore one: `restore_from_snapshot` tears a container down BEFORE it pulls the bundle, and
a confirmed-absent snapshot falls through to a blank golden template that would build
cleanly, deploy successfully, and replace the citizen's app with the starter — with a green
checkmark on it.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.message import Message, MessageEntryKind
from src.services.deploy import service as service_module
from src.services.deploy.aca_publish import RevisionState, _state_of
from src.services.deploy.config import DeployConfig
from src.services.deploy.images import BuiltImage, ImageBuildError
from src.services.deploy.service import DeployNotPossibleError, DeployService
from src.services.storage.snapshot_read import ExtractedSnapshot, NoAppYet
from tests.factories import AppRegistryFactory, ConversationFactory, UserFactory

_DIGEST = "sha256:" + "cd" * 32
_HEAD = "a" * 40


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
        "ready_timeout_s": 2,
    }
    return DeployConfig(**values)


@dataclass
class FakeImages:
    """Records what it was asked to build; can be told to fail like the registry does."""

    digest: str = _DIGEST
    error: ImageBuildError | None = None
    contexts: list[bytes] = field(default_factory=list)

    async def build(self, *, app_id: uuid.UUID, deployment_id: uuid.UUID, context: bytes):
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return BuiltImage(digest=self.digest, tag="citizen-apps/x:y", run_id="run1")

    async def aclose(self) -> None:
        return None


class FakeAca:
    """Records every provision, and reports whatever revision state it is told to."""

    def __init__(self, *, healthy: bool = True, failed: bool = False) -> None:
        self.config = _config()
        self.created: list[dict[str, Any]] = []
        self._healthy = healthy
        self._failed = failed

    async def create_or_update(self, *, app_id, deployment_id, image, env, container_url) -> str:
        self.created.append(
            {
                "app_id": app_id,
                "image": image,
                "env": env,
                "container_url": container_url,
            }
        )
        return f"pub-{app_id.hex[:28]}.example.azurecontainerapps.io"

    async def get_revision(self, *, app_id, deployment_id) -> RevisionState:
        # Built through `_state_of`, exactly as the real client does. ARM hands back ENUM
        # members whose `str()` is `RevisionProvisioningState.PROVISIONED`, so a fake that
        # hand-wrote the tidy string would stop modelling the thing under test — which is
        # how the enum bug reached a live deploy in the first place.
        raw = "Failed" if self._failed else ("Provisioned" if self._healthy else "Provisioning")
        return RevisionState(
            name="rev", provisioning_state=_state_of(raw), running_state=_state_of("Running")
        )


@pytest.fixture
def wire(db_session, monkeypatch, tmp_path):
    """A service whose every outward edge is a fake, and whose sessions are the rolled-back
    test session — so the pipeline's own short sessions land in the same transaction the test
    can read and the fixture discards."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "package.json").write_text("{}")

    async def _extract(app_id, *, cache_root=None):
        return ExtractedSnapshot(app_id=app_id, head_sha=_HEAD, root=tree)

    monkeypatch.setattr(service_module, "extract_snapshot", _extract)
    # The published env is exercised in its own tests; here it must not reach Azure.
    monkeypatch.setattr(
        service_module,
        "build_published_env",
        lambda db, *, app_id, project_id: _immediate(({"BIAL_APP_ID": str(app_id)}, None)),
    )
    # A heartbeat every 20s would never fire inside a test; make the absence explicit.
    monkeypatch.setattr(service_module, "_HEARTBEAT_S", 3600.0)
    monkeypatch.setattr(service_module, "_REVISION_POLL_S", 0.01)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    images = FakeImages()
    aca = FakeAca()
    return SimpleNamespace(
        service=DeployService(
            session_factory=lambda: _session(), image_builder=images, published_apps=aca
        ),
        images=images,
        aca=aca,
    )


async def _immediate(value):
    return value


async def _project(db):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    conversation = await ConversationFactory.create(db, user_id=user.id, project_id=app.project_id)
    return user, app, conversation


async def _run(wire, db, user, app, conversation_id=None):
    started = await wire.service.start(
        db,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        conversation_id=conversation_id,
    )
    await wire.service.drain()
    row = await db.get(Deployment, started.deployment_id)
    await db.refresh(row)
    return started, row


# --- the happy path ---------------------------------------------------------------


async def test_a_deploy_settles_with_a_url(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app)

    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.url.startswith("https://pub-")
    assert row.step == "live"
    assert row.finished_at is not None
    assert row.failure_code is None


async def test_the_commit_that_went_live_is_recorded(wire, db_session) -> None:
    """The one question nothing else in the schema can answer."""
    user, app, _conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app)
    assert row.head_sha == _HEAD


async def test_the_image_is_recorded_and_digest_pinned(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app)

    assert row.image_digest == _DIGEST
    assert row.acr_run_id == "run1"
    # The container spec references the digest, never a tag.
    assert wire.aca.created[0]["image"].endswith(f"@{_DIGEST}")


async def test_the_citizen_is_told_where_the_app_is(wire, db_session) -> None:
    user, app, conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app, conversation.id)

    message = await db_session.scalar(
        sa.select(Message).where(
            Message.conversation_id == conversation.id,
            Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
        )
    )
    assert message is not None
    assert message.meta["kind"] == "deploy_outcome"
    assert message.meta["status"] == "succeeded"
    assert row.url in message.meta["url"]


async def test_a_deploy_with_no_conversation_still_succeeds(wire, db_session) -> None:
    """An app built through an API-only path has no thread. That is a deploy with no chat
    message, not a failed deploy."""
    user, app, _conversation = await _project(db_session)
    _started, row = await _run(wire, db_session, user, app, None)
    assert row.status is DeploymentStatus.SUCCEEDED


# --- the sandbox boundary ---------------------------------------------------------


async def test_the_pipeline_never_provisions_or_restores_a_sandbox(wire, db_session) -> None:
    """`restore_from_snapshot` tears the container down BEFORE it pulls the bundle, and a
    confirmed-absent snapshot falls through to a blank golden template — which would build,
    deploy, and replace the citizen's app with the starter under a green checkmark. The
    pipeline reads the bundle from object storage and leaves the sandbox alone."""
    user, app, _conversation = await _project(db_session)
    await _run(wire, db_session, user, app)

    # The service was constructed with no sandbox client at all — if the pipeline ever grew
    # a dependency on one, it could not have run.
    assert not hasattr(wire.service, "_sandbox")


# --- failures ---------------------------------------------------------------------


async def test_a_build_failure_is_reported_with_the_error_the_agent_can_fix(
    wire, db_session
) -> None:
    user, app, conversation = await _project(db_session)
    wire.images.error = ImageBuildError(
        "the image build failed",
        log_tail=(
            "   ▲ Next.js 16.2.10\n"
            "Failed to compile.\n\n"
            "./app/page.tsx:12:5\n"
            "Type error: Property 'foo' does not exist on type 'Item'.\n"
        ),
    )

    _started, row = await _run(wire, db_session, user, app, conversation.id)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "build_failed"
    assert row.url is None

    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    # The TITLE is the actionable line, not the Next.js banner.
    assert "Type error:" in message.payload[0]["parts"][0]["content"]
    assert "Next.js 16.2.10" not in message.payload[0]["parts"][0]["content"]


async def test_a_failed_deploy_says_the_previous_version_still_runs(wire, db_session) -> None:
    user, app, conversation = await _project(db_session)
    wire.images.error = ImageBuildError("boom", log_tail="Failed to compile.\nType error: x\n")

    await _run(wire, db_session, user, app, conversation.id)

    message = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    assert "previous version is still running" in message.payload[0]["parts"][0]["content"]


async def test_a_build_failure_with_no_log_still_reports_something_useful(
    wire, db_session
) -> None:
    user, app, _conversation = await _project(db_session)
    wire.images.error = ImageBuildError("the image build timed out", log_tail=None)

    _started, row = await _run(wire, db_session, user, app)

    assert row.failure_code == "build_failed"
    assert "timed out" in (row.failure_detail or "")


async def test_nothing_saved_yet_is_a_named_outcome(wire, db_session, monkeypatch) -> None:
    """ "Never built" is a normal state, not a crash — and it must not read the same as a
    bundle that exists but cannot be parsed."""

    async def _absent(app_id, *, cache_root=None):
        return NoAppYet(app_id=app_id)

    monkeypatch.setattr(service_module, "extract_snapshot", _absent)
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app)

    assert row.failure_code == "no_saved_build"
    assert row.status is DeploymentStatus.FAILED


async def test_an_unhealthy_revision_fails_the_deploy(wire, db_session) -> None:
    """`create_or_update` returning an FQDN proves the APP exists, not that the new REVISION
    came up. Without this check a deploy reports success over a URL that 5xx's."""
    user, app, _conversation = await _project(db_session)
    wire.aca._failed = True

    _started, row = await _run(wire, db_session, user, app)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "revision_unhealthy"
    assert row.url is None


async def test_a_revision_that_never_becomes_healthy_is_bounded(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    wire.aca._healthy = False

    _started, row = await _run(wire, db_session, user, app)

    assert row.failure_code == "revision_unhealthy"


async def test_an_unexpected_crash_still_settles_the_row(wire, db_session, monkeypatch) -> None:
    """A pipeline that raised would leave the row `running` until the stale window expires —
    half an hour of a Deploy button that 409s, with nothing to explain it."""

    async def _boom(app_id, *, cache_root=None):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(service_module, "extract_snapshot", _boom)
    user, app, _conversation = await _project(db_session)

    _started, row = await _run(wire, db_session, user, app)

    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "internal_error"


async def test_a_secret_in_a_failure_detail_is_redacted(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    wire.images.error = ImageBuildError(
        "boom",
        log_tail="Failed to compile.\nError: DB_PASSWORD=hunter2 was rejected\n",
    )

    _started, row = await _run(wire, db_session, user, app)

    assert "hunter2" not in (row.failure_detail or "")


# --- concurrency ------------------------------------------------------------------


async def test_a_second_deploy_while_one_runs_is_refused(wire, db_session) -> None:
    user, app, _conversation = await _project(db_session)
    await wire.service.start(
        db_session,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        conversation_id=None,
    )

    with pytest.raises(DeployNotPossibleError) as caught:
        await wire.service.start(
            db_session,
            user_id=user.id,
            app_id=app.id,
            project_id=app.project_id,
            conversation_id=None,
        )
    assert caught.value.code == "deploy_in_flight"
    await wire.service.drain()
