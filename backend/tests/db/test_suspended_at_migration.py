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
    # 0034_project_description_fts to 0035_chat_kind (the two three-valued enums collapsing
    # into one two-valued `chat_kind`, on both tables). 0034 had already been re-parented
    # TWICE by this assertion: authored as an 0029 off 0028_deployment_unpublished_at, moved
    # to 0033 off 0032_rejection_standing on one rebase, and to 0034 off 0033_harness_counters
    # on the next — each time because main took the ordinal first. Which is exactly the silent
    # divergence this line exists to catch, twice over.
    #
    # SAY THIS OUT LOUD IN THE PULL REQUEST when it moves: CI runs the static gates and the
    # single-head COUNT (`tests/test_alembic_single_head.py`) and deliberately does not run
    # pytest, so this name-pinned assertion goes red locally while CI stays green. That is the
    # change's own failure, not a pre-existing one — the exact write-off that blocked PR #120.
    # Pinning
    # the exact head — rather than just the COUNT, which `test_alembic_single_head.py`
    # already guards — is what makes a rebase that silently re-parents a revision fail here
    # instead of at deploy. Updating this line is the deliberate acknowledgement that a new
    # migration landed; if you are here because it failed, check that your revision's
    # `down_revision` really is the head you expected to build on.
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0035_chat_kind"]
