"""`users.suspended_at` round-trips against the REAL migrated schema.

The test DB carries the column from `alembic upgrade head` (revision
0016_user_suspended_at), so these exercise the actual DDL — nullable timestamptz,
no default — inside the rolled-back per-test transaction. The upgrade/downgrade
round-trip itself is verified out-of-band via `alembic upgrade head` / `downgrade`;
`tests/test_alembic_single_head.py` guards the head count.
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

    fetched.suspended_at = None
    await db_session.flush()
    cleared = await db_session.scalar(select(User).where(User.id == user.id))
    assert cleared is not None
    assert cleared.suspended_at is None


def test_chain_ends_at_a_single_linear_head() -> None:
    # Pins the exact head, not just the count `test_alembic_single_head.py` already guards, so a
    # rebase that silently re-parents a revision fails here instead of at deploy. The head moved
    # past 0038_app_previous_status along TWO lines that both branched from it: main's
    # 0039_drop_current_code (#191 deleted Generate Description, current_code's one remaining
    # reader, so the column followed it) then 0040_description_embedding (#191 slice 3 — the
    # semantic-search vector column; shortened from 0040_project_description_embedding, which
    # overran alembic_version's VARCHAR(32)), and the connector line's 0039_connector_access (the
    # two connector state machines: `connector_access_requests`, keyed on the PERSON, and
    # `project_connectors`, carrying each project's switch and the days it reads). They meet at
    # 0041_merge_connector_heads, a no-op merge — chosen over re-parenting 0039_connector_access
    # because a database that already ran it keeps a revision alembic still knows, so a plain
    # `alembic upgrade head` finishes the job. 0041_project_shares (#198 slice 1 — the platform's
    # first junction table) grew off 0040 alongside it, and 0042_merge_shares_connectors joins the
    # two the same way. 0043_pending_teardown sits on top of that merge: the durable record of a
    # container deletion the platform still owes, which has to survive the per-user registry being
    # overwritten by whatever the citizen opened next. 0034 had already been re-parented
    # TWICE by this assertion: authored as an 0029 off 0028_deployment_unpublished_at, moved
    # to 0033 off 0032_rejection_standing on one rebase, and to 0034 off 0033_harness_counters
    # on the next — each time because main took the ordinal first. Which is exactly the silent
    # divergence this line exists to catch, twice over.
    #
    # SAY THIS OUT LOUD IN THE PULL REQUEST when it moves: CI runs the static gates and the
    # single-head COUNT and deliberately does not run pytest, so this name-pinned assertion
    # goes red locally while CI stays green. That is the change's own failure, not a
    # pre-existing one. If you're here because it failed, check that your revision's
    # `down_revision` really is the head you expected to build on.
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0043_pending_teardown"]
