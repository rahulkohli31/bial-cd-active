"""Operator-invoked orphan reconciler for per-project databases and roles — REPORT-ONLY,
plus the advisory `pg_database_size()` probe the admin listing renders (R9, R10).

Teardown is per-step best-effort (`salt_the_earth` never raises), so a failed drop leaves a
database — or, worse, a surviving LOGIN role whose database is already gone — with nothing
but a `_log.warning` naming it. This module turns that into a number an operator can act on:
enumerate the cluster, diff it against the `project_databases` registry, and report.

NOTHING HERE DELETES ANYTHING. That is the mechanism, not a courtesy, because the blob
reconciler's safety argument does not transfer. `reconcile_orphaned_storage` can delete
because `head().last_modified` gives every key a provable age and a 24h grace protects an
in-flight write; `pg_database` carries NO creation timestamp at all (and `pg_stat_file`
needs a superuser Azure will not grant). The provision-time `COMMENT ON DATABASE`
(`provision._stamp_provisioned_at`, read back here via `shobj_description(oid,
'pg_database')`) is the only age source that survives orphaning, and a comment is
mutable, absent on anything not provisioned by us, and trivially lost. Delete-eligibility
is therefore a human ruling made with this report in hand, never an inference made here.

Three independent guards decide "not ours", and ALL of them must agree before a name is
even reported:

1. **Denylist** — the control-plane databases, the maintenance database, PostgreSQL's own
   `postgres`/`template0`/`template1`, and Azure's `azure_maintenance`/`azure_sys`. None of
   these could pass guard 2 either; the redundancy IS the point.
2. **Full-UUID name anchor** — only `bialapp_<32 hex>` / `bialrole_<32 hex>` are candidates,
   parsed by `names.project_id_from_*`, which already fail closed. This is what protects the
   unrelated databases sharing a dev cluster (`ascend`, `badger`, `knit`, `neox`,
   `singularity`, `tod`, …): none of them can be named by the derivation, so none of them
   can ever be attributed to a project.
3. **Fail closed on anything unparseable** — an unparseable name, and an absent or
   unparseable provision comment, both land in a not-actionable bucket. Mirrors
   `_owned_by_app_row` (`services/storage/reconcile.py`), which returns "owned" for a key it
   cannot parse: a thing we cannot attribute is a thing we must not propose destroying.

TWO SERVERS, ONE DIFF. The enumeration runs against whatever cluster the MAINTENANCE DSN
points at, and it is diffed against whatever registry the CONTROL-PLANE session reads. On a
shared dev box those two can legitimately disagree — a database belonging to a `citizen_one`
project looks orphaned to a `citizen_one_test` session — which is a second reason nothing
here deletes, and the reason the future separate-apps-server lever must not silently point
this at a cluster the control plane does not own.

COUNTS ONLY, NEVER NAMES. Database names embed the project uuid, so a name list is an
inventory of who has what; it is the exact analogue of the storage report's key list, which
is pinned counts-only by test. The report, the response body and the audit `detail` all carry
integers and nothing else.

OPERATOR-INVOKED: THIS reconciler is not on a schedule. A superadmin drives
`POST /v1/admin/apps/reconcile-databases`. (Corrected 2026-08-11 from "there is no scheduler in
this repo, by decision" — false when written, doubly false now that ADR-0011 is Accepted; see
ADR-0029. Scheduling it is available and unclaimed.) No per-object fan-out exists to bound (the
whole cluster answers in two catalog round trips), so the storage reconciler's semaphore has no
analogue here — the bound it enforces is achieved by construction. Failures PROPAGATE, so a
cluster we could not reach surfaces as a retryable 503 rather than a silent partial report.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.db.models.project_database import ProjectDatabase
from src.services.appdb.names import (
    database_name,
    project_id_from_database_name,
    project_id_from_role_name,
)

_log = structlog.get_logger()

# Never a candidate, whatever else is true of them. PostgreSQL's own three, the control
# plane's databases across every lane, and Azure Database for PostgreSQL's two system
# databases (which exist on a managed server and are invisible on a local one).
_SYSTEM_DATABASES: Final = frozenset(
    {
        "postgres",
        "template0",
        "template1",
        "citizen_one",
        "citizen_one_test",
        "citizen_one_e2e",
        "azure_maintenance",
        "azure_sys",
    }
)

# The role half of the same idea. `pg_*` predefined roles fall out of the name anchor on
# their own; these are the named identities that must never be reported however the cluster
# is configured.
_SYSTEM_ROLES: Final = frozenset({"postgres", "azure_superuser", "azuresu"})

# `shobj_description` is the shared-object flavour of `obj_description`: `pg_database` rows
# are cluster-wide, so their comments live in `pg_shdescription`, not `pg_description`.
_DATABASES_SQL: Final = "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database"
_ROLES_SQL: Final = "SELECT rolname FROM pg_roles"

# `has_database_privilege(..., 'CONNECT')` is not decoration: `pg_database_size()` RAISES for
# a database the caller cannot connect to, and one such database on a shared cluster would
# turn the whole probe — and therefore the admin listing — into an error. The predicate makes
# an unreachable database simply absent from the result, which is exactly what "advisory"
# means.
_SIZES_SQL: Final = (
    "SELECT datname, pg_database_size(oid) FROM pg_database "
    "WHERE datname IN :names AND has_database_privilege(oid, 'CONNECT')"
)


@dataclass(frozen=True)
class CatalogDatabase:
    """One `pg_database` row as the classifier sees it: the name, and the provision COMMENT
    that is the only surviving evidence of when we created it."""

    name: str
    provision_comment: str | None


@dataclass(frozen=True)
class DatabaseCounts:
    """What the database sweep found. Counts ONLY — never a name list.

    `scanned == not_ours + owned + orphaned + unknown_age`, always.

    The blob reconciler collapses "fresh" and "age we cannot prove" into one `within_grace`
    bucket because both are protected identically. Here they are genuinely different
    findings, so `unknown_age` is its OWN counter rather than a silent share of `orphaned`:
    an orphan with a readable provision stamp is a real reclaim candidate an operator can
    age out, while an orphan with no parseable stamp is something we refuse to reason about
    at all. Conflating them would either overstate the actionable set or hide it.

    - `not_ours` — denylisted, or a name the derivation could never have produced.
    - `owned` — a `project_databases` row claims this exact name. NOT filtered by
      `db_ready`: a claim whose external sequence is still running already owns its name,
      and reporting a mid-provision database as an orphan would be a lie with a race in it.
    - `orphaned` — ours by name, claimed by no registry row, with a parseable provision
      stamp. The actionable set, and still nothing is deleted.
    - `unknown_age` — the same, minus the parseable stamp. Fail-closed: not actionable.
    """

    scanned: int
    not_ours: int
    owned: int
    orphaned: int
    unknown_age: int
    # Whole hours since the oldest orphan's provision stamp, or None when there are none.
    # An age, not an identity — it is what tells an operator whether the orphans are stale
    # or were minted by a provision that failed minutes ago.
    oldest_orphan_age_hours: int | None


@dataclass(frozen=True)
class RoleCounts:
    """What the role sweep found. Counts ONLY.

    `scanned == not_ours + owned + stranded + paired`, always.

    Roles are swept because teardown is best-effort PER STEP: `salt_the_earth` drops the
    database and then the role, so a failure between the two leaves a LOGIN role with a
    password the platform has already forgotten and a registry row that no longer exists —
    a latent re-entry handle, precisely the thing `NOCREATEROLE` exists to prevent
    elsewhere. That strand is `stranded`, and it is invisible to a database-only diff.

    - `paired` — ours by name, unregistered, but its `bialapp_<hex>` database still exists,
      so the DATABASE is already the finding; counting the role again would double-report it.
    """

    scanned: int
    not_ours: int
    owned: int
    stranded: int
    paired: int


@dataclass(frozen=True)
class AppDatabaseReconcileReport:
    """The whole sweep's outcome. Frozen value type, mapped to the API response by the
    router — deliberately NOT folded into `StorageReconcileReport`, whose
    `scanned == owned + within_grace + eligible` invariant means nothing for `pg_database`."""

    databases: DatabaseCounts
    roles: RoleCounts


def classify_databases(
    rows: Iterable[CatalogDatabase],
    *,
    registered: frozenset[str],
    denylist: frozenset[str],
    now: datetime.datetime,
) -> DatabaseCounts:
    """Bucket every enumerated database. Pure, so the guards can be proven exhaustively
    against a synthetic cluster that includes every dangerous name at once."""
    scanned = not_ours = owned = orphaned = unknown_age = 0
    oldest: datetime.timedelta | None = None
    for row in rows:
        scanned += 1
        # Guard 1, first and unconditionally: a denylisted name is never even looked at.
        if row.name in denylist:
            not_ours += 1
            continue
        if row.name in registered:
            owned += 1
            continue
        # Guard 2: the full-UUID anchor. `None` here means the derivation could not have
        # produced this name, so it belongs to someone else — leave it alone.
        if project_id_from_database_name(row.name) is None:
            not_ours += 1
            continue
        # Guard 3: no provable age is not an orphan we are willing to name as actionable.
        age = _provisioned_age(row.provision_comment, now=now)
        if age is None:
            unknown_age += 1
            continue
        orphaned += 1
        if oldest is None or age > oldest:
            oldest = age
    return DatabaseCounts(
        scanned=scanned,
        not_ours=not_ours,
        owned=owned,
        orphaned=orphaned,
        unknown_age=unknown_age,
        oldest_orphan_age_hours=None if oldest is None else int(oldest.total_seconds() // 3600),
    )


def classify_roles(
    names: Iterable[str],
    *,
    registered: frozenset[str],
    live_databases: frozenset[str],
    denylist: frozenset[str],
) -> RoleCounts:
    """Bucket every enumerated role, under the same three guards as the database sweep."""
    scanned = not_ours = owned = stranded = paired = 0
    for name in names:
        scanned += 1
        if name in denylist:
            not_ours += 1
            continue
        if name in registered:
            owned += 1
            continue
        project_id = project_id_from_role_name(name)
        if project_id is None:
            not_ours += 1
            continue
        # The paired database is re-derived, never guessed: same project id, same
        # derivation the provisioner used.
        if database_name(project_id) in live_databases:
            paired += 1
        else:
            stranded += 1
    return RoleCounts(
        scanned=scanned, not_ours=not_ours, owned=owned, stranded=stranded, paired=paired
    )


def _provisioned_age(comment: str | None, *, now: datetime.datetime) -> datetime.timedelta | None:
    """Age from the provision COMMENT, or `None` when it cannot be proven.

    Every failure mode collapses to `None` on purpose — absent comment, free-text comment
    someone wrote by hand, and a NAIVE timestamp (which cannot be compared to an aware
    `now` without inventing a timezone, and inventing one is how an orphan gets aged wrong).
    """
    if comment is None:
        return None
    try:
        stamped = datetime.datetime.fromisoformat(comment)
    except ValueError:
        return None
    if stamped.tzinfo is None:
        return None
    return now - stamped


async def reconcile_orphaned_app_databases(
    db: AsyncSession,
    engine: AsyncEngine,
    *,
    now: datetime.datetime | None = None,
) -> AppDatabaseReconcileReport:
    """Diff the maintenance cluster against the `project_databases` registry. Reports; never
    deletes, severs, or alters anything.

    Two catalog round trips and one registry read, so the counts are a consistent-enough
    snapshot without holding a lock on anything. `now` is injectable for tests only. A
    `SQLAlchemyError` (including an unreachable cluster) PROPAGATES — a partial report that
    read as "no orphans" would be the worst possible output of this function.
    """
    moment = now or datetime.datetime.now(datetime.UTC)
    claims: Sequence[sa.Row[tuple[str, str]]] = (
        await db.execute(sa.select(ProjectDatabase.db_name, ProjectDatabase.role_name))
    ).all()
    registered_databases = frozenset(str(row[0]) for row in claims)
    registered_roles = frozenset(str(row[1]) for row in claims)

    async with engine.connect() as conn:
        catalog = [
            CatalogDatabase(
                name=str(name), provision_comment=None if comment is None else str(comment)
            )
            for name, comment in (await conn.execute(sa.text(_DATABASES_SQL))).all()
        ]
        role_names = [str(row[0]) for row in (await conn.execute(sa.text(_ROLES_SQL))).all()]

    databases = classify_databases(
        catalog,
        registered=registered_databases,
        denylist=_database_denylist(),
        now=moment,
    )
    roles = classify_roles(
        role_names,
        registered=registered_roles,
        live_databases=frozenset(row.name for row in catalog),
        denylist=_role_denylist(),
    )
    # Counts only, here as everywhere: a log line naming an orphan database names a project.
    _log.info(
        "app_database_reconcile_completed",
        orphaned_databases=databases.orphaned,
        unknown_age_databases=databases.unknown_age,
        stranded_roles=roles.stranded,
    )
    return AppDatabaseReconcileReport(databases=databases, roles=roles)


async def advisory_database_sizes(engine: AsyncEngine, db_names: Sequence[str]) -> dict[str, int]:
    """On-disk bytes per database, keyed by name. **ADVISORY — never a limit.**

    Nothing in the platform reads this as a quota, a gate, or a precondition, and nothing
    may start: it replaced the retired `data_bytes` counter as an OBSERVATION for the admin
    listing, not as its enforcement half. `pg_database_size` is a whole-cluster point-in-time
    figure including bloat and indexes; treating it as a limit would throttle an app for
    disk it does not logically hold and for which no user-facing story exists.

    One query for every name — never per-app, which on the listing would be an N+1 against
    the cluster. A database the caller cannot connect to is simply absent from the result.
    """
    if not db_names:
        return {}
    statement = sa.text(_SIZES_SQL).bindparams(sa.bindparam("names", expanding=True))
    async with engine.connect() as conn:
        result = (await conn.execute(statement, {"names": list(db_names)})).all()
    return {str(name): int(size) for name, size in result}


def _database_denylist() -> frozenset[str]:
    """Guard 1 for databases: the static system/control-plane set, plus whatever databases
    THIS deployment's own DSNs point at (so a renamed control-plane database is covered
    without editing a constant)."""
    from src.config import settings  # lazy: avoid an import cycle via src.config

    names = set(_SYSTEM_DATABASES)
    names.add(_dsn_database(settings.DATABASE_URL.get_secret_value()))
    if settings.app_db is not None:
        names.add(_dsn_database(settings.app_db.maintenance_dsn.get_secret_value()))
    names.discard("")
    return frozenset(names)


def _role_denylist() -> frozenset[str]:
    """Guard 1 for roles: the static set plus the maintenance identity itself — the one role
    whose loss would take the whole substrate with it."""
    from src.config import settings  # lazy: avoid an import cycle via src.config

    names = set(_SYSTEM_ROLES)
    if settings.app_db is not None:
        names.add(_dsn_username(settings.app_db.maintenance_dsn.get_secret_value()))
    names.discard("")
    return frozenset(names)


def _dsn_database(dsn: str) -> str:
    return make_url(dsn).database or ""


def _dsn_username(dsn: str) -> str:
    return make_url(dsn).username or ""
