"""Revision 0036: the `deleted_projects` tombstone.

DEFAULT LANE ONLY (L11 \u2014 every up/down round-trip permanently burns `pg_attribute` slots on
the shared `citizen_one_test` database, so round-trip coverage is budgeted). This revision
creates one table and drops it; there is no data step to walk, so the shape the fresh upgrade
left is the whole of what there is to assert.

The three properties below are each a decision that was made twice \u2014 once in the model and
once in the migration \u2014 and `--autogenerate` only catches a disagreement between them, never
a matching pair that is wrong. So they are pinned against the REAL migrated schema:

  * `project_id` is UNIQUE, which is what makes a double-submitted delete write one tombstone
    instead of two. The ownership read takes no row lock and the cascade deletes through Core
    `sa.delete()`, so nothing else in the path would notice the second request.
  * `id` defaults to `uuidv7()` like every other table (ADR-0006). It was briefly a
    Python-side `uuid4`, which left this the one table whose keys do not order by creation
    time \u2014 on an audit table read newest-first, the worst one to lose.
  * `project_id` carries NO foreign key. The row it would reference is gone by the time this
    is written, so an FK here would be unsatisfiable rather than merely unnecessary.
"""

from __future__ import annotations

import sqlalchemy as sa

_TABLE = "deleted_projects"

_INDEX_SQL = (
    "SELECT i.relname AS name, ix.indisunique AS is_unique "
    "FROM pg_class t "
    "JOIN pg_index ix ON t.oid = ix.indrelid "
    "JOIN pg_class i ON i.oid = ix.indexrelid "
    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey) "
    "WHERE t.relname = :table AND a.attname = :column"
)
_COLUMN_SQL = (
    "SELECT is_nullable, column_default FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = :column"
)


async def test_one_tombstone_per_project_is_enforced_by_the_database(db_session) -> None:
    """THE GUARD, not an optimisation.

    Two concurrent DELETEs both passed the ownership read and both ran the cascade, writing
    two tombstones and duplicating the audit rows for ONE physical deletion \u2014 and both
    returned 200. On the table whose entire job is to be an accurate record of an
    irreversible action, an administrator who cannot tell one deletion from two is the
    failure. The loser of that race now fails closed here.
    """
    rows = (
        await db_session.execute(sa.text(_INDEX_SQL), {"table": _TABLE, "column": "project_id"})
    ).all()
    assert rows, "project_id has no index at all"
    found = [(r.name, r.is_unique) for r in rows]
    assert any(r.is_unique for r in rows), f"no UNIQUE index on project_id, found {found}"


async def test_the_primary_key_is_uuidv7_like_every_other_table(db_session) -> None:
    row = (await db_session.execute(sa.text(_COLUMN_SQL), {"table": _TABLE, "column": "id"})).one()
    assert row.is_nullable == "NO"
    assert row.column_default is not None, "no server default \u2014 a Python-side uuid4 again?"
    assert "uuidv7" in row.column_default


async def test_project_id_is_deliberately_not_a_foreign_key(db_session) -> None:
    """The row it would point at is deleted in the same transaction that writes this one."""
    found = (
        await db_session.execute(
            sa.text(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :table AND c.contype = 'f'"
            ),
            {"table": _TABLE},
        )
    ).all()
    assert found == []


async def test_the_owner_is_indexed_so_what_one_person_deleted_is_cheap_to_read(
    db_session,
) -> None:
    """One of the two ways an administrator reads this table; the other is by project."""
    rows = (
        await db_session.execute(sa.text(_INDEX_SQL), {"table": _TABLE, "column": "owner_id"})
    ).all()
    assert rows, "owner_id is not indexed"
