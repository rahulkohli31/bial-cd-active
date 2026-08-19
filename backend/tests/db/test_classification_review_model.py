"""The `classification_reviews` row shape and — the real subject — the one-row-per-app
constraint the claim-or-return semantics stand on.

`uq_classification_reviews_app` is both the invariant (a stored answer can never coexist
with a rival answer for the same app) and the store's `ON CONFLICT` inference target, so
these tests pin it against the real migrated schema, not the ORM's idea of it: the DATABASE
refuses a second row, and the migration round-trip proves the constraint, the enum type and
every column actually travel with the revision in both directions.

Mirrors `test_deployments_model.py` / `test_deployments_migration.py` — the round-trip is
marked `destructive_migration` (out of the default lane) because creating and dropping a
table permanently burns pg_attribute slots on the shared test DB.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.db.models.classification_review import ClassificationReview, ClassificationReviewStatus
from tests.factories import AppRegistryFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_REVISION = "0028_deployment_unpublished_at"
_UNIQUE = "uq_classification_reviews_app"
_SHA = "c" * 40


# --- defaults -------------------------------------------------------------------


async def test_a_fresh_row_is_running_stamped_and_uncounted(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    row = ClassificationReview(app_id=app.id, user_id=user.id, head_sha=_SHA)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    # Server defaults, not Python defaults — proving the migration and the model agree.
    assert row.status is ClassificationReviewStatus.RUNNING
    assert row.attempt == 1
    assert row.head_sha == _SHA
    assert row.started_at is not None
    assert row.finished_at is None
    # Everything the run fills in later starts empty…
    assert row.verdicts is None
    assert row.evidence is None
    assert row.answers_complete is None
    assert row.failure_code is None
    # …and the spend counters start at zero, not NULL — "spent nothing" is the truth.
    assert row.input_tokens == 0
    assert row.output_tokens == 0
    assert row.cache_read_tokens == 0
    assert row.cache_write_tokens == 0


# --- one row per app ---------------------------------------------------------------


async def test_a_second_row_for_the_same_app_is_refused_by_the_database(db_session) -> None:
    """Belt over braces: the invariant is enforced by the DATABASE, not merely by the
    store's upsert. Wrapped in a SAVEPOINT so the expected violation does not poison the
    surrounding transaction."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    db_session.add(ClassificationReview(app_id=app.id, user_id=user.id, head_sha=_SHA))
    await db_session.flush()

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(ClassificationReview(app_id=app.id, user_id=user.id, head_sha="d" * 40))
            await db_session.flush()
    assert _UNIQUE in str(caught.value)


async def test_different_apps_each_carry_their_own_row(db_session) -> None:
    """The constraint is per-app: reviewing project A must not lock out project B."""
    user = await UserFactory.create(db_session)
    app_a = await AppRegistryFactory.create(db_session, user_id=user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id)

    db_session.add(ClassificationReview(app_id=app_a.id, user_id=user.id, head_sha=_SHA))
    db_session.add(ClassificationReview(app_id=app_b.id, user_id=user.id, head_sha=_SHA))
    await db_session.flush()


# --- ownership ------------------------------------------------------------------


async def test_deleting_the_app_cascades_the_review(db_session) -> None:
    """ON DELETE CASCADE — a deleted app can never leave a stale verdict behind for an
    app id nothing can resolve anymore."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    row = ClassificationReview(app_id=app.id, user_id=user.id, head_sha=_SHA)
    db_session.add(row)
    await db_session.flush()

    await db_session.execute(sa.text("DELETE FROM app_registry WHERE id = :i"), {"i": app.id})

    survivor = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ClassificationReview)
        .where(ClassificationReview.app_id == app.id)
    )
    assert survivor == 0


# --- the migration round-trip -----------------------------------------------------


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _run_sql(work) -> Any:
    """Run one async callable against a fresh NullPool engine — sync wrapper (alembic's
    env.py owns the loop during the commands)."""

    async def _go() -> Any:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _snapshot() -> dict[str, Any]:
    """Columns, the unique index behind the one-row-per-app constraint, and whether the
    enum type exists."""

    async def _read(conn) -> dict[str, Any]:
        rows = await conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'classification_reviews'"
            )
        )
        unique_def = await conn.scalar(
            sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": _UNIQUE},
        )
        enum_present = await conn.scalar(
            sa.text("SELECT 1 FROM pg_type WHERE typname = 'classification_review_status'")
        )
        return {
            "columns": {row[0] for row in rows},
            "unique_def": unique_def,
            "enum_present": enum_present,
        }

    return _run_sql(_read)


_EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "user_id",
        "app_id",
        "head_sha",
        "status",
        "attempt",
        "verdicts",
        "evidence",
        "answers_complete",
        "failure_code",
        "failure_detail",
        "started_at",
        "finished_at",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "created_at",
        "updated_at",
    }
)


@pytest.mark.destructive_migration
def test_classification_reviews_round_trip() -> None:
    """Head → 0028 → head against the real test DB: the table, the UNIQUE index and the
    enum type all travel with the revision — a downgrade that leaves the type behind makes
    the next upgrade fail on a duplicate type, on someone else's machine."""
    config = _alembic_config()
    command.upgrade(config, "head")

    at_head = _snapshot()
    assert at_head["columns"] == _EXPECTED_COLUMNS
    assert at_head["enum_present"] == 1
    # THE assertion this round-trip exists for: one row per app is enforced by a UNIQUE
    # index the claim can infer against — without it the upsert store degrades to
    # last-writer-wins duplicate rows.
    assert at_head["unique_def"] is not None
    assert "UNIQUE" in at_head["unique_def"]

    try:
        command.downgrade(config, _PRE_REVISION)
        gone = _snapshot()
        assert gone["columns"] == set()
        assert gone["unique_def"] is None
        assert gone["enum_present"] is None
    finally:
        # ALWAYS return to head so the rest of the suite sees the table.
        command.upgrade(config, "head")

    restored = _snapshot()
    assert restored["columns"] == _EXPECTED_COLUMNS
    assert restored["enum_present"] == 1
    assert restored["unique_def"] is not None
