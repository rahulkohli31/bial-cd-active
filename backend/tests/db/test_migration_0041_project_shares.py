"""Revision 0041: `project_shares`, the platform's first junction table (#198 slice 1).

DEFAULT LANE ONLY (L11) — round-trip coverage is budgeted: every up/down cycle permanently
burns `pg_attribute` slots on the shared `citizen_one_test` database. This revision only
creates and drops one table, so the fresh-upgrade shape is all there is to assert.

Each property below was decided twice — once in the model (`db/models/project_share.py`),
once in the migration — and `--autogenerate` only catches a disagreement between them, never
a matching pair that is wrong. So each is pinned against the REAL migrated schema, not just
the model.
"""

from __future__ import annotations

import sqlalchemy as sa

_TABLE = "project_shares"

_COLUMN_SQL = (
    "SELECT is_nullable, column_default FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = :column"
)
_FK_SQL = (
    "SELECT a.attname AS column, confrelid::regclass AS references_table, confdeltype "
    "FROM pg_constraint c "
    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
    # `CAST(:table AS regclass)`, not `:table::regclass` — SQLAlchemy's `text()` bind scanner
    # does not treat `:table` as a parameter when a `::` cast follows it immediately, so the
    # literal reached Postgres unbound and this assertion never actually ran (a syntax error,
    # not a passing check). The unambiguous cast keeps the bind alive.
    "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'f' AND a.attname = :column"
)


async def test_the_primary_key_is_uuidv7_like_every_other_table(db_session) -> None:
    row = (await db_session.execute(sa.text(_COLUMN_SQL), {"table": _TABLE, "column": "id"})).one()
    assert row.is_nullable == "NO"
    assert row.column_default is not None
    assert "uuidv7" in row.column_default


async def test_one_share_per_project_and_recipient_is_enforced_by_the_database(
    db_session,
) -> None:
    """THE re-share idempotency guard (R3) — `create_share`'s `ON CONFLICT DO NOTHING`
    inference target only works if this unique index actually exists."""
    row = (
        await db_session.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = :table AND indexname = 'uq_project_shares_project_recipient'"
            ),
            {"table": _TABLE},
        )
    ).one_or_none()
    assert row is not None, "uq_project_shares_project_recipient is missing"
    assert "UNIQUE" in row.indexdef
    assert "project_id" in row.indexdef
    assert "shared_with_user_id" in row.indexdef


async def test_both_foreign_keys_cascade_on_delete(db_session) -> None:
    """No object-store footprint to sweep for a share row, unlike `delete_project_cascade`'s
    explicit per-row app/conversation deletes — so both FKs cascade at the DB level rather
    than needing an application-level teardown step."""
    project_fk = (
        await db_session.execute(sa.text(_FK_SQL), {"table": _TABLE, "column": "project_id"})
    ).one()
    assert project_fk.references_table == "projects"
    assert project_fk.confdeltype == b"c"  # 'c' = CASCADE; asyncpg reads pg "char" as bytes

    recipient_fk = (
        await db_session.execute(
            sa.text(_FK_SQL), {"table": _TABLE, "column": "shared_with_user_id"}
        )
    ).one()
    assert recipient_fk.references_table == "users"
    assert recipient_fk.confdeltype == b"c"


async def test_project_id_and_recipient_are_each_individually_indexed(db_session) -> None:
    """Beyond the composite unique index above — `list_shares_for_project` filters by
    `project_id` alone, `list_shared_with_me`/`resolve_project_access` filter by
    `shared_with_user_id` alone, and neither is the LEADING column of the other's lookup."""
    for column, index_name in (
        ("project_id", "ix_project_shares_project_id"),
        ("shared_with_user_id", "ix_project_shares_shared_with_user_id"),
    ):
        row = (
            await db_session.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": index_name},
            )
        ).one_or_none()
        assert row is not None, f"{index_name} is missing"
        assert column in row.indexdef


async def test_there_is_no_shared_by_user_id_column(db_session) -> None:
    """Deliberately absent (`db/models/project_share.py`'s own design note) — it would always
    equal `projects.user_id`, since only an owner can create a share. Pinned here so a future
    author who "helpfully" denormalizes it back in gets caught."""
    row = (
        await db_session.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = 'shared_by_user_id'"
            ),
            {"table": _TABLE},
        )
    ).one_or_none()
    assert row is None
