"""Admin app-registry governance (APPROVAL U5–U7, AE1, R27/R29): super-admin-only +
audited, the exact state machine, the reviewed-submission-id approve guard (D5),
the artifact-exists pin check (R11), the audited bundle download (R15), the
mark-deployed marker (R17) and the deployed URL it records (PILOT R5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import MAX_DEPLOYED_URL, AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.main import create_app
from src.services.appserving.governance import nuke_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import AppContainerStore, StorageError, snapshot_key, submission_key
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds
_SHA = "1f" * 20  # 40 lowercase hex chars — the shape the bundle parser guarantees
# The address a runbook operator pastes at mark-deployed (R5). Already normalized
# (scheme + host + path, no trailing-slash ambiguity), so it round-trips byte-for-byte
# through pydantic's URL parse and the assertions can compare it verbatim.
_LIVE_URL = "https://apps.bial.example.com/gate-ops"


class _RecordingContainerStore(AppContainerStore):
    """A per-app container store double that records the containers it was asked to delete.
    Deliberately skips `AppContainerStore.__init__` (no Azure config) — the sweep only calls
    `delete_container`, so the un-set `_config` is never touched."""

    def __init__(self) -> None:  # noqa: D107 — no super().__init__ on purpose (see class docstring)
        self.deleted: list[uuid.UUID] = []

    async def delete_container(self, app_id: uuid.UUID) -> None:
        self.deleted.append(app_id)


class _ExplodingHeadStorage(FakeStorage):
    """A store whose `head()` fails TRANSIENTLY (not not-found) — approve's R11
    verify-before-pin seam must map a storage ERROR to 503 (ambiguity denies), never a
    409 (absent). Mirrors `_ExplodingGetStorage`/`_ExplodingPutStorage` in test_lifecycle."""

    async def head(self, key):
        raise StorageError("head blip", provider="fake", key=key)


class _ExplodingSignedUrlStorage(FakeStorage):
    """A store whose signed-URL mint explodes — bundle-url's fail-closed twin: a storage
    ERROR is 503, and no bearer URL (or audit row) is produced."""

    async def _signed_read_url_impl(self, key, *, expires_in):
        raise StorageError("sign blip", provider="fake", key=key)


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
    """One in-memory store behind BOTH storage seams this router uses. `approve` / `bundle-url`
    take the None-tolerant `storage_or_none_dependency` (they document a 503); `hard_delete`
    keeps the raising `storage_dependency`. Overriding both to the same instance keeps every
    test in this file blind to which seam its route happens to sit on."""
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
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


async def test_approve_storage_head_error_is_503_no_pin_no_audit(client, app, db_session) -> None:
    # R11 fail-closed: a storage ERROR on the verify-before-pin head-check is ambiguity,
    # NOT absence — 503 (not 409), nothing pinned, and no approve audit row written.
    store = _ExplodingHeadStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    row = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
    )
    assert resp.status_code == 503
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING
    assert fresh.approved_submission_id is None  # nothing pinned
    rows = (
        (
            await db_session.execute(
                sa.select(AuditLog).where(
                    AuditLog.resource_id == str(row.id), AuditLog.action == "approve"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # fail-closed before any side effect


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


async def test_list_never_surfaces_the_owner_data_classification_answers(
    client, db_session
) -> None:
    # V4: the questionnaire is owner-facing ONLY (`AppStatusResponse`) — the admin
    # projection must never carry it, even though the row itself has it.
    pending = await _app(
        db_session,
        **_pending(),
        data_classification={
            "credentials_secrets": True,
            "health_data": False,
            "personal_information": False,
            "financial_data": False,
            "confidential_business_data": False,
            "public_data": False,
            "notes": "should never leave the owner-facing endpoint",
        },
    )
    headers = await _admin(db_session)
    listed = await client.get("/v1/admin/apps?status=pending", headers=headers)
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(pending.id))
    assert "dataClassification" not in row


async def test_list_sources_the_display_name_from_the_owning_project(client, db_session) -> None:
    # F5 (#48): app_registry has no name column; the admin registry shows the OWNING PROJECT's
    # name (never the old "(untitled)"), for both a pending and a non-pending app.
    owner = await UserFactory.create(db_session)
    pending_project = await ProjectFactory.create(db_session, owner.id, name="Acme Expenses")
    pending = await AppRegistryFactory.create(
        db_session, user_id=owner.id, project_id=pending_project.id, **_pending()
    )
    approved_project = await ProjectFactory.create(db_session, owner.id, name="Gate Roster")
    approved = await AppRegistryFactory.create(
        db_session, user_id=owner.id, project_id=approved_project.id, **_approved()
    )
    headers = await _admin(db_session)

    by_id = {
        a["appId"]: a for a in (await client.get("/v1/admin/apps", headers=headers)).json()["apps"]
    }
    assert by_id[str(pending.id)]["name"] == "Acme Expenses"
    assert by_id[str(approved.id)]["name"] == "Gate Roster"


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


async def test_bundle_url_storage_error_is_503_and_unaudited(client, app, db_session) -> None:
    # Fail-closed twin of the 409 case: a storage ERROR while minting the signed URL is
    # 503 (ambiguity denies), and no bearer credential or audit row is produced.
    store = _ExplodingSignedUrlStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    row = await _app(db_session, **_pending())  # has a submission to sign
    headers = await _admin(db_session)
    resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)
    assert resp.status_code == 503
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
    assert rows == []  # no audit row for a pull that never produced a URL


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


# --- deployed URL (R5): the address the runbook operator pastes ----------------------


async def test_mark_deployed_records_the_url_and_projects_it(client, db_session) -> None:
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)

    resp = await client.post(
        f"/v1/admin/apps/{app.id}/mark-deployed",
        json={"deployedUrl": _LIVE_URL},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["deployedUrl"] == _LIVE_URL

    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_url == _LIVE_URL

    # The admin queue surfaces it (the SPA's prompt default on the next re-mark).
    listed = await client.get("/v1/admin/apps?status=approved", headers=headers)
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(app.id))
    assert row["deployedUrl"] == _LIVE_URL

    # The URL is the app's public address, not a credential — it belongs in the trail.
    audit = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_id == str(app.id), AuditLog.action == "mark-deployed"
            )
        )
    ).scalar_one()
    assert audit.detail["deployedUrl"] == _LIVE_URL


async def test_mark_deployed_without_a_url_still_marks(client, db_session) -> None:
    # Back-compat, twice over: the endpoint shipped bodiless and the SPA posts a bare
    # `{}`. Both must still stamp the marker and simply leave `deployed_url` unset.
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)

    resp = await client.post(f"/v1/admin/apps/{app.id}/mark-deployed", json={}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["deployedUrl"] is None

    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_at is not None  # the marker landed…
    assert fresh.deployed_url is None  # …with no address to show


async def test_a_bare_remark_keeps_the_recorded_url(client, db_session) -> None:
    # A re-deploy of the same app keeps the same address: omitting `deployedUrl` means
    # "leave it alone", NOT "blank it". Getting this wrong would silently kill the Live
    # link the owner is already using, on the most routine admin action there is.
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)
    await client.post(
        f"/v1/admin/apps/{app.id}/mark-deployed", json={"deployedUrl": _LIVE_URL}, headers=headers
    )

    remark = await client.post(f"/v1/admin/apps/{app.id}/mark-deployed", json={}, headers=headers)
    assert remark.status_code == 200
    assert remark.json()["deployedUrl"] == _LIVE_URL

    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_url == _LIVE_URL

    # …and a NEW url is simply passed: re-recording overwrites.
    moved = "https://apps.bial.example.com/gate-ops-v2"
    await client.post(
        f"/v1/admin/apps/{app.id}/mark-deployed", json={"deployedUrl": moved}, headers=headers
    )
    await db_session.refresh(fresh)
    assert fresh.deployed_url == moved


async def test_mark_deployed_rejects_a_non_https_or_junk_url(client, db_session) -> None:
    """The boundary parse (422) is the whole validation story — the recorded URL becomes
    a link the OWNER clicks, so plaintext http, a `javascript:` payload and free text all
    die here rather than reaching a user. Each rejection writes nothing at all."""
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)

    for bad in ("http://apps.bial.example.com/gate-ops", "javascript:alert(1)", "not-a-url", ""):
        resp = await client.post(
            f"/v1/admin/apps/{app.id}/mark-deployed", json={"deployedUrl": bad}, headers=headers
        )
        assert resp.status_code == 422, bad

    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_url is None
    assert fresh.deployed_at is None  # a rejected body never stamps the marker either


async def test_mark_deployed_bounds_the_url_pydantic_normalized_not_the_raw_input(
    client, db_session
) -> None:
    """The overflow the input-length constraint cannot see: pydantic NORMALIZES a path-less URL
    by appending `/`, so a 2083-char one passed `UrlConstraints(max_length=…)` and then
    serialized to 2084 — one over `varchar(2083)`, i.e. an uncaught asyncpg error surfacing as a
    500 where the admin deserves a 422. The bound belongs on the value that reaches the column.
    """
    app = await _app(db_session, **_approved())
    headers = await _admin(db_session)

    # Path-less and EXACTLY at the boundary going in — 2084 coming out.
    host_only = "https://" + "a" * (MAX_DEPLOYED_URL - len("https://"))
    assert len(host_only) == MAX_DEPLOYED_URL
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/mark-deployed",
        json={"deployedUrl": host_only},
        headers=headers,
    )
    assert resp.status_code == 422  # a rejection, not a database error
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_url is None
    assert fresh.deployed_at is None  # the 422 stamped nothing

    # One char shorter: normalizes to exactly MAX_DEPLOYED_URL and therefore still fits — the
    # boundary is honoured, not merely avoided.
    fits = host_only[:-1]
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/mark-deployed", json={"deployedUrl": fits}, headers=headers
    )
    assert resp.status_code == 200
    assert len(resp.json()["deployedUrl"]) == MAX_DEPLOYED_URL
    await db_session.refresh(fresh)
    assert fresh.deployed_url == fits + "/"


async def test_citizen_cannot_record_a_deployed_url(client, db_session) -> None:
    # The gate outranks the body: a valid URL from a non-superadmin is still 403, and
    # the 403 must come with nothing written (RBAC at the API, never the frontend).
    app = await _app(db_session, **_approved())
    headers = await _citizen(db_session)
    resp = await client.post(
        f"/v1/admin/apps/{app.id}/mark-deployed", json={"deployedUrl": _LIVE_URL}, headers=headers
    )
    assert resp.status_code == 403
    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.deployed_url is None
    assert fresh.deployed_at is None


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


async def test_patch_login_required_is_audited(client, db_session) -> None:
    # ADR-0005: audit every gated action. The login-required gate is the only admin-patchable
    # field now that the app display name is sourced from the owning project (#48).
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    await client.patch(f"/v1/admin/apps/{app.id}", json={"loginRequired": True}, headers=headers)
    assert "config:loginRequired" in await _audited_actions(db_session, app.id)


async def test_patch_ignores_a_name_key_and_does_not_audit_it(client, db_session) -> None:
    # The app name is project-sourced (#48); `PatchAppRequest` no longer carries `name`, and
    # `CamelModel` ignores unknown keys — so a stray `{"name": ...}` is silently dropped (200,
    # not 422) and writes no `config:name` audit row. The response name stays the project's.
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.patch(
        f"/v1/admin/apps/{app.id}", json={"name": "Ignored"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Project"  # project-sourced, not the ignored key
    assert "config:name" not in await _audited_actions(db_session, app.id)


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
    # The C4 snapshot bundle is the app's object-store artifact nuke_app must sweep (the per-app
    # file model and the shared data plane were both retired, so nothing else is left to sweep).
    store.objects[snapshot_key(row.id)] = b"bundle-bytes"
    await db_session.flush()
    headers = await _admin(db_session)

    resp = await client.delete(f"/v1/admin/apps/{row.id}", headers=headers)
    assert resp.json() == {"ok": True}
    # Registry row gone; the snapshot blob swept.
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


async def test_hard_delete_sweeps_every_retained_submission(client, db_session, app) -> None:
    # R23: submissions are retained forever — until the app is hard-deleted, at which
    # point the whole submissions/{app_id}/ prefix goes with it. Another app's
    # submissions are untouched (the prefix is app-scoped).
    store = _wire_storage(app)
    row = await _app(db_session, **_pending())
    bystander = await _app(db_session, **_pending())
    _stage_bundle(store, bystander)
    assert bystander.source_submission_id is not None
    bystander_key = submission_key(bystander.id, bystander.source_submission_id)
    store.objects[snapshot_key(row.id)] = b"snapshot"
    for sid in (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()):
        store.objects[submission_key(row.id, sid)] = b"# v2 git bundle\nretained"
    await db_session.flush()
    headers = await _admin(db_session)

    resp = await client.delete(f"/v1/admin/apps/{row.id}", headers=headers)
    assert resp.json() == {"ok": True}
    # Snapshot + all three submissions swept; the bystander's submission survives.
    assert set(store.objects) == {bystander_key}


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


# --- The storage-off contract (FIX 9) ------------------------------------------


@pytest.mark.parametrize("route", ["approve", "bundle-url"])
async def test_storage_route_is_503_not_500_when_storage_is_unconfigured(
    client, db_session, route: str
) -> None:
    """Fixture-free store-off baseline (`.claude/rules/testing.md`) for the two governance routes
    that ADVERTISE a 503: with no store wired at all, `storage_or_none_dependency` resolves
    `get_storage()` -> StorageUnconfiguredError -> None, and each body maps None onto the
    documented 503. The eager `Storage` dependency they used to take raised at dependency-solve
    time — before the route body, and before even the 404/409 guards above it — so the client got
    an undocumented 500 in the catch-all's `{"detail": ...}` envelope instead. Deliberately
    fixture-free: any fixture that binds a store makes this branch unreachable BY CONSTRUCTION."""
    from src.services.storage import accessor as _storage_accessor

    _storage_accessor._backend_singleton = None  # store off: no backend configured in .env.test
    row = await _app(db_session, **_pending())
    headers = await _admin(db_session)

    if route == "approve":
        resp = await client.post(
            f"/v1/admin/apps/{row.id}/approve", json=_approve_body(row), headers=headers
        )
    else:
        resp = await client.get(f"/v1/admin/apps/{row.id}/bundle-url", headers=headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["message"] == "Storage is temporarily unavailable. Please try again."
    assert "detail" not in body  # pin the ENVELOPE, not just the status
    # Fail-closed twin of the exploding-store cases: nothing pinned, nothing audited.
    fresh = await db_session.get(AppRegistry, row.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING
    assert fresh.approved_submission_id is None
    audited = (
        (await db_session.execute(sa.select(AuditLog).where(AuditLog.resource_id == str(row.id))))
        .scalars()
        .all()
    )
    assert audited == []
