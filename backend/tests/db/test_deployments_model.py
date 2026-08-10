"""The `deployments` row shape and — the real subject — the partial unique index that
serializes one-click deploys.

`uq_deployments_one_in_flight` is the first partial index in the repo, and its whole value
is in the WHERE clause: without it the index would forbid an app from ever being deployed
twice, and with the wrong predicate it would let two pipelines race for the same container
app name. So these tests pin BOTH halves — it rejects a second in-flight deploy, and it
lets a terminal one be superseded — against the real migrated schema, not the ORM's idea
of it.

The claim is exercised in its production form (`ON CONFLICT ... DO NOTHING RETURNING` by
INFERENCE, since a partial index cannot be an `ON CONSTRAINT` target). A test that used a
plain INSERT and caught `IntegrityError` would pass while the real claim path was broken.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.deployment import Deployment, DeploymentStatus
from tests.factories import AppRegistryFactory, UserFactory


async def _claim(db: AsyncSession, *, app_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID | None:
    """The production claim, verbatim: exactly one racer gets a row back."""
    stmt = (
        pg_insert(Deployment)
        .values(app_id=app_id, user_id=user_id)
        .on_conflict_do_nothing(
            index_elements=[Deployment.app_id],
            index_where=sa.text("status = 'running'"),
        )
        .returning(Deployment.id)
    )
    claimed: uuid.UUID | None = await db.scalar(stmt)
    return claimed


# --- defaults -------------------------------------------------------------------


async def test_a_fresh_claim_is_running_and_unfinished(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    deployment_id = await _claim(db_session, app_id=app.id, user_id=user.id)
    assert deployment_id is not None

    row = await db_session.get(Deployment, deployment_id)
    assert row is not None
    # Server defaults, not Python defaults — proving the migration and the model agree.
    assert row.status is DeploymentStatus.RUNNING
    assert row.step == "claimed"
    assert row.heartbeat_at is not None
    assert row.finished_at is None
    # Everything the pipeline fills in later starts empty.
    assert row.head_sha is None
    assert row.image_digest is None
    assert row.container_app_name is None
    assert row.url is None
    assert row.failure_code is None


# --- the guard ------------------------------------------------------------------


async def test_a_second_in_flight_claim_returns_nothing(db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    first = await _claim(db_session, app_id=app.id, user_id=user.id)
    second = await _claim(db_session, app_id=app.id, user_id=user.id)

    assert first is not None
    # No row back IS the "already deploying" signal — the route turns this into a 409.
    assert second is None


async def test_a_terminal_deploy_does_not_block_the_next_one(db_session) -> None:
    """The append-only property. If this fails the index is not partial and an app can
    never be deployed a second time."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    first = await _claim(db_session, app_id=app.id, user_id=user.id)
    assert first is not None
    await db_session.execute(
        sa.update(Deployment)
        .where(Deployment.id == first)
        .values(status=DeploymentStatus.SUCCEEDED, finished_at=sa.func.now())
    )

    second = await _claim(db_session, app_id=app.id, user_id=user.id)
    assert second is not None and second != first

    # And a FAILED one is equally superseded — both terminals leave the slot free.
    await db_session.execute(
        sa.update(Deployment)
        .where(Deployment.id == second)
        .values(status=DeploymentStatus.FAILED, finished_at=sa.func.now())
    )
    third = await _claim(db_session, app_id=app.id, user_id=user.id)
    assert third is not None and third not in {first, second}


async def test_the_history_survives_every_supersede(db_session) -> None:
    """One row per attempt — a failed deploy must never overwrite the record of the
    version still serving traffic."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    for _ in range(3):
        claimed = await _claim(db_session, app_id=app.id, user_id=user.id)
        assert claimed is not None
        await db_session.execute(
            sa.update(Deployment)
            .where(Deployment.id == claimed)
            .values(status=DeploymentStatus.SUCCEEDED)
        )

    count = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Deployment).where(Deployment.app_id == app.id)
    )
    assert count == 3


async def test_two_apps_deploy_independently(db_session) -> None:
    """The guard is per-app, not per-user: deploying project A must not 409 project B."""
    user = await UserFactory.create(db_session)
    app_a = await AppRegistryFactory.create(db_session, user_id=user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id)

    assert await _claim(db_session, app_id=app_a.id, user_id=user.id) is not None
    assert await _claim(db_session, app_id=app_b.id, user_id=user.id) is not None


async def test_a_plain_insert_still_hits_the_index(db_session) -> None:
    """Belt over braces: the constraint is enforced by the DATABASE, not merely by the
    claim helper's ON CONFLICT clause. Wrapped in a SAVEPOINT so the expected violation
    does not poison the surrounding transaction."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    assert await _claim(db_session, app_id=app.id, user_id=user.id) is not None

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(Deployment(app_id=app.id, user_id=user.id))
            await db_session.flush()
    assert "uq_deployments_one_in_flight" in str(caught.value)


# --- ownership ------------------------------------------------------------------


async def test_deleting_the_app_cascades_the_deployments(db_session) -> None:
    """ON DELETE CASCADE — a row can never outlive its app. This is also why the delete
    paths must read `container_app_name` out BEFORE committing: after the cascade there is
    nothing left to tell a sweeper which Azure container to remove."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    claimed = await _claim(db_session, app_id=app.id, user_id=user.id)
    assert claimed is not None

    await db_session.execute(sa.text("DELETE FROM app_registry WHERE id = :i"), {"i": app.id})

    survivor = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Deployment).where(Deployment.id == claimed)
    )
    assert survivor == 0
