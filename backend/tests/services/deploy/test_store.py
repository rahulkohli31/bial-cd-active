"""The deployments store: the claim, the stale-claim self-heal, and the terminal write.

Three properties carry the design and each gets a test that would fail loudly if it broke:

* the claim serializes — a second deploy of the same app gets nothing back;
* a crashed pipeline's row is recoverable — otherwise a platform restart wedges that app's
  Deploy button forever, with no signal to the citizen beyond a 409;
* a row settles EXACTLY ONCE — a late-arriving pipeline that was already taken over must not
  contradict what is now on record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy import store
from tests.factories import AppRegistryFactory, UserFactory


async def _app(db):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    return user, app


async def _claimed(db, *, app_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    """Claim, asserting it succeeded. `claim` returns Optional BY CONTRACT — `None` is how a
    caller learns a deploy is already in flight — so the tests that need a live row narrow it
    once here instead of restating the assertion at every call site."""
    deployment_id = await store.claim(db, app_id=app_id, user_id=user_id)
    assert deployment_id is not None
    return deployment_id


async def _age_the_heartbeat(db, deployment_id: uuid.UUID, *, seconds: float) -> None:
    """Push a row's heartbeat into the past. Cheaper and far more honest than sleeping, and
    it exercises the real predicate rather than a monkeypatched clock."""
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id)
        .values(heartbeat_at=datetime.now(UTC) - timedelta(seconds=seconds))
    )


# --- the claim ---------------------------------------------------------------------


async def test_the_first_claim_wins_and_the_second_gets_nothing(db_session) -> None:
    user, app = await _app(db_session)

    first = await store.claim(db_session, app_id=app.id, user_id=user.id)
    second = await store.claim(db_session, app_id=app.id, user_id=user.id)

    assert first is not None
    assert second is None


async def test_a_finished_deploy_frees_the_slot(db_session) -> None:
    user, app = await _app(db_session)

    first = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await store.succeed(db_session, first, url="https://pub-x.example")

    assert await store.claim(db_session, app_id=app.id, user_id=user.id) is not None


async def test_the_claim_is_per_app_not_per_user(db_session) -> None:
    """A per-user guard would 409 project B while project A deploys."""
    user = await UserFactory.create(db_session)
    app_a = await AppRegistryFactory.create(db_session, user_id=user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id)

    assert await store.claim(db_session, app_id=app_a.id, user_id=user.id) is not None
    assert await store.claim(db_session, app_id=app_b.id, user_id=user.id) is not None


# --- the stale-claim self-heal ------------------------------------------------------


async def test_a_crashed_pipelines_row_is_taken_over(db_session) -> None:
    """The control plane restarts on every platform deploy, and a pipeline runs for minutes.
    Without this, the row it left behind wedges that app's Deploy button forever."""
    user, app = await _app(db_session)
    abandoned = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await _age_the_heartbeat(db_session, abandoned, seconds=store.DEPLOY_STALE_AFTER_S + 60)

    fresh = await _claimed(db_session, app_id=app.id, user_id=user.id)

    assert fresh is not None and fresh != abandoned
    stale = await db_session.get(Deployment, abandoned)
    assert stale.status is DeploymentStatus.FAILED
    assert stale.failure_code == store.INTERRUPTED
    assert stale.finished_at is not None


async def test_the_takeover_message_tells_the_citizen_nothing_was_lost(db_session) -> None:
    user, app = await _app(db_session)
    abandoned = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await _age_the_heartbeat(db_session, abandoned, seconds=store.DEPLOY_STALE_AFTER_S + 60)
    await store.claim(db_session, app_id=app.id, user_id=user.id)

    detail = (await db_session.get(Deployment, abandoned)).failure_detail
    assert "press Deploy again" in detail


async def test_a_live_pipeline_is_never_stolen_from(db_session) -> None:
    """Staleness is measured from the HEARTBEAT. A row inside the window belongs to a
    running pipeline, and taking it over would put two pipelines on one app."""
    user, app = await _app(db_session)
    live = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await _age_the_heartbeat(db_session, live, seconds=store.DEPLOY_STALE_AFTER_S - 60)

    assert await store.claim(db_session, app_id=app.id, user_id=user.id) is None
    assert (await db_session.get(Deployment, live)).status is DeploymentStatus.RUNNING


async def test_a_heartbeat_rescues_a_slow_but_healthy_deploy(db_session) -> None:
    """A long image build must not get stolen from underneath itself."""
    user, app = await _app(db_session)
    live = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await _age_the_heartbeat(db_session, live, seconds=store.DEPLOY_STALE_AFTER_S + 60)

    await store.heartbeat(db_session, live)

    assert await store.claim(db_session, app_id=app.id, user_id=user.id) is None


# --- progress and terminals ---------------------------------------------------------


async def test_advance_records_the_phase_and_beats(db_session) -> None:
    user, app = await _app(db_session)
    deployment = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await _age_the_heartbeat(db_session, deployment, seconds=600)

    await store.advance(db_session, deployment, step="building", head_sha="a" * 40)

    row = await db_session.get(Deployment, deployment)
    await db_session.refresh(row)
    assert row.step == "building"
    assert row.head_sha == "a" * 40
    # A phase change is proof of life; a separate renew would be a redundant round trip.
    assert (datetime.now(UTC) - row.heartbeat_at).total_seconds() < 60


async def test_a_row_settles_exactly_once(db_session) -> None:
    """A pipeline that was taken over and then finishes must not overwrite the terminal row
    that replaced it."""
    user, app = await _app(db_session)
    deployment = await _claimed(db_session, app_id=app.id, user_id=user.id)

    assert await store.succeed(db_session, deployment, url="https://first.example") is True
    assert await store.fail(db_session, deployment, code="too_late") is False

    row = await db_session.get(Deployment, deployment)
    await db_session.refresh(row)
    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.url == "https://first.example"
    assert row.failure_code is None


async def test_a_taken_over_pipeline_cannot_write_progress(db_session) -> None:
    user, app = await _app(db_session)
    abandoned = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await _age_the_heartbeat(db_session, abandoned, seconds=store.DEPLOY_STALE_AFTER_S + 60)
    await store.claim(db_session, app_id=app.id, user_id=user.id)

    await store.advance(db_session, abandoned, step="building")

    row = await db_session.get(Deployment, abandoned)
    await db_session.refresh(row)
    assert row.step != "building"
    assert row.status is DeploymentStatus.FAILED


# --- reads --------------------------------------------------------------------------


async def test_latest_for_app_is_creation_ordered(db_session) -> None:
    """UUIDv7 keys are time-sortable, so `id DESC` IS creation order."""
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await store.succeed(db_session, first, url="https://one.example")
    second = await _claimed(db_session, app_id=app.id, user_id=user.id)

    latest = await store.latest_for_app(db_session, app_id=app.id)
    assert latest is not None and latest.id == second


async def test_last_successful_skips_failures_and_digestless_rows(db_session) -> None:
    """The rollback source must name an image that actually exists in the registry."""
    user, app = await _app(db_session)

    good = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await store.succeed(
        db_session, good, url="https://good.example", image_digest="sha256:" + "aa" * 32
    )
    # A success that never got as far as an image — not a rollback target.
    digestless = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await store.succeed(db_session, digestless, url="https://nodigest.example")
    broken = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await store.fail(db_session, broken, code="acr_build_failed")

    rollback = await store.last_successful(db_session, app_id=app.id)
    assert rollback is not None and rollback.id == good


async def test_stalled_lists_only_unbeaten_in_flight_rows(db_session) -> None:
    user, app_a = await _app(db_session)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id)
    app_c = await AppRegistryFactory.create(db_session, user_id=user.id)

    crashed = await _claimed(db_session, app_id=app_a.id, user_id=user.id)
    await _age_the_heartbeat(db_session, crashed, seconds=5_000)
    live = await _claimed(db_session, app_id=app_b.id, user_id=user.id)
    settled = await _claimed(db_session, app_id=app_c.id, user_id=user.id)
    await _age_the_heartbeat(db_session, settled, seconds=5_000)
    await store.succeed(db_session, settled, url="https://done.example")

    found = {row.id for row in await store.stalled(db_session, older_than_s=120)}
    assert found == {crashed}
    assert live not in found


# --- the declaration that authorised the deploy --------------------------------------


async def test_the_declaration_lands_in_the_same_insert_that_claims(db_session) -> None:
    """One statement, not a claim followed by an update.

    A second write would leave a window in which a crash produces a `running` deploy with no
    record of what authorised it — precisely the row a post-incident review would open first,
    and precisely the question it could then not answer.
    """
    user, app = await _app(db_session)
    declared = {
        "credentials_secrets": True,
        "health_data": False,
        "personal_information": False,
        "financial_data": False,
        "confidential_business_data": True,
        "public_data": False,
        "notes": "Holds the vendor API key.",
    }

    deployment_id = await store.claim(
        db_session,
        app_id=app.id,
        user_id=user.id,
        classification=declared,
        classification_score=55,
    )

    assert deployment_id is not None
    row = await db_session.get(Deployment, deployment_id)
    assert row is not None
    assert row.classification == declared
    assert row.classification_score == 55


async def test_a_row_from_before_the_gate_reads_as_never_asked(db_session) -> None:
    """NULL, not an all-False set. A synthesised declaration would read as "declared to
    handle nothing" — a claim nobody made, recorded as though they had."""
    user, app = await _app(db_session)

    deployment_id = await _claimed(db_session, app_id=app.id, user_id=user.id)

    row = await db_session.get(Deployment, deployment_id)
    assert row is not None
    assert row.classification is None
    assert row.classification_score is None


async def test_the_declaration_survives_a_stale_takeover(db_session) -> None:
    """The retry after stealing a wedged slot must carry the answers too.

    The takeover path claims a SECOND time, and an earlier version of this threading passed
    the declaration on the first attempt only — so a citizen unlucky enough to deploy right
    after a platform restart got a row with no record of what they declared."""
    user, app = await _app(db_session)
    wedged = await _claimed(db_session, app_id=app.id, user_id=user.id)
    await _age_the_heartbeat(db_session, wedged, seconds=999_999)

    declared = {"credentials_secrets": True, "public_data": False}
    deployment_id = await store.claim(
        db_session,
        app_id=app.id,
        user_id=user.id,
        classification=declared,
        classification_score=40,
    )

    assert deployment_id is not None
    assert deployment_id != wedged
    row = await db_session.get(Deployment, deployment_id)
    assert row is not None
    assert row.classification == declared
    assert row.classification_score == 40
