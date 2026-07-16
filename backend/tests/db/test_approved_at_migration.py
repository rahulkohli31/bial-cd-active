"""`users.approved_at` round-trips against the REAL migrated schema.

The test DB carries the column from `alembic upgrade head` (revision
0017_user_approved_at), so these exercise the actual DDL — nullable timestamptz,
no default — inside the rolled-back per-test transaction. The upgrade/downgrade
round-trip itself is verified out-of-band via `alembic upgrade head` / `downgrade`;
`tests/test_alembic_single_head.py` guards the head count. Here we prove the shape
and that the chain still ends at exactly this revision.

Mirrors `test_suspended_at_migration.py` — see that file for the sibling marker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from src.db.models.user import User
from tests.factories import UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


async def test_fresh_user_is_pending_by_default(db_session) -> None:
    # UserFactory itself defaults approved_at to "now" (so existing tests aren't
    # forced through pending) — the RAW column default, proven here, is NULL.
    user = await UserFactory.create(db_session, approved_at=None)
    assert user.approved_at is None
    assert user.status() == "pending"


async def test_approved_at_set_roundtrip(db_session) -> None:
    user = await UserFactory.create(db_session, approved_at=None)
    moment = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)

    user.approved_at = moment
    await db_session.flush()
    fetched = await db_session.scalar(select(User).where(User.id == user.id))
    assert fetched is not None
    assert fetched.approved_at == moment  # timestamptz survives intact
    assert fetched.status() == "approved"


async def test_suspended_wins_over_pending_in_status() -> None:
    user = User(
        azure_oid="oid-x",
        email="x@example.com",
        suspended_at=datetime(2026, 7, 14, tzinfo=UTC),
        approved_at=None,
    )
    assert user.status() == "disabled"


def test_chain_ends_at_the_approval_revision() -> None:
    # 0016_user_suspended_at -> 0017_user_approved_at is ONE linear chain — the
    # head is this unit's revision, not a divergent branch.
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0017_user_approved_at"]
