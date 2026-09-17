"""Operator sweep: diff object storage against the DB and reclaim keys a failed cleanup stranded.

WHY THIS EXISTS: the only trail of a failed post-commit sweep is a `_log.warning` with no
failure list to replay — so this DIFFS the store against the DB: every key resolved to an
owning row, deleting only one with NO owner AND past grace. Idempotent; safe to run any time.

THE 24h GRACE IS THE ENTIRE CORRECTNESS ARGUMENT: it protects a blob whose row hasn't landed yet
(`put` before `commit`). The ONLY path to "eligible" runs through the grace check, so an unknown
age (`head()` returns no `last_modified`) FAILS CLOSED to within-grace — deleting a blob whose
age we cannot prove is the one mistake this unit must not make.

Per-prefix rules:
- `att/{user_id}/` — owned-set from `Attachment.storage_key`, NEVER a PK-derived key (uploads
  mint a fresh uuid7 unrelated to the row's PK); built via `_blob_keys_for`, one key per row.
- `snapshots/`, `recovery/` — reconciled against `AppRegistry` existence; delete-eligible.
- `submissions/{app_id}/` — REPORT-ONLY: the immutable approval record. "No app row" is NOT a
  licence to delete — an append-only audit row outlives the app and still names the bundle via
  `detail.submissionId`.
- `apps/{app_id}/` — REPORT-ONLY. `app_files` was dropped in migration 0017 so `app_file_key`
  has no writer, and whether anything exists here in a deployed env cannot be answered from the
  repo. Report first; decide after someone with tenant access looks.

Failures SURFACE: a `StorageError` propagates (→ 503, retryable) instead of being logged away.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.db.models.attachment import Attachment

# REUSED, not reimplemented (drift guard): the owned-set for `att/` derives blob keys through the
# SAME helper the conversation cascade and the never-sent-upload reclaim use, so a key shape one
# honours is honoured here too.
from src.services.conversations.delete import _blob_keys_for
from src.services.storage.base import ObjectStorage
from src.services.storage.listing import all_keys_under

_log = structlog.get_logger()

# The err-long grace: 24 hours, a large multiple of the longest realistic submit / upload
# transaction. Too short deletes citizen source for which the blob is the only copy; too long
# costs a few hours of stored bytes. Size it long.
RECONCILE_GRACE = datetime.timedelta(hours=24)

# Bound the per-key head()/delete() fan-out so a large first run doesn't open an unbounded number
# of object-store connections — or walk every key strictly sequentially and time the synchronous
# `POST /admin/apps/reconcile-storage` request out. Mirrors `sweep.py::_SWEEP_CONCURRENCY`: the
# classification is per-key and order-independent, so the bound changes throughput, never the
# counts.
_RECONCILE_CONCURRENCY = 8

# The top-level object-store namespaces, mirroring the `keys.py` builders by hand (there is no
# bare-root builder — `owner_prefix`/`snapshot_key`/… all take an id). A key under each root is
# `att/{user_id}/…`, `snapshots/{app_id}/app.bundle`, `submissions/{app_id}/{sid}.bundle`,
# `apps/{app_id}/{file_id}` — so the owner id is always path segment index 1.
_ATTACHMENTS_ROOT = "att/"
_SNAPSHOTS_ROOT = "snapshots/"
_RECOVERY_ROOT = "recovery/"
_VERSIONS_ROOT = "versions/"
_SUBMISSIONS_ROOT = "submissions/"
_APPS_ROOT = "apps/"


@dataclass(frozen=True)
class PrefixCounts:
    """What one prefix's reconciliation found. Counts ONLY, never a key list: the list would
    carry the storage layout out with every report that travels.
    `scanned == owned + within_grace + eligible` always; `deleted <= eligible` (and
    `deleted == 0` on a report-only prefix)."""

    scanned: int
    owned: int
    within_grace: int
    eligible: int
    deleted: int


@dataclass(frozen=True)
class StorageReconcileReport:
    """The whole sweep's outcome: per-prefix counts plus the ownerless-submission tally as its
    own named field. Frozen, mirroring the `AttachmentReclaimResult` value-type idiom — the
    router maps it to the professional API response."""

    attachments: PrefixCounts
    snapshots: PrefixCounts
    recovery: PrefixCounts
    versions: PrefixCounts
    submissions: PrefixCounts
    apps: PrefixCounts
    # Ownerless submission bundles past grace — `submissions/{app_id}/` blobs whose `app_id`
    # resolves to no `AppRegistry` row. Report-only; a governance call rules on these.
    ownerless_submissions: int


def _membership(keys: frozenset[str]) -> Callable[[str], bool]:
    """Owner check for `att/`: a key is owned iff it is one of the persisted attachment blob keys
    (one `storage_key` per row)."""

    def _is_owned(key: str) -> bool:
        return key in keys

    return _is_owned


def _owned_by_app_row(app_ids: frozenset[uuid.UUID]) -> Callable[[str], bool]:
    """Owner check for the app-keyed prefixes: parse the `app_id` (path segment index 1) and ask
    whether an `AppRegistry` row still exists for it. FAILS CLOSED — a key whose `app_id` cannot
    be parsed is treated as owned (never deleted), because a blob we cannot attribute is a blob we
    must not destroy."""

    def _is_owned(key: str) -> bool:
        parts = key.split("/")
        if len(parts) < 2:
            return True
        try:
            app_id = uuid.UUID(parts[1])
        except ValueError:
            return True
        return app_id in app_ids

    return _is_owned


async def _reconcile_prefix(
    storage: ObjectStorage,
    prefix: str,
    is_owned: Callable[[str], bool],
    *,
    cutoff: datetime.datetime,
    delete_eligible: bool,
) -> PrefixCounts:
    """Walk one prefix to exhaustion, bucket every key, and — only on a delete-enabled prefix —
    delete the eligible ones. Owned keys short-circuit before any age check; an unowned key
    reaches `eligible` only after `head()` proves a known `last_modified` older than `cutoff` —
    an unknown age (or a key that vanished mid-walk) falls to `within_grace`, fail closed.

    Unowned keys' head()/delete run CONCURRENTLY under a bounded semaphore; classification is
    per-key and order-independent, so counts match the sequential walk. A `StorageError` from
    any head or delete still propagates, surfacing a mid-sweep failure retryably."""
    keys = await all_keys_under(storage, prefix)
    owned = 0
    unowned: list[str] = []
    for key in keys:
        if is_owned(key):
            owned += 1  # owned short-circuits before any age test — pure predicate, no head()
        else:
            unowned.append(key)

    limiter = asyncio.Semaphore(_RECONCILE_CONCURRENCY)

    async def _classify(key: str) -> tuple[bool, bool]:
        """`(eligible, deleted)` for one unowned key, held under the concurrency bound across BOTH
        its head() and (when eligible on a delete-enabled prefix) its delete()."""
        async with limiter:
            meta = await storage.head(key)
            last_modified = meta.last_modified if meta is not None else None
            if last_modified is None or last_modified >= cutoff:
                return (False, False)  # within grace / unknown age → fail closed, never deleted
            if delete_eligible:
                await storage.delete(key)  # idempotent on a missing object
                return (True, True)
            return (True, False)

    verdicts = await asyncio.gather(*(_classify(key) for key in unowned))
    eligible = sum(1 for is_eligible, _ in verdicts if is_eligible)
    within_grace = sum(1 for is_eligible, _ in verdicts if not is_eligible)
    deleted = sum(1 for _, was_deleted in verdicts if was_deleted)
    return PrefixCounts(
        scanned=len(keys),
        owned=owned,
        within_grace=within_grace,
        eligible=eligible,
        deleted=deleted,
    )


async def reconcile_orphaned_storage(
    db: AsyncSession,
    storage: ObjectStorage,
    *,
    now: datetime.datetime | None = None,
) -> StorageReconcileReport:
    """Diff the whole object store against the database and reclaim ownerless, past-grace blobs.

    A GLOBAL operator sweep (superadmin): no `user_id` scopes the app-keyed prefixes, so
    cross-user SAFETY is per-key — an `att/{user_id}/…` blob is owned only by a row whose
    `storage_key` embeds that owner id: one user's rows can never protect or expose another's.

    `now` is injectable; deletes blobs only — the CALLER owns the audit row and the commit.
    `StorageError` always propagates (retryable), never swallowed."""
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - RECONCILE_GRACE

    # Owned-set for `att/`: the persisted blob keys, NEVER a PK-derived key (`_blob_keys_for`
    # yields one `storage_key` per row).
    attachments: Sequence[Attachment] = (await db.execute(sa.select(Attachment))).scalars().all()
    owned_att_keys = frozenset(_blob_keys_for(attachments))
    # Owned-set for the app-keyed prefixes: every live app id.
    app_ids = frozenset((await db.execute(sa.select(AppRegistry.id))).scalars().all())

    attachments_counts = await _reconcile_prefix(
        storage,
        _ATTACHMENTS_ROOT,
        _membership(owned_att_keys),
        cutoff=cutoff,
        delete_eligible=True,
    )
    snapshots_counts = await _reconcile_prefix(
        storage, _SNAPSHOTS_ROOT, _owned_by_app_row(app_ids), cutoff=cutoff, delete_eligible=True
    )
    # Same ownership rule and same disposition as `snapshots/` — a recovery bundle whose app row
    # is gone is exactly as orphaned as a saved one, and holds exactly as much of the user's
    # code. A root missing from this list is not merely undeleted: `_reconcile_prefix` never
    # scans it, so it never even appears in the report as something to look at.
    recovery_counts = await _reconcile_prefix(
        storage, _RECOVERY_ROOT, _owned_by_app_row(app_ids), cutoff=cutoff, delete_eligible=True
    )
    # ★ THE LIST OFFERS TWO AND DELETES NONE, so this prefix grows by one full source tree per
    # save and never shrinks on its own. That makes it the root most worth reconciling, and the
    # ownership rule is the same as the other two: a version bundle whose app row is gone is an
    # orphan. Deleting one here is not the version list changing its mind — the app it belonged
    # to no longer exists.
    versions_counts = await _reconcile_prefix(
        storage, _VERSIONS_ROOT, _owned_by_app_row(app_ids), cutoff=cutoff, delete_eligible=True
    )
    # Report-only: the immutable approval record — surface, never delete.
    submissions_counts = await _reconcile_prefix(
        storage,
        _SUBMISSIONS_ROOT,
        _owned_by_app_row(app_ids),
        cutoff=cutoff,
        delete_eligible=False,
    )
    # Report-only: no known writer since migration 0017; deleting blind is a silent leak risk.
    apps_counts = await _reconcile_prefix(
        storage, _APPS_ROOT, _owned_by_app_row(app_ids), cutoff=cutoff, delete_eligible=False
    )

    report = StorageReconcileReport(
        attachments=attachments_counts,
        snapshots=snapshots_counts,
        recovery=recovery_counts,
        versions=versions_counts,
        submissions=submissions_counts,
        apps=apps_counts,
        # The ownerless-submission set = unowned + past-grace under `submissions/`. Within-grace
        # ownerless bundles are omitted on purpose: a fresh one may be a legitimate in-flight
        # submit, and it becomes "ownerless" only once it ages past the grace with no row.
        ownerless_submissions=submissions_counts.eligible,
    )
    _log.info(
        "storage_reconcile_completed",
        att_deleted=attachments_counts.deleted,
        snapshots_deleted=snapshots_counts.deleted,
        ownerless_submissions=report.ownerless_submissions,
    )
    return report
