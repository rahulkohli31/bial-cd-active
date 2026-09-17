"""Row operations on `deployments` — the claim, the heartbeat, and the terminal write.

The claim is an `INSERT ... ON CONFLICT DO NOTHING RETURNING` against a PARTIAL unique index
(`uq_deployments_one_in_flight`): one caller gets a row back, everyone else learns "already
deploying" without a race or lock — inferred via `index_elements`/`index_where` (a partial
index cannot be an `ON CONSTRAINT` target), so the predicate must match the index's exactly.

WHY THIS EXISTS: the control plane restarts mid-pipeline, so a killed deploy's `running` row
would wedge that app forever behind a 409ing Deploy button — a claim nobody is beating for
takes over instead (same shape as `appdb/provision.py`'s stale-claim reprovision arm).
Staleness is measured from `heartbeat_at`, never `created_at`, since a legitimate build can
run for minutes and a start-time threshold would either kill live deploys or let a crashed
one hold the slot just as long.

Every function takes an `AsyncSession`; every PIPELINE writer commits its own work since it
outlives its request. `unpublish` is the one exception — called mid-request, it leaves the
commit boundary to its caller; see its own docstring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.deployment import Deployment, DeploymentStatus

_log = structlog.get_logger()

# How long a `running` row may go unbeaten before another claim may take it over.
#
# Must exceed the longest legitimate pipeline, or a slow-but-healthy deploy gets stolen from
# underneath itself and two pipelines write the same app. The budget it has to clear is
# roughly: snapshot extraction + context packing + the image build ceiling + provisioning +
# the readiness wait. 30 minutes leaves generous headroom over that sum while still bounding
# how long a crashed deploy can hold the slot.
DEPLOY_STALE_AFTER_S: Final = 1800

# The heartbeat cadence the pipeline renews at. Comfortably inside the staleness window so
# a few missed beats (a slow ARM call holding the loop) never look like a crash.
HEARTBEAT_CADENCE_S: Final = 20.0

# The failure recorded on a row that was taken over. Distinct from every pipeline-authored
# code so "the control plane died" is never mistaken for "the app failed to build".
INTERRUPTED: Final = "interrupted"

# The predicate must match `uq_deployments_one_in_flight`'s exactly, or Postgres cannot
# infer the index and the insert raises instead of conflicting.
_IN_FLIGHT_PREDICATE: Final = sa.text("status = 'running'")


def _rows_touched(result: object) -> int:
    """How many rows an UPDATE actually changed.

    `AsyncSession.execute` is declared to return the general `Result`, which has no
    `rowcount` — only the `CursorResult` a DML statement really yields does. Reading it
    through one narrow accessor keeps that cast in a single place instead of scattering
    ignores across every guarded update, and every guarded update in this module depends on
    the count: it is how a caller learns whether it was the one that settled the row."""
    return int(getattr(result, "rowcount", 0) or 0)


async def claim(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    classification: dict[str, Any] | None = None,
    classification_score: int | None = None,
) -> uuid.UUID | None:
    """Claim the one in-flight deploy slot for `app_id`; `None` if one is genuinely running.
    Commits. The classification that cleared the gate is written by the SAME insert that
    claims the slot, so a row cannot exist without its authorising answers — a second UPDATE
    could let a crash leave a running deploy unjustified, the first thing a post-incident
    review checks. Two attempts at most: plain claim, then one retry only if a stale row was
    taken over; unbounded retries against a row a live pipeline keeps beating would spin.
    """
    claimed = await _try_claim(
        db,
        app_id=app_id,
        user_id=user_id,
        classification=classification,
        classification_score=classification_score,
    )
    if claimed is not None:
        return claimed

    if not await _take_over_stale(db, app_id=app_id):
        return None

    return await _try_claim(
        db,
        app_id=app_id,
        user_id=user_id,
        classification=classification,
        classification_score=classification_score,
    )


async def _try_claim(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    classification: dict[str, Any] | None,
    classification_score: int | None,
) -> uuid.UUID | None:
    stmt = (
        pg_insert(Deployment)
        .values(
            app_id=app_id,
            user_id=user_id,
            step="claimed",
            classification=classification,
            classification_score=classification_score,
        )
        .on_conflict_do_nothing(
            index_elements=[Deployment.app_id],
            index_where=_IN_FLIGHT_PREDICATE,
        )
        .returning(Deployment.id)
    )
    claimed: uuid.UUID | None = await db.scalar(stmt)
    await db.commit()
    return claimed


async def _take_over_stale(db: AsyncSession, *, app_id: uuid.UUID) -> bool:
    """Fail an in-flight row nobody has beaten for. True iff a row was actually taken over.

    The `heartbeat_at` predicate is what makes this safe: a live pipeline renews inside the
    window, so this can only ever reach a row whose owner is gone."""
    cutoff = datetime.now(UTC) - timedelta(seconds=DEPLOY_STALE_AFTER_S)
    result = await db.execute(
        sa.update(Deployment)
        .where(
            Deployment.app_id == app_id,
            Deployment.status == DeploymentStatus.RUNNING,
            Deployment.heartbeat_at < cutoff,
        )
        .values(
            status=DeploymentStatus.FAILED,
            failure_code=INTERRUPTED,
            failure_detail=(
                "The deploy did not finish — the platform restarted while it was running. "
                "Nothing was changed for your app; press Deploy again."
            ),
            finished_at=sa.func.now(),
        )
    )
    await db.commit()
    taken = bool(_rows_touched(result))
    if taken:
        _log.warning("deployment_stale_claim_taken_over", app_id=str(app_id))
    return taken


async def heartbeat(db: AsyncSession, deployment_id: uuid.UUID) -> None:
    """Renew the liveness stamp. Scoped to `running` so a terminal row is never resurrected
    by a heartbeat that raced the finish."""
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.status == DeploymentStatus.RUNNING)
        .values(heartbeat_at=sa.func.now())
    )
    await db.commit()


async def advance(db: AsyncSession, deployment_id: uuid.UUID, *, step: str, **fields: Any) -> None:
    """Record the phase, plus whatever the pipeline learned reaching it (a head SHA, a run
    id, a digest). Beats the heartbeat in the same statement — every phase change is proof
    of life, so a separate renew would be a redundant round trip.

    Guarded on `running`: a pipeline that keeps writing after being taken over must not
    overwrite the terminal row that replaced it."""
    await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.status == DeploymentStatus.RUNNING)
        .values(step=step, heartbeat_at=sa.func.now(), **fields)
    )
    await db.commit()


async def succeed(db: AsyncSession, deployment_id: uuid.UUID, *, url: str, **fields: Any) -> bool:
    """Write the terminal success. True iff this call was the one that settled the row."""
    return await _finish(
        db, deployment_id, status=DeploymentStatus.SUCCEEDED, step="live", url=url, **fields
    )


async def fail(
    db: AsyncSession,
    deployment_id: uuid.UUID,
    *,
    code: str,
    detail: str | None = None,
    **fields: Any,
) -> bool:
    """Write the terminal failure. True iff this call was the one that settled the row.

    NOT EVERY FAILED ROW IS A BROKEN DEPLOY, and `code` is the only thing that tells them apart.
    The drift re-check's `routed_for_review` settles here too: that deploy did exactly what it
    should — it stopped and put the version in front of an administrator, so a reader (or a
    dashboard) that treats `status = failed` as "something went wrong" will mis-report it.
    Adding a fourth `DeploymentStatus` instead would move what `uq_deployments_one_in_flight`'s
    partial index covers, which is a real schema decision this outcome does not need to make."""
    return await _finish(
        db,
        deployment_id,
        status=DeploymentStatus.FAILED,
        step="failed",
        failure_code=code,
        failure_detail=detail,
        **fields,
    )


async def _finish(
    db: AsyncSession, deployment_id: uuid.UUID, *, status: DeploymentStatus, **fields: Any
) -> bool:
    """The single terminal write, guarded on `running` so a row settles exactly once. `False`
    means someone else already settled it, so a late pipeline must stop rather than contradict
    the record. Clearing `unpublished_at` here is NOT housekeeping: `unpublish` targets via
    `latest_for_app` (no status predicate), so a race can stamp a NEW running row mid-publish;
    without this clear it settles SUCCEEDED wearing a takedown stamp and the kill-switch jams
    on the app it exists to kill. Safe by construction — guarded on RUNNING, so a real
    takedown always targets an already-settled row.
    """
    result = await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.status == DeploymentStatus.RUNNING)
        # `unpublished_at` first so an explicit caller-supplied value in `**fields` still wins.
        .values(unpublished_at=None, status=status, finished_at=sa.func.now(), **fields)
    )
    await db.commit()
    return bool(_rows_touched(result))


async def unpublish(db: AsyncSession, deployment_id: uuid.UUID, *, at: datetime) -> bool:
    """Mark a deployment as taken down. True iff this call set it — a repeat, or one that lost a
    race with a concurrent unpublish, touches zero rows and returns False, read as "already
    unpublished" rather than an error. Takes the timestamp as a parameter so the caller can
    report the exact value written. NO STATUS GUARD: the route resolves the row via
    `latest_for_app`, so even a FAILED attempt is a legitimate target, and the RETURN VALUE
    alone — never a prior read, stale the instant it lands — tells the caller whether it won.
    DOES NOT COMMIT: it runs mid-request from the router's `unpublish` route, which sequences
    its own commits and decides the next boundary from this value."""
    result = await db.execute(
        sa.update(Deployment)
        .where(Deployment.id == deployment_id, Deployment.unpublished_at.is_(None))
        .values(unpublished_at=at)
    )
    return bool(_rows_touched(result))


# --- reads -------------------------------------------------------------------------


async def latest_for_app(db: AsyncSession, *, app_id: uuid.UUID) -> Deployment | None:
    """The most recent deploy attempt for an app. UUIDv7 primary keys are time-sortable, so
    `id DESC` is a creation-order sort with no second column and no index gymnastics."""
    row: Deployment | None = await db.scalar(
        sa.select(Deployment)
        .where(Deployment.app_id == app_id)
        .order_by(Deployment.id.desc())
        .limit(1)
    )
    return row


async def latest_published(db: AsyncSession, *, app_id: uuid.UUID) -> Deployment | None:
    """The most recent attempt that actually PUBLISHED something — succeeded, with a digest
    naming the image it put there.

    WHY THIS IS NOT `latest_for_app`. `deployments` is append-only and a RESTART claims a row
    of its own, so a restart that fails leaves a `failed` row newer than the one that published
    the container still serving. The newest row is then an ATTEMPT fact and says nothing about
    what is in production — which is exactly the collapse `liveness.live_app_ids` already makes
    for the lists, and the reason a route asking "what is live" must ask it the same way.

    The takedown axis is deliberately NOT read here — see `latest_takedown`, which is the other
    half of the same question."""
    row: Deployment | None = await db.scalar(
        sa.select(Deployment)
        .where(
            Deployment.app_id == app_id,
            Deployment.status == DeploymentStatus.SUCCEEDED,
            Deployment.image_digest.is_not(None),
        )
        .order_by(Deployment.id.desc())
        .limit(1)
    )
    return row


async def latest_takedown(db: AsyncSession, *, app_id: uuid.UUID) -> uuid.UUID | None:
    """The id of the newest row an owner's take-down has stamped, or `None` if none has.

    COMPARE IT AGAINST `latest_published`, NEVER AGAINST THE NEWEST ROW. `unpublish` stamps
    whichever row was newest when it ran, and attempts keep arriving afterwards — so the stamp
    can end up on a row that is neither the newest nor the published one, and a caller testing
    either of those alone reads a taken-down app as serving. The collapse that answers is a
    comparison: the app is off when the newest stamp is not older than the newest publish, which
    is the same `last_unpublished.id < last_success.id` that `liveness.live_app_ids` applies to
    the lists. UUIDv7 ids are time-sortable, which is what makes the comparison creation order."""
    stamped: uuid.UUID | None = await db.scalar(
        sa.select(Deployment.id)
        .where(Deployment.app_id == app_id, Deployment.unpublished_at.is_not(None))
        .order_by(Deployment.id.desc())
        .limit(1)
    )
    return stamped


async def in_flight(db: AsyncSession, *, app_id: uuid.UUID) -> uuid.UUID | None:
    """The running deployment id for this app, if any. Used to block unpublish while a
    deploy is in progress — letting it through would race the in-flight pipeline's
    own `create_or_update`, which could silently re-publish the app moments after an admin
    tears it down."""
    running: uuid.UUID | None = await db.scalar(
        sa.select(Deployment.id)
        .where(Deployment.app_id == app_id, Deployment.status == DeploymentStatus.RUNNING)
        .limit(1)
    )
    return running


async def stalled(db: AsyncSession, *, older_than_s: float) -> list[Deployment]:
    """Every in-flight row whose owner has stopped beating — the reconciler's work list."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
    rows = await db.scalars(
        sa.select(Deployment).where(
            Deployment.status == DeploymentStatus.RUNNING,
            Deployment.heartbeat_at < cutoff,
        )
    )
    return list(rows)
