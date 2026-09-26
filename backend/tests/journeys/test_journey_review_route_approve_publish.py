"""The journey that makes the publish flow TERMINATE: route -> approve -> live.

A weighted Yes routes every time it is sent, so the flow ends only because approving
publishes. This drives it end to end through the real composition root:

1. a citizen's publish ROUTES (review found financial data the citizen did not declare);
2. an administrator approves that exact version, and that starts the publish — as the
   owner, from the submission copy, with no second click and no second queue entry.

Only object storage and the deploy pipeline are faked; the classification review is the
REAL service, so a mock cannot green this file.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI

from src.api.deps import storage_or_none_dependency
from src.api.v1.deploy.deps import deploy_service_or_none
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.audit import AuditLog
from src.services.classification import store as review_store
from src.services.deploy.classification import CLASSIFICATION_KEYS
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
        classification: dict[str, Any] | None = None,
        classification_score: int | None = None,
        expected_commit_sha: str | None = None,
        bundle_key: str | None = None,
    ) -> StartedDeploy:
        self.started.append(
            {
                "user_id": user_id,
                "app_id": app_id,
                "project_id": project_id,
                "conversation_id": conversation_id,
                "classification": classification,
                "classification_score": classification_score,
                "expected_commit_sha": expected_commit_sha,
                "bundle_key": bundle_key,
            }
        )
        return StartedDeploy(deployment_id=uuid.uuid4(), app_id=app_id)


def _answers(**yes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "credentialsSecrets": False,
        "healthData": False,
        "personalInformation": False,
        "financialData": False,
        "confidentialBusinessData": False,
        "publicData": False,
    }
    body.update(yes)
    return body


def _verdicts_doc(**by_key: str) -> dict[str, Any]:
    """A stored COMPLETE verdicts document, the exact shape the review runner writes."""
    return {
        "source": "review",
        "questions": {
            key: {
                "verdict": by_key.get(key, "no"),
                "reason": f"What the review found about {key}.",
                "agreed_with_scan": None,
                "downgraded_from_yes": False,
            }
            for key in CLASSIFICATION_KEYS
        },
        "scan": {
            "tier_a_hit": False,
            "tier_b_hit": False,
            "incomplete": False,
            "tier_a_dispute": False,
        },
    }


async def _seed_complete_review(
    db, *, app_id: uuid.UUID, user_id: uuid.UUID, **by_key: str
) -> None:
    outcome = await review_store.claim(db, app_id=app_id, user_id=user_id, head_sha=_SHA)
    assert outcome.claimed
    settled = await review_store.succeed(
        db,
        review_id=outcome.review.review_id,
        head_sha=_SHA,
        attempt=outcome.review.attempt,
        verdicts=_verdicts_doc(**by_key),
        evidence={"questions": {}, "scan_hits": [], "downgraded": []},
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
    # The review agrees on personal information and ALSO found financial data the
    # citizen's declaration below does not carry — the disagreement the admin reads.
    await _seed_complete_review(
        db_session,
        app_id=app_row.id,
        user_id=citizen.id,
        personal_information="yes",
        financial_data="yes",
    )

    declared = _answers(personalInformation=True)
    body = {
        "commitSha": _SHA,
        "answers": {
            **declared,
            "notes": "Stores traveller names so the pickup desk can match bookings.",
        },
    }

    # --- 1. the weighted Yes ROUTES: a queue entry, not a refusal and not a deploy ---
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
    assert fresh.approval_route is ApprovalRoute.SELF_PUBLISH
    assert fresh.source_commit_sha == _SHA
    declaration = fresh.declaration
    assert declaration is not None
    # Both answer sets and the difference reached the queue: the citizen said
    # No to financial data, the review said Yes, and the record names it.
    assert declaration["citizen"]["answers"]["financial_data"] is False
    assert declaration["review"]["answers"]["financial_data"] == "yes"
    assert declaration["differences"]["financial_data"] == ["review_yes_over_citizen_no"]
    assert declaration["merged"]["answers"]["financial_data"] is True
    assert declaration["review"]["available"] is True
    assert declaration["commits"]["shipping"] == _SHA

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
    assert gate_row.detail["declaration"]["citizen"]["answers"]["personal_information"] is True
