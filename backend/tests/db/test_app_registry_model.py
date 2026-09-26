"""AppRegistry model shape after the submissions re-shape (0018).

Two jobs: (1) PIN the lifecycle state machine — `STATUS_TRANSITIONS` is preserved
verbatim by the re-shape, and this pin makes any future edit a deliberate,
reviewed change instead of a drive-by; (2) prove the
ORM's typed submission columns round-trip against the REAL migrated schema (no
model↔migration drift)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from src.db.models.app_registry import (
    STATUS_TRANSITIONS,
    AppRegistry,
    AppStatus,
)
from tests.factories import AppRegistryFactory, UserFactory

# --- the state-machine pin -------------------------------------------------


def test_status_transitions_pinned_verbatim() -> None:
    # Target → allowed sources. If this fails, someone changed the lifecycle state
    # machine — that must be a deliberate, reviewed decision, not a side effect.
    # Two post-Express additions, each one such a decision:
    #   * `DRAFT: {PENDING}` is the withdrawal path — an owner pulls their own pending
    #     submission back out of the queue, and draft stopped being provision-only the day
    #     that route landed.
    #   * `DISABLED: {APPROVED, DRAFT, REJECTED}` is the kill switch reaching the
    #     ordinary catalog member (a self-published DRAFT) and a REJECTED app, not approved
    #     ones only. PENDING is deliberately absent: an app in the review queue is rejected,
    #     not switched off.
    # And one deliberate NON-change, which this pin is the guard for: the DRAFT row stays
    # `{PENDING}`. `apps/router.py::withdraw` reads it with only an ownership predicate, so
    # DISABLED in that set would let an owner undo an administrator's kill switch.
    assert STATUS_TRANSITIONS == {
        AppStatus.DRAFT: frozenset({AppStatus.PENDING}),
        AppStatus.PENDING: frozenset({AppStatus.DRAFT, AppStatus.REJECTED, AppStatus.APPROVED}),
        AppStatus.APPROVED: frozenset({AppStatus.PENDING, AppStatus.DISABLED}),
        AppStatus.REJECTED: frozenset({AppStatus.PENDING}),
        AppStatus.DISABLED: frozenset({AppStatus.APPROVED, AppStatus.DRAFT, AppStatus.REJECTED}),
    }


# --- typed submission columns ------------------------------------------------


async def test_fresh_row_has_all_submission_refs_null(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    assert app.source_submission_id is None
    assert app.source_commit_sha is None
    assert app.submitted_at is None
    assert app.approved_submission_id is None
    assert app.approved_commit_sha is None


async def test_submission_refs_roundtrip_typed(db_session) -> None:
    user = await UserFactory.create(db_session)
    sid = uuid.uuid4()
    sha = "a" * 40
    now = datetime.now(UTC)
    app = await AppRegistryFactory.create(
        db_session,
        user_id=user.id,
        status=AppStatus.PENDING,
        source_submission_id=sid,
        source_commit_sha=sha,
        submitted_at=now,
    )
    fetched = await db_session.get(AppRegistry, app.id)
    assert fetched is not None
    assert fetched.source_submission_id == sid
    assert isinstance(fetched.source_submission_id, uuid.UUID)
    assert fetched.source_commit_sha == sha
    assert fetched.submitted_at == now


async def test_orm_columns_match_live_schema(db_session) -> None:
    live = set(
        (
            await db_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'app_registry'"
                )
            )
        ).scalars()
    )
    mapped = {column.name for column in AppRegistry.__table__.columns}
    assert mapped == live
