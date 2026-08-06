"""App lifecycle — submit / status (APPROVAL U4, R18/R4).

Owner-scoped via the session cookie; submit forks an immutable copy of the app's
bundle (blob FIRST, row second — D3), is atomic and audited, and fails closed on
every storage/lock ambiguity. Cross-user reads and mutations fail closed (404).

The app ROW is minted by `resolve_app_for_project` (the build session's path) — the
standalone `POST /apps/provision` endpoint was removed in U6, so `_provision_app`
below calls that service directly. Its own contract (reuse, cross-user 404, foreign
app 409, project-delete race) is tested at
`tests/services/build_sessions/test_appdata.py`; those endpoint-shaped duplicates
went away with the endpoint."""

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
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.redis.keys import lock_key
from src.services.storage import StorageError, snapshot_key, submission_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

_SHA = "ab" * 20  # 40 lowercase hex chars
# The exact artifact shape `write_snapshot` ships: a raw v2 bundle (R5).
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fake-bytes"

# V4 Part 1: submit's body is now required. A minimal all-No answer set — total weight 0,
# so `notes` stays optional — for the tests below that don't care about the questionnaire
# itself.
#
# V4 Part 2: submit now decides approve/reject FROM this score, with no human step. Weight
# 0 is well below `AUTO_APPROVE_AT` (50), so this default answer set AUTO-REJECTS every
# submission that uses it as-is — that's intentional and exercised directly below, and it's
# why tests that only care about mechanics OTHER than the decision (bundle copy, storage
# errors, lock refusal, ownership) never assert a specific outcome status; they either
# short-circuit before the decision is made, or don't inspect `status` at all.
_ANSWERS = {
    "credentialsSecrets": False,
    "healthData": False,
    "personalInformation": False,
    "financialData": False,
    "confidentialBusinessData": False,
    "publicData": False,
    "notes": None,
}
_SUBMIT_BODY = {"answers": _ANSWERS}

# A score comfortably at/above AUTO_APPROVE_AT (50): Credentials/Secrets (40) + Financial
# Data (20) = 60. Also crosses the notes-required threshold (25), so `notes` is supplied.
_HEAVY_ANSWERS = {
    **_ANSWERS,
    "credentialsSecrets": True,
    "financialData": True,
    "notes": "Reviewed internally before submission.",
}
_HEAVY_SUBMIT_BODY = {"answers": _HEAVY_ANSWERS}


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
    """Mint the user's app inside a fresh project (project-first); return the appId. Commits,
    because the endpoints under test read through their own session."""
    project = await ProjectFactory.create(db_session, user.id)
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    return str(app_id)


async def test_submit_copies_bundle_and_auto_approves_at_or_above_threshold(
    client, app, db_session
) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_HEAVY_SUBMIT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == app_id
    assert body["status"] == "approved"
    assert body["commitSha"] == _SHA
    assert body["rejectionNote"] is None

    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row is not None
    assert row.status is AppStatus.APPROVED
    assert str(row.source_submission_id) == body["submissionId"]
    assert row.source_commit_sha == _SHA
    assert row.submitted_at is not None
    # V4 Part 2: pinned exactly like a human approve would, but no human did it.
    assert row.approved_submission_id == row.source_submission_id
    assert row.approved_commit_sha == _SHA
    assert row.approved_by is None
    assert row.approved_at is not None
    assert row.decided_automatically is True

    # R1/R5: the immutable copy exists at the derived key, byte-identical to the
    # snapshot, and is a RAW bundle — never base64.
    copied = store.objects[submission_key(row.id, row.source_submission_id)]
    assert copied == _BUNDLE
    assert copied.startswith(b"# v")


async def test_submit_below_threshold_auto_rejects_with_a_plain_note(
    client, app, db_session
) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["rejectionNote"] is not None
    assert "50" in body["rejectionNote"]  # the threshold, in plain language

    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.REJECTED
    assert row.rejection_note == body["rejectionNote"]
    assert row.decided_automatically is True
    # Nothing to pin — a fresh app has never been approved.
    assert row.approved_submission_id is None
    assert row.approved_commit_sha is None
    assert row.approved_by is None


async def test_submit_writes_an_audit_row_with_artifact_and_decision_detail(
    client, app, db_session
) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    submitted = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)

    row = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_type == "app", AuditLog.resource_id == app_id
            )
        )
    ).scalar_one()
    assert row.action == "submit"
    assert row.actor_id == user.id
    # R14: the audit detail identifies the artifact. V4 Part 1 adds the
    # data-classification answers + weight; V4 Part 2 adds which route the auto-decision
    # took (all-No here == weight 0 == auto-rejected, below the 50 threshold).
    assert row.detail == {
        "submissionId": submitted.json()["submissionId"],
        "commitSha": _SHA,
        "dataClassification": _ANSWERS,
        "dataClassificationWeight": 0,
        "decision": "rejected",
        "threshold": 50,
    }


async def test_submit_missing_answers_is_422_and_writes_nothing(client, app, db_session) -> None:
    # V4: the body is now required — an incomplete/absent questionnaire is a 422 at
    # the Pydantic boundary, before the transaction, so nothing is ever persisted.
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    incomplete = dict(_ANSWERS)
    del incomplete["healthData"]
    resp = await client.post(
        f"/v1/apps/{app_id}/submit", headers=headers, json={"answers": incomplete}
    )
    assert resp.status_code == 422
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT
    assert row.data_classification is None


async def test_submit_high_weight_without_notes_is_422(client, app, db_session) -> None:
    # The soft gate's server-side half (task-sheet Part 1): Credentials/Secrets alone
    # (weight 40) crosses the 25-point notes-required threshold, so a blank/whitespace
    # `notes` is refused even though every OTHER field is a valid, complete answer.
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    heavy = {**_ANSWERS, "credentialsSecrets": True, "notes": "   "}
    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json={"answers": heavy})
    assert resp.status_code == 422
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT
    assert row.data_classification is None


async def test_submit_high_weight_with_notes_succeeds_and_round_trips(
    client, app, db_session
) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    heavy = {**_ANSWERS, "healthData": True, "notes": "De-identified patient counts only."}
    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json={"answers": heavy})
    assert resp.status_code == 200
    # Crosses the 25-point notes-required gate but NOT the 50-point auto-approve gate —
    # the two thresholds are independent (V4 Part 2); this submission still auto-rejects.
    assert resp.json()["status"] == "rejected"

    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.data_classification == {
        "credentials_secrets": False,
        "health_data": True,
        "personal_information": False,
        "financial_data": False,
        "confidential_business_data": False,
        "public_data": False,
        "notes": "De-identified patient counts only.",
    }

    status_read = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    body = status_read.json()["dataClassification"]
    assert body == heavy


async def test_status_data_classification_is_null_before_first_submit(client, db_session) -> None:
    # Distinguishes "never submitted" from "answered, all No" — both are legitimate,
    # and only the former is null.
    user, headers = await _auth_user(db_session, email="preclassify@rvaiglobal.com")
    app_id = await _provision_app(client, db_session, user, headers)
    resp = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    assert resp.json()["dataClassification"] is None


async def test_submit_without_a_bundle_is_409_and_writes_nothing(client, app, db_session) -> None:
    # R9: no snapshot blob → refuse with the "go build first" intent, and no
    # submission blob or ref appears anywhere.
    store = _wire_storage(app)  # empty store
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
    assert resp.status_code == 503
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT  # nothing recorded


async def test_submit_corrupt_bundle_is_409_and_writes_nothing(client, app, db_session) -> None:
    # R3: the snapshot bytes fail the git-bundle gate → refuse before any copy.
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = b"not a bundle at all"

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)

    assert resp.status_code == 409
    assert "before submitting" in resp.json()["error"]["message"]
    assert list(store.objects) == [snapshot_key(uuid.UUID(app_id))]


async def test_submit_proceeds_after_lock_released(client, app, db_session, fake_redis) -> None:
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    # No lock key set — the session ended and released it.
    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
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

    first = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
    second = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
    assert first.status_code == 200 and second.status_code == 200
    sid_a, sid_b = first.json()["submissionId"], second.json()["submissionId"]
    assert sid_a != sid_b
    # BOTH immutable copies still exist.
    assert submission_key(uuid.UUID(app_id), uuid.UUID(sid_a)) in store.objects
    assert submission_key(uuid.UUID(app_id), uuid.UUID(sid_b)) in store.objects


async def test_resubmit_from_approved_with_low_score_auto_rejects_but_keeps_the_approved_pin(
    client, app, db_session
) -> None:
    # V4 Part 2, R6: a re-submit that scores below the threshold moves approved→rejected
    # DIRECTLY (no PENDING stop) — but the PRIOR approved pin survives (the prior approved
    # artifact keeps serving/deployable until a future submission re-crosses the threshold).
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    await db_session.refresh(row)
    assert row.status is AppStatus.REJECTED
    assert row.approved_submission_id == pinned  # untouched — the reject does not clear it
    assert row.approved_commit_sha == _SHA
    assert row.decided_automatically is True


async def test_resubmit_from_rejected_with_high_score_auto_approves_and_pins_the_new_submission(
    client, app, db_session
) -> None:
    # The mirror case: a previously-rejected app whose NEXT submission crosses the
    # threshold moves rejected→approved directly, pinning the NEW submission (not
    # whatever — there was nothing — the old one pointed at).
    store = _wire_storage(app)
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(client, db_session, user, headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(status=AppStatus.REJECTED, rejection_note="Automatically rejected — score 0.")
    )
    await db_session.flush()

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_HEAVY_SUBMIT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["rejectionNote"] is None
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    await db_session.refresh(row)
    assert row.status is AppStatus.APPROVED
    assert str(row.approved_submission_id) == body["submissionId"]
    assert row.rejection_note is None


async def test_resubmit_with_high_score_clears_a_stale_rejection_note(
    client, app, db_session
) -> None:
    # An approving resubmit clears whatever note was on the row before — same as the
    # old human-reject-then-resubmit behavior, just reached via the score gate.
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_HEAVY_SUBMIT_BODY)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    status_read = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    assert status_read.json()["rejectionNote"] is None


async def test_resubmit_with_low_score_replaces_a_stale_note_with_the_new_auto_reject_copy(
    client, app, db_session
) -> None:
    # V4 Part 2: a resubmit that AGAIN scores below threshold does not just clear the
    # old note — it's replaced by the FRESH auto-reject copy for the new submission
    # (the old contract of "always cleared to None" is gone; a submit can produce one).
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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    status_read = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    note = status_read.json()["rejectionNote"]
    assert note is not None
    assert note != "fix the header"
    assert "50" in note


async def test_submit_cross_user_is_404_and_writes_nothing(client, app, db_session) -> None:
    # R12: a stranger submitting another user's app gets the same non-leaking 404
    # the reads return, and neither a blob nor a row change happens.
    store = _wire_storage(app)
    owner, owner_headers = await _auth_user(db_session, email="subowner@rvaiglobal.com")
    app_id = await _provision_app(client, db_session, owner, owner_headers)
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

    _, stranger_headers = await _auth_user(db_session, email="substranger@rvaiglobal.com")
    resp = await client.post(
        f"/v1/apps/{app_id}/submit", headers=stranger_headers, json=_SUBMIT_BODY
    )
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
    submitted = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)

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


async def test_submit_unknown_app_is_404(client, app, db_session) -> None:
    _wire_storage(app)  # the Storage dependency resolves before the 404 check
    _, headers = await _auth_user(db_session)
    resp = await client.post(f"/v1/apps/{uuid.uuid4()}/submit", headers=headers, json=_SUBMIT_BODY)
    assert resp.status_code == 404


async def test_lifecycle_requires_authentication(client) -> None:
    resp = await client.get(f"/v1/apps/{uuid.uuid4()}/status")
    assert resp.status_code == 401


def test_lifecycle_routes_document_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    # `.500` is inherited from the v1-router default; the rest are declared per route.
    submit = set(paths["/v1/apps/{app_id}/submit"]["post"]["responses"])
    assert {"401", "404", "409", "503", "500"} <= submit
    assert {"401", "404", "500"} <= set(paths["/v1/apps/{app_id}/status"]["get"]["responses"])
    # The retired endpoints are gone from the schema entirely (U6) — a request to one is a
    # 404 from the router, never a 500 from a half-removed handler.
    assert "/v1/apps/provision" not in paths
    assert "/v1/apps/{app_id}/source" not in paths


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

    resp = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=_SUBMIT_BODY)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["message"] == "Storage is temporarily unavailable. Please try again."
    assert "detail" not in body  # pin the ENVELOPE, not just the status
    row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert row.status is AppStatus.DRAFT  # nothing recorded
    assert _submission_refs(row) == (None, None, None)
