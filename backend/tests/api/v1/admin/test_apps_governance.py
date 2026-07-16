"""Admin app-registry governance (APPROVAL U5–U7, AE1, R27/R29): super-admin-only +
audited, the exact state machine, the reviewed-submission-id approve guard (D5),
the artifact-exists pin check (R11), the audited bundle download (R15), the
mark-deployed marker (R17), and the durable single-use clear-data token."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.clear_data_token import ClearDataToken
from src.db.models.data_record import DataRecord
from src.main import create_app
from src.services.appserving.governance import nuke_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import AppContainerStore, snapshot_key, submission_key
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds
_SHA = "1f" * 20  # 40 lowercase hex chars — the shape the bundle parser guarantees


class _RecordingContainerStore(AppContainerStore):
    """A per-app container store double that records the containers it was asked to delete.
    Deliberately skips `AppContainerStore.__init__` (no Azure config) — the sweep only calls
    `delete_container`, so the un-set `_config` is never touched."""

    def __init__(self) -> None:  # noqa: D107 — no super().__init__ on purpose (see class docstring)
        self.deleted: list[uuid.UUID] = []

    async def delete_container(self, app_id: uuid.UUID) -> None:
        self.deleted.append(app_id)


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _app(db: AsyncSession, **overrides) -> AppRegistry:
    owner = await UserFactory.create(db)
    return await AppRegistryFactory.create(db, user_id=owner.id, **overrides)


def _pending(**extra):
    """A pending app carrying a submitted (typed) submission ref — the approve
    gate's precondition. The BLOB is seeded separately via `_stage_bundle`."""
    return {
        "status": AppStatus.PENDING,
        "source_submission_id": uuid.uuid4(),
        "source_commit_sha": _SHA,
        "submitted_at": datetime.now(UTC),
        **extra,
    }


def _approved(**extra):
    """An approved app whose pin matches its source submission (the post-approve
    steady state)."""
    sid = uuid.uuid4()
    return {
        "status": AppStatus.APPROVED,
        "source_submission_id": sid,
        "source_commit_sha": _SHA,
        "submitted_at": datetime.now(UTC),
        "approved_submission_id": sid,
        "approved_commit_sha": _SHA,
        **extra,
    }


def _stage_bundle(store: FakeStorage, row: AppRegistry) -> None:
    """Seed the submission blob approve's R11 head-check verifies."""
    assert row.source_submission_id is not None
    store.objects[submission_key(row.id, row.source_submission_id)] = b"# v2 git bundle\nfake"


def _wire_storage(app) -> FakeStorage:
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    return store


def _approve_body(row: AppRegistry) -> dict[str, str]:
    return {"submissionId": str(row.source_submission_id)}


# --- AE1: super-admin gating ---------------------------------------------------


async def test_citizen_is_forbidden(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _citizen(db_session)
    assert (await client.get("/v1/admin/apps", headers=headers)).status_code == 403
    assert (
        await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
    ).status_code == 403
    assert (
        await client.get(f"/v1/admin/apps/{app.id}/bundle-url", headers=headers)
    ).status_code == 403
    assert (
        await client.post(f"/v1/admin/apps/{app.id}/mark-deployed", headers=headers)
    ).status_code == 403


async def test_unauthenticated_is_401(client, db_session) -> None:
    assert (await client.get("/v1/admin/apps")).status_code == 401


async def test_gate_denials_are_detail_shaped_not_envelope(client, db_session) -> None:
    # The gate raises bare HTTPException -> `{"detail"}`, NOT the AppApiError envelope.
    citizen = await _citizen(db_session)
    forbidden = await client.get("/v1/admin/apps", headers=citizen)
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Super-admin privileges required."}
    unauth = await client.get("/v1/admin/apps")
    assert unauth.status_code == 401
    assert set(unauth.json()) == {"detail"}


def test_admin_routes_document_error_codes_in_openapi() -> None:
    # Each governance route lists its explicit 4xx/5xx plus the inherited
    # dependency 401/403 (DetailBody) and the v1-router 500 default.
    paths = create_app().openapi()["paths"]
    approve = set(paths["/v1/admin/apps/{app_id}/approve"]["post"]["responses"])
    assert {"404", "409", "503", "401", "403", "500"} <= approve
    assert {"400", "401", "403", "500"} <= set(paths["/v1/admin/apps"]["get"]["responses"])
    bundle = set(paths["/v1/admin/apps/{app_id}/bundle-url"]["get"]["responses"])
    assert {"404", "409", "503", "401", "403", "500"} <= bundle
    deployed = set(paths["/v1/admin/apps/{app_id}/mark-deployed"]["post"]["responses"])
    assert {"404", "409", "401", "403", "500"} <= deployed


# --- D5/R7/R11: approve pins exactly the reviewed submission ---------------------


async def test_approve_pins_the_reviewed_submission(client, app, db_session) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"appId": str(row.id), "status": "approved"}

    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.APPROVED
    # The pin IS the reviewed submission (D5), SHA carried over from submit (R4).
    assert fresh.approved_submission_id == fresh.source_submission_id
    assert fresh.approved_commit_sha == _SHA
    assert fresh.approved_by is not None
    assert fresh.approved_at is not None


async def test_approve_race_resubmitted_since_review_is_409(client, app, db_session) -> None:
    # THE race (R7): admin reviews submission A; the owner re-submits (B) before the
    # admin clicks approve-with-A. A status-only guard cannot see this
    # (PENDING→PENDING is legal); the reviewed-id predicate updates zero rows.
    store = _wire_storage(app)
    reviewed_sid = uuid.uuid4()
    row = await _app(db_session, **_pending())
    # Both blobs exist (submissions are retained), so ONLY the guard can refuse.
    store.objects[submission_key(row.id, reviewed_sid)] = b"# v2 git bundle\nA"
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve",
        json={"submissionId": str(reviewed_sid)},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "re-submitted" in resp.json()["error"]["message"]

    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING  # nothing promoted
    assert fresh.approved_submission_id is None  # nothing pinned


async def test_approve_missing_artifact_is_409_and_no_pin(client, app, db_session) -> None:
    # R11: the reviewed submission's blob is gone (or never existed) → refuse, so an
    # app can never reach APPROVED with an artifact that 404s at runbook time.
    _wire_storage(app)  # empty store — no blob staged
    row = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 409
    assert "missing" in resp.json()["error"]["message"]
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING
    assert fresh.approved_submission_id is None


async def test_approve_foreign_submission_id_is_409(client, app, db_session) -> None:
    # A submission id belonging to ANOTHER app: the key derivation scopes the blob
    # under THIS app's prefix, so the head misses and approve refuses — one app's
    # review can never pin another app's artifact.
    store = _wire_storage(app)
    other = await _app(db_session, **_pending())
    _stage_bundle(store, other)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(other), headers=headers
    )
    assert resp.status_code == 409


async def test_approve_requires_pending(client, app, db_session) -> None:
    _wire_storage(app)
    row = await _app(db_session, status=AppStatus.DRAFT)
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve",
        json={"submissionId": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_approve_disabled_directly_is_409(client, app, db_session) -> None:
    # R10 mirror: →approved legally permits DISABLED (that is enable's path), and a
    # kill-switched app's source_submission_id is frozen — so WITHOUT the explicit
    # PENDING-only pre-check, approve-with-the-frozen-id would re-stamp the pin and
    # promote a kill-switched app. Approve reaches APPROVED only from PENDING.
    store = _wire_storage(app)
    row = await _app(db_session, **_approved(status=AppStatus.DISABLED))
    _stage_bundle(store, row)
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 409
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.DISABLED  # still kill-switched


# --- state machine -------------------------------------------------------------


async def test_reject_transition(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/reject", json={"note": "no good"}, headers=headers
    )
    assert resp.json()["status"] == "rejected"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.rejection_note == "no good"


async def test_reject_leaves_the_approved_pin_intact(client, db_session) -> None:
    # Status governs liveness; the pin governs WHICH artifact. A previously-approved
    # app re-submitted and then rejected keeps its approved pin — the documented
    # "a pending re-submit keeps serving the prior approved artifact" invariant.
    pinned_sid = uuid.uuid4()
    app = await _app(
        db_session,
        **_pending(approved_submission_id=pinned_sid, approved_commit_sha=_SHA),
    )
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/reject", json={"note": "regressed"}, headers=headers
    )
    assert resp.json()["status"] == "rejected"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.approved_submission_id == pinned_sid  # pin survives rejection


async def test_disable_then_enable_preserves_the_pin(client, db_session) -> None:
    app = await _app(db_session, **_approved())
    pinned = app.approved_submission_id
    headers = await _admin(db_session)
    dis = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert dis.json()["status"] == "disabled"
    en = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert en.json()["status"] == "approved"
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.approved_submission_id == pinned  # the pin survives the round trip


async def test_disable_requires_approved(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert resp.status_code == 409


async def test_enable_guard_rejects_non_disabled(client, db_session) -> None:
    # A pending app must not be promotable to approved via enable (approve-gate bypass).
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert resp.status_code == 409


# --- the queue projection (R15/R16) -----------------------------------------------


async def test_list_and_status_filter(client, db_session) -> None:
    await _app(db_session, **_pending())
    approved = await _app(db_session, **_approved())
    headers = await _admin(db_session)
    listed = await client.get("/v1/admin/apps?status=approved", headers=headers)
    ids = [a["appId"] for a in listed.json()["apps"]]
    assert str(approved.id) in ids
    # The projection never leaks the app key or mints a signed URL (R15).
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(approved.id))
    assert "appKey" not in row
    assert "url" not in row and "bundleUrl" not in row
    assert row["hasApprovedSnapshot"] is True


async def test_unknown_status_filter_is_400(client, db_session) -> None:
    headers = await _admin(db_session)
    resp = await client.get("/v1/admin/apps?status=bogus", headers=headers)
    assert resp.status_code == 400  # fail-closed, never a silent full list


async def test_pending_queue_is_ordered_by_submitted_at(client, db_session) -> None:
    # R16: the pending list is a REVIEW QUEUE — oldest submission first. created_at
    # (provision time) is deliberately not the axis.
    now = datetime.now(UTC)
    newer = await _app(db_session, **_pending(submitted_at=now))
    older = await _app(db_session, **_pending(submitted_at=now - timedelta(hours=2)))
    headers = await _admin(db_session)
    listed = await client.get("/v1/admin/apps?status=pending", headers=headers)
    ids = [a["appId"] for a in listed.json()["apps"]]
    assert ids.index(str(older.id)) < ids.index(str(newer.id))


async def test_pending_row_carries_the_review_payload(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    listed = await client.get("/v1/admin/apps?status=pending", headers=headers)
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(app.id))
    assert row["submissionId"] == str(app.source_submission_id)
    assert row["commitSha"] == _SHA
    assert row["submittedAt"] is not None
    assert row["hasApprovedSnapshot"] is False
    assert row["redeployNeeded"] is False  # never approved → nothing to deploy


# --- the audited bundle download (R15) ---------------------------------------------


async def test_bundle_url_mints_and_audits(client, app, db_session) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)

    resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["submissionId"] == str(row.source_submission_id)
    assert body["commitSha"] == _SHA
    assert body["url"].startswith("https://")
    # Minutes-scale TTL — far under the ABC's 7-day ceiling (R15).
    assert 0 < body["expiresInSeconds"] <= 3600

    audit = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(row.id), AuditLog.action == "bundle:download"
            )
        )
    ).scalar_one()
    # The detail identifies the artifact — and NEVER carries the bearer URL (R15).
    assert audit.detail == {"submissionId": str(row.source_submission_id), "commitSha": _SHA}
    assert body["url"] not in str(audit.detail)


async def test_bundle_url_without_submission_is_409_and_unaudited(client, app, db_session) -> None:
    _wire_storage(app)
    row = await _app(db_session)  # draft, never submitted
    headers = await _admin(db_session)
    resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)
    assert resp.status_code == 409
    # No audit row for a non-event.
    rows = (
        (
            await db_session.execute(
                sa.select(AuditLog).where(
                    AuditLog.resource_id == str(row.id), AuditLog.action == "bundle:download"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# --- mark-deployed (R17, D7) --------------------------------------------------------


async def test_mark_deployed_stamps_marker_and_audits(client, db_session) -> None:
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)

    resp = await client.post(f"/v1/admin/apps/{app.id}/mark-deployed", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["deployedSubmissionId"] == str(app.approved_submission_id)

    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.APPROVED  # a marker, NOT a status transition (D7)
    assert fresh.deployed_submission_id == fresh.approved_submission_id
    assert fresh.deployed_at is not None

    listed = await client.get("/v1/admin/apps?status=approved", headers=headers)
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(app.id))
    assert row["redeployNeeded"] is False  # deployed == approved

    audit = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(app.id), AuditLog.action == "mark-deployed"
            )
        )
    ).scalar_one()
    assert audit.detail["submissionId"] == str(app.approved_submission_id)


async def test_mark_deployed_refuses_unapproved(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/mark-deployed", headers=headers)
    assert resp.status_code == 409
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_submission_id is None  # nothing written


async def test_reapproval_after_deploy_surfaces_redeploy_needed(client, app, db_session) -> None:
    # approve → mark-deployed → re-submit → re-approve: the approved pin moved past
    # the deployed marker, so the queue must show a re-deploy is needed (R17).
    store = _wire_storage(app)
    row = await _app(db_session, **_approved())
    headers = await _admin(db_session)
    assert (
        await client.post(f"/v1/admin/apps/{row.id}/mark-deployed", headers=headers)
    ).status_code == 200

    # The owner re-submits (fresh submission id), the admin re-approves it.
    new_sid = uuid.uuid4()
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == row.id)
        .values(
            status=AppStatus.PENDING,
            source_submission_id=new_sid,
            source_commit_sha="2e" * 20,
            submitted_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    store.objects[submission_key(row.id, new_sid)] = b"# v2 git bundle\nB"
    assert (
        await client.post(
            f"/v1/admin/apps/{row.id}/approve",
            json={"submissionId": str(new_sid)},
            headers=headers,
        )
    ).status_code == 200

    listed = await client.get("/v1/admin/apps?status=approved", headers=headers)
    projected = next(a for a in listed.json()["apps"] if a["appId"] == str(row.id))
    assert projected["redeployNeeded"] is True  # approved pin != deployed marker


async def test_disable_enable_do_not_disturb_the_deployed_marker(client, db_session) -> None:
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)
    await client.post(f"/v1/admin/apps/{app.id}/mark-deployed", headers=headers)
    await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_submission_id == fresh.approved_submission_id
    assert fresh.deployed_at is not None


# --- audit (ADR-0005) -------------------------------------------------------------


async def test_governance_actions_are_audited_with_artifact_detail(
    client, app, db_session
) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    _stage_bundle(store, row)
    headers = await _admin(db_session)
    await client.post(f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers)
    events = await client.get(f"/v1/admin/apps/{row.id}/audit", headers=headers)
    approve_event = next(e for e in events.json()["events"] if e["action"] == "approve")
    # R14: the audit detail identifies the artifact (submission id + commit SHA).
    assert approve_event["detail"]["submissionId"] == str(row.source_submission_id)
    assert approve_event["detail"]["commitSha"] == _SHA


async def _audited_actions(db_session, app_id) -> list[str]:
    rows = await db_session.execute(
        sa.select(AuditLog.action).where(AuditLog.resource_id == str(app_id))
    )
    return list(rows.scalars().all())


async def test_patch_name_and_login_required_are_both_audited(client, db_session) -> None:
    # ADR-0005: audit every gated action. A rename went unaudited (Express parity), so an
    # admin could relabel any app with no accountability row.
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    renamed = await client.patch(
        f"/v1/admin/apps/{app.id}", json={"name": "Renamed"}, headers=headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed"
    await client.patch(f"/v1/admin/apps/{app.id}", json={"loginRequired": True}, headers=headers)

    actions = await _audited_actions(db_session, app.id)
    assert "config:name" in actions
    assert "config:loginRequired" in actions


async def test_patch_no_op_rename_is_not_audited(client, db_session) -> None:
    # Only a real change earns an audit row, matching the loginRequired flip check.
    app = await _app(db_session, name="Same", **_pending())
    headers = await _admin(db_session)
    await client.patch(f"/v1/admin/apps/{app.id}", json={"name": "Same"}, headers=headers)
    assert "config:name" not in await _audited_actions(db_session, app.id)


async def test_patch_over_long_name_is_422_not_silently_truncated(client, db_session) -> None:
    app = await _app(db_session, name="Original", **_pending())
    headers = await _admin(db_session)
    resp = await client.patch(
        f"/v1/admin/apps/{app.id}", json={"name": "x" * 121}, headers=headers
    )
    assert resp.status_code == 422  # was a silent chop to 120 chars
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.name == "Original"


async def test_reject_over_long_note_is_422_not_silently_truncated(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/reject", json={"note": "x" * 1001}, headers=headers
    )
    assert resp.status_code == 422  # was a silent chop to 1000 chars the admin never saw
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING  # not rejected
    assert fresh.rejection_note is None


# --- hard-delete -----------------------------------------------------------------


async def test_hard_delete_purges_everything(client, db_session, app) -> None:
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    db_session.add(DataRecord(app_id=row.id, collection="c", data={"x": 1}, bytes=8))
    # The C4 snapshot bundle is the app's object-store artifact nuke_app must sweep (the per-app
    # file model was retired, so there are no app-file blobs).
    store.objects[snapshot_key(row.id)] = b"bundle-bytes"
    await db_session.flush()
    headers = await _admin(db_session)

    resp = await client.delete(f"/v1/admin/apps/{row.id}", headers=headers)
    assert resp.json() == {"ok": True}
    # Registry row + records gone (CASCADE); the snapshot blob swept.
    assert await db_session.get(AppRegistry, row.id) is None
    assert store.objects == {}
    audited = (
        await db_session.execute(
            sa.select(AuditLog.action).where(
                AuditLog.resource_id == str(row.id), AuditLog.action == "app:delete"
            )
        )
    ).scalar_one()
    assert audited == "app:delete"


async def test_nuke_app_sweeps_the_per_app_container(db_session) -> None:
    # nuke_app now receives the container store by injection (not a global singleton), so it
    # sweeps the app's per-app Blob container alongside the snapshot bundle. Drives the service
    # directly with a recording store to prove the container sweep fires.
    row = await _app(db_session, **_pending())
    await db_session.flush()
    store = FakeStorage()
    store.objects[snapshot_key(row.id)] = b"bundle-bytes"
    containers = _RecordingContainerStore()

    await nuke_app(db_session, store, row.id, containers)
    await db_session.flush()

    assert containers.deleted == [row.id]  # the per-app container was swept
    assert store.objects == {}  # the snapshot blob was swept
    assert await db_session.get(AppRegistry, row.id) is None  # registry row dropped


# --- durable single-use clear-data token ---------------------------------------


async def test_clear_data_token_is_single_use_and_durable(client, db_session, app) -> None:
    _wire_storage(app)
    row = await _app(db_session, **_pending())
    db_session.add(DataRecord(app_id=row.id, collection="c", data={"x": 1}, bytes=8))
    await db_session.flush()
    headers = await _admin(db_session)

    summary = await client.get(f"/v1/admin/apps/{row.id}/data-summary", headers=headers)
    token = summary.json()["confirmToken"]

    # The token is DB-backed (survives a stateless/multi-worker backend) — it exists as a row.
    persisted = (
        await db_session.execute(sa.select(ClearDataToken).where(ClearDataToken.token == token))
    ).scalar_one()
    assert persisted.app_id == row.id

    first = await client.post(
        f"/v1/admin/apps/{row.id}/clear-data", json={"confirmToken": token}, headers=headers
    )
    assert first.status_code == 200
    assert first.json()["removed"] == 1

    # Replay with the same token → rejected (single-use).
    replay = await client.post(
        f"/v1/admin/apps/{row.id}/clear-data", json={"confirmToken": token}, headers=headers
    )
    assert replay.status_code == 400


async def test_clear_data_token_is_app_bound(client, db_session, app) -> None:
    _wire_storage(app)
    app_a = await _app(db_session, **_pending())
    app_b = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    summary = await client.get(f"/v1/admin/apps/{app_a.id}/data-summary", headers=headers)
    token = summary.json()["confirmToken"]
    # A token minted for app A cannot clear app B.
    resp = await client.post(
        f"/v1/admin/apps/{app_b.id}/clear-data", json={"confirmToken": token}, headers=headers
    )
    assert resp.status_code == 400


async def test_expired_token_is_rejected(client, db_session, app) -> None:
    _wire_storage(app)
    row = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    summary = await client.get(f"/v1/admin/apps/{row.id}/data-summary", headers=headers)
    token = summary.json()["confirmToken"]
    # Force-expire the token in the DB.
    await db_session.execute(
        sa.update(ClearDataToken)
        .where(ClearDataToken.token == token)
        .values(expires_at=sa.func.now() - timedelta(minutes=5))
    )
    await db_session.flush()
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/clear-data", json={"confirmToken": token}, headers=headers
    )
    assert resp.status_code == 400
