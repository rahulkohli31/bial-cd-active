"""Operator-invoked orphan reconciler for per-project databases and roles — REPORT-ONLY, plus
the advisory `pg_database_size()` probe the admin listing renders.

Teardown is per-step best-effort, so a failed drop can leave a database, or a surviving LOGIN
role whose database is already gone. This diffs the cluster against `project_databases`.

NOTHING HERE DELETES ANYTHING: `pg_database` carries no creation timestamp, and the
provision-time `COMMENT ON DATABASE` standing in for one is mutable and easily lost, so
delete-eligibility is a human ruling made with this report in hand. Three guards must all agree
before a name is reported — denylist, full-UUID anchor, fail closed on anything unparseable.
The MAINTENANCE cluster enumerated and the CONTROL-PLANE registry it is diffed against can
legitimately disagree on a shared box."""

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
    """What the database sweep found. `scanned == not_ours + owned + orphaned + unknown_age`.

    - `not_ours` — denylisted, or a name the derivation could never have produced.
    - `owned` — a `project_databases` row claims this name. NOT filtered by `db_ready`: a claim
      whose external sequence is still running already owns its name.
    - `orphaned` — ours by name, unclaimed, with a parseable provision stamp. The actionable
      set, and still nothing is deleted.
    - `unknown_age` — the same, minus the stamp. Its own counter, never a share of `orphaned`."""

    scanned: int
    not_ours: int
    owned: int
    orphaned: int
    unknown_age: int
    # Whole hours since the oldest orphan's provision stamp, or None when there are none —
    # what tells an operator whether the orphans are stale or were minted by a provision
    # that failed minutes ago.
    oldest_orphan_age_hours: int | None


@dataclass(frozen=True)
class RoleCounts:
    """What the role sweep found. `scanned == not_ours + owned + stranded + paired`.

    Roles are swept because teardown is best-effort PER STEP: `salt_the_earth` drops the
    database and then the role, so a failure between the two leaves a LOGIN role with a
    password the platform has already forgotten and a registry row that no longer exists —
    a latent re-entry handle invisible to a database-only diff. That strand is `stranded`.
    `paired` is ours by name and unregistered but its `bialapp_<hex>` database still exists,
    so the DATABASE is already the finding."""

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
    claims: Sequence[sa.Row[str, str]] = (
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
    # Counts, never names — the same rule `append_audit` holds for audit rows.
    _log.info(
        "app_database_reconcile_completed",
        orphaned_databases=databases.orphaned,
        unknown_age_databases=databases.unknown_age,
        stranded_roles=roles.stranded,
    )
    return AppDatabaseReconcileReport(databases=databases, roles=roles)


async def advisory_database_sizes(engine: AsyncEngine, db_names: Sequence[str]) -> dict[str, int]:
    """On-disk bytes per database, keyed by name. **ADVISORY — never a limit.**

    Nothing reads this as a quota, a gate, or a precondition, and nothing may start:
    `pg_database_size` is a point-in-time figure including bloat and indexes, so treating it as
    a limit would throttle an app for disk it does not logically hold.

    One query for every name — never per-app, which on the listing would be an N+1 against the
    cluster. A database the caller cannot connect to is simply absent from the result."""
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
