"""The journey that makes the publish flow TERMINATE: route -> approve -> live.

A hard block routes every time it is sent, so the flow ends only because approving
publishes. This drives it end to end through the real composition root:

1. an owner's publish ROUTES (the reviewer found financial data, a hard block), with a note;
2. an administrator approves that exact version, and that starts the publish — as the
   owner, from the submission copy, with no second click and no second queue entry.

Only object storage and the deploy pipeline are faked; the classification review is the
REAL service, so a mock cannot green this file.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import FastAPI

from src.api.deps import storage_or_none_dependency
from src.api.v1.deploy.deps import deploy_service_or_none
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.services.classification import store as review_store
from src.services.classification.config import load_live_config
from src.services.deploy.service import StartedDeploy
from src.services.storage import snapshot_key, submission_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage, a_git_bundle

_DEPLOY = "/v1/projects/{pid}/deploy"
_SHA = "5a" * 20


class _RecordingDeployService:
    """Stands in for the deploy pipeline: records every start, reaches no Azure."""

    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []

    async def start(
        self,
        db: object,
        *,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        expected_commit_sha: str | None = None,
        bundle_key: str | None = None,
    ) -> StartedDeploy:
        self.started.append(
            {
                "user_id": user_id,
                "app_id": app_id,
                "project_id": project_id,
                "conversation_id": conversation_id,
                "expected_commit_sha": expected_commit_sha,
                "bundle_key": bundle_key,
            }
        )
        return StartedDeploy(deployment_id=uuid.uuid4(), app_id=app_id)


async def _seed_complete_review(
    db, *, app_id: uuid.UUID, user_id: uuid.UUID, yes: tuple[str, ...]
) -> None:
    """A stored COMPLETE review under the live class definitions, in the runner's shape."""
    config = await load_live_config(db)
    outcome = await review_store.claim(
        db, app_id=app_id, user_id=user_id, head_sha=_SHA, fingerprint=config.fingerprint
    )
    assert outcome.claimed
    settled = await review_store.succeed(
        db,
        review_id=outcome.review.review_id,
        head_sha=_SHA,
        fingerprint=config.fingerprint,
        attempt=outcome.review.attempt,
        verdicts={
            "classes": {
                entry.key: {
                    "verdict": "yes" if entry.key in yes else "no",
                    "reason": f"What the reviewer found about {entry.key}.",
                }
                for entry in config.classes
            }
        },
        evidence={"classes": {}, "scan_hits": []},
        answers_complete=True,
    )
    assert settled


async def test_route_approve_publish_terminates(app: FastAPI, client, db_session) -> None:
    store = FakeStorage()
    pipeline = _RecordingDeployService()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: pipeline

    # --- the app, its saved version, and the REAL review service's stored row --------
    citizen = await UserFactory.create(db_session, email="citizen@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(db_session, user_id=citizen.id)
    store.objects[snapshot_key(app_row.id)] = a_git_bundle(_SHA)
    store.meta[snapshot_key(app_row.id)] = {"head_sha": _SHA}
    # The reviewer found bank details (a hard block) and an outside AI service.
    await _seed_complete_review(
        db_session, app_id=app_row.id, user_id=citizen.id, yes=("financial_data", "ai_usage")
    )
    body = {
        "commitSha": _SHA,
        "answers": {"ai_usage": False},
        "note": "Stores vendor bank details so finance can pay invoices.",
    }

    # --- 1. the hard block ROUTES: a queue entry, not a refusal and not a deploy ---
    routed = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(citizen), json=body
    )
    assert routed.status_code == 200
    routed_body = routed.json()
    assert routed_body["outcome"] == "routed_for_review"
    assert routed_body["commitSha"] == _SHA
    submission_id = routed_body["submissionId"]
    assert pipeline.started == []

    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.PENDING
    assert fresh.source_commit_sha == _SHA
    declaration = fresh.declaration
    assert declaration is not None
    # Both answer sets, the scores and the reason reached the queue.
    assert declaration["version"] == 2
    assert declaration["commit"] == _SHA
    assert declaration["reason"] == "hard_block"
    assert declaration["reviewerAnswers"]["financial_data"] is True
    assert declaration["ownerAnswers"] == {"ai_usage": False}
    assert (declaration["reviewerScore"], declaration["score"]) == (20, 0)
    assert declaration["note"] == "Stores vendor bank details so finance can pay invoices."

    # --- 2. an administrator approves EXACTLY that version, and that publishes it --
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    approved = await client.post(
        f"/v1/admin/apps/{app_row.id}/approve",
        headers=auth_headers(admin),
        json={"submissionId": submission_id},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    (started,) = pipeline.started
    assert started["app_id"] == app_row.id
    assert started["user_id"] == citizen.id
    assert started["project_id"] == app_row.project_id
    assert started["conversation_id"] is None
    assert started["expected_commit_sha"] == _SHA
    assert started["bundle_key"] == submission_key(app_row.id, uuid.UUID(submission_id))

    final = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert final is not None
    assert final.status is AppStatus.APPROVED

    # --- the trail: every decision app-scoped and readable in one query ---
    rows = (
        (
            await db_session.execute(
                sa.select(AuditLog)
                .where(AuditLog.resource_type == "app", AuditLog.resource_id == str(app_row.id))
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    actions = [row.action for row in rows]
    assert "submit" in actions
    (approve_row,) = [row for row in rows if row.action == "approve"]
    assert approve_row.actor_id == admin.id
    assert approve_row.detail is not None
    assert approve_row.detail["publishing"] == "started"
    gate_rows = [row for row in rows if row.action == "publish_gate"]
    decisions = [row.detail["decision"] for row in gate_rows if row.detail is not None]
    assert decisions == ["routed"]
    (gate_row,) = gate_rows
    assert gate_row.detail is not None
    # The actor reference nulls when a user is removed; the email keeps the trail
    # saying WHO, and the declaration keeps it saying WHAT was decided on.
    assert gate_row.detail["email"] == "citizen@rvaiglobal.com"
    assert gate_row.detail["declaration"] == declaration
