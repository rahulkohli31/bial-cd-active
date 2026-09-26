"""Approving an app publishes the exact copy the administrator reviewed.

The real route and the real deploy pipeline, with fakes only where the platform leaves the
process: the image registry, ARM and the object store. The snapshot bundles are REAL git
bundles and the pipeline extracts them with the real `extract_snapshot`, so "the pipeline
shipped H" is read off a tree that was actually cloned, not off a fake that echoed a SHA.

The pipeline shares the test's session, so every extraction waits on `wire.gate` until the
request that started it has answered; `_settle` opens it and drains.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import event

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.api.v1.deploy.deps import deploy_service_or_none
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.message import Message
from src.services.deploy import service as service_module
from src.services.deploy.images import ImageBuildError
from src.services.deploy.service import DeployService
from src.services.storage import snapshot_key, snapshot_read, submission_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, ConversationFactory, UserFactory
from tests.fakes import FakeStorage
from tests.services.deploy.test_service import FakeAca, FakeImages

_DEPLOY = "/v1/projects/{pid}/deploy"
_STATUS = "/v1/projects/{pid}/deployment"
_APPROVE = "/v1/admin/apps/{app_id}/approve"


def _two_versions(tmp_path: Path) -> tuple[tuple[bytes, str], tuple[bytes, str]]:
    """Two real HEAD bundles of one repository: the version submitted, then a later save."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)

    def _git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def _commit(page: str, bundle: str) -> tuple[bytes, str]:
        (repo / "app" / "page.tsx").write_text(page)
        (repo / "package.json").write_text('{"name": "visitor-log"}\n')
        _git("add", "-A")
        _git("commit", "-q", "-m", "bial-snapshot")
        _git("bundle", "create", str(repo / bundle), "HEAD")
        return (repo / bundle).read_bytes(), _git("rev-parse", "HEAD")

    _git("init", "-q")
    submitted = _commit("export default () => <main>v1</main>\n", "v1.bundle")
    later = _commit("export default () => <main>v2</main>\n", "v2.bundle")
    return submitted, later


async def _immediate(value: Any) -> Any:
    return value


@pytest.fixture
async def wire(app: FastAPI, db_session, monkeypatch, tmp_path):
    store = FakeStorage()
    gate = asyncio.Event()
    real_extract = snapshot_read.extract_snapshot
    extracted_from: list[str | None] = []

    async def _extract_when_the_request_has_answered(app_id, *, bundle_key=None):
        await gate.wait()
        extracted_from.append(bundle_key)
        return await real_extract(app_id, bundle_key=bundle_key, cache_root=tmp_path / "cache")

    monkeypatch.setattr(snapshot_read, "get_storage", lambda: store)
    monkeypatch.setattr(service_module, "extract_snapshot", _extract_when_the_request_has_answered)
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

    images = FakeImages()
    pipeline = DeployService(
        session_factory=lambda: _session(), image_builder=images, published_apps=FakeAca()
    )
    app.dependency_overrides[storage_dependency] = lambda: store
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
    submitted, later = _two_versions(tmp_path)
    yield SimpleNamespace(
        app=app,
        store=store,
        gate=gate,
        pipeline=pipeline,
        images=images,
        extracted_from=extracted_from,
        submitted=submitted,
        later=later,
    )
    gate.set()
    await pipeline.drain()


async def _settle(wire) -> None:
    wire.gate.set()
    await wire.pipeline.drain()
    wire.gate.clear()


def _save(wire, app_id: uuid.UUID, version: tuple[bytes, str]) -> str:
    data, sha = version
    wire.store.objects[snapshot_key(app_id)] = data
    wire.store.meta[snapshot_key(app_id)] = {"head_sha": sha}
    return sha


def _all_no() -> dict[str, object]:
    return {
        "credentialsSecrets": False,
        "healthData": False,
        "personalInformation": False,
        "financialData": False,
        "confidentialBusinessData": False,
        "publicData": False,
    }


async def _submitted_for_review(wire, client, db_session, **app_overrides):
    """An owner whose publish ROUTED: no review exists for the version, so the gate sends it
    to an administrator and forks the submission copy."""
    owner = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(db_session, user_id=owner.id, **app_overrides)
    head = _save(wire, app_row.id, wire.submitted)
    routed = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(owner),
        json={"commitSha": head, "answers": _all_no()},
    )
    assert routed.status_code == 200, routed.text
    assert routed.json()["outcome"] == "routed_for_review"
    return owner, app_row, uuid.UUID(routed.json()["submissionId"])


async def _approve(client, db_session, app_row: AppRegistry, submission_id: uuid.UUID):
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    resp = await client.post(
        _APPROVE.format(app_id=app_row.id),
        headers=auth_headers(admin),
        json={"submissionId": str(submission_id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    return admin


async def _deployments(db, app_id: uuid.UUID) -> list[Deployment]:
    rows = await db.execute(
        sa.select(Deployment)
        .where(Deployment.app_id == app_id)
        .order_by(Deployment.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _approve_audit(db, app_id: uuid.UUID) -> AuditLog:
    return (
        await db.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(app_id), AuditLog.action.like("approve%")
            )
        )
    ).scalar_one()


async def _status(client, owner, app_row: AppRegistry) -> dict[str, Any]:
    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(owner))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_approval_publishes_the_submitted_copy_whatever_was_saved_since(
    wire, client, db_session
) -> None:
    owner, app_row, submission_id = await _submitted_for_review(wire, client, db_session)
    submitted_sha = wire.submitted[1]
    newer_sha = _save(wire, app_row.id, wire.later)

    admin = await _approve(client, db_session, app_row, submission_id)
    await _settle(wire)

    (row,) = await _deployments(db_session, app_row.id)
    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.head_sha == submitted_sha
    assert row.user_id == owner.id
    assert wire.extracted_from == [submission_key(app_row.id, submission_id)]

    audit = await _approve_audit(db_session, app_row.id)
    assert audit.actor_id == admin.id
    assert audit.detail is not None
    assert audit.detail["publishing"] == "started"
    assert audit.detail["deploymentId"] == str(row.id)

    status = await _status(client, owner, app_row)
    assert status["publishState"] == "live_newer_work"
    assert status["headSha"] == submitted_sha
    assert status["savedHead"] == newer_sha
    assert status["approvedRetryCommit"] is None


async def test_a_snapshot_overwritten_after_submit_cannot_change_what_approval_publishes(
    wire, client, db_session
) -> None:
    _owner, app_row, submission_id = await _submitted_for_review(wire, client, db_session)
    wire.store.objects[snapshot_key(app_row.id)] = b"a container wrote this back"

    await _approve(client, db_session, app_row, submission_id)
    await _settle(wire)

    (row,) = await _deployments(db_session, app_row.id)
    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.head_sha == wire.submitted[1]


async def test_approval_during_a_restart_still_commits_and_try_again_publishes_the_copy(
    wire, client, db_session
) -> None:
    owner, app_row, submission_id = await _submitted_for_review(wire, client, db_session)
    submitted_sha = wire.submitted[1]
    _save(wire, app_row.id, wire.later)
    restart = Deployment(
        app_id=app_row.id,
        user_id=owner.id,
        step="restarting",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.add(restart)
    await db_session.commit()

    await _approve(client, db_session, app_row, submission_id)

    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.APPROVED
    audit = await _approve_audit(db_session, app_row.id)
    assert audit.detail is not None
    assert audit.detail["publishing"] == "not_started"
    assert audit.detail["reason"] == "deploy_in_flight"
    assert "deploymentId" not in audit.detail
    status = await _status(client, owner, app_row)
    assert status["publishState"] == "did_not_start"
    assert status["approvedRetryCommit"] == submitted_sha

    restart.status = DeploymentStatus.SUCCEEDED
    await db_session.commit()
    retried = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(owner),
        json={"commitSha": submitted_sha},
    )
    assert retried.status_code == 202, retried.text
    await _settle(wire)

    newest = (await _deployments(db_session, app_row.id))[-1]
    assert newest.status is DeploymentStatus.SUCCEEDED
    assert newest.head_sha == submitted_sha
    assert wire.extracted_from == [submission_key(app_row.id, submission_id)]


async def test_approval_with_publishing_unconfigured_still_commits_and_says_why(
    wire, client, db_session
) -> None:
    owner, app_row, submission_id = await _submitted_for_review(wire, client, db_session)
    wire.app.dependency_overrides[deploy_service_or_none] = lambda: None

    await _approve(client, db_session, app_row, submission_id)

    audit = await _approve_audit(db_session, app_row.id)
    assert audit.detail is not None
    assert audit.detail["publishing"] == "not_started"
    assert audit.detail["reason"] == "publishing_unavailable"
    assert await _deployments(db_session, app_row.id) == []
    status = await _status(client, owner, app_row)
    assert status["publishState"] == "did_not_start"
    assert status["approvedRetryCommit"] == wire.submitted[1]

    wire.app.dependency_overrides[deploy_service_or_none] = lambda: wire.pipeline
    retried = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(owner),
        json={"commitSha": wire.submitted[1]},
    )
    assert retried.status_code == 202, retried.text
    await _settle(wire)
    (row,) = await _deployments(db_session, app_row.id)
    assert row.head_sha == wire.submitted[1]


async def test_approving_a_manual_route_queue_item_publishes_it(wire, client, db_session) -> None:
    owner, app_row, submission_id = await _submitted_for_review(wire, client, db_session)
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == app_row.id)
        .values(approval_route=ApprovalRoute.RUNBOOK)
    )
    await db_session.commit()

    await _approve(client, db_session, app_row, submission_id)
    await _settle(wire)

    (row,) = await _deployments(db_session, app_row.id)
    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.head_sha == wire.submitted[1]
    assert (await _status(client, owner, app_row))["publishState"] == "live_current"


async def _approved_earlier(wire, db_session, **overrides):
    """An app an administrator approved before approving published anything: its copy is
    in the store, nothing has been attempted since, and newer work is saved."""
    owner = await UserFactory.create(db_session)
    submission_id = uuid.uuid4()
    data, sha = wire.submitted
    app_row = await AppRegistryFactory.create(
        db_session,
        user_id=owner.id,
        status=AppStatus.APPROVED,
        approval_route=ApprovalRoute.SELF_PUBLISH,
        source_submission_id=submission_id,
        source_commit_sha=sha,
        approved_submission_id=submission_id,
        approved_commit_sha=sha,
        approved_at=datetime.now(UTC) - timedelta(days=3),
        declaration={"citizen": {"answers": {}, "explanation": None}},
        **overrides,
    )
    wire.store.objects[submission_key(app_row.id, submission_id)] = data
    _save(wire, app_row.id, wire.later)
    return owner, app_row, submission_id


async def test_an_app_approved_before_this_release_offers_try_again_for_its_copy(
    wire, client, db_session
) -> None:
    owner, app_row, submission_id = await _approved_earlier(wire, db_session)

    status = await _status(client, owner, app_row)
    assert status["publishState"] == "did_not_start"
    assert status["approvedRetryCommit"] == wire.submitted[1]

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(owner),
        json={"commitSha": wire.submitted[1]},
    )
    assert resp.status_code == 202, resp.text
    await _settle(wire)

    (row,) = await _deployments(db_session, app_row.id)
    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.head_sha == wire.submitted[1]
    assert wire.extracted_from == [submission_key(app_row.id, submission_id)]
    gate = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(app_row.id), AuditLog.action == "publish_gate"
            )
        )
    ).scalar_one()
    assert gate.detail is not None
    assert gate.detail["rule"] == "approved_override"
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.APPROVED


async def test_an_approved_app_with_no_stored_copy_presents_as_a_draft(
    wire, client, db_session
) -> None:
    owner = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(
        db_session,
        user_id=owner.id,
        status=AppStatus.APPROVED,
        approved_commit_sha=None,
        approved_submission_id=None,
    )
    _save(wire, app_row.id, wire.later)

    status = await _status(client, owner, app_row)

    assert status["publishState"] == "draft"
    assert status["approvedRetryCommit"] is None


async def test_the_approved_version_taken_offline_publishes_again_without_review(
    wire, client, db_session
) -> None:
    owner, app_row, submission_id = await _approved_earlier(wire, db_session)
    db_session.add(
        Deployment(
            app_id=app_row.id,
            user_id=owner.id,
            status=DeploymentStatus.SUCCEEDED,
            head_sha=wire.submitted[1],
            url="https://pub.example/",
            created_at=datetime.now(UTC) - timedelta(days=1),
            unpublished_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.commit()

    status = await _status(client, owner, app_row)
    assert status["publishState"] == "taken_offline"
    assert status["approvedRetryCommit"] == wire.submitted[1]

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(owner),
        json={"commitSha": wire.submitted[1]},
    )
    assert resp.status_code == 202, resp.text
    await _settle(wire)
    newest = (await _deployments(db_session, app_row.id))[-1]
    assert newest.status is DeploymentStatus.SUCCEEDED
    assert newest.head_sha == wire.submitted[1]
    assert wire.extracted_from == [submission_key(app_row.id, submission_id)]


async def test_a_failed_approved_publish_writes_no_chat_card_and_offers_the_copy_again(
    wire, client, db_session
) -> None:
    owner, app_row, submission_id = await _submitted_for_review(wire, client, db_session)
    await ConversationFactory.create(
        db_session, user_id=owner.id, project_id=app_row.project_id, id=app_row.conversation_id
    )
    wire.images.error = ImageBuildError("the registry refused the build", log_tail=None)

    await _approve(client, db_session, app_row, submission_id)
    await _settle(wire)

    (row,) = await _deployments(db_session, app_row.id)
    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == "build_failed"
    cards = await db_session.execute(
        sa.select(Message.id).where(Message.conversation_id == app_row.conversation_id)
    )
    assert cards.all() == []
    status = await _status(client, owner, app_row)
    assert status["publishState"] == "did_not_start"
    assert status["approvedRetryCommit"] == wire.submitted[1]


def _counting_selects() -> tuple[list[str], Any]:
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    return statements, _record


async def test_a_newer_version_failing_before_it_names_a_commit_is_not_the_copy_again(
    wire, client, db_session, test_engine
) -> None:
    """The approved copy went live; the owner later sent a newer version, and that attempt
    failed before extraction. Try again is about the newer version — it goes through the
    gate, and the copy already serving is not republished."""
    owner, app_row, _submission_id = await _approved_earlier(wire, db_session)
    db_session.add_all(
        [
            Deployment(
                app_id=app_row.id,
                user_id=owner.id,
                status=DeploymentStatus.SUCCEEDED,
                head_sha=wire.submitted[1],
                image_digest="sha256:" + "ab" * 32,
                url="https://pub.example/",
                created_at=datetime.now(UTC) - timedelta(days=2),
            ),
            Deployment(
                app_id=app_row.id,
                user_id=owner.id,
                status=DeploymentStatus.FAILED,
                failure_code="snapshot_moved",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            ),
        ]
    )
    await db_session.commit()

    statements, record = _counting_selects()
    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        status = await _status(client, owner, app_row)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)

    assert status["publishState"] == "did_not_start"
    assert status["approvedRetryCommit"] is None
    # The one extra read that decided it — this row could not say for itself.
    assert len(statements) == 4, statements


async def test_a_first_attempt_failing_before_it_names_a_commit_retries_the_copy(
    wire, client, db_session
) -> None:
    owner, app_row, _submission_id = await _approved_earlier(wire, db_session)
    db_session.add(
        Deployment(
            app_id=app_row.id,
            user_id=owner.id,
            status=DeploymentStatus.FAILED,
            failure_code="snapshot_unreadable",
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.commit()

    status = await _status(client, owner, app_row)

    assert status["publishState"] == "did_not_start"
    assert status["approvedRetryCommit"] == wire.submitted[1]


async def test_a_row_that_names_its_commit_costs_the_poll_no_extra_read(
    wire, client, db_session, test_engine
) -> None:
    owner, app_row, _submission_id = await _approved_earlier(wire, db_session)
    db_session.add(
        Deployment(
            app_id=app_row.id,
            user_id=owner.id,
            status=DeploymentStatus.FAILED,
            failure_code="build_failed",
            head_sha=wire.submitted[1],
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.commit()

    statements, record = _counting_selects()
    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        status = await _status(client, owner, app_row)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)

    assert status["approvedRetryCommit"] == wire.submitted[1]
    assert len(statements) == 3, statements
