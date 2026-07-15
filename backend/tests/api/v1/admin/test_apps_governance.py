"""Admin app-registry governance (U8, AE1/AE4, R27/R29): super-admin-only + audited,
the exact state machine, client-artifact approval (no server compile), and the durable
single-use clear-data token."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.clear_data_token import ClearDataToken
from src.db.models.data_record import DataRecord
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import snapshot_key
from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageNotFoundError
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_COMPILED = "var PreviewApp=()=>React.createElement('div',null,'x');"


class _DictStorage(ObjectStorage):
    def __init__(self) -> None:
        super().__init__(provider="fake")
        self.objects: dict[str, bytes] = {}

    async def put(self, key, data, *, content_type=None, metadata=None):
        self.objects[key] = data
        return ObjectMeta(
            key=key, size=len(data), content_type=content_type, etag=None, last_modified=None
        )

    async def get(self, key):
        if key not in self.objects:
            raise StorageNotFoundError("missing", provider="fake", key=key)
        return self.objects[key]

    async def head(self, key):
        return None

    async def delete(self, key):
        self.objects.pop(key, None)

    async def list(self, prefix, *, page_size=1000, token=None):
        return ListPage(keys=(), next_token=None)

    async def _signed_read_url_impl(self, key, *, expires_in):
        return f"https://fake.local/{key}"

    async def aclose(self):
        return None


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
    return {
        "status": AppStatus.PENDING,
        "source_snapshot": {"compiled": _COMPILED, "src": "x", "entry": "PreviewApp"},
        **extra,
    }


# --- AE1: super-admin gating ---------------------------------------------------


async def test_citizen_is_forbidden(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _citizen(db_session)
    assert (await client.get("/v1/admin/apps", headers=headers)).status_code == 403
    assert (
        await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
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
    # A representative governance route lists its explicit 4xx plus the inherited
    # dependency 401/403 (DetailBody) and the v1-router 500 default.
    paths = create_app().openapi()["paths"]
    approve = set(paths["/v1/admin/apps/{app_id}/approve"]["post"]["responses"])
    assert {"400", "404", "409", "401", "403", "500"} <= approve
    assert {"400", "401", "403", "500"} <= set(paths["/v1/admin/apps"]["get"]["responses"])


# --- AE4: approve stores the CLIENT artifact (no server compile) ----------------


async def test_approve_stores_client_artifact(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"appId": str(app.id), "status": "approved"}

    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.APPROVED
    # The approved snapshot carries the client-submitted compiled artifact verbatim, plus the
    # real `src`/`entry` that `submit` wrote — never a re-defaulted ''/'PreviewApp'.
    assert fresh.approved_snapshot["compiled"] == _COMPILED
    assert fresh.approved_snapshot["src"] == "x"
    assert fresh.approved_snapshot["entry"] == "PreviewApp"
    assert fresh.approved_by is not None


async def test_approve_corrupt_snapshot_is_409_not_silently_defaulted(client, db_session) -> None:
    # `submit` is the sole writer and always sets src + entry beside compiled. A snapshot
    # missing them is corrupt: `approve` used to fill in ''/'PreviewApp' and promote it to the
    # approved artifact users actually run.
    app = await _app(db_session, status=AppStatus.PENDING, source_snapshot={"compiled": _COMPILED})
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
    assert resp.status_code == 409
    assert resp.json() == {"error": {"message": "This app has no valid submitted snapshot."}}

    fresh = await db_session.get(AppRegistry, app.id)
    await db_session.refresh(fresh)
    assert fresh.status is AppStatus.PENDING  # not promoted
    assert fresh.approved_snapshot is None


async def test_approve_requires_pending(client, db_session) -> None:
    app = await _app(db_session, status=AppStatus.DRAFT)
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
    assert resp.status_code == 409


async def test_approve_without_submitted_code_is_400(client, db_session) -> None:
    app = await _app(db_session, status=AppStatus.PENDING, source_snapshot=None)
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
    assert resp.status_code == 400


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


async def test_disable_then_enable(client, db_session) -> None:
    app = await _app(
        db_session, status=AppStatus.APPROVED, approved_snapshot={"compiled": _COMPILED}
    )
    headers = await _admin(db_session)
    dis = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert dis.json()["status"] == "disabled"
    en = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert en.json()["status"] == "approved"


async def test_disable_requires_approved(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/disable", headers=headers)
    assert resp.status_code == 409


async def test_enable_guard_rejects_non_disabled(client, db_session) -> None:
    # A pending app must not be promotable to approved via enable (compile-gate bypass).
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    resp = await client.post(f"/v1/admin/apps/{app.id}/enable", headers=headers)
    assert resp.status_code == 409


async def test_list_and_status_filter(client, db_session) -> None:
    await _app(db_session, **_pending())
    approved = await _app(
        db_session, status=AppStatus.APPROVED, approved_snapshot={"compiled": _COMPILED}
    )
    headers = await _admin(db_session)
    listed = await client.get("/v1/admin/apps?status=approved", headers=headers)
    ids = [a["appId"] for a in listed.json()["apps"]]
    assert str(approved.id) in ids
    # The projection never leaks the code blob / app key.
    row = next(a for a in listed.json()["apps"] if a["appId"] == str(approved.id))
    assert "appKey" not in row and "approvedSnapshot" not in row
    assert row["hasApprovedSnapshot"] is True


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


# --- audit + hard-delete -------------------------------------------------------


async def test_governance_actions_are_audited(client, db_session) -> None:
    app = await _app(db_session, **_pending())
    headers = await _admin(db_session)
    await client.post(f"/v1/admin/apps/{app.id}/approve", headers=headers)
    events = await client.get(f"/v1/admin/apps/{app.id}/audit", headers=headers)
    actions = [e["action"] for e in events.json()["events"]]
    assert "approve" in actions


async def test_hard_delete_purges_everything(client, db_session, app) -> None:
    store = _DictStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
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


# --- durable single-use clear-data token ---------------------------------------


async def test_clear_data_token_is_single_use_and_durable(client, db_session, app) -> None:
    store = _DictStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
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
    store = _DictStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
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
    store = _DictStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
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
