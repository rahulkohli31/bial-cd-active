"""The orphan reconciler — REPORT-ONLY, for databases and for stranded roles.

Two layers, deliberately:

* **Pure classifier tests** feed a synthetic cluster containing every dangerous name at
  once — PostgreSQL's own, both control-plane lanes, Azure's, and the six unrelated
  databases that really do share this dev box. That is the DECISION site, so proving the
  guards there proves them exhaustively; an integration test can only prove them for
  whatever names happen to exist on the machine it runs on.
* **Integration tests** create real orphans on the real cluster and assert both that the
  report finds them AND that they are still there afterwards. "Nothing is deleted" is the
  entire safety contract of this unit, so it is asserted against the catalog, never inferred
  from the absence of a `DROP` in the source.

Every project that gets provisioned is registered with `salted` FIRST, so a failure
mid-test still cleans up — the `db_session` rollback cannot undo cluster DDL.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.project_database import ProjectDatabase
from src.services.appdb.engine import (
    get_maintenance_engine,
    reset_maintenance_engine_for_tests,
)
from src.services.appdb.names import database_name, quote_identifier, role_name
from src.services.appdb.provision import ensure_project_database
from src.services.appdb.reconcile import (
    AppDatabaseReconcileReport,
    CatalogDatabase,
    DatabaseCounts,
    advisory_database_sizes,
    classify_databases,
    classify_roles,
    reconcile_orphaned_app_databases,
)
from tests.factories import ProjectFactory, UserFactory

_DATABASE_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :db)"
_ROLE_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"

# The names that must NEVER be actionable. The first block is the system/control-plane
# denylist (D20); the second is the set of unrelated databases this dev cluster genuinely
# hosts — they are protected by the full-uuid name anchor, not by the denylist, and this is
# the test that proves it.
_SYSTEM_NAMES = (
    "postgres",
    "template0",
    "template1",
    "citizen_one",
    "citizen_one_test",
    "citizen_one_e2e",
    "azure_maintenance",
    "azure_sys",
)
_UNRELATED_NAMES = ("ascend", "badger", "badger_test", "knit", "neox", "singularity", "tod")

_NOW = datetime.datetime(2026, 7, 21, 12, 0, tzinfo=datetime.UTC)


def _stamp(hours_ago: int) -> str:
    return (_NOW - datetime.timedelta(hours=hours_ago)).isoformat()


def _catalog(*names_and_comments: tuple[str, str | None]) -> list[CatalogDatabase]:
    return [
        CatalogDatabase(name=name, provision_comment=comment)
        for name, comment in names_and_comments
    ]


# --- the guards, proven against a synthetic cluster ------------------------------------


def test_no_system_or_unrelated_database_is_ever_actionable() -> None:
    # THE test this unit exists to be safe under: a cluster full of databases that are not
    # ours, none of them registered, all of them carrying a perfectly parseable comment (so
    # the age guard cannot be what saves them). Every single one must land in `not_ours`.
    rows = _catalog(*((name, _stamp(500)) for name in _SYSTEM_NAMES + _UNRELATED_NAMES))

    counts = classify_databases(
        rows, registered=frozenset(), denylist=frozenset(_SYSTEM_NAMES), now=_NOW
    )

    assert counts.scanned == len(_SYSTEM_NAMES) + len(_UNRELATED_NAMES)
    assert counts.not_ours == counts.scanned
    assert counts.orphaned == 0
    assert counts.unknown_age == 0
    assert counts.owned == 0
    assert counts.oldest_orphan_age_hours is None


def test_a_name_that_does_not_parse_is_not_actionable() -> None:
    # Near-misses on the derivation: right prefix, wrong tail. The parser fails closed, so
    # each one is someone else's database as far as this sweep is concerned.
    rows = _catalog(
        ("bialapp_", _stamp(72)),
        ("bialapp_notahexstring", _stamp(72)),
        ("bialapp_" + "f" * 31, _stamp(72)),  # 31 hex chars — one short of a full uuid
        ("bialapp_" + "f" * 33, _stamp(72)),
        ("bialapp_" + "F" * 32, _stamp(72)),  # uppercase — the derivation emits lowercase
        ("bialappx_" + "f" * 32, _stamp(72)),
    )

    counts = classify_databases(rows, registered=frozenset(), denylist=frozenset(), now=_NOW)

    assert counts.not_ours == 6
    assert counts.orphaned == 0


def test_an_orphan_with_a_parseable_stamp_is_the_actionable_bucket() -> None:
    old, recent = uuid.uuid7(), uuid.uuid7()
    rows = _catalog(
        (database_name(old), _stamp(200)),
        (database_name(recent), _stamp(3)),
    )

    counts = classify_databases(rows, registered=frozenset(), denylist=frozenset(), now=_NOW)

    assert counts.orphaned == 2
    assert counts.unknown_age == 0
    # The oldest, not the newest — an operator sizing up how stale the strand is.
    assert counts.oldest_orphan_age_hours == 200


@pytest.mark.parametrize(
    "comment",
    [
        None,  # never provisioned by us, or the comment was dropped
        "",
        "provisioned by hand",
        "2026-07-21T12:00:00",  # NAIVE — comparing it means inventing a timezone
        "2026-13-45T99:99:99+00:00",
    ],
)
def test_an_orphan_with_no_provable_age_gets_its_own_bucket(comment: str | None) -> None:
    # `pg_database` has no creation timestamp, so an unreadable COMMENT means the age is
    # simply unknown. It is a DIFFERENT finding from "orphaned for 200 hours", and the blob
    # reconciler's habit of folding unknown-age into the protected bucket would hide it.
    rows = _catalog((database_name(uuid.uuid7()), comment))

    counts = classify_databases(rows, registered=frozenset(), denylist=frozenset(), now=_NOW)

    assert counts.unknown_age == 1
    assert counts.orphaned == 0
    assert counts.oldest_orphan_age_hours is None


def test_a_registered_database_is_owned_whatever_its_comment_says() -> None:
    # Registry existence wins over the age guard: a claim whose external sequence is still
    # running has no comment yet, and reporting it as an orphan would be a lie with a race
    # in it.
    live = database_name(uuid.uuid7())
    rows = _catalog((live, None))

    counts = classify_databases(rows, registered=frozenset({live}), denylist=frozenset(), now=_NOW)

    assert counts == DatabaseCounts(
        scanned=1, not_ours=0, owned=1, orphaned=0, unknown_age=0, oldest_orphan_age_hours=None
    )


def test_the_database_sum_invariant_holds() -> None:
    registered = database_name(uuid.uuid7())
    rows = _catalog(
        ("postgres", None),
        ("ascend", None),
        (registered, _stamp(1)),
        (database_name(uuid.uuid7()), _stamp(100)),
        (database_name(uuid.uuid7()), "junk"),
    )

    counts = classify_databases(
        rows, registered=frozenset({registered}), denylist=frozenset({"postgres"}), now=_NOW
    )

    assert counts.scanned == counts.not_ours + counts.owned + counts.orphaned + counts.unknown_age
    assert (counts.not_ours, counts.owned, counts.orphaned, counts.unknown_age) == (2, 1, 1, 1)


# --- the role half ----------------------------------------------------------------------


def test_a_role_whose_database_is_gone_is_stranded() -> None:
    # The finding a database-only diff cannot make: `salt_the_earth` drops the database and
    # THEN the role, so a failure between the two leaves a LOGIN role holding a password the
    # platform has already forgotten.
    stranded = uuid.uuid7()

    counts = classify_roles(
        [role_name(stranded)],
        registered=frozenset(),
        live_databases=frozenset(),
        denylist=frozenset(),
    )

    assert counts.stranded == 1
    assert counts.paired == 0


def test_a_role_whose_database_still_exists_is_only_paired() -> None:
    # Its database is already the reported orphan; counting the role again would
    # double-report one strand as two.
    paired = uuid.uuid7()

    counts = classify_roles(
        [role_name(paired)],
        registered=frozenset(),
        live_databases=frozenset({database_name(paired)}),
        denylist=frozenset(),
    )

    assert (counts.paired, counts.stranded) == (1, 0)


def test_system_and_unparseable_roles_are_never_stranded() -> None:
    names = [
        "postgres",
        "bial_appdb_maint",
        "pg_read_all_stats",
        "pg_monitor",
        "bial",
        "bialrole_",
        "bialrole_notahex",
        "bialrole_" + "f" * 31,
    ]

    counts = classify_roles(
        names,
        registered=frozenset(),
        live_databases=frozenset(),
        denylist=frozenset({"postgres", "bial_appdb_maint"}),
    )

    assert counts.not_ours == len(names)
    assert counts.stranded == 0
    assert counts.scanned == counts.not_ours + counts.owned + counts.stranded + counts.paired


def test_a_registered_role_is_owned() -> None:
    project_id = uuid.uuid7()

    counts = classify_roles(
        [role_name(project_id)],
        registered=frozenset({role_name(project_id)}),
        live_databases=frozenset(),
        denylist=frozenset(),
    )

    assert (counts.owned, counts.stranded) == (1, 0)


# --- integration: real orphans on the real cluster --------------------------------------


async def _report(db: AsyncSession) -> AppDatabaseReconcileReport:
    engine = get_maintenance_engine()
    assert engine is not None, "APP_DB__* must be configured in .env.test"
    return await reconcile_orphaned_app_databases(db, engine)


async def _catalog_scalar(sql: str, **params: Any) -> Any:
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        return await conn.scalar(sa.text(sql), params)


async def _provisioned(db: AsyncSession, salted: list[uuid.UUID]) -> ProjectDatabase:
    user = await UserFactory.create(db, email=f"appdb-{uuid.uuid4().hex}@rvaiglobal.com")
    project = await ProjectFactory.create(db, user.id)
    salted.append(project.id)  # register FIRST — cleanup must survive a mid-test failure
    record = await ensure_project_database(db, project.id)
    assert record is not None
    return record


async def _forget_the_registry_row(db: AsyncSession, record: ProjectDatabase) -> None:
    """Exactly what a failed `salt_the_earth` leaves behind: the row is gone (the delete
    committed) and the cluster objects survive."""
    await db.execute(sa.delete(ProjectDatabase).where(ProjectDatabase.id == record.id))


async def _verdict_for(db: AsyncSession, db_name: str) -> DatabaseCounts:
    """The real classifier's verdict on ONE real database, read live from the catalog.

    A whole-cluster before/after delta is NOT a stable measurement here and pretending
    otherwise buys a flaky test: `.env.test` configures a real substrate, so every project
    any test in the session creates leaves a real database behind (the conftest hook salts
    them only at session END), and a background provision can land between two reports.
    Restricting the catalog read to the name this test created makes the bucket assertion
    exact while still measuring the REAL cluster row and the REAL registry through the same
    classifier the driver uses. The driver's own wiring is covered separately.
    """
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT datname, shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = :db"
                ),
                {"db": db_name},
            )
        ).one_or_none()
    assert row is not None, f"{db_name} is not on the cluster"
    claims = (await db.execute(sa.select(ProjectDatabase.db_name))).scalars().all()
    return classify_databases(
        [CatalogDatabase(name=str(row[0]), provision_comment=row[1])],
        registered=frozenset(claims),
        # Empty: a `bialapp_*` name can never be denylisted, and the denylist is proven
        # exhaustively against every dangerous name in the pure tests above.
        denylist=frozenset(),
        now=datetime.datetime.now(datetime.UTC),
    )


async def test_a_healthy_provisioned_project_is_owned_never_an_orphan(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    before = await _report(db_session)
    record = await _provisioned(db_session, salted)

    verdict = await _verdict_for(db_session, record.db_name)

    assert verdict == DatabaseCounts(
        scanned=1, not_ours=0, owned=1, orphaned=0, unknown_age=0, oldest_orphan_age_hours=None
    )
    # The full driver agrees: `owned` is the one bucket nothing else in the session can move
    # (every other test's registry row is rolled back, so its database reads as an orphan).
    after = await _report(db_session)
    assert after.databases.owned == before.databases.owned + 1
    assert after.roles.owned == before.roles.owned + 1


async def test_an_orphan_database_is_reported_and_still_exists_afterwards(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    before = await _report(db_session)
    record = await _provisioned(db_session, salted)
    await _forget_the_registry_row(db_session, record)

    verdict = await _verdict_for(db_session, record.db_name)

    # A freshly provisioned database is hours-old at most, and its stamp is readable — which
    # is what puts it in the ACTIONABLE bucket rather than the unknown-age one.
    assert (verdict.orphaned, verdict.owned, verdict.unknown_age) == (1, 0, 0)
    assert verdict.oldest_orphan_age_hours is not None
    # THE contract of this whole unit: reported, and untouched. Report-only is not a phase.
    assert await _catalog_scalar(_DATABASE_EXISTS, db=record.db_name) is True
    assert await _catalog_scalar(_ROLE_EXISTS, role=record.role_name) is True
    after = await _report(db_session)
    assert after.databases.orphaned >= before.databases.orphaned + 1
    # Its role is `paired`, not double-counted as a second strand: the database is already
    # the finding. Nothing else in the session strands a role, so this delta IS exact.
    assert after.roles.stranded == before.roles.stranded


async def test_an_orphan_with_an_unreadable_comment_lands_in_unknown_age(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    before = await _report(db_session)
    record = await _provisioned(db_session, salted)
    await _forget_the_registry_row(db_session, record)
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(f"COMMENT ON DATABASE {quote_identifier(record.db_name)} IS 'not a date'")
        )

    verdict = await _verdict_for(db_session, record.db_name)

    assert (verdict.unknown_age, verdict.orphaned, verdict.owned) == (1, 0, 0)
    assert await _catalog_scalar(_DATABASE_EXISTS, db=record.db_name) is True
    # No other test writes an unreadable comment, so the driver's delta is exact too.
    after = await _report(db_session)
    assert after.databases.unknown_age == before.databases.unknown_age + 1


async def test_a_role_stranded_by_a_half_finished_teardown_is_reported(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    # The exact residue of a `salt_the_earth` that dropped the database and then failed to
    # drop the role: a LOGIN role with no database and no registry row. Nothing else in the
    # session produces one (every other provision keeps its database), so unlike the orphan
    # DATABASE count this delta is a stable measurement.
    project_id = uuid.uuid7()
    salted.append(project_id)  # `salt_the_earth` is idempotent — this drops the role after
    before = await _report(db_session)
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(f"CREATE ROLE {quote_identifier(role_name(project_id))} LOGIN NOSUPERUSER")
        )

    after = await _report(db_session)

    assert after.roles.stranded == before.roles.stranded + 1
    assert await _catalog_scalar(_ROLE_EXISTS, role=role_name(project_id)) is True
    # Reported, and still there — the role half is report-only too.
    assert await _catalog_scalar(_DATABASE_EXISTS, db=database_name(project_id)) is False


async def test_the_live_control_plane_databases_are_never_reported(
    db_session: AsyncSession,
) -> None:
    # Belt over the synthetic braces: run the real sweep against the real cluster (which
    # hosts `citizen_one`, `citizen_one_test`, `postgres`, and whatever else a developer
    # keeps here) with an empty registry expectation, and confirm nothing was attributed.
    report = await _report(db_session)

    assert report.databases.scanned >= 3
    assert report.databases.scanned == (
        report.databases.not_ours
        + report.databases.owned
        + report.databases.orphaned
        + report.databases.unknown_age
    )
    assert report.databases.not_ours >= 3


# --- the advisory size probe -------------------------------------------------------------


async def test_the_size_probe_answers_for_a_real_database(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    record = await _provisioned(db_session, salted)
    engine = get_maintenance_engine()
    assert engine is not None

    sizes = await advisory_database_sizes(engine, [record.db_name])

    assert sizes[record.db_name] > 0


async def test_the_size_probe_omits_a_database_that_does_not_exist(
    db_session: AsyncSession,
) -> None:
    # Absent, not zero and not an error: a name with no database is simply missing from the
    # map, so the listing renders no number rather than an authoritative-looking `0 B`.
    del db_session
    engine = get_maintenance_engine()
    assert engine is not None

    assert await advisory_database_sizes(engine, [database_name(uuid.uuid7())]) == {}
    assert await advisory_database_sizes(engine, []) == {}


async def test_the_reconciler_needs_no_substrate_to_be_absent_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The unconfigured deployment is a supported one (`X | None` + a prod gate), so the
    # accessor the endpoint resolves lazily must simply answer `None` rather than raise.
    await reset_maintenance_engine_for_tests()
    monkeypatch.setattr(settings, "app_db", None)
    try:
        assert get_maintenance_engine() is None
    finally:
        await reset_maintenance_engine_for_tests()
