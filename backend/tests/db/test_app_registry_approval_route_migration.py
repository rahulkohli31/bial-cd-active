"""`app_registry.approval_route` + `declaration` against the REAL migrated schema (U4).

The test DB carries both columns from `alembic upgrade head` (revision
0030_approval_route_and_declaration), so the shape assertions exercise the actual
DDL — a native two-label enum and a nullable JSONB — inside the rolled-back
per-test transaction. `test_suspended_at_migration.py` pins the chain's exact head
at 0030 and `tests/test_alembic_single_head.py` guards the head count.

DELIBERATELY NO downgrade/upgrade round-trip (the U4 execution note): every
round-trip on `app_registry` permanently burns pg_attribute slots on the shared
test database, and seven destructive-lane tests already do it. The backfill is
tested instead by importing the migration module and executing EXACTLY the
statement `upgrade()` runs (`BACKFILL_RUNBOOK_LINEAGE`) over ORM-seeded rows —
the fresh-upgrade DDL path is what built this database in the first place.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from sqlalchemy import select

from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from tests.factories import AppRegistryFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_PATH = (
    _BACKEND_ROOT / "alembic" / "versions" / "2026_08_19_0030_approval_route_and_declaration.py"
)
_SHA = "3c" * 20  # 40 lowercase hex chars — the shape the bundle parser guarantees


def _migration_module() -> ModuleType:
    """Import the 0030 migration BY PATH (the versions dir is not a package), so the
    backfill tests run the statement the migration itself runs — never a copy that
    could drift from it."""
    spec = importlib.util.spec_from_file_location("migration_0030", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- shape: the DDL the fresh upgrade applied ------------------------------------


async def test_columns_land_nullable_with_the_declared_types(db_session) -> None:
    rows = (
        await db_session.execute(
            sa.text(
                "SELECT column_name, data_type, udt_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'app_registry' "
                "AND column_name IN ('approval_route', 'declaration')"
            )
        )
    ).all()
    by_name = {row.column_name: row for row in rows}
    assert set(by_name) == {"approval_route", "declaration"}
    # The lineage is the NATIVE enum (ADR-0008), not a varchar with a check.
    assert by_name["approval_route"].data_type == "USER-DEFINED"
    assert by_name["approval_route"].udt_name == "approval_route"
    assert by_name["approval_route"].is_nullable == "YES"  # NULL = no lineage yet
    assert by_name["declaration"].data_type == "jsonb"
    assert by_name["declaration"].is_nullable == "YES"


async def test_enum_labels_are_exactly_the_two_lineages(db_session) -> None:
    labels = (
        (
            await db_session.execute(
                sa.text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'approval_route' ORDER BY e.enumsortorder"
                )
            )
        )
        .scalars()
        .all()
    )
    assert labels == ["runbook", "self_publish"]
    # …and the Python enum's values ARE those labels (values_callable convention).
    assert [member.value for member in ApprovalRoute] == labels


async def test_a_fresh_row_has_no_lineage_and_no_declaration(db_session) -> None:
    owner = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id)
    assert app.approval_route is None  # a never-submitted draft belongs to no lineage
    assert app.declaration is None


async def test_lineage_and_declaration_round_trip_through_the_orm(db_session) -> None:
    owner = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=owner.id)
    payload = {
        "citizen": {"personal_information": "no"},
        "review": {"personal_information": "yes"},
        "differences": ["personal_information"],
        "explanation": "It only stores gate numbers.",
    }
    app.approval_route = ApprovalRoute.SELF_PUBLISH
    app.declaration = payload
    await db_session.flush()

    fetched = await db_session.scalar(select(AppRegistry).where(AppRegistry.id == app.id))
    assert fetched is not None
    assert fetched.approval_route is ApprovalRoute.SELF_PUBLISH
    assert fetched.declaration == payload  # JSONB survives byte-for-byte in meaning


# --- the P5 backfill: the exact statement upgrade() runs --------------------------


async def _seed(db_session, **overrides) -> AppRegistry:
    owner = await UserFactory.create(db_session)
    return await AppRegistryFactory.create(db_session, user_id=owner.id, **overrides)


async def test_backfill_marks_the_old_guard_and_only_the_old_guard(db_session) -> None:
    """One pass of `BACKFILL_RUNBOOK_LINEAGE` over every seeded shape at once — the
    statement runs once in the migration, so the test runs it once too and asserts
    every bucket, rather than proving each bucket against a statement the others
    never shared a pass with."""
    # A pre-feature approval, whatever the status now shows: a re-submitted-then-
    # rejected app KEEPS its approved pin (reject deliberately does not clear it),
    # and that pin is an approval granted for the out-of-band code review (P5).
    pin = uuid.uuid4()
    pinned_rejected = await _seed(
        db_session,
        status=AppStatus.REJECTED,
        approved_submission_id=pin,
        approved_commit_sha=_SHA,
    )
    # The half that is easy to miss: a queue item outstanding at release. Left NULL,
    # the admin's approval would not satisfy the gate and the citizen would need a
    # SECOND approval — marked runbook, approve refuses with re-submit copy instead.
    outstanding_pending = await _seed(
        db_session,
        status=AppStatus.PENDING,
        source_submission_id=uuid.uuid4(),
        source_commit_sha=_SHA,
    )
    # The D13 legacy remainder: a DISABLED row 0018 spared with a NULL pin was still
    # approved once (disabled is only reachable FROM approved) — status catches it.
    legacy_disabled = await _seed(db_session, status=AppStatus.DISABLED)
    # Never approved, nothing outstanding → NO lineage; its first trip through the
    # publish flow stamps self_publish.
    untouched_draft = await _seed(db_session, status=AppStatus.DRAFT)
    # Already stamped by the publish flow → the backfill is blind to it
    # (`approval_route IS NULL`), never a downgrade to runbook.
    already_self_publish = await _seed(
        db_session,
        status=AppStatus.PENDING,
        source_submission_id=uuid.uuid4(),
        source_commit_sha=_SHA,
        approval_route=ApprovalRoute.SELF_PUBLISH,
    )

    await db_session.execute(_migration_module().BACKFILL_RUNBOOK_LINEAGE)
    db_session.expire_all()  # the raw UPDATE bypassed the identity map

    await db_session.refresh(pinned_rejected)
    await db_session.refresh(outstanding_pending)
    await db_session.refresh(legacy_disabled)
    await db_session.refresh(untouched_draft)
    await db_session.refresh(already_self_publish)
    assert pinned_rejected.approval_route is ApprovalRoute.RUNBOOK
    assert outstanding_pending.approval_route is ApprovalRoute.RUNBOOK
    assert legacy_disabled.approval_route is ApprovalRoute.RUNBOOK
    assert untouched_draft.approval_route is None
    assert already_self_publish.approval_route is ApprovalRoute.SELF_PUBLISH
    # The backfill writes lineage ONLY — no declaration is invented for old rows
    # (the review screen says "declaration unavailable" rather than rendering blanks).
    assert outstanding_pending.declaration is None
