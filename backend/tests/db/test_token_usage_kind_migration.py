"""`token_usage.kind` against the REAL migrated schema (U15).

The test DB carries the column from `alembic upgrade head` (revision
0031_token_usage_kind), so the shape assertions exercise the actual DDL — a native
two-label enum, NOT NULL with a `build` default, and the widened `(user_id,
usage_date, kind)` uniqueness — inside the rolled-back per-test transaction.
`test_suspended_at_migration.py` pins the chain's exact head at 0031 and
`tests/test_alembic_single_head.py` guards the head count.

The backfill and the reverse CANNOT be exercised the 0030 way (importing the
migration's statement and running it over ORM-seeded rows): the fresh-upgrade
schema already pins `kind` NOT NULL, so a pre-dimension NULL-kind row is
unseedable in-suite and `BACKFILL_KIND_BUILD` would be a no-op over anything the
suite can create. The round-trip test below therefore walks the chain for real —
head → 0030 → head over raw-SQL-seeded rows — and lives in the destructive lane
(`uv run pytest -m destructive_migration`), because every up/down round-trip
permanently burns pg_attribute slots on the shared test DB.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.db.models.token_usage import TokenUsage, TokenUsageKind
from src.services.usage.gate import ist_today, record_usage
from tests.factories import UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


# --- shape: the DDL the fresh upgrade applied ------------------------------------


async def test_kind_lands_not_null_with_the_build_default(db_session) -> None:
    row = (
        await db_session.execute(
            sa.text(
                "SELECT data_type, udt_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'token_usage' AND column_name = 'kind'"
            )
        )
    ).one()
    # The kind is the NATIVE enum (ADR-0008), not a varchar with a check…
    assert row.data_type == "USER-DEFINED"
    assert row.udt_name == "token_usage_kind"
    # …and it is NOT NULL with `build` as the database-side default: `build` is the
    # DEFINED meaning of an unspecified kind (every pre-dimension writer was a build
    # writer), so raw inserts behave exactly as they always did.
    assert row.is_nullable == "NO"
    assert row.column_default == "'build'::token_usage_kind"


async def test_enum_labels_are_exactly_the_two_kinds(db_session) -> None:
    labels = (
        (
            await db_session.execute(
                sa.text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'token_usage_kind' ORDER BY e.enumsortorder"
                )
            )
        )
        .scalars()
        .all()
    )
    assert labels == ["build", "review"]
    # …and the Python enum's values ARE those labels (values_callable convention).
    assert [member.value for member in TokenUsageKind] == labels


async def test_uniqueness_is_user_date_kind_and_the_old_constraint_is_gone(db_session) -> None:
    rows = (
        await db_session.execute(
            sa.text(
                "SELECT conname, pg_get_constraintdef(oid) AS condef FROM pg_constraint "
                "WHERE conrelid = 'token_usage'::regclass AND contype = 'u'"
            )
        )
    ).all()
    # The upsert's conflict target widened: one row per user per IST day PER KIND. The
    # old two-column uniqueness must be GONE — were both present, a same-day review row
    # would violate the old one and the U15 second row could never exist.
    assert {row.conname for row in rows} == {"uq_token_usage_user_date_kind"}
    assert rows[0].condef == "UNIQUE (user_id, usage_date, kind)"


async def test_a_second_same_day_row_exists_per_kind_and_only_per_kind(db_session) -> None:
    # The widened constraint holds exactly what U15 needs: same user + same day is fine
    # across kinds (two rows), and still ONE row within a kind (the fold's target).
    user = await UserFactory.create(db_session)
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=1)
    await record_usage(
        db_session, user.id, input_tokens=20, output_tokens=2, kind=TokenUsageKind.REVIEW
    )
    rows = (
        (
            await db_session.execute(
                select(TokenUsage).where(
                    TokenUsage.user_id == user.id, TokenUsage.usage_date == ist_today()
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.kind for row in rows} == {TokenUsageKind.BUILD, TokenUsageKind.REVIEW}
    assert len(rows) == 2


# --- the backfill + the reverse: the real chain walk (destructive lane) -----------


_COLUMNS_SQL = "SELECT column_name FROM information_schema.columns WHERE table_name = :table"
_CONSTRAINTS_SQL = "SELECT conname FROM pg_constraint WHERE conrelid = 'token_usage'::regclass"


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _sql(statements: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Run raw parameterized SQL between alembic commands on a fresh engine (alembic's
    env.py owns the loop during the commands, so this runs outside the suite's
    per-test transaction and must commit its own work)."""

    async def _run() -> list[Any]:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        results: list[Any] = []
        try:
            async with engine.begin() as conn:
                for statement, params in statements:
                    result = await conn.execute(sa.text(statement), params)
                    # INSERT/DELETE results hold no rows; .all() on them raises.
                    results.append(result.all() if result.returns_rows else None)
        finally:
            await engine.dispose()
        return results

    return asyncio.run(_run())


@pytest.mark.destructive_migration
def test_downgrade_deletes_review_rows_and_upgrade_backfills_build() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize the start state (a no-op when at head)
    user_id = uuid.uuid4()
    try:
        # Seed one build row and one same-day review row for a throwaway user.
        _sql(
            [
                (
                    "INSERT INTO users (id, azure_oid, email, token_version) "
                    "VALUES (:id, :oid, :email, 0)",
                    {
                        "id": user_id,
                        "oid": f"u15-oid-{user_id.hex[:8]}",
                        "email": f"u15-roundtrip-{user_id.hex[:8]}@rvaiglobal.com",
                    },
                ),
                (
                    "INSERT INTO token_usage (id, user_id, usage_date, kind, input_tokens, "
                    "output_tokens) VALUES (:id, :user_id, CURRENT_DATE, 'build', 100, 20)",
                    {"id": uuid.uuid4(), "user_id": user_id},
                ),
                (
                    "INSERT INTO token_usage (id, user_id, usage_date, kind, input_tokens, "
                    "output_tokens) VALUES (:id, :user_id, CURRENT_DATE, 'review', 7, 3)",
                    {"id": uuid.uuid4(), "user_id": user_id},
                ),
            ]
        )

        command.downgrade(config, "0030_approval_route_declaration")
        columns, constraints, rows, enum_type = _sql(
            [
                (_COLUMNS_SQL, {"table": "token_usage"}),
                (_CONSTRAINTS_SQL, {}),
                (
                    "SELECT input_tokens, output_tokens FROM token_usage WHERE user_id = :user_id",
                    {"user_id": user_id},
                ),
                ("SELECT typname FROM pg_type WHERE typname = 'token_usage_kind'", {}),
            ]
        )
        # The kind column and its enum are gone; the old two-column uniqueness is back.
        assert "kind" not in {row.column_name for row in columns}
        constraint_names = {row.conname for row in constraints}
        assert "uq_token_usage_user_date" in constraint_names
        assert "uq_token_usage_user_date_kind" not in constraint_names
        assert enum_type == []
        # The review row is DELETED (the pre-kind schema cannot hold it, and folding it
        # into build would retroactively bill the citizen for reviews); the build row's
        # counters survive untouched.
        assert [(row.input_tokens, row.output_tokens) for row in rows] == [(100, 20)]

        command.upgrade(config, "head")
        constraints, rows = _sql(
            [
                (_CONSTRAINTS_SQL, {}),
                (
                    "SELECT kind::text AS kind, input_tokens FROM token_usage "
                    "WHERE user_id = :user_id",
                    {"user_id": user_id},
                ),
            ]
        )
        # Every surviving row is backfilled to `build` — that is what it has always
        # been — and the widened uniqueness is back in force.
        constraint_names = {row.conname for row in constraints}
        assert "uq_token_usage_user_date_kind" in constraint_names
        assert "uq_token_usage_user_date" not in constraint_names
        assert [(row.kind, row.input_tokens) for row in rows] == [("build", 100)]
    finally:
        # ALWAYS restore head and drop the seeded user (token_usage rows cascade) —
        # even on assertion failure — so the rest of the suite runs clean.
        command.upgrade(config, "head")
        _sql([("DELETE FROM users WHERE id = :user_id", {"user_id": user_id})])
