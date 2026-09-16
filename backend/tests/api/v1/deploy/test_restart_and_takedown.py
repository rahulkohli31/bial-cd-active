"""The two owner-facing production levers: `POST /v1/projects/{id}/restart` and
`POST /v1/projects/{id}/takedown`.

No Azure — the container seams are Protocols so a fake can stand in, exactly as the admin
kill-switch's own tests do. What is under test is the routes' state machines: who may pull
each lever, which states refuse and with what reason, what is written before Azure is
touched, and the two facts a reader of the UI alone could not check — that a take-down moves
the deployment axis and never the app's status, and that a restart runs the commit already
live rather than the one saved since.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import event as sa_event

from src.api.deps import storage_or_none_dependency
from src.api.v1.deploy.deps import deploy_service_or_none, published_app_remover_or_none
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.conversation import Conversation
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.auth.session_jwt import mint_session_jwt
from src.services.deploy import service as service_module
from src.services.deploy.aca_publish import AcaTransientError, RevisionState, _state_of
from src.services.deploy.config import DeployConfig
from src.services.deploy.images import BuiltImage
from src.services.deploy.names import published_app_name
from src.services.deploy.service import DeployService
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    MessageFactory,
    UserFactory,
)
from tests.fakes import FakeStorage, a_git_bundle

_RESTART = "/v1/projects/{pid}/restart"
_TAKEDOWN = "/v1/projects/{pid}/takedown"
_STATUS = "/v1/projects/{pid}/deployment"
_UNPUBLISH = "/v1/admin/apps/{app_id}/unpublish"

_LIVE_DIGEST = "sha256:" + "cd" * 32
_LIVE_HEAD = "a" * 40
# What the citizen has saved since going live — the version restart must never publish.
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
        "ready_timeout_s": 1,
    }
    return DeployConfig(**values)


class FakeImages:
    """Records every build. A restart must leave this empty — nothing is being built."""

    def __init__(self) -> None:
        self.contexts: list[bytes] = []

    async def build(self, *, app_id: uuid.UUID, deployment_id: uuid.UUID, context: bytes):
        self.contexts.append(context)
        return BuiltImage(digest=_LIVE_DIGEST, tag="citizen-apps/x:y", run_id="run1")

    async def aclose(self) -> None:
        return None


class FakeAca:
    """The provisioning half of the published-app client, recording what it was handed."""

    def __init__(self) -> None:
        self.config = _config()
        self.created: list[dict[str, Any]] = []
        self.healthy = True

    async def create_or_update(self, *, app_id, deployment_id, image, env, container_url) -> str:
        self.created.append({"app_id": app_id, "image": image, "deployment_id": deployment_id})
        return f"pub-{app_id.hex[:28]}.example.azurecontainerapps.io"

    async def get_revision(self, *, app_id, deployment_id) -> RevisionState:
        raw = "Provisioned" if self.healthy else "Provisioning"
        return RevisionState(
            name="rev", provisioning_state=_state_of(raw), running_state=_state_of("Running")
        )


class FakeRemover:
    """Records every `delete_app`; can be told to raise a bounded number of times, the way a
    transient ARM failure that a retry then clears behaves."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[uuid.UUID] = []
        self._fail_times = fail_times

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        self.calls.append(app_id)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise AcaTransientError("simulated ACA delete failure")


class CommitCountingRemover(FakeRemover):
    """Counts COMMITS from inside the ARM call — the only vantage point that can prove the
    accountability row was made DURABLE before Azure was touched.

    Reading the audit table instead would prove nothing: the route is handed the test's own
    session, so an uncommitted row is visible to a reader on it either way. What has to
    survive is the request dying at the gateway mid-delete, and that is a commit, not a row.
    SQLAlchemy's own `after_commit` hook rather than a wrapper around `db.commit`, so a caller
    that reaches past the wrapper cannot fool it."""

    def __init__(self, db) -> None:
        super().__init__()
        self.commits_before_delete = -1
        self._count = 0
        sa_event.listen(db.sync_session, "after_commit", self._on_commit)

    def _on_commit(self, _session: object) -> None:
        self._count += 1

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        await super().delete_app(app_id=app_id)
        self.commits_before_delete = self._count


@pytest.fixture
async def wire(app: FastAPI, db_session, monkeypatch, tmp_path):
    """The real pipeline behind the real routes; fakes only where the platform leaves the
    process. `extractions` records every snapshot read, which is how a restart proves it
    never went near the citizen's saved bundle."""
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
    monkeypatch.setattr(service_module, "_HEARTBEAT_S", 3600.0)
    monkeypatch.setattr(service_module, "_REVISION_POLL_S", 0.01)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    store = FakeStorage()
    monkeypatch.setattr(service_module, "get_storage", lambda: store)

    images = FakeImages()
    aca = FakeAca()
    remover = FakeRemover()
    pipeline = DeployService(
        session_factory=lambda: _session(), image_builder=images, published_apps=aca
    )
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
    app.dependency_overrides[published_app_remover_or_none] = lambda: remover
    yield SimpleNamespace(
        app=app,
        pipeline=pipeline,
        aca=aca,
        images=images,
        remover=remover,
        store=store,
        extractions=extractions,
    )
    await pipeline.drain()


async def _immediate(value):
    return value


async def _live_app(db, *, status: AppStatus = AppStatus.DRAFT, **app_fields: Any):
    """An owner whose app is serving traffic: an app row plus the succeeded deployment that
    published it."""
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id, status=status, **app_fields)
    deployment = await _deployment(db, app_id=app_row.id, user_id=user.id)
    return user, app_row, deployment


async def _deployment(
    db,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    unpublished_at: datetime | None = None,
    image_digest: str | None = _LIVE_DIGEST,
    failure_code: str | None = None,
) -> Deployment:
    row = Deployment(
        app_id=app_id,
        user_id=user_id,
        status=status,
        failure_code=failure_code,
        step="live" if status is DeploymentStatus.SUCCEEDED else "building",
        head_sha=_LIVE_HEAD,
        image_digest=image_digest,
        container_app_name=published_app_name(app_id),
        revision_name=f"{published_app_name(app_id)}--d0000000000",
        url=settings.app_url(published_app_name(app_id)),
        unpublished_at=unpublished_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def _audits(db, action: str) -> list[AuditLog]:
    rows = await db.execute(sa.select(AuditLog).where(AuditLog.action == action))
    return list(rows.scalars().all())


def _detail(audit: AuditLog) -> dict[str, Any]:
    """`AuditLog.detail` is nullable; every row these two levers write carries one."""
    assert audit.detail is not None
    return audit.detail


async def _admin_headers(db) -> dict[str, str]:
    # The .env.test allowlist maps admin@bial.com to super-admin.
    admin = await UserFactory.create(db, email="admin@bial.com")
    from src.config import settings

    jwt = mint_session_jwt(admin.id, admin.token_version, settings.auth.access_ttl_seconds)
    return {"Cookie": f"session={jwt}"}


# --- restart ---------------------------------------------------------------------------


async def test_a_restart_recycles_the_live_revision_and_reports_it_on_the_same_url(
    wire, client, db_session
) -> None:
    """The happy path, end to end: the poll the client already uses reports the restart
    running, then live again at the address it was always on."""
    user, app_row, live = await _live_app(db_session)

    started = await client.post(
        _RESTART.format(pid=app_row.project_id), headers=auth_headers(user)
    )

    assert started.status_code == 202
    body = started.json()
    assert body["appId"] == str(app_row.id)
    assert body["status"] == "running"

    await wire.pipeline.drain()
    polled = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))
    assert polled.status_code == 200
    reported = polled.json()
    assert reported["deploymentId"] == body["deploymentId"]
    assert reported["status"] == "succeeded"
    assert reported["url"] == live.url
    assert reported["unpublishedAt"] is None


async def test_the_deployment_read_reports_a_restart_in_progress(wire, client, db_session) -> None:
    """One source for "what is it doing now". The client polls the deployment read it already
    polls for a publish, and a recycled revision is told apart from a fresh one by its phase —
    both are a `running` row, so the status alone could not say.

    Mutation receipt: move the second advance in `_restart` to `starting` — the publish
    pipeline's own readiness phase — and the poll can no longer tell the two apart."""
    user, app_row, live = await _live_app(db_session)
    wire.aca.healthy = False

    started = await client.post(
        _RESTART.format(pid=app_row.project_id), headers=auth_headers(user)
    )
    assert started.status_code == 202

    polled = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert polled.status_code == 200
    reported = polled.json()
    assert reported["deploymentId"] == started.json()["deploymentId"]
    assert reported["status"] == "running"
    assert reported["step"] == "restarting"
    # The commit it is putting back is the one that was already live, on the wire as well as
    # in the row.
    assert reported["headSha"] == live.head_sha


async def test_a_restart_never_runs_a_commit_other_than_the_one_already_live(
    wire, client, db_session
) -> None:
    """The publish-gate bypass this rule exists to prevent. The citizen has saved twice since
    going live; restart still runs the version that is serving, so nothing reaches production
    without passing the gate.

    Mutation receipt: drop `_run`'s restart arm so a restart falls through to the deploy
    pipeline and every assertion below flips — a build is recorded, the snapshot is read, and
    the row settles at the commit saved since."""
    user, app_row, _live = await _live_app(db_session)
    key = snapshot_key(app_row.id)
    wire.store.objects[key] = a_git_bundle(_SAVED_SINCE)
    wire.store.meta[key] = {"head_sha": _SAVED_SINCE}

    started = await client.post(
        _RESTART.format(pid=app_row.project_id), headers=auth_headers(user)
    )
    assert started.status_code == 202
    await wire.pipeline.drain()

    row = await db_session.get(Deployment, uuid.UUID(started.json()["deploymentId"]))
    assert row is not None
    await db_session.refresh(row)
    assert row.head_sha == _LIVE_HEAD
    assert row.image_digest == _LIVE_DIGEST
    assert wire.images.contexts == []
    assert wire.extractions == []
    assert wire.aca.created[0]["image"].endswith(f"@{_LIVE_DIGEST}")


async def test_a_restart_is_refused_with_a_reason_on_an_app_that_is_not_live(
    wire, client, db_session
) -> None:
    """A deploy that never came up leaves a failed row and no container to recycle. A stated
    refusal, never a 500 from a pipeline asked to restart nothing."""
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deployment(
        db_session, app_id=app_row.id, user_id=user.id, status=DeploymentStatus.FAILED
    )

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "not_live"
    assert wire.aca.created == []


async def test_a_restart_is_refused_on_an_app_that_has_never_been_deployed(
    wire, client, db_session
) -> None:
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "never_deployed"
    assert wire.aca.created == []


async def test_a_restart_is_refused_on_an_app_an_administrator_disabled(
    wire, client, db_session
) -> None:
    """The interlock with the other lever over the same container. Disable severs the app's
    database credential, so an owner quietly restarting past it would be a way around the
    administrator's decision.

    Mutation receipt: remove the `AppStatus.DISABLED` check and this answers 202."""
    user, app_row, _live = await _live_app(db_session, status=AppStatus.DISABLED)

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "app_disabled"
    assert wire.aca.created == []
    assert await _audits(db_session, "restart") == []


async def test_a_restart_is_refused_on_an_app_its_owner_took_down(
    wire, client, db_session
) -> None:
    """Restart implies something running. A taken-down app is put back by publishing it
    again, which goes through the gate — so the refusal names that, rather than silently
    doing it.

    Mutation receipt: drop the `unpublished_at` clause from the liveness check and this
    answers 202 over a container that is not there."""
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        unpublished_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "taken_offline"
    assert "Publish again" in resp.json()["error"]["message"]
    assert wire.aca.created == []


async def test_a_second_restart_while_one_is_in_flight_starts_no_second_operation(
    wire, client, db_session
) -> None:
    """One in-flight deployment per app is the guard that already exists, and both new levers
    claim through it rather than inventing a second concurrency story."""
    user, app_row, _live = await _live_app(db_session)
    wire.aca.healthy = False

    first = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))
    second = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "deploy_in_flight"
    await wire.pipeline.drain()
    assert len(wire.aca.created) == 1
    # And the refused press left no container removal behind it either.
    assert wire.remover.calls == []


async def test_a_restart_is_audited_naming_the_actor_and_the_version(
    wire, client, db_session
) -> None:
    user, app_row, live = await _live_app(db_session)

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 202
    await wire.pipeline.drain()
    audit = (await _audits(db_session, "restart"))[0]
    assert audit.actor_id == user.id
    assert audit.resource_type == "app"
    assert audit.resource_id == str(app_row.id)
    assert _detail(audit)["projectId"] == str(app_row.project_id)
    assert _detail(audit)["headSha"] == live.head_sha
    assert _detail(audit)["containerAppName"] == published_app_name(app_row.id)


async def test_another_persons_app_cannot_be_restarted(wire, client, db_session) -> None:
    """A colleague with the project id gets the same non-leaking 404 a missing project gives.
    The owner's own press on the identical URL is what proves the refusal is the scoping
    predicate rather than a route that does not exist."""
    owner, app_row, _live = await _live_app(db_session)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")

    refused = await client.post(
        _RESTART.format(pid=app_row.project_id), headers=auth_headers(stranger)
    )

    assert refused.status_code == 404
    assert wire.aca.created == []
    assert await _audits(db_session, "restart") == []

    allowed = await client.post(
        _RESTART.format(pid=app_row.project_id), headers=auth_headers(owner)
    )
    assert allowed.status_code == 202


async def test_a_restart_without_a_csrf_token_is_refused(wire, client, db_session) -> None:
    user, app_row, _live = await _live_app(db_session)

    resp = await client.post(
        _RESTART.format(pid=app_row.project_id), headers=auth_headers(user, with_csrf=False)
    )

    assert resp.status_code == 403
    assert wire.aca.created == []


async def test_a_restart_needs_publishing_to_be_configured(app, client, db_session) -> None:
    """The fixture-free baseline: with `DEPLOY__*` unset there is no publish plane at all, so
    the lever says so terminally rather than inviting a retry that can never help."""
    user, app_row, _live = await _live_app(db_session)
    app.dependency_overrides[deploy_service_or_none] = lambda: None

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "publishing_unavailable"


# --- take-down -------------------------------------------------------------------------


async def test_a_takedown_removes_the_container_and_keeps_every_artefact(
    wire, client, db_session
) -> None:
    """Take-down is not delete. The container goes; the app, its chats and its data stay
    exactly where they were, which is what makes publishing it again a one-click return."""
    user, app_row, live = await _live_app(db_session)
    conversation = await ConversationFactory.create(
        db_session, user_id=user.id, project_id=app_row.project_id
    )
    await MessageFactory.create(db_session, user_id=user.id, conversation_id=conversation.id)

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_row.id)
    assert body["deploymentId"] == str(live.id)
    assert body["unpublishedAt"]
    assert wire.remover.calls == [app_row.id]

    row = await db_session.get(Deployment, live.id)
    assert row is not None
    assert row.unpublished_at is not None
    # Everything the app holds survives.
    assert await db_session.get(AppRegistry, app_row.id) is not None
    assert await db_session.get(Conversation, conversation.id) is not None
    assert (
        await db_session.scalar(
            sa.select(sa.func.count()).select_from(Deployment).where(Deployment.id == live.id)
        )
    ) == 1


async def test_a_takedown_is_audited_and_stays_apart_from_the_administrators_lever(
    wire, client, db_session
) -> None:
    user, app_row, live = await _live_app(db_session)

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    audit = (await _audits(db_session, "takedown"))[0]
    assert audit.actor_id == user.id
    assert audit.resource_type == "app"
    assert audit.resource_id == str(app_row.id)
    assert _detail(audit)["deploymentId"] == str(live.id)
    assert _detail(audit)["containerAppName"] == published_app_name(app_row.id)
    # The administrator's kill-switch writes `unpublish`; an owner's lever is never mistaken
    # for it in the trail, which is what keeps two different severities readable apart.
    assert await _audits(db_session, "unpublish") == []


@pytest.mark.parametrize(
    "status", [AppStatus.DRAFT, AppStatus.APPROVED, AppStatus.PENDING, AppStatus.REJECTED]
)
async def test_a_takedown_never_writes_the_apps_status(
    wire, client, db_session, status: AppStatus
) -> None:
    """Whether an application is in production is a SEPARATE AXIS from Draft / In review /
    Approved. Publishing unattended never writes the status at all, so an ordinary live app
    still reads Draft — and nothing here "returns to Approved".

    Mutation receipt: have the route write any `AppStatus` and this goes red on every row."""
    user, app_row, _live = await _live_app(db_session, status=status)

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is status


async def test_a_takedown_leaves_a_waiting_submission_alone_and_says_so(
    wire, client, db_session
) -> None:
    """A newer version can be sitting in the administrator's queue while the old one serves.
    Taking the old one down withdraws nothing — and the answer says that, rather than leaving
    the owner to discover it."""
    submitted_at = datetime(2026, 9, 10, tzinfo=UTC)
    user, app_row, _live = await _live_app(
        db_session,
        status=AppStatus.PENDING,
        source_commit_sha=_SAVED_SINCE,
        submitted_at=submitted_at,
    )

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert "review" in resp.json()["message"]
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.PENDING
    assert fresh.source_commit_sha == _SAVED_SINCE
    assert fresh.submitted_at == submitted_at


async def test_a_takedown_during_an_in_flight_deploy_is_refused_with_a_reason(
    wire, client, db_session
) -> None:
    """Letting it through would race the running pipeline's own provision, and the container
    the owner just removed would reappear moments later.

    Mutation receipt: remove the in-flight check and the sweep runs, so the call below stops
    being empty and the stamp lands on a row a live deploy is about to replace."""
    user, app_row, live = await _live_app(db_session)
    await _deployment(
        db_session, app_id=app_row.id, user_id=user.id, status=DeploymentStatus.RUNNING
    )

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "deploy_in_flight"
    assert wire.remover.calls == []
    row = await db_session.get(Deployment, live.id)
    assert row is not None
    assert row.unpublished_at is None


async def test_a_takedown_that_was_never_observed_leaves_the_app_published(
    wire, client, db_session, app
) -> None:
    """A survivor means "not observed", never "failed" — the sweep collapses a terminal ARM
    refusal and a delete still running past the ceiling into the same entry. Leaving the stamp
    unwritten is the conservative half of that: marking an app down that is still serving is
    the lie that costs."""
    user, app_row, live = await _live_app(db_session)
    remover = FakeRemover(fail_times=1)
    app.dependency_overrides[published_app_remover_or_none] = lambda: remover

    refused = await client.post(
        _TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user)
    )

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "teardown_unconfirmed"
    row = await db_session.get(Deployment, live.id)
    assert row is not None
    assert row.unpublished_at is None
    # The attempt is on record even though the request failed, because it was committed
    # before Azure was called.
    assert len(await _audits(db_session, "takedown")) == 1
    unconfirmed = (await _audits(db_session, "takedown:unconfirmed"))[0]
    assert _detail(unconfirmed)["reason"] == "teardown_unconfirmed"

    retried = await client.post(
        _TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user)
    )
    assert retried.status_code == 200
    assert len(remover.calls) == 2


async def test_a_repeated_takedown_does_not_call_azure_again(wire, client, db_session) -> None:
    user, app_row, _live = await _live_app(db_session)

    first = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))
    second = await client.post(
        _TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user)
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["unpublishedAt"] == second.json()["unpublishedAt"]
    assert len(wire.remover.calls) == 1


async def test_another_persons_app_cannot_be_taken_down(wire, client, db_session) -> None:
    """Same predicate, the destructive lever. The owner's own press on the identical URL is
    what proves the 404 is scoping rather than a missing route."""
    owner, app_row, live = await _live_app(db_session)
    stranger = await UserFactory.create(db_session, email="intruder@rvaiglobal.com")

    refused = await client.post(
        _TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(stranger)
    )

    assert refused.status_code == 404
    assert wire.remover.calls == []
    row = await db_session.get(Deployment, live.id)
    assert row is not None
    assert row.unpublished_at is None
    assert await _audits(db_session, "takedown") == []

    allowed = await client.post(
        _TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(owner)
    )
    assert allowed.status_code == 200
    assert wire.remover.calls == [app_row.id]


async def test_a_takedown_without_a_csrf_token_is_refused(wire, client, db_session) -> None:
    user, app_row, _live = await _live_app(db_session)

    resp = await client.post(
        _TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user, with_csrf=False)
    )

    assert resp.status_code == 403
    assert wire.remover.calls == []


async def test_a_takedown_needs_publishing_to_be_configured(app, client, db_session) -> None:
    user, app_row, _live = await _live_app(db_session)
    app.dependency_overrides[published_app_remover_or_none] = lambda: None

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "publishing_unavailable"


async def test_a_takedown_on_an_app_never_deployed_is_refused(wire, client, db_session) -> None:
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "never_deployed"
    assert wire.remover.calls == []


async def test_the_takedown_is_recorded_before_azure_is_touched(app, client, db_session) -> None:
    """An ARM delete is bounded at five minutes behind an edge gateway that gives up at
    twenty seconds, so the failure this ordering exists for is "the request never returns" —
    and a request that never returns cannot audit on its way out.

    Mutation receipt: move the `append_audit` + `db.commit()` pair below the sweep and the
    count read from inside the delete drops to zero."""
    user, app_row, _live = await _live_app(db_session)
    remover = CommitCountingRemover(db_session)
    app.dependency_overrides[published_app_remover_or_none] = lambda: remover

    resp = await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert remover.commits_before_delete >= 1


async def test_the_administrators_kill_switch_still_works_on_an_app_its_owner_took_down(
    wire, client, db_session
) -> None:
    """The owner's lever is not a lockout. An administrator reaching for the kill-switch after
    a take-down still gets an answer, and the trail keeps the two apart."""
    user, app_row, live = await _live_app(db_session)
    await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    admin = await client.post(
        _UNPUBLISH.format(app_id=app_row.id), headers=await _admin_headers(db_session)
    )

    assert admin.status_code == 200
    assert admin.json()["deploymentId"] == str(live.id)
    assert len(await _audits(db_session, "takedown")) == 1


async def test_the_reconciler_does_not_resurrect_an_app_its_owner_took_down(
    wire, client, db_session
) -> None:
    """Nothing comes back on its own. The reconciler resolves ABANDONED in-flight rows against
    ARM; a taken-down app has no in-flight row, so its stamp is never revisited."""
    from src.services.deploy.reconcile import reconcile_stalled_deployments

    user, app_row, live = await _live_app(db_session)
    await client.post(_TAKEDOWN.format(pid=app_row.project_id), headers=auth_headers(user))

    class StillServing:
        async def get_app_fqdn(self, *, app_id: uuid.UUID) -> str | None:
            return f"{published_app_name(app_id)}.example.azurecontainerapps.io"

        async def get_app_image(self, *, app_id: uuid.UUID) -> str | None:
            return f"registry/repo@{_LIVE_DIGEST}"

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    resolved = await reconcile_stalled_deployments(lambda: _session(), StillServing())

    assert resolved == 0
    row = await db_session.get(Deployment, live.id, populate_existing=True)
    assert row is not None
    assert row.unpublished_at is not None


# --- a restart that failed must not strand the app it left running -----------------------


async def test_a_second_restart_is_accepted_after_the_first_one_failed(
    wire, client, db_session
) -> None:
    """★ THE TRAP THIS EXISTS FOR.

    `deployments` is append-only and a restart claims a row of its own, so a restart that fails
    leaves a `failed` row NEWER than the attempt that published the container still serving.
    Resolving "what is live" from the newest row refused every retry with "this app isn't
    running in production right now" — about a container the projects list was simultaneously
    showing as live, because `liveness.live_app_ids` reads the last attempt that PUBLISHED.

    The owner's only remaining lever was a full publish, which for anything but the self-publish
    lineage means another review round. The commonest way in is benign: the readiness budget is
    180 seconds and a slow-but-healthy restart exceeds it, settling `restart_not_ready` while
    the previous revision keeps serving — and that timeout's own citizen message invites the
    retry this refused.
    """
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    published = await _deployment(db_session, app_id=app_row.id, user_id=user.id)
    await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        status=DeploymentStatus.FAILED,
        failure_code="restart_not_ready",
    )

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 202, resp.text
    await wire.pipeline.drain()
    # AND IT RUNS THE PUBLISHED VERSION, not whatever the failed attempt was carrying: the
    # digest comes off the row that actually put a container there.
    assert wire.aca.created[0]["image"].endswith(f"@{published.image_digest}")


async def test_a_failed_publish_still_refuses_a_restart(wire, client, db_session) -> None:
    """The paired negative, and the reason the fix keys on the failure CODE rather than on
    "there exists an older success". A publish that never came up is a production fact: nothing
    newer is serving, and offering to recycle it would be offering to recycle nothing."""
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        status=DeploymentStatus.FAILED,
        failure_code="build_failed",
        image_digest=None,
    )

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "not_live"
    assert wire.aca.created == []


async def test_a_takedown_after_a_failed_restart_still_refuses_a_restart(
    wire, client, db_session
) -> None:
    """OFFLINE OUTRANKS THE ATTEMPT. `unpublish` stamps whichever row was newest when it ran —
    here, the failed restart — so the takedown axis is read off the NEWEST row while the live
    version is read off the last published one. Reading both off the same row gets one of these
    two cases wrong whichever row is chosen."""
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deployment(db_session, app_id=app_row.id, user_id=user.id)
    await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        status=DeploymentStatus.FAILED,
        failure_code="restart_failed",
        unpublished_at=datetime.now(UTC),
    )

    resp = await client.post(_RESTART.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "taken_offline"
    assert wire.aca.created == []


async def test_the_deployment_read_calls_a_failed_restart_live_not_did_not_start(
    client, db_session
) -> None:
    """★ THE OTHER HALF OF THE SAME DISAGREEMENT, at the surface rather than the route.

    `compute_publish_state` read the newest row and answered `did_not_start`, so the Production
    tab told an owner their app had not started — beside a list showing it live — and withheld
    Take down, which is gated on a live state. The row keeps its failure code, so the surface
    can still say the restart did not finish; what it may not do is say the app is down.
    """
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _deployment(db_session, app_id=app_row.id, user_id=user.id)
    await _deployment(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        status=DeploymentStatus.FAILED,
        failure_code="restart_failed",
    )

    resp = await client.get(
        f"/v1/projects/{app_row.project_id}/deployment", headers=auth_headers(user)
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["publishState"].startswith("live_"), body["publishState"]
    # NOT SWALLOWED: the attempt's own ending still rides on the row, which is what lets the
    # production surface state it beside a status that is true.
    assert body["failureCode"] == "restart_failed"


async def test_the_published_read_skips_a_success_that_published_no_image(db_session) -> None:
    """★ `latest_published` answers "what is in production", and a row with no digest cannot
    name the image that is there. Composing one from a mutable tag is exactly the "newer bits" a
    restart exists to prevent, so such a row is not an answer — it is skipped, and the route's
    refusal is what the citizen gets."""
    from src.services.deploy import store

    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=user.id)
    published = await _deployment(db_session, app_id=app_row.id, user_id=user.id)
    await _deployment(db_session, app_id=app_row.id, user_id=user.id, image_digest=None)

    found = await store.latest_published(db_session, app_id=app_row.id)

    assert found is not None
    assert found.id == published.id
