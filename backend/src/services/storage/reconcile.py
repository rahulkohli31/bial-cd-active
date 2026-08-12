"""Operator-invoked reconciling sweep: diff object storage against the database and
reclaim keys that a failed cleanup stranded (R11, R12, R13).

Today the ONLY trail of a failed post-commit sweep is a `_log.warning` (`sweep.py:43`,
`:66`, `projects/delete.py:168`) with no persisted failure list — so a reconciler cannot
replay it. Instead it DIFFS the store against the DB: enumerate every key under a prefix,
resolve each to an owning row, and delete only a key with NO owner AND older than the grace
period. Safe to run at any time, idempotent (two runs converge, the second a no-op), and
recoverable — a failed cleanup becomes a swept orphan on the next operator run.

THE 24h GRACE IS THE ENTIRE CORRECTNESS ARGUMENT (KD-7). It is what makes the sweep safe to
run mid-submit / mid-upload (R12): a blob a request is about to record a row for (`put` lands
before the `commit`) is FRESH, so the grace protects it. Get the polarity right — the ONLY
path to "eligible" runs through the grace check (`_reconcile_prefix` below), so a within-grace
or unknown-age blob is NEVER deleted, even with no owning row. An unknown age (`head()` returns
`last_modified is None`) FAILS CLOSED to within-grace: deleting a blob whose age we cannot prove
is exactly the irreversible mistake this unit must not make.

Per-prefix rules (get every one right):

- `att/{user_id}/` — owned-set built from the persisted `Attachment.storage_key` column, NEVER
  a PK-derived key. `attachment_key(user_id, uuid.uuid7())` mints a FRESH uuid7 at upload
  (`attachments/router.py`), unrelated to the PK, so `attachment_key(user_id, att.id)` matches
  no stored object for any row — a PK-derived diff would flag 100% of `att/` blobs as unowned.
  A PPTX upload also writes a derived `{storage_key}.pdf` sibling that no column points at, so
  the owned-set is built with `_blob_keys_for` (the SAME helper the conversation cascade and the
  U9 reclaimer use), which yields both `storage_key` and `storage_key + ".pdf"` for a deck — or
  the sweep permanently deletes the only rendered form the Azure-hosted model can read.
- `snapshots/{app_id}/` — reconciled against `AppRegistry`: a bundle whose `app_id` resolves to
  no row is unowned. Owner diff + grace → delete eligible.
- `submissions/{app_id}/` — REPORT-ONLY until D7 (submission-bundle retention) is answered. These
  are the immutable record of what was approved; deleting them is a governance call. The report
  names the ownerless bundles (`ownerless_submissions`) — that is where U7's residual window and
  the accepted orphan at `apps/router.py` land, the set D7 must rule on. "No app row" is NOT a
  licence to delete: an append-only `audit_logs` row (`resource_id` is a plain String, no FK) can
  outlive the app and still name the bundle via `detail.submissionId`.
- `apps/{app_id}/` — REPORT-ONLY. `app_files` was dropped in migration 0017 so `app_file_key` has
  no writer; whether anything exists here in a deployed env cannot be answered from the repo.
  Report first, decide after someone with tenant access looks.

OPERATOR-INVOKED (KD-7): THIS reconciler is not on a schedule — nothing but a superadmin calls it.
A superadmin drives this endpoint headlessly; a grace-period sweep that nothing calls
reclaims nothing, so the runbook says operator-invoked plainly rather than implying automation.

Corrected 2026-08-11 (ADR-0029): this said "there is no scheduler in this repo and an in-process
one was deliberately rejected". False when written (a 300 s sandbox sweeper had run in the lifespan
since v1.6.5) and doubly false now that ADR-0011 is Accepted. Only the narrower claim above holds;
scheduling this sweep is available and unclaimed.

FAILURES SURFACE, they are not swallowed. Unlike the best-effort post-commit `sweep_blobs`, a
`StorageError` from the walk / head / delete propagates UP (→ 503 at the endpoint, retryable) —
the whole point of this unit is to stop losing failures to a log line. Idempotence guarantees a
retry converges, so a partial run followed by a retry is safe.
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

# REUSED, not reimplemented (drift guard): the owned-set for `att/` derives blob keys — including
# each deck's `{key}.pdf` sibling — through the SAME helper the conversation cascade and the U9
# reclaimer use, so a key shape one honours is honoured here too.
from src.services.conversations.delete import _blob_keys_for
from src.services.storage.base import ObjectStorage
from src.services.storage.listing import all_keys_under

_log = structlog.get_logger()

# The err-long grace (KD-7, D6): 24 hours, a large multiple of the longest realistic submit /
# upload transaction. Too short deletes citizen source for which the blob is the only copy; too
# long costs a few hours of stored bytes. Size it long.
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
_SUBMISSIONS_ROOT = "submissions/"
_APPS_ROOT = "apps/"


@dataclass(frozen=True)
class PrefixCounts:
    """What one prefix's reconciliation found. Counts ONLY — never a key list (R13 posture:
    dumping storage keys leaks the internal layout). `scanned == owned + within_grace + eligible`
    always; `deleted <= eligible` (and `deleted == 0` on a report-only prefix)."""

    scanned: int
    owned: int
    within_grace: int
    eligible: int
    deleted: int


@dataclass(frozen=True)
class StorageReconcileReport:
    """The whole sweep's outcome: per-prefix counts plus the ownerless-submission tally as its
    own named field (the D7 set). Frozen, mirroring the `AttachmentReclaimResult` value-type
    idiom — the router maps it to the professional API response."""

    attachments: PrefixCounts
    snapshots: PrefixCounts
    recovery: PrefixCounts
    submissions: PrefixCounts
    apps: PrefixCounts
    # Ownerless submission bundles past grace — `submissions/{app_id}/` blobs whose `app_id`
    # resolves to no `AppRegistry` row. Report-only; the D7 governance call rules on these.
    ownerless_submissions: int


def _membership(keys: frozenset[str]) -> Callable[[str], bool]:
    """Owner check for `att/`: a key is owned iff it is one of the persisted attachment blob keys
    (each `storage_key`, plus each deck's `{storage_key}.pdf` sibling)."""

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
    """Walk one prefix to exhaustion (paginated; a `StorageError` RAISES upward, never reads as
    empty), bucket every key, and — only on a delete-enabled prefix — delete the eligible ones.

    The grace check is UNAVOIDABLE on the way to `eligible`: an owned key short-circuits before
    any age test, and an unowned key reaches `eligible` ONLY after `head()` proves a known
    `last_modified` strictly older than `cutoff`. A `None` last_modified (unknown age, or an
    object that vanished between the list and the head) falls to `within_grace` — fail closed.

    Owned keys are bucketed first (a pure predicate, no I/O), then the unowned keys' head()/delete
    run CONCURRENTLY under a bounded semaphore (mirroring `sweep.py`) rather than one strictly
    sequential await per key. Concurrency changes only throughput: classification is per-key and
    order-independent, so the counts (`scanned == owned + within_grace + eligible`,
    `deleted <= eligible`) are identical to the sequential walk. A `StorageError` from any head or
    delete still propagates (gather's default), so a mid-sweep failure surfaces retryably."""
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

    A GLOBAL operator sweep (superadmin): the app-keyed prefixes reconcile against
    `AppRegistry` existence, and an orphan is precisely a key whose app row is GONE — there is no
    surviving `user_id` to scope by, so the sweep cannot be per-user for those. Cross-user SAFETY
    is preserved by per-key attribution instead: an `att/{user_id}/…` blob is owned only by a row
    whose persisted `storage_key` matches it, and `storage_key` embeds the owner id, so one user's
    rows can never protect or expose another user's blob.

    `now` is injectable for tests; `cutoff = now - RECONCILE_GRACE`. Deletes blobs only — the
    caller owns the audit + commit. `StorageError` propagates (retryable), never swallowed."""
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - RECONCILE_GRACE

    # Owned-set for `att/`: the persisted blob keys, NEVER a PK-derived key (`_blob_keys_for`
    # yields each `storage_key` plus each deck's `{storage_key}.pdf` sibling).
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
    # Report-only (D7): the immutable approval record — surface, never delete.
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
        submissions=submissions_counts,
        apps=apps_counts,
        # The ownerless-submission set = unowned + past-grace under `submissions/`. Within-grace
        # ownerless bundles are omitted on purpose: a fresh one may be a legitimate in-flight
        # submit (R12), and it becomes "ownerless" only once it ages past the grace with no row.
        ownerless_submissions=submissions_counts.eligible,
    )
    _log.info(
        "storage_reconcile_completed",
        att_deleted=attachments_counts.deleted,
        snapshots_deleted=snapshots_counts.deleted,
        ownerless_submissions=report.ownerless_submissions,
    )
    return report
