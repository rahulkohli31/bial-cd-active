"""`reconcile_orphaned_storage` — the operator-invoked reconciling sweep (U10, R11/R12/R13).

The crux is the 24h grace and its POLARITY: the ONLY route to "eligible" runs through the grace
check, so a within-grace or unknown-age blob is never deleted even with no owning row (the
recorded lifecycle-triad bug was a liveness check that short-circuited to eligible with ZERO
grace). `submissions/` and `apps/` are report-only until D7; `att/` and `snapshots/` delete the
ownerless-and-past-grace keys. Owned-set for `att/` is built from the persisted `storage_key`
column (never a PK-derived key) and includes each deck's `{key}.pdf` sibling.

These are SERVICE-level tests (fixed injected `now`). The owned-attachment / derived-key /
deck-sibling pins that must run through the REAL upload path live in
`tests/api/v1/admin/test_storage_reconcile.py`.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select

from src.db.models.attachment import Attachment
from src.services.storage import app_file_key, snapshot_key, submission_key
from src.services.storage.constants import DEFAULT_PAGE_SIZE
from src.services.storage.errors import StorageError
from src.services.storage.reconcile import (
    RECONCILE_GRACE,
    PrefixCounts,
    reconcile_orphaned_storage,
)
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_NOW = datetime.datetime(2026, 7, 21, 12, 0, tzinfo=datetime.UTC)
_OLD = _NOW - datetime.timedelta(hours=48)  # comfortably past the 24h grace
_FRESH = _NOW - datetime.timedelta(hours=1)  # inside the grace


def _put(store: FakeStorage, key: str, *, mtime: datetime.datetime | None = None) -> None:
    store.objects[key] = b"x"
    if mtime is not None:
        store.mtimes[key] = mtime


def test_grace_constant_is_the_err_long_24h() -> None:
    # D6/KD-7: 24h, a large multiple of the longest realistic submit transaction.
    assert RECONCILE_GRACE == datetime.timedelta(hours=24)


# --- polarity: grace is reachable on every path (the recorded bug) --------------


async def test_within_grace_unowned_never_deleted(db_session) -> None:
    # AE4 second half + the polarity pin: a FRESH blob with NO owning row survives, because the
    # only route to "eligible" runs through the grace check — no short-circuit gives it zero grace.
    store = FakeStorage()
    user = await UserFactory.create(db_session)
    key = f"att/{user.id}/{uuid.uuid7()}"  # no Attachment row references it
    _put(store, key, mtime=_FRESH)

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert key in store.objects
    assert report.attachments.within_grace == 1
    assert report.attachments.eligible == 0
    assert report.attachments.deleted == 0


async def test_unknown_age_fails_closed(db_session) -> None:
    # A candidate whose head() returns last_modified=None (unknown age) is treated as within-grace
    # and never deleted — deleting a blob whose age we cannot prove is the irreversible mistake.
    store = FakeStorage()
    user = await UserFactory.create(db_session)
    key = f"att/{user.id}/{uuid.uuid7()}"
    _put(store, key)  # no mtime -> head() returns last_modified=None

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert key in store.objects
    assert report.attachments.within_grace == 1
    assert report.attachments.deleted == 0


async def test_grace_reachable_on_every_delete_prefix(db_session) -> None:
    # No delete-enabled prefix short-circuits past the grace: a FRESH unowned blob under both
    # att/ and snapshots/ survives, and neither is counted eligible.
    store = FakeStorage()
    user = await UserFactory.create(db_session)
    att_key = f"att/{user.id}/{uuid.uuid7()}"
    snap_key = snapshot_key(uuid.uuid7())  # app_id with no AppRegistry row
    _put(store, att_key, mtime=_FRESH)
    _put(store, snap_key, mtime=_FRESH)

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert att_key in store.objects and snap_key in store.objects
    assert report.attachments.eligible == 0
    assert report.snapshots.eligible == 0


# --- deletion of genuine orphans ------------------------------------------------


async def test_unowned_past_grace_is_deleted(db_session) -> None:
    store = FakeStorage()
    user = await UserFactory.create(db_session)
    key = f"att/{user.id}/{uuid.uuid7()}"
    _put(store, key, mtime=_OLD)

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert key not in store.objects
    assert report.attachments.eligible == 1
    assert report.attachments.deleted == 1


async def test_snapshots_owned_within_eligible_split(db_session) -> None:
    # The plan's canonical mix: one owned, one unowned-within-grace, one unowned-past-grace.
    store = FakeStorage()
    user = await UserFactory.create(db_session)
    live_app = await AppRegistryFactory.create(db_session, user_id=user.id)

    owned = snapshot_key(live_app.id)  # app row exists -> owned regardless of age
    fresh = snapshot_key(uuid.uuid7())  # no row, 1h old -> within grace
    stale = snapshot_key(uuid.uuid7())  # no row, 48h old -> eligible
    _put(store, owned, mtime=_OLD)
    _put(store, fresh, mtime=_FRESH)
    _put(store, stale, mtime=_OLD)

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert report.snapshots == PrefixCounts(
        scanned=3, owned=1, within_grace=1, eligible=1, deleted=1
    )
    assert owned in store.objects and fresh in store.objects  # both survive
    assert stale not in store.objects  # the genuine orphan is reclaimed


# --- report-only prefixes: submissions + apps -----------------------------------


async def test_submissions_reported_never_deleted(db_session) -> None:
    # An ownerless submission bundle past grace is REPORTED (eligible + ownerless) but survives:
    # deleting the immutable approval record is the open D7 governance call.
    store = FakeStorage()
    user = await UserFactory.create(db_session)
    owned_app = await AppRegistryFactory.create(db_session, user_id=user.id)

    owned = submission_key(owned_app.id, uuid.uuid7())
    ownerless = submission_key(uuid.uuid7(), uuid.uuid7())  # app_id resolves to no row
    _put(store, owned, mtime=_OLD)
    _put(store, ownerless, mtime=_OLD)

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert report.submissions == PrefixCounts(
        scanned=2, owned=1, within_grace=0, eligible=1, deleted=0
    )
    assert report.ownerless_submissions == 1
    # Nothing was deleted — both bundles still there.
    assert owned in store.objects and ownerless in store.objects


async def test_ownerless_submission_category_is_idempotent(db_session) -> None:
    store = FakeStorage()
    ownerless = submission_key(uuid.uuid7(), uuid.uuid7())
    _put(store, ownerless, mtime=_OLD)

    first = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    second = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert first.ownerless_submissions == 1
    assert second.ownerless_submissions == 1  # reported identically
    assert second.submissions.deleted == 0
    assert ownerless in store.objects  # never deleted, either run


async def test_apps_prefix_reported_never_deleted(db_session) -> None:
    # apps/ has no known writer since migration 0017; report-only, never delete.
    store = FakeStorage()
    key = app_file_key(uuid.uuid7(), uuid.uuid7())  # no AppRegistry row
    _put(store, key, mtime=_OLD)

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert report.apps == PrefixCounts(scanned=1, owned=0, within_grace=0, eligible=1, deleted=0)
    assert key in store.objects


# --- idempotence + clean system -------------------------------------------------


async def test_second_run_is_a_noop(db_session) -> None:
    store = FakeStorage()
    user = await UserFactory.create(db_session)
    _put(store, f"att/{user.id}/{uuid.uuid7()}", mtime=_OLD)
    _put(store, snapshot_key(uuid.uuid7()), mtime=_OLD)

    first = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert first.attachments.deleted == 1 and first.snapshots.deleted == 1
    second = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert second.attachments.deleted == 0 and second.snapshots.deleted == 0
    assert second.attachments.eligible == 0 and second.snapshots.eligible == 0


async def test_clean_system_reports_all_zero(db_session) -> None:
    report = await reconcile_orphaned_storage(db_session, FakeStorage(), now=_NOW)
    for counts in (report.attachments, report.snapshots, report.submissions, report.apps):
        assert counts == PrefixCounts(scanned=0, owned=0, within_grace=0, eligible=0, deleted=0)
    assert report.ownerless_submissions == 0


# --- pagination -----------------------------------------------------------------


async def test_prefix_spanning_multiple_pages_is_fully_walked(db_session) -> None:
    # DEFAULT_PAGE_SIZE + 1 unowned, past-grace snapshot keys → every one is walked and swept.
    # A single-page walk would leave the +1 behind (deleted == DEFAULT_PAGE_SIZE, not +1).
    store = FakeStorage()
    for _ in range(DEFAULT_PAGE_SIZE + 1):
        _put(store, snapshot_key(uuid.uuid7()), mtime=_OLD)

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert report.snapshots.scanned == DEFAULT_PAGE_SIZE + 1
    assert report.snapshots.deleted == DEFAULT_PAGE_SIZE + 1


# --- failures surface, they are not swallowed -----------------------------------


class _ExplodingDeleteStorage(FakeStorage):
    """A store whose `delete` fails transiently mid-sweep. The reconciler must SURFACE it
    retryably (→ 503 at the endpoint), never swallow it — the whole point is to stop losing
    failures to a log line. Idempotence makes the retry safe."""

    async def delete(self, key):
        raise StorageError("delete blip", provider="fake", key=key)


async def test_storage_error_mid_sweep_is_surfaced_not_swallowed(db_session) -> None:
    store = _ExplodingDeleteStorage()
    _put(store, snapshot_key(uuid.uuid7()), mtime=_OLD)  # eligible -> triggers a delete
    try:
        await reconcile_orphaned_storage(db_session, store, now=_NOW)
    except StorageError:
        return
    raise AssertionError("expected the StorageError to propagate, not be swallowed")


# --- cross-user attribution -----------------------------------------------------


async def test_cross_user_attribution(db_session) -> None:
    # A global operator sweep, but ownership is attributed PER persisted key: user A's orphan (no
    # row) under att/A/ is reclaimed, while user B's OWNED blob under att/B/ is protected by B's
    # row. One user's rows can never expose or protect another user's blob.
    store = FakeStorage()
    user_a = await UserFactory.create(db_session)
    user_b = await UserFactory.create(db_session)

    a_orphan = f"att/{user_a.id}/{uuid.uuid7()}"
    b_key = f"att/{user_b.id}/{uuid.uuid7()}"
    db_session.add(
        Attachment(
            user_id=user_b.id,
            attachment_id="att_b",
            media_type="image/png",
            name="",
            size=3,
            storage_key=b_key,
        )
    )
    await db_session.flush()
    _put(store, a_orphan, mtime=_OLD)
    _put(store, b_key, mtime=_OLD)  # owned -> age is irrelevant

    report = await reconcile_orphaned_storage(db_session, store, now=_NOW)
    assert a_orphan not in store.objects  # A's genuine orphan reclaimed
    assert b_key in store.objects  # B's owned blob protected by B's row
    assert report.attachments.deleted == 1
    row_b = await db_session.scalar(select(Attachment).where(Attachment.attachment_id == "att_b"))
    assert row_b is not None
