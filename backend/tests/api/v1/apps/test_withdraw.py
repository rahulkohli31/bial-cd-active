"""Withdrawal — the owner's way OUT of the queue (U8: P6, R15b's counterpart).

A pending submission can be withdrawn but not overwritten: re-submitting over an
item an administrator may be reading is forbidden (the submit service refuses it),
and withdrawal is what replaced that escape hatch — pending→draft, the pin, the
declaration and the lineage cleared, the queue item REMOVED rather than replaced.
Owner-scoped like every `/apps/*` route: a cross-user withdraw is the non-leaking
404, never a 403.

Pending state is seeded through the REAL writer (`services/approvals/submit`)
wherever the flow matters, so these tests walk the exact submit→withdraw cycle a
citizen walks; pure state-guard cases seed the row directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.audit import AuditLog
from src.main import create_app
from src.services.approvals.submit import submit_app_for_review
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import snapshot_key, submission_key
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

_SHA = "ab" * 20
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fake-bytes"

_DECLARATION = {
    "citizen": {"personal_information": "no"},
    "review": {"personal_information": "yes"},
    "differences": ["personal_information"],
    "explanation": "It only stores visitor gate numbers.",
}


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _submitted_app(db, user, store: FakeStorage):
    """An app taken to PENDING through the real submit service — the publish gate's
    call, minus the gate (U9)."""
    app_row = await AppRegistryFactory.create(db, user_id=user.id)
    store.objects[snapshot_key(app_row.id)] = _BUNDLE
    receipt = await submit_app_for_review(
        db,
        store,
        user_id=user.id,
        app=app_row,
        declaration=_DECLARATION,
        route=ApprovalRoute.SELF_PUBLISH,
    )
    await db.commit()
    return app_row, receipt


async def test_withdraw_returns_a_pending_submission_to_draft_and_clears_the_pin(
    client, db_session, fake_storage
) -> None:
    user, headers = await _auth_user(db_session)
    app_row, receipt = await _submitted_app(db_session, user, fake_storage)

    resp = await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"appId": str(app_row.id), "status": "draft"}
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.DRAFT
    # The pin, the declaration AND the lineage all clear: a withdrawn submission
    # entered through a route that no longer describes it, so NULL (the documented
    # "no current submission" state) is what the next submit builds on.
    assert row.source_submission_id is None
    assert row.source_commit_sha is None
    assert row.submitted_at is None
    assert row.declaration is None
    assert row.approval_route is None
    # The immutable submission BLOB survives — submissions are retained and ids
    # never reused (R2); withdrawal removes the queue item, not the artifact.
    assert submission_key(app_row.id, receipt.submission_id) in fake_storage.objects


async def test_withdraw_is_audited_app_scoped_with_the_departing_submission(
    client, db_session, fake_storage
) -> None:
    user, headers = await _auth_user(db_session)
    app_row, receipt = await _submitted_app(db_session, user, fake_storage)

    await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=headers)

    rows = (
        (
            await db_session.execute(
                sa.select(AuditLog).where(
                    AuditLog.resource_type == "app",
                    AuditLog.resource_id == str(app_row.id),
                    AuditLog.action == "withdraw",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor_id == user.id
    # The trail names the exact submission that left the queue.
    assert rows[0].detail == {
        "submissionId": str(receipt.submission_id),
        "commitSha": receipt.commit_sha,
    }


async def test_withdraw_removes_the_item_from_the_admin_queue(
    client, db_session, fake_storage
) -> None:
    # "Removes rather than replaces": the admin pending queue simply no longer
    # lists it — an administrator mid-review sees it disappear, never mutate.
    user, headers = await _auth_user(db_session)
    app_row, _receipt = await _submitted_app(db_session, user, fake_storage)
    _, admin_headers = await _auth_user(db_session, email="admin@bial.com")

    before = await client.get("/v1/admin/apps?status=pending", headers=admin_headers)
    assert str(app_row.id) in [a["appId"] for a in before.json()["apps"]]

    assert (
        await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=headers)
    ).status_code == 200

    after = await client.get("/v1/admin/apps?status=pending", headers=admin_headers)
    assert str(app_row.id) not in [a["appId"] for a in after.json()["apps"]]


async def test_withdraw_then_approve_conflicts_the_existing_guard_holds(
    client, db_session, fake_storage
) -> None:
    # The approve-versus-withdrawal race needs NO new machinery: an approval naming
    # a submission id the row no longer carries updates zero rows and conflicts —
    # the same D5 guard that answers the approve-versus-resubmit race. (What the
    # administrator READS in that moment is U13's; this pins that nothing lands.)
    user, headers = await _auth_user(db_session)
    app_row, receipt = await _submitted_app(db_session, user, fake_storage)
    _, admin_headers = await _auth_user(db_session, email="admin@bial.com")

    assert (
        await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=headers)
    ).status_code == 200

    approve = await client.post(
        f"/v1/admin/apps/{app_row.id}/approve",
        json={"submissionId": str(receipt.submission_id)},
        headers=admin_headers,
    )
    assert approve.status_code == 409
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.DRAFT  # the withdrawal stood
    assert row.approved_submission_id is None  # nothing was pinned


async def test_withdraw_of_a_non_pending_app_is_refused(client, db_session) -> None:
    # Withdrawal only ever un-queues; it never un-decides an administrator. Every
    # non-pending status refuses with the same 409 and writes nothing.
    user, headers = await _auth_user(db_session)
    for status in (AppStatus.DRAFT, AppStatus.APPROVED, AppStatus.REJECTED, AppStatus.DISABLED):
        app_row = await AppRegistryFactory.create(db_session, user_id=user.id, status=status)
        resp = await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=headers)
        assert resp.status_code == 409, status
        assert "withdrawn" in resp.json()["error"]["message"]
        row = await db_session.get(AppRegistry, app_row.id)
        await db_session.refresh(row)
        assert row.status is status  # untouched


async def test_withdraw_keeps_the_approved_pin_of_an_earlier_approval(client, db_session) -> None:
    # A re-submitted app carries BOTH the pending pin and the earlier approved pin.
    # Withdraw clears only the pending half — same rule as reject: status governs
    # liveness, the approved pin governs WHICH artifact the runbook lineage serves.
    earlier = uuid.uuid4()
    user, headers = await _auth_user(db_session)
    app_row = await AppRegistryFactory.create(
        db_session,
        user_id=user.id,
        status=AppStatus.PENDING,
        source_submission_id=uuid.uuid4(),
        source_commit_sha=_SHA,
        submitted_at=datetime.now(UTC),
        approved_submission_id=earlier,
        approved_commit_sha=_SHA,
    )

    resp = await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=headers)

    assert resp.status_code == 200
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.DRAFT
    assert row.source_submission_id is None
    assert row.approved_submission_id == earlier  # untouched


async def test_withdraw_of_another_users_app_is_a_non_leaking_404(client, db_session) -> None:
    # 404, NOT 403 (ADR-0004): a cross-user id is indistinguishable from a missing
    # one, and nothing about the app — including that it exists — leaks.
    owner, _ = await _auth_user(db_session, email="wdowner@rvaiglobal.com")
    app_row = await AppRegistryFactory.create(
        db_session,
        user_id=owner.id,
        status=AppStatus.PENDING,
        source_submission_id=uuid.uuid4(),
        source_commit_sha=_SHA,
        submitted_at=datetime.now(UTC),
    )
    _, stranger_headers = await _auth_user(db_session, email="wdstranger@rvaiglobal.com")

    resp = await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=stranger_headers)

    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "App not found."}}
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.PENDING  # untouched


async def test_withdraw_unknown_app_is_404(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    resp = await client.post(f"/v1/apps/{uuid.uuid4()}/withdraw", headers=headers)
    assert resp.status_code == 404


async def test_withdraw_requires_authentication(client) -> None:
    resp = await client.post(f"/v1/apps/{uuid.uuid4()}/withdraw")
    assert resp.status_code == 401


def test_withdraw_documents_its_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    withdraw = set(paths["/v1/apps/{app_id}/withdraw"]["post"]["responses"])
    # `.500` is inherited from the v1-router default; the rest are declared per route.
    assert {"401", "404", "409", "500"} <= withdraw
