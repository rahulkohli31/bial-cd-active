"""The `pending_teardowns` row shape and — the real subject — the two invariants a switch's
shutdown routine stands on: `uq_pending_teardowns_app_name` (at most one owed deletion per
container name) and the `claimed_until` conditional UPDATE (the concurrency claim itself; there
is no advisory lock).

Mirrors `test_deployments_model.py` / `test_classification_review_model.py`: the claim is
exercised in its real shape (a conditional UPDATE, not a plain write caught for an error), and
the migration round-trip is pinned against the real migrated schema, not the ORM's idea of it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.db.models.pending_teardown import PendingTeardown
from src.services.build_sessions import app_name_for
from tests.factories import AppRegistryFactory, ConversationFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_REVISION = "0042_merge_shares_connectors"
_UNIQUE = "uq_pending_teardowns_app_name"


def _future(**kwargs: float) -> datetime:
    return datetime.now(UTC) + timedelta(**kwargs)


def _past(**kwargs: float) -> datetime:
    return datetime.now(UTC) - timedelta(**kwargs)


async def _write_owed_row(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    app_id: uuid.UUID,
    project_id: uuid.UUID,
    **overrides: Any,
) -> PendingTeardown:
    data: dict[str, Any] = {
        "user_id": user_id,
        "app_id": app_id,
        "app_name": app_name_for(app_id),
        "project_id": project_id,
        "instance_ref": _past(minutes=5),
        "claimed_until": _future(minutes=1),
    }
    data.update(overrides)
    row = PendingTeardown(**data)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def _claim_if_lapsed(
    db: AsyncSession, *, id: uuid.UUID, new_deadline: datetime
) -> uuid.UUID | None:
    """The concurrency claim, verbatim: a conditional UPDATE on `claimed_until <= now()` IS
    the mutual exclusion — no advisory lock, no `SELECT ... FOR UPDATE`."""
    stmt = (
        sa.update(PendingTeardown)
        .where(PendingTeardown.id == id, PendingTeardown.claimed_until <= sa.func.now())
        .values(claimed_until=new_deadline, attempts=PendingTeardown.attempts + 1)
        .returning(PendingTeardown.id)
    )
    claimed: uuid.UUID | None = await db.scalar(stmt)
    return claimed


# --- defaults -------------------------------------------------------------------


async def test_a_fresh_row_starts_at_one_attempt_and_carries_no_error(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    row = await _write_owed_row(
        db_session, user_id=user.id, app_id=app.id, project_id=app.project_id
    )

    # Server defaults, not Python defaults — proving the migration and the model agree.
    assert row.attempts == 1
    assert row.last_error is None
    assert row.conversation_id is None
    assert row.created_at is not None


# --- the happy path ---------------------------------------------------------------


async def test_written_claimed_and_deleted_on_success(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    conversation = await ConversationFactory.create(
        db_session, user_id=user.id, project_id=app.project_id
    )

    row = await _write_owed_row(
        db_session,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        conversation_id=conversation.id,
        claimed_until=_past(minutes=1),
    )

    claimed = await _claim_if_lapsed(db_session, id=row.id, new_deadline=_future(minutes=1))
    assert claimed == row.id

    await db_session.execute(sa.delete(PendingTeardown).where(PendingTeardown.id == row.id))

    survivor = await db_session.scalar(
        sa.select(sa.func.count()).select_from(PendingTeardown).where(PendingTeardown.id == row.id)
    )
    assert survivor == 0


# --- one owed row per container name -----------------------------------------------


async def test_a_second_row_for_the_same_container_name_is_refused_by_the_database(
    db_session,
) -> None:
    """Belt over braces: the invariant is enforced by the DATABASE, not merely by whichever
    call site writes the row. Wrapped in a SAVEPOINT so the expected violation does not
    poison the surrounding transaction."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    await _write_owed_row(db_session, user_id=user.id, app_id=app.id, project_id=app.project_id)

    other_app = await AppRegistryFactory.create(db_session, user_id=user.id)
    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(
                PendingTeardown(
                    user_id=user.id,
                    app_id=other_app.id,
                    # Deliberately colliding: a second claim on the SAME container name,
                    # not merely the same app.
                    app_name=app_name_for(app.id),
                    project_id=other_app.project_id,
                    instance_ref=_past(minutes=1),
                    claimed_until=_future(minutes=1),
                )
            )
            await db_session.flush()
    assert _UNIQUE in str(caught.value)


# --- the claim: a conditional UPDATE is the mutual exclusion -----------------------


async def test_a_claim_before_the_deadline_lapses_affects_no_rows(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    row = await _write_owed_row(
        db_session,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        claimed_until=_future(minutes=10),
    )

    claimed = await _claim_if_lapsed(db_session, id=row.id, new_deadline=_future(minutes=20))
    assert claimed is None

    await db_session.refresh(row)
    assert row.attempts == 1
    assert row.claimed_until > _future(minutes=9)


async def test_a_lapsed_claim_is_reclaimable_and_increments_attempts(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    row = await _write_owed_row(
        db_session,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        claimed_until=_past(minutes=1),
    )

    claimed = await _claim_if_lapsed(db_session, id=row.id, new_deadline=_future(minutes=5))
    assert claimed == row.id

    await db_session.refresh(row)
    assert row.attempts == 2
    assert row.claimed_until > datetime.now(UTC)


async def test_a_second_claimant_cannot_also_win_a_lapsed_row(db_session) -> None:
    """Two concurrent claimants cannot both proceed. The row's own state after the first
    claim is what stops the second — the conditional UPDATE re-reads `claimed_until` for
    itself rather than trusting anything read earlier."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    row = await _write_owed_row(
        db_session,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        claimed_until=_past(minutes=1),
    )

    first = await _claim_if_lapsed(db_session, id=row.id, new_deadline=_future(minutes=5))
    second = await _claim_if_lapsed(db_session, id=row.id, new_deadline=_future(minutes=5))

    assert first == row.id
    assert second is None

    await db_session.refresh(row)
    # Exactly one claim landed — the loser's UPDATE touched zero rows and so never ran
    # its `attempts + 1`.
    assert row.attempts == 2


# --- the instance discriminator -----------------------------------------------------


async def test_a_row_names_the_instance_it_was_written_for_not_a_same_named_recreate(
    db_session,
) -> None:
    """The name is stable across teardown and recreate; `instance_ref` is what tells a row
    written for the outgoing container apart from one a citizen reopened during the wait."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    outgoing_instance = _past(minutes=10)
    row = await _write_owed_row(
        db_session,
        user_id=user.id,
        app_id=app.id,
        project_id=app.project_id,
        instance_ref=outgoing_instance,
    )

    # The container currently answering to this name is a DIFFERENT instance — the
    # citizen reopened the project while the row's deletion was still owed.
    reopened_instance = _past(minutes=1)
    matches_current = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(PendingTeardown)
        .where(
            PendingTeardown.app_name == row.app_name,
            PendingTeardown.instance_ref == reopened_instance,
        )
    )
    assert matches_current == 0

    matches_written_for = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(PendingTeardown)
        .where(
            PendingTeardown.app_name == row.app_name,
            PendingTeardown.instance_ref == outgoing_instance,
        )
    )
    assert matches_written_for == 1


# --- cross-user isolation ------------------------------------------------------------


async def test_a_query_scoped_by_user_id_never_returns_another_users_row(db_session) -> None:
    owner = await UserFactory.create(db_session, email="owner@rvaiglobal.com")
    other = await UserFactory.create(db_session, email="other@rvaiglobal.com")
    owner_app = await AppRegistryFactory.create(db_session, user_id=owner.id)
    other_app = await AppRegistryFactory.create(db_session, user_id=other.id)

    await _write_owed_row(
        db_session, user_id=owner.id, app_id=owner_app.id, project_id=owner_app.project_id
    )
    await _write_owed_row(
        db_session, user_id=other.id, app_id=other_app.id, project_id=other_app.project_id
    )

    rows = (
        (
            await db_session.execute(
                sa.select(PendingTeardown).where(PendingTeardown.user_id == owner.id)
            )
        )
        .scalars()
        .all()
    )

    assert [r.app_id for r in rows] == [owner_app.id]


# --- ownership: user_id cascades, app_id/project_id do not -----------------------------


async def test_deleting_the_user_cascades_the_row(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    row = await _write_owed_row(
        db_session, user_id=user.id, app_id=app.id, project_id=app.project_id
    )

    await db_session.execute(sa.text("DELETE FROM users WHERE id = :i"), {"i": user.id})

    survivor = await db_session.scalar(
        sa.select(sa.func.count()).select_from(PendingTeardown).where(PendingTeardown.id == row.id)
    )
    assert survivor == 0


async def test_deleting_the_app_leaves_the_row_the_debt_still_stands(db_session) -> None:
    """No ForeignKey on `app_id`: the row must outlive the app it names, or a citizen who
    deletes the outgoing project mid-wait would silently forgive a container the platform
    still owes. Deleting the app row directly (bypassing the object-store-aware cascade,
    which is out of scope here) must not touch this row."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    row = await _write_owed_row(
        db_session, user_id=user.id, app_id=app.id, project_id=app.project_id
    )

    await db_session.execute(sa.text("DELETE FROM app_registry WHERE id = :i"), {"i": app.id})

    survivor = await db_session.get(PendingTeardown, row.id)
    assert survivor is not None
    assert survivor.app_id == app.id


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
    async def _read(conn) -> dict[str, Any]:
        rows = await conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'pending_teardowns'"
            )
        )
        unique_def = await conn.scalar(
            sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": _UNIQUE},
        )
        return {"columns": {row[0] for row in rows}, "unique_def": unique_def}

    return _run_sql(_read)


_EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "user_id",
        "app_id",
        "app_name",
        "project_id",
        "instance_ref",
        "conversation_id",
        "attempts",
        "claimed_until",
        "last_error",
        "created_at",
        "updated_at",
    }
)


@pytest.mark.destructive_migration
def test_pending_teardowns_round_trip() -> None:
    """Head → 0042 → head against the real test DB: the table and the UNIQUE index that
    serializes the claim both travel with the revision — a downgrade that leaves either
    behind makes the next upgrade fail, or silently drops the invariant, on someone else's
    machine."""
    config = _alembic_config()
    command.upgrade(config, "head")

    at_head = _snapshot()
    assert at_head["columns"] == _EXPECTED_COLUMNS
    assert at_head["unique_def"] is not None
    assert "UNIQUE" in at_head["unique_def"]

    try:
        command.downgrade(config, _PRE_REVISION)
        gone = _snapshot()
        assert gone["columns"] == set()
        assert gone["unique_def"] is None
    finally:
        # ALWAYS return to head so the rest of the suite sees the table.
        command.upgrade(config, "head")

    restored = _snapshot()
    assert restored["columns"] == _EXPECTED_COLUMNS
    assert restored["unique_def"] is not None
