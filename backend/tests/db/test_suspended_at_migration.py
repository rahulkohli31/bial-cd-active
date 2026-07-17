"""`users.suspended_at` round-trips against the REAL migrated schema (U10a).

The test DB carries the column from `alembic upgrade head` (revision
0016_user_suspended_at), so these exercise the actual DDL — nullable timestamptz,
no default — inside the rolled-back per-test transaction. The upgrade/downgrade
round-trip itself is verified out-of-band via `alembic upgrade head` / `downgrade`
(U10a Verification); `tests/test_alembic_single_head.py` guards the head count.
Here we prove the shape and that the chain still ends at exactly this revision.
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


async def test_fresh_user_is_not_suspended(db_session) -> None:
    user = await UserFactory.create(db_session)
    assert user.suspended_at is None  # NULL = active; no default suspends anyone


async def test_suspended_at_set_and_clear_roundtrip(db_session) -> None:
    user = await UserFactory.create(db_session)
    moment = datetime(2026, 7, 9, 12, 30, tzinfo=UTC)

    user.suspended_at = moment
    await db_session.flush()
    fetched = await db_session.scalar(select(User).where(User.id == user.id))
    assert fetched is not None
    assert fetched.suspended_at == moment  # timestamptz survives intact

    fetched.suspended_at = None  # reactivate clears back to NULL
    await db_session.flush()
    cleared = await db_session.scalar(select(User).where(User.id == user.id))
    assert cleared is not None
    assert cleared.suspended_at is None


def test_chain_ends_at_a_single_linear_head() -> None:
    # The migration chain stays ONE linear head (no divergent branch). The head moved past
    # 0018_app_registry_submissions (the APPROVAL re-shape) to 0019_app_registry_deployed_url
    # (the recorded deployed URL). Pinning the exact head — rather than just the COUNT, which
    # `test_alembic_single_head.py` already guards — is what makes a rebase that silently
    # re-parents a revision fail here instead of at deploy time.
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0019_app_registry_deployed_url"]
