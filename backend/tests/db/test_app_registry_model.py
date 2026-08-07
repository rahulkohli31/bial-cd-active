"""AppRegistry model shape after the submissions re-shape (0018, APPROVAL U3).

Two jobs: (1) PIN the lifecycle state machine — `STATUS_TRANSITIONS` is preserved
verbatim by the re-shape (R6), and this pin makes any future edit a deliberate,
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

# --- the state-machine pin (R6) -------------------------------------------------


def test_status_transitions_pinned_verbatim() -> None:
    # The exact lifecycle state machine, target → allowed sources. If this fails,
    # someone changed it — that must be a deliberate, reviewed decision, not a
    # side effect (R6).
    #
    # V4 Part 2 tried widening APPROVED/REJECTED to also accept every source
    # `submit` is legal from, on the theory that `submit`'s own auto-decide needed
    # it. Reverted after code review: `submit` never reads these two entries at all
    # (its guarded UPDATE uses `_SUBMIT_FROM`, independent of this table), so the
    # widening bought it nothing while quietly weakening `reject()`'s atomic guard
    # (`admin/router.py::_transition`'s `WHERE status IN STATUS_TRANSITIONS[target]`
    # is the real race-safety net `approve()`'s own comment describes). Keep this
    # exactly as narrow as the admin `approve`/`reject`/`enable` endpoints need.
    assert STATUS_TRANSITIONS == {
        AppStatus.PENDING: frozenset({AppStatus.DRAFT, AppStatus.REJECTED, AppStatus.APPROVED}),
        AppStatus.APPROVED: frozenset({AppStatus.PENDING, AppStatus.DISABLED}),
        AppStatus.REJECTED: frozenset({AppStatus.PENDING}),
        AppStatus.DISABLED: frozenset({AppStatus.APPROVED}),
    }
    # `draft` is minted only by provision — never a transition target.
    assert AppStatus.DRAFT not in STATUS_TRANSITIONS


# --- typed submission columns (D1) ------------------------------------------------


async def test_fresh_row_has_all_submission_refs_null(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    assert app.source_submission_id is None
    assert app.source_commit_sha is None
    assert app.submitted_at is None
    assert app.approved_submission_id is None
    assert app.approved_commit_sha is None
    assert app.deployed_submission_id is None
    assert app.deployed_at is None


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
    # No drift between the model and the 0018-migrated DB, in either direction.
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
