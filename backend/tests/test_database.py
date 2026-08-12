"""DB layer: mixin column shape (pure) + a UUIDv7/timestamp round-trip (needs PG)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


# Two separate metadatas so a create_all in the round-trip touches ONLY the
# widget table — never the owned-thing table, whose FK to the (not-yet-existing)
# users table would fail DDL. Both stay off the app's Base.metadata.
class _WidgetBase(DeclarativeBase):
    pass


class _OwnedBase(DeclarativeBase):
    pass


class _Widget(UUIDv7PrimaryKeyMixin, TimestampMixin, _WidgetBase):
    __tablename__ = "test_widgets"
    label: Mapped[str] = mapped_column(sa.String(50), nullable=False)


class _OwnedThing(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, _OwnedBase):
    __tablename__ = "test_owned_things"


def test_uuid_pk_column_declared() -> None:
    id_col = _Widget.__table__.c.id
    assert id_col.primary_key is True


def test_timestamp_columns_declared() -> None:
    cols = _Widget.__table__.c
    assert isinstance(cols.created_at.type, sa.DateTime)
    assert isinstance(cols.updated_at.type, sa.DateTime)
    assert cols.created_at.nullable is False
    assert cols.updated_at.nullable is False


def test_owned_by_user_is_scoped_and_indexed() -> None:
    # Single-tenant ownership boundary: user_id present, non-nullable, indexed —
    # and NO org_id anywhere (ADR-0004).
    col = _OwnedThing.__table__.c.user_id
    assert col.nullable is False
    assert col.index is True
    assert "org_id" not in _OwnedThing.__table__.c


async def test_uuidv7_and_timestamps_roundtrip(db_session) -> None:
    # Integration-in-default-lane: needs the Postgres test DB (db_session fixture).
    # Create only the widget table inside the rolled-back test transaction (the
    # owned-thing table's FK to users.id doesn't exist yet).
    conn = await db_session.connection()
    await conn.run_sync(_WidgetBase.metadata.create_all)

    first = _Widget(label="first")
    second = _Widget(label="second")
    db_session.add_all([first, second])
    await db_session.flush()

    # App-side default generated a v7 UUID for each row.
    assert first.id.version == 7
    assert second.id.version == 7
    assert first.id != second.id
    # UUIDv7 is time-sortable: a later insert sorts after an earlier one.
    assert second.id > first.id
    # Server default populated the timestamps.
    assert first.created_at is not None
    assert first.updated_at is not None
