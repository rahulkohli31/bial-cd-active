"""App lifecycle — provision / submit / status (APPROVAL U4, R18/R4).

Owner-scoped via the session cookie; appKey is a secure token; submit forks an
immutable copy of the app's bundle (blob FIRST, row second — D3), is atomic and
audited, and fails closed on every storage/lock ambiguity. Cross-user reads and
mutations fail closed (404)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.redis.keys import lock_key
from src.services.storage import StorageError, snapshot_key, submission_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

_SHA = "ab" * 20  # 40 lowercase hex chars
# The exact artifact shape `write_snapshot` ships: a raw v2 bundle (R5).
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fake-bytes"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


def _wire_storage(app, store: FakeStorage | None = None) -> FakeStorage:
    # `submit` takes the None-tolerant `storage_or_none_dependency` (it documents a 503), so
    # that — not `storage_dependency` — is the seam a fake has to be injected through.
    store = store or FakeStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    return store


class _ExplodingGetStorage(FakeStorage):
    """A store whose reads fail TRANSIENTLY (not not-found) — the D9 seam."""

    async def get(self, key):
        raise StorageError("transient blip", provider="fake", key=key)


class _ExplodingPutStorage(FakeStorage):
    """A store whose writes explode — the copy seam's unhappy-path twin
    (mocks-mask-composition-seams: a fake must be able to model failure)."""

    async def put(self, key, data, *, content_type=None, metadata=None):
        raise StorageError("put exploded", provider="fake", key=key)


def _submission_refs(app_row: AppRegistry) -> tuple[object, object, object]:
    return (app_row.source_submission_id, app_row.source_commit_sha, app_row.submitted_at)


async def _provision_app(client, db_session, user, headers) -> str:
    """Provision the user's app inside a fresh project (project-first); return the appId."""
    project = await ProjectFactory.create(db_session, user.id)
    prov = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(uuid.uuid4()), "projectId": str(project.id)},
        headers=headers,
    )
    assert prov.status_code == 201
    return prov.json()["appId"]


async def test_provision_mints_appkey_and_draft(client, db_session) -> None:
    user, headers = await _auth_user(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    resp = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(uuid.uuid4()), "projectId": str(project.id)},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"
    assert body["appKey"].startswith("bial_")
    assert body["loginRequired"] is False
    # A secure token, never a raw UUID (ADR-0006).
    assert uuid.UUID(body["appId"])  # appId is a UUID
    assert "bial_" not in body["appId"]


async def test_provision_without_project_is_422(client, db_session) -> None:
    # Project-first: every app lives in a caller-owned project — a provision naming
    # none is rejected at the schema boundary (there is no fallback project).
    user, headers = await _auth_user(db_session)
    resp = await client.post(
        "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=headers
    )
    assert resp.status_code == 422
    apps = (
        (await db_session.execute(sa.select(AppRegistry).where(AppRegistry.user_id == user.id)))
        .scalars()
        .all()
    )
    assert apps == []  # nothing provisioned


async def test_provision_reuses_the_single_project_app(client, db_session) -> None:
    # One app per project (KD-4): a second provision in the SAME project — even from a
    # DIFFERENT builder session — reuses that one app (no second row), and the app's
    # head/last-builder pointer advances to the new conversation.
    user, headers = await _auth_user(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv_a, conv_b = str(uuid.uuid4()), str(uuid.uuid4())
    first = await client.post(
        "/v1/apps/provision",
        json={"conversationId": conv_a, "projectId": str(project.id)},
        headers=headers,
    )
    second = await client.post(
        "/v1/apps/provision",
        json={"conversationId": conv_b, "projectId": str(project.id)},
        headers=headers,
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["appId"] == second.json()["appId"]  # reused, not a new row

    apps = (
        (
            await db_session.execute(
                sa.select(AppRegistry).where(AppRegistry.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(apps) == 1  # uq_app_registry_project holds
    assert apps[0].project_id == project.id
    assert str(apps[0].conversation_id) == conv_b  # head advanced to the latest session


async def test_provision_losing_race_to_project_delete_is_404_not_500(
    client, db_session, monkeypatch
) -> None:
    # Provision vs a concurrent project delete: the project passes `owned_project_or_404`
    # but is gone by the upsert INSERT, which then violates app_registry_project_id_fkey —
    # the loser must get the same non-leaking 404 a provision one second later would,
    # never a 500 (same race-technique as the conversations patch-vs-delete test).
    import src.api.v1.apps.router as apps_router
    from src.db.models.project import Project
    from src.services.projects import owned_project_or_404 as real_load

    user, headers = await _auth_user(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    async def _load_then_lose_race(db, user_id, project_id):
        owned = await real_load(db, user_id, project_id)
        # The concurrent DELETE lands between the ownership check and the INSERT.
        await db.execute(
            sa.delete(Project).where(Project.id == owned.id),
            execution_options={"synchronize_session": False},
        )
        return owned

    monkeypatch.setattr(apps_router, "owned_project_or_404", _load_then_lose_race)
    resp = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(uuid.uuid4()), "projectId": str(project.id)},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Project not found."}}


async def test_provision_cross_user_project_is_404(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    other = await UserFactory.create(db_session)
    stranger_project = await ProjectFactory.create(db_session, other.id)
    resp = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(uuid.uuid4()), "projectId": str(stranger_project.id)},
        headers=headers,
    )
    assert resp.status_code == 404
    # Nothing was written into the stranger's project.
    apps = (
        (
            await db_session.execute(
                sa.select(AppRegistry).where(AppRegistry.project_id == stranger_project.id)
            )
        )
        .scalars()
        .all()
    )
    assert apps == []


async def test_submit_copies_bundle_and_moves_draft_to_pending(client, app, db_session) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == app_id
    assert body["status"] == "pending"
    assert body["commitSha"] == _SHA

    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row is not None
    assert row.status is AppStatus.PENDING
    assert str(row.source_submission_id) == body["submissionId"]
    assert row.source_commit_sha == _SHA
    assert row.submitted_at is not None

    # R1/R5: the immutable copy exists at the derived key, byte-identical to the
    # snapshot, and is a RAW bundle — never base64.
    copied = store.objects[submission_key(row.id, row.source_submission_id)]
    assert copied == _BUNDLE
    assert copied.startswith(b"# v")


async def test_submit_writes_an_audit_row_with_artifact_detail(client, app, db_session) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    submitted = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)

    row = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_type == "app", AuditLog.resource_id == app_id
            )
        )
    ).scalar_one()
    assert row.action == "submit"
    assert row.actor_id == user.id
    # R14: the audit detail identifies the artifact.
    assert row.detail == {
        "submissionId": submitted.json()["submissionId"],
        "commitSha": _SHA,
    }


async def test_submit_without_a_bundle_is_409_and_writes_nothing(client, app, db_session) -> None:
    # R9: no snapshot blob → refuse with the "go build first" intent, and no
    # submission blob or ref appears anywhere.
    store = _wire_storage(app)  # empty store
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "Nothing to submit — generate an app first."
    assert store.objects == {}
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


async def test_submit_on_transient_storage_error_is_503_not_409(client, app, db_session) -> None:
    # D9/R9: a storage blip must NOT masquerade as "you have nothing to submit" —
    # that message sends someone whose app is fully built off to rebuild it.
    _wire_storage(app, _ExplodingGetStorage())
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 503
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT  # nothing recorded


async def test_submit_corrupt_bundle_is_409_and_writes_nothing(client, app, db_session) -> None:
    # R3: the snapshot bytes fail the git-bundle gate → refuse before any copy.
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = b"not a bundle at all"

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 409
    assert "bundle" in resp.json()["error"]["message"]
    # Only the snapshot exists — no submission copy was written.
    assert list(store.objects) == [snapshot_key(uuid.UUID(app_id))]
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


async def test_submit_put_failure_records_no_ref(client, app, db_session) -> None:
    # The copy seam's unhappy-path twin (mocks-mask-composition-seams): when the
    # PUT explodes, the row must be untouched — no ref pointing at a blob that
    # never landed (the exact failure D3's ordering exists to prevent).
    store = _ExplodingPutStorage()
    _wire_storage(app, store)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 503
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


async def test_submit_refused_while_build_session_holds_lock(
    client, app, db_session, fake_redis
) -> None:
    # D8: a live build session means the snapshot can be overwritten mid-copy (or
    # the copy captures the PREVIOUS build) — submit refuses while the lock is held.
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    await fake_redis.set(lock_key(user.id), "holder-token")

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 409
    assert "build session" in resp.json()["error"]["message"]
    # No copy, no row change.
    assert list(store.objects) == [snapshot_key(uuid.UUID(app_id))]
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT


async def test_submit_on_redis_error_during_lock_check_is_503(
    client, app, db_session, fake_redis, monkeypatch
) -> None:
    # D8/fail-first: a Redis ERROR (as opposed to a HELD lock) during the build-session
    # lock check is real ambiguity — submit fails closed (503), copies no bundle, and
    # leaves the row untouched. (A held lock is 409; a MISSING Redis proceeds.) Proves the
    # 503 mapping in `_refuse_while_build_session_live` is reachable end-to-end at the HTTP
    # layer, not just documented in OpenAPI.
    import src.services.build_sessions as build_sessions

    async def _boom(_redis, _user_uuid):
        raise RedisError("redis blip")

    # The router does `from src.services.build_sessions import lock_is_held` per call, so
    # patching the attribute on that package module reaches the name it resolves.
    monkeypatch.setattr(build_sessions, "lock_is_held", _boom)

    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 503
    # No submission copy written; only the snapshot remains; the row stays draft.
    assert list(store.objects) == [snapshot_key(uuid.UUID(app_id))]
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


async def test_submit_stays_coarse_and_refuses_while_any_app_of_this_user_builds(
    client, app, db_session, fake_redis
) -> None:
    # PINS THE U8 LIFT. `_refuse_while_build_session_live` moved to the shared
    # `api/v1/live_build.py` helper, which grew an OPTIONAL `app_id` narrowing for the
    # project-delete call site. Submit deliberately does NOT pass it: its refusal is per-user
    # as shipped, and this pins that the narrowing was not applied here as a side effect of
    # the lift. A live build for a DIFFERENT app still refuses this submit.
    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, registry_key

    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    # The lock is held by a session building some OTHER app entirely.
    await fake_redis.set(lock_key(user.id), "holder-token")
    await fake_redis.hset(
        registry_key(user.id), REGISTRY_FIELD_APP_NAME, app_name_for(uuid.uuid7())
    )

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)

    assert resp.status_code == 409
    assert "before submitting" in resp.json()["error"]["message"]
    assert list(store.objects) == [snapshot_key(uuid.UUID(app_id))]


async def test_submit_proceeds_after_lock_released(client, app, db_session, fake_redis) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    # No lock key set — the session ended and released it.
    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 200


async def test_submit_from_disabled_is_409(client, app, db_session) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(status=AppStatus.DISABLED)
    )
    await db_session.flush()

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 409
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    await db_session.refresh(row)
    assert row.status is AppStatus.DISABLED


async def test_resubmit_mints_fresh_id_and_retains_prior_blob(client, app, db_session) -> None:
    # R2: every submission is retained; ids are never reused.
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    first = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    second = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    sid_a, sid_b = first.json()["submissionId"], second.json()["submissionId"]
    assert sid_a != sid_b
    # BOTH immutable copies still exist.
    assert submission_key(uuid.UUID(app_id), uuid.UUID(sid_a)) in store.objects
    assert submission_key(uuid.UUID(app_id), uuid.UUID(sid_b)) in store.objects


async def test_resubmit_from_approved_keeps_the_approved_pin(client, app, db_session) -> None:
    # R6: a re-submit moves approved→pending but the approved pin survives (the
    # prior approved artifact keeps serving until re-approval).
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    pinned = uuid.uuid4()
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(
            status=AppStatus.APPROVED,
            approved_submission_id=pinned,
            approved_commit_sha=_SHA,
        )
    )
    await db_session.flush()

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    await db_session.refresh(row)
    assert row.approved_submission_id == pinned  # untouched


async def test_submit_clears_a_stale_rejection_note(client, app, db_session) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(status=AppStatus.REJECTED, rejection_note="fix the header")
    )
    await db_session.flush()

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 200
    status_read = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    assert status_read.json()["rejectionNote"] is None


async def test_submit_cross_user_is_404_and_writes_nothing(client, app, db_session) -> None:
    # R12: a stranger submitting another user's app gets the same non-leaking 404
    # the reads return, and neither a blob nor a row change happens.
    store = _wire_storage(app)
    owner, owner_headers = await _auth_user(db_session, email="subowner@rvaiglobal.com")
    app_id = await _provision_app(client, db_session, owner, owner_headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    _, stranger_headers = await _auth_user(db_session, email="substranger@rvaiglobal.com")
    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=stranger_headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "App not found."}}
    assert list(store.objects) == [snapshot_key(uuid.UUID(app_id))]
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT


async def test_status_surfaces_submission_metadata(client, app, db_session) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    submitted = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)

    resp = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    body = resp.json()
    assert body["submissionId"] == submitted.json()["submissionId"]
    assert body["commitSha"] == _SHA
    assert body["submittedAt"] is not None


async def test_status_surfaces_the_deployed_url_and_marker(client, app, db_session) -> None:
    """ "Your app is live" (R5), owner side: once an admin records the deploy, the
    owner's status read carries `deployedAt` + `deployedUrl` — the SubmitControl's
    Live link. Read-only: the citizen route never writes these, it projects them."""
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session, email="liveowner@rvaiglobal.com")
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    # Before any deploy the marker is simply absent — no Live link, no timestamp.
    fresh_read = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    assert fresh_read.json()["deployedAt"] is None
    assert fresh_read.json()["deployedUrl"] is None

    # The admin's mark-deployed, simulated at the row (the endpoint itself is proven
    # in the admin governance suite — this asserts the OWNER's projection of it).
    live_url = "https://apps.bial.example.com/gate-ops"
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(deployed_at=sa.func.now(), deployed_url=live_url)
    )
    await db_session.flush()

    body = (await client.get(f"/v1/apps/{app_id}/status", headers=headers)).json()
    assert body["deployedUrl"] == live_url
    assert body["deployedAt"] is not None


async def test_deployed_url_does_not_leak_across_users(client, db_session) -> None:
    # The Live link is owner-scoped like every other field on this read (ADR-0004):
    # a stranger gets the non-leaking 404, never a peek at where the app lives.
    owner, owner_headers = await _auth_user(db_session, email="liveowner2@rvaiglobal.com")
    app_id = await _provision_app(client, db_session, owner, owner_headers)
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(deployed_at=sa.func.now(), deployed_url="https://apps.bial.example.com/secret-ops")
    )
    await db_session.flush()

    _, stranger_headers = await _auth_user(db_session, email="livestranger@rvaiglobal.com")
    denied = await client.get(f"/v1/apps/{app_id}/status", headers=stranger_headers)
    assert denied.status_code == 404
    assert denied.json() == {"error": {"message": "App not found."}}


async def test_status_read_is_owner_scoped(client, db_session) -> None:
    owner, owner_headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    app_id = await _provision_app(client, db_session, owner, owner_headers)

    # The owner reads status fine.
    ok = await client.get(f"/v1/apps/{app_id}/status", headers=owner_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "draft"

    # A different user gets the same non-leaking 404 `submit` returns — indistinguishable from
    # an app that simply doesn't exist (the `200 {status:null}` shim is gone).
    _, other_headers = await _auth_user(db_session, email="other@rvaiglobal.com")
    denied = await client.get(f"/v1/apps/{app_id}/status", headers=other_headers)
    assert denied.status_code == 404
    assert denied.json() == {"error": {"message": "App not found."}}


async def test_status_unknown_app_is_404(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    resp = await client.get(f"/v1/apps/{uuid.uuid4()}/status", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "App not found."}}


async def test_source_returns_the_projects_durable_code(client, db_session) -> None:
    # The app's code is the project's ONE durable source (KD-9). Any builder chat in the
    # project — not just the one that first generated it — must be able to READ it back to
    # render the preview, so this endpoint serves `current_code.current.source` by appId.
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    app = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert app is not None
    source = "export default function PreviewApp(){ return <div>hi</div>; }"
    app.current_code = {"current": {"source": source, "entry": "PreviewApp"}}
    await db_session.commit()

    resp = await client.get(f"/v1/apps/{app_id}/source", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "appId": app_id,
        "source": source,
        "entry": "PreviewApp",
    }


async def test_source_is_empty_before_any_code_lands(client, db_session) -> None:
    # A freshly provisioned app has no code yet: the read is a clean empty string, not a 404
    # and not null — the SPA treats "" as "nothing to render" (LivePreview's empty state).
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    resp = await client.get(f"/v1/apps/{app_id}/source", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"appId": app_id, "source": "", "entry": "PreviewApp"}


async def test_source_read_is_owner_scoped(client, db_session) -> None:
    owner, owner_headers = await _auth_user(db_session, email="srcowner@rvaiglobal.com")
    app_id = await _provision_app(client, db_session, owner, owner_headers)

    ok = await client.get(f"/v1/apps/{app_id}/source", headers=owner_headers)
    assert ok.status_code == 200

    # A stranger gets the same non-leaking 404 the sibling reads return — never another
    # user's app source (ADR-0004).
    _, other_headers = await _auth_user(db_session, email="srcother@rvaiglobal.com")
    denied = await client.get(f"/v1/apps/{app_id}/source", headers=other_headers)
    assert denied.status_code == 404
    assert denied.json() == {"error": {"message": "App not found."}}


async def test_source_unknown_app_is_404(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    resp = await client.get(f"/v1/apps/{uuid.uuid4()}/source", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "App not found."}}


async def test_submit_unknown_app_is_404(client, app, db_session) -> None:
    _wire_storage(app)  # the Storage dependency resolves before the 404 check
    _, headers = await _auth_user(db_session)
    resp = await client.post(f"/v1/apps/{uuid.uuid4()}/submit", headers=headers)
    assert resp.status_code == 404


async def test_lifecycle_requires_authentication(client) -> None:
    resp = await client.post("/v1/apps/provision", json={"conversationId": str(uuid.uuid4())})
    assert resp.status_code == 401


def test_lifecycle_routes_document_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    # `.500` is inherited from the v1-router default; the rest are declared per route.
    assert {"401", "409", "500"} <= set(paths["/v1/apps/provision"]["post"]["responses"])
    submit = set(paths["/v1/apps/{app_id}/submit"]["post"]["responses"])
    assert {"401", "404", "409", "503", "500"} <= submit
    assert {"401", "404", "500"} <= set(paths["/v1/apps/{app_id}/status"]["get"]["responses"])
    assert {"401", "404", "500"} <= set(paths["/v1/apps/{app_id}/source"]["get"]["responses"])


async def test_submit_is_503_not_500_when_storage_is_unconfigured(client, db_session) -> None:
    """Fixture-free store-off baseline (`.claude/rules/testing.md`) for the 503 submit ADVERTISES:
    with no store wired at all, `storage_or_none_dependency` resolves `get_storage()` ->
    StorageUnconfiguredError -> None, and the body maps None onto the same documented 503 a
    transient blip gets. The eager `Storage` dependency submit used to take raised at
    dependency-solve time — before the body, and before even the ownership 404 — so the client got
    an undocumented 500 in the catch-all's `{"detail": ...}` envelope, which the SPA cannot
    render. Deliberately wires NO storage: a fixture that binds one forecloses this branch."""
    from src.services.storage import accessor as _storage_accessor

    _storage_accessor._backend_singleton = None  # store off: no backend configured in .env.test
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["message"] == "Storage is temporarily unavailable. Please try again."
    assert "detail" not in body  # pin the ENVELOPE, not just the status
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT  # nothing recorded
    assert _submission_refs(row) == (None, None, None)
