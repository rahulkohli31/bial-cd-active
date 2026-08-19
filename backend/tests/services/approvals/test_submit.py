"""The submit-into-queue service (U8: R15a, R15b, ASM9, ASM18).

The behavioural suite for what used to be `POST /apps/{app_id}/submit` — those route
tests are flipped into route-is-gone guards (`tests/api/v1/apps/test_submit_retired.py`)
and the behaviour they proved is re-proved HERE, against the service the body became.
Ordering-critical properties carried over verbatim: blob FIRST, row second (D3); the
fail-closed bundle read (D9); the guarded UPDATE with the ownership predicate
(ADR-0004); the orphan-blob log when the guard refuses after the copy landed.

New under U8, proved here and nowhere else:

* PENDING is no longer a legal submit source (R15b) — the way out is withdrawal;
* the row write carries the LINEAGE and the DECLARATION (the queue item can never
  arrive without one — ASM18);
* the build-session guard is APP-scoped (the documented divergence): a live build on
  a DIFFERENT app of the same user no longer refuses this submit, a live build on
  THIS app still does.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from structlog.testing import capture_logs

from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.audit import AuditLog
from src.services.approvals.submit import submit_app_for_review
from src.services.storage import StorageError, snapshot_key, submission_key
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_SHA = "ab" * 20  # 40 lowercase hex chars
# The exact artifact shape `write_snapshot` ships: a raw v2 bundle (R5).
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fake-bytes"

# The shape the publish gate (U9) hands over — opaque to the service, so any dict
# proves the pass-through; this one mirrors the governance suite's fixture.
_DECLARATION: dict[str, Any] = {
    "citizen": {"personal_information": "no"},
    "review": {"personal_information": "yes"},
    "differences": ["personal_information"],
    "explanation": "It only stores visitor gate numbers.",
}


class _ExplodingGetStorage(FakeStorage):
    """A store whose reads fail TRANSIENTLY (not not-found) — the D9 seam."""

    async def get(self, key):
        raise StorageError("transient blip", provider="fake", key=key)


class _ExplodingPutStorage(FakeStorage):
    """A store whose writes explode — the copy seam's unhappy-path twin
    (mocks-mask-composition-seams: a fake must be able to model failure)."""

    async def put(self, key, data, *, content_type=None, metadata=None):
        raise StorageError("put exploded", provider="fake", key=key)


async def _owned_app(db, **overrides):
    """A user and their app row, snapshot NOT yet staged."""
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id, **overrides)
    return user, app_row


def _staged(app_row: AppRegistry, store: FakeStorage | None = None) -> FakeStorage:
    store = store or FakeStorage()
    store.objects[snapshot_key(app_row.id)] = _BUNDLE
    return store


async def _submit(db, store, user, app_row, *, route=ApprovalRoute.SELF_PUBLISH):
    return await submit_app_for_review(
        db, store, user_id=user.id, app=app_row, declaration=_DECLARATION, route=route
    )


def _submission_refs(app_row: AppRegistry) -> tuple[object, object, object]:
    return (app_row.source_submission_id, app_row.source_commit_sha, app_row.submitted_at)


# --- the happy path: what the gate's call produces ---------------------------------


async def test_submit_copies_the_bundle_and_moves_draft_to_pending(db_session) -> None:
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)

    receipt = await _submit(db_session, store, user, app_row)

    assert receipt.commit_sha == _SHA
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.PENDING
    assert row.source_submission_id == receipt.submission_id
    assert row.source_commit_sha == _SHA
    assert row.submitted_at == receipt.submitted_at
    # R1/R5: the immutable copy exists at the derived key, byte-identical to the
    # snapshot, and is a RAW bundle — never base64.
    copied = store.objects[submission_key(row.id, receipt.submission_id)]
    assert copied == _BUNDLE
    assert copied.startswith(b"# v")


async def test_submit_records_the_lineage_and_the_declaration(db_session) -> None:
    # ASM18's whole point: a queue item can no longer arrive without its declaration,
    # and the row says WHICH route it entered through (U4's columns, this service's
    # writes). The declaration is opaque — stored verbatim, never reshaped.
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)

    await _submit(db_session, store, user, app_row)

    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.approval_route is ApprovalRoute.SELF_PUBLISH
    assert row.declaration == _DECLARATION


async def test_submit_writes_an_audit_row_with_artifact_and_route_detail(db_session) -> None:
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)

    receipt = await _submit(db_session, store, user, app_row)

    audit = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_type == "app", AuditLog.resource_id == str(app_row.id)
            )
        )
    ).scalar_one()
    assert audit.action == "submit"
    assert audit.actor_id == user.id
    # R14: the detail identifies the artifact — and now also the lineage, so the
    # trail can tell a publish-flow entry from any future admin-initiated one.
    assert audit.detail == {
        "submissionId": str(receipt.submission_id),
        "commitSha": _SHA,
        "route": "self_publish",
    }


async def test_submit_is_commitless_the_caller_owns_the_commit(db_session, monkeypatch) -> None:
    # The service writes in the CALLER'S transaction (like `append_audit` itself)
    # and NEVER commits it — the gate's own decision record and the submit are meant
    # to share one fate, which a service-side commit would silently split. Pinned
    # with a spy rather than a rollback probe: the test harness's outer transaction
    # makes commit-then-rollback measure the fixture, not the service.
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)
    commits: list[str] = []
    original_commit = db_session.commit

    async def _recording_commit() -> None:
        commits.append("commit")
        await original_commit()

    monkeypatch.setattr(db_session, "commit", _recording_commit)
    await _submit(db_session, store, user, app_row)
    assert commits == []  # every write is still pending in the caller's transaction


# --- R15b: pending is no longer a submit source ------------------------------------


async def test_submit_over_a_pending_item_is_refused(db_session) -> None:
    # R15b/P6: the retired route treated this as a refresh; overwriting an item an
    # administrator may be mid-review on is now forbidden, and the copy points at
    # the way out (withdrawal) instead of a dead end.
    user, app_row = await _owned_app(
        db_session,
        status=AppStatus.PENDING,
        source_submission_id=uuid.uuid4(),
        source_commit_sha=_SHA,
    )
    store = _staged(app_row)

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 409
    assert "withdraw" in excinfo.value.message
    # Nothing copied, nothing changed — the pending pin survives untouched.
    assert list(store.objects) == [snapshot_key(app_row.id)]
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.PENDING


async def test_submit_from_disabled_is_refused(db_session) -> None:
    user, app_row = await _owned_app(db_session, status=AppStatus.DISABLED)
    store = _staged(app_row)

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 409
    assert excinfo.value.message == "This app cannot be submitted in its current state."


async def test_resubmit_from_approved_keeps_the_approved_pin(db_session) -> None:
    # R6: a re-submit moves approved→pending but the approved pin survives (the
    # prior approved artifact keeps serving until re-approval).
    pinned = uuid.uuid4()
    user, app_row = await _owned_app(
        db_session,
        status=AppStatus.APPROVED,
        approved_submission_id=pinned,
        approved_commit_sha=_SHA,
    )
    store = _staged(app_row)

    await _submit(db_session, store, user, app_row)

    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.PENDING
    assert row.approved_submission_id == pinned  # untouched


async def test_every_resubmit_mints_a_fresh_id_and_retains_the_prior_blob(db_session) -> None:
    # R2: every submission is retained; ids are never reused. The second submit now
    # travels through a rejection (R15b closed the pending-refresh path), and BOTH
    # immutable copies still exist afterwards.
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)

    first = await _submit(db_session, store, user, app_row)
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == app_row.id)
        .values(status=AppStatus.REJECTED)
    )
    await db_session.refresh(app_row)
    second = await _submit(db_session, store, user, app_row)

    assert first.submission_id != second.submission_id
    assert submission_key(app_row.id, first.submission_id) in store.objects
    assert submission_key(app_row.id, second.submission_id) in store.objects


async def test_resubmit_from_rejected_clears_the_stale_rejection_note(db_session) -> None:
    user, app_row = await _owned_app(
        db_session, status=AppStatus.REJECTED, rejection_note="fix the header"
    )
    store = _staged(app_row)

    await _submit(db_session, store, user, app_row)

    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.PENDING
    assert row.rejection_note is None


# --- the app-scoped build-session guard (the documented divergence) ----------------


async def test_a_live_build_on_a_different_app_does_not_block_the_submit(
    db_session, fake_redis
) -> None:
    # THE DIVERGENCE, positive half: the retired route's guard was user-wide — a
    # citizen building project A was refused when submitting project B. The service
    # narrows to the app axis (matching the deploy route), so a lock held for a
    # session that positively names a DIFFERENT app proceeds.
    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, lock_key, registry_key

    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)
    await fake_redis.set(lock_key(user.id), "holder-token")
    await fake_redis.hset(
        registry_key(user.id), REGISTRY_FIELD_APP_NAME, app_name_for(uuid.uuid7())
    )

    receipt = await _submit(db_session, store, user, app_row)

    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.PENDING
    assert submission_key(row.id, receipt.submission_id) in store.objects


async def test_a_live_build_on_this_app_still_refuses_the_submit(db_session, fake_redis) -> None:
    # D8 survives the narrowing: the session the lock represents IS this app's, so
    # copying the snapshot would capture the previous build's bundle (valid bytes,
    # wrong tree) or torn bytes under a concurrent finalize.
    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, lock_key, registry_key

    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)
    await fake_redis.set(lock_key(user.id), "holder-token")
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_APP_NAME, app_name_for(app_row.id))

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 409
    assert "build session" in excinfo.value.message
    # No copy, no row change.
    assert list(store.objects) == [snapshot_key(app_row.id)]
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.DRAFT


async def test_a_redis_error_during_the_lock_check_is_503_fail_closed(
    db_session, fake_redis, monkeypatch
) -> None:
    # D8/fail-first: a Redis ERROR (as opposed to a HELD lock) during the
    # build-session lock check is real ambiguity — the submit fails closed (503),
    # copies no bundle, and leaves the row untouched. (A held lock is 409; a
    # MISSING Redis proceeds.)
    from redis.exceptions import RedisError

    import src.services.build_sessions as build_sessions

    async def _boom(_redis, _user_uuid):
        raise RedisError("redis blip")

    # The guard does `from src.services.build_sessions import lock_is_held` per
    # call, so patching the attribute on that package module reaches the name.
    monkeypatch.setattr(build_sessions, "lock_is_held", _boom)
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 503
    assert list(store.objects) == [snapshot_key(app_row.id)]
    row = await db_session.get(AppRegistry, app_row.id)
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


async def test_a_lock_that_names_no_app_fails_closed_and_refuses(db_session, fake_redis) -> None:
    # The narrowing keeps the guard's fail-closed posture: lock held + registry
    # unresolvable is the mid-provision window, which is AMBIGUITY, not innocence —
    # the submit refuses rather than racing it.
    from src.services.redis.keys import lock_key

    user, app_row = await _owned_app(db_session)
    store = _staged(app_row)
    await fake_redis.set(lock_key(user.id), "holder-token")  # no registry hash at all

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 409
    assert list(store.objects) == [snapshot_key(app_row.id)]


# --- the fail-closed bundle read (D9) ----------------------------------------------


async def test_submit_without_a_bundle_is_409_and_writes_nothing(db_session) -> None:
    user, app_row = await _owned_app(db_session)
    store = FakeStorage()  # empty: no snapshot staged

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 409
    assert excinfo.value.message == "Nothing to submit — generate an app first."
    assert store.objects == {}
    row = await db_session.get(AppRegistry, app_row.id)
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


async def test_submit_on_transient_storage_error_is_503_not_409(db_session) -> None:
    # D9/R9: a storage blip must NOT masquerade as "you have nothing to submit" —
    # that message sends someone whose app is fully built off to rebuild it.
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row, _ExplodingGetStorage())

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 503
    row = await db_session.get(AppRegistry, app_row.id)
    assert row.status is AppStatus.DRAFT  # nothing recorded


async def test_submit_with_no_storage_configured_is_503(db_session) -> None:
    # An unconfigured store arrives as None (the caller's None-tolerant seam) and
    # answers the same documented 503 as a transient blip.
    user, app_row = await _owned_app(db_session)

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, None, user, app_row)
    assert excinfo.value.status_code == 503
    row = await db_session.get(AppRegistry, app_row.id)
    assert row.status is AppStatus.DRAFT


async def test_submit_corrupt_bundle_is_409_and_writes_nothing(db_session) -> None:
    # R3: the snapshot bytes fail the git-bundle gate → refuse before any copy.
    user, app_row = await _owned_app(db_session)
    store = FakeStorage()
    store.objects[snapshot_key(app_row.id)] = b"not a bundle at all"

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 409
    assert "bundle" in excinfo.value.message
    # Only the snapshot exists — no submission copy was written.
    assert list(store.objects) == [snapshot_key(app_row.id)]
    row = await db_session.get(AppRegistry, app_row.id)
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


# --- the blob-first-row-second ordering (D3) ---------------------------------------


async def test_a_put_failure_during_the_copy_leaves_no_row_change(db_session) -> None:
    # When the PUT explodes, the row must be untouched — no ref pointing at a blob
    # that never landed (the exact failure D3's ordering exists to prevent).
    user, app_row = await _owned_app(db_session)
    store = _staged(app_row, _ExplodingPutStorage())

    with pytest.raises(AppApiError) as excinfo:
        await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 503
    row = await db_session.get(AppRegistry, app_row.id)
    assert row.status is AppStatus.DRAFT
    assert _submission_refs(row) == (None, None, None)


async def test_the_guard_refusing_after_the_blob_landed_logs_the_orphan(db_session) -> None:
    # The accepted D3 residual, end to end: the non-authoritative pre-check reads a
    # stale DRAFT while the authoritative row has moved to DISABLED — a concurrent
    # kill-switch, simulated by an UPDATE that deliberately skips identity-map
    # synchronization so the ORM instance keeps its stale view (the exact state a
    # second session's write produces). The copy lands, the guarded UPDATE refuses,
    # and the orphan gets its structured log line for the deferred blob-GC's trail.
    user, app_row = await _owned_app(db_session)  # ORM instance reads DRAFT
    store = _staged(app_row)
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == app_row.id)
        .values(status=AppStatus.DISABLED)
        .execution_options(synchronize_session=False)
    )

    with capture_logs() as logs:
        with pytest.raises(AppApiError) as excinfo:
            await _submit(db_session, store, user, app_row)
    assert excinfo.value.status_code == 409
    # The blob DID land (that is what makes it an orphan) and the log names it.
    orphan_keys = [key for key in store.objects if key != snapshot_key(app_row.id)]
    assert len(orphan_keys) == 1
    orphan_logs = [entry for entry in logs if "orphan blob" in entry["event"]]
    assert len(orphan_logs) == 1
    assert orphan_logs[0]["key"] == orphan_keys[0]
    assert orphan_logs[0]["app_id"] == str(app_row.id)
    # And the refused row carries no ref to it.
    row = await db_session.get(AppRegistry, app_row.id)
    await db_session.refresh(row)
    assert row.status is AppStatus.DISABLED
    assert _submission_refs(row) == (None, None, None)


# --- ownership (ADR-0004) ----------------------------------------------------------


async def test_a_caller_supplied_mismatched_user_is_a_non_leaking_404(db_session) -> None:
    # The service re-checks ownership fail-closed even though callers resolve the
    # app through their own owner-scoped 404 — a service that trusts its caller
    # with the predicate is one refactor away from a cross-user write.
    _owner, app_row = await _owned_app(db_session)
    stranger = await UserFactory.create(db_session)
    store = _staged(app_row)

    with pytest.raises(AppApiError) as excinfo:
        await submit_app_for_review(
            db_session,
            store,
            user_id=stranger.id,
            app=app_row,
            declaration=_DECLARATION,
            route=ApprovalRoute.SELF_PUBLISH,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.message == "App not found."
    assert list(store.objects) == [snapshot_key(app_row.id)]
    row = await db_session.get(AppRegistry, app_row.id)
    assert row.status is AppStatus.DRAFT
