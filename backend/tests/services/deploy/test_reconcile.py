"""Crash recovery for deployments whose pipeline died with the process.

The four answers this must keep apart, in order of how expensive it is to get them wrong:

  ARM unreachable  -> leave the row alone. Getting this wrong eventually marks a LIVE app
                      failed, because a throttled request is indistinguishable from absence
                      to anything that collapses the two.
  digest matches   -> promote. The deploy worked; only the bookkeeping died.
  digest differs   -> fail the row, touch nothing. The running container is the citizen's
                      previous, working version.
  confirmed absent -> fail the row.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy import reconcile, store
from src.services.deploy.names import published_app_name
from src.services.sandbox.aca import AcaTransientError
from tests.factories import AppRegistryFactory, UserFactory

_DIGEST = "sha256:" + "ef" * 32


class FakeAca:
    """Answers as ARM does: `None` is CONFIRMED absent, a blip RAISES."""

    def __init__(self, *, fqdn: str | None, image: str | None = None, blip: bool = False) -> None:
        self._fqdn = fqdn
        self._image = image
        self._blip = blip
        self.deleted: list[uuid.UUID] = []

    async def get_app_fqdn(self, *, app_id: uuid.UUID) -> str | None:
        if self._blip:
            raise AcaTransientError("ARM is throttling")
        return self._fqdn

    async def get_app_image(self, *, app_id: uuid.UUID) -> str | None:
        return self._image

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        self.deleted.append(app_id)


def _factory(db_session):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


async def _abandoned(db, *, digest: str | None = _DIGEST):
    """A row whose pipeline stopped beating."""
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    deployment_id = await store.claim(db, app_id=app.id, user_id=user.id)
    assert deployment_id is not None
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id)
        .values(
            image_digest=digest,
            heartbeat_at=datetime.now(UTC) - timedelta(seconds=reconcile.STALE_AFTER_S + 60),
        )
    )
    return app, deployment_id


async def _row(db, deployment_id):
    row = await db.get(Deployment, deployment_id)
    await db.refresh(row)
    return row


# --- the four answers -------------------------------------------------------------


async def test_a_deploy_that_landed_before_the_crash_is_promoted(db_session) -> None:
    """The pipeline provisioned successfully and died before writing it down. The app IS the
    one this row built, so the honest record is success — not a failure the citizen would
    retry for no reason."""
    app, deployment_id = await _abandoned(db_session)
    aca = FakeAca(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}")

    resolved = await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)

    assert resolved == 1
    row = await _row(db_session, deployment_id)
    assert row.status is DeploymentStatus.SUCCEEDED
    # The reconciler and the pipeline write the SAME column, so they must agree about what an
    # address means. Composed from the container name rather than the fqdn the reconciler just
    # read from ARM — that fqdn proves the app is live, it is not where a person goes.
    assert row.url == f"https://citizenapps.bialairport.com/a/{published_app_name(row.app_id)}"


async def test_a_different_image_means_ours_never_landed(db_session) -> None:
    """A PREVIOUS deploy is serving. Fail the row — and leave the container alone, because it
    is the citizen's working version."""
    app, deployment_id = await _abandoned(db_session)
    aca = FakeAca(fqdn="pub-x.example.io", image="reg/app@sha256:" + "99" * 32)

    await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)

    row = await _row(db_session, deployment_id)
    assert row.status is DeploymentStatus.FAILED
    assert row.failure_code == store.INTERRUPTED
    assert aca.deleted == []


async def test_a_confirmed_absent_app_fails_the_row(db_session) -> None:
    app, deployment_id = await _abandoned(db_session)
    aca = FakeAca(fqdn=None)

    await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)

    row = await _row(db_session, deployment_id)
    assert row.status is DeploymentStatus.FAILED
    assert aca.deleted == []


async def test_an_unreachable_arm_leaves_the_row_exactly_as_it_was(db_session) -> None:
    """THE most expensive one to get wrong. A throttled request must never read as "gone" —
    collapsing the two would eventually mark a live app failed."""
    app, deployment_id = await _abandoned(db_session)
    aca = FakeAca(fqdn=None, blip=True)

    resolved = await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)

    assert resolved == 0
    row = await _row(db_session, deployment_id)
    assert row.status is DeploymentStatus.RUNNING
    assert row.failure_code is None


# --- what it must not touch -------------------------------------------------------


async def test_a_live_pipeline_is_left_alone(db_session) -> None:
    """Staleness is measured from the heartbeat. A row inside the window belongs to a running
    pipeline, and settling it would have two writers on one deploy."""
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    live = await store.claim(db_session, app_id=app.id, user_id=user.id)
    aca = FakeAca(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}")

    resolved = await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)

    assert resolved == 0
    assert (await _row(db_session, live)).status is DeploymentStatus.RUNNING


async def test_a_row_with_no_digest_is_never_promoted(db_session) -> None:
    """The digest is the PROOF that the running app is ours. Without one there is nothing to
    match on, so the only honest answer is that the deploy did not complete."""
    app, deployment_id = await _abandoned(db_session, digest=None)
    aca = FakeAca(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}")

    await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)

    row = await _row(db_session, deployment_id)
    assert row.status is DeploymentStatus.FAILED


async def test_the_reconciler_never_deletes_a_container_app(db_session) -> None:
    """It may PROMOTE a row it did not write. It may never delete an app it cannot prove it
    created — and this test is the guard on that distinction."""
    for image in (f"reg/app@{_DIGEST}", "reg/app@sha256:" + "11" * 32, None):
        app, _deployment_id = await _abandoned(db_session)
        aca = FakeAca(fqdn="pub-x.example.io", image=image)
        await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)
        assert aca.deleted == []


async def test_the_published_name_is_derived_not_stored_for_the_lookup(db_session) -> None:
    """The reconciler asks ARM by app id, so a row written before `container_app_name` was
    populated is still resolvable."""
    app, deployment_id = await _abandoned(db_session)
    assert published_app_name(app.id).startswith("pub-")
    aca = FakeAca(fqdn="pub-x.example.io", image=f"reg/app@{_DIGEST}")
    await reconcile.reconcile_stalled_deployments(_factory(db_session), aca)
    assert (await _row(db_session, deployment_id)).status is DeploymentStatus.SUCCEEDED
